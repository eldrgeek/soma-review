#!/usr/bin/env python3
"""
soma-review v2 — local interactive review server.

Serves whitelisted estate markdown as linked in-app pages with anchored,
persistent comments. Stdlib only (http.server + json), no external deps.

See soma-review/CLAUDE.md and README.md for the API and sidecar format.
"""
import html as _html
import json
import os
import re
import subprocess
import sys
import time
import uuid
import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mdblocks import (  # noqa: E402
    parse_markdown, render_inline, build_lexicon_index, strip_auto_lexicon_marker,
    _esc, _esc_attr, _first_sentence,
)
import blockmap  # noqa: E402
import tours as tour_engine  # noqa: E402  (Quinn tours of completed jobs — see tours.py)
import cursor_intake  # noqa: E402  (Grok/Cursor intake — see SOMA/cursor-intake/README.md)

PROJECTS_ROOT = os.path.expanduser('~/Projects')
DISPATCH_PROMPT_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dispatch-prompt-template.md')
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
WORKSPACES_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'workspaces.json')
PUBLIC_FILMS_PATH = os.path.join(PROJECTS_ROOT, '_estate', 'public-films.json')
LEXICON_MD_PATH = os.path.join(PROJECTS_ROOT, 'soma-lexicon', 'SOMA-LEXICON.md')

# Nightly worktree reports get their own synthetic route: nightly/<worktree-name>
NIGHTLY_PREFIX = 'nightly'

DEFAULT_WORKSPACE = 'estate'


def load_workspaces():
    """Load workspaces.json fresh on every call (cheap, small file) so edits don't
    need a server restart. Each workspace config:
      roots: [[route_prefix, path_relative_to_PROJECTS_ROOT], ...]
      nav: [[label, route_path], ...]
      home: route_path (default page for this workspace)
      feedback_dir: path relative to PROJECTS_ROOT for this workspace's sidecar JSONLs
      nightly: bool - whether to auto-discover .nightly-* worktrees into this workspace
      nightly_filter: optional regex - only include nightly slugs matching this pattern
    """
    with open(WORKSPACES_CONFIG, 'r', encoding='utf-8') as f:
        raw = json.load(f)
    out = {}
    for slug, cfg in raw.items():
        out[slug] = {
            'label': cfg.get('label', slug),
            'roots': [(p, os.path.join(PROJECTS_ROOT, rel)) for p, rel in cfg.get('roots', [])],
            'nav': [(label, route) for label, route in cfg.get('nav', [])],
            'home': cfg.get('home'),
            'feedback_dir': os.path.join(PROJECTS_ROOT, cfg.get('feedback_dir', '_estate/review-feedback')),
            'nightly': cfg.get('nightly', False),
            'nightly_filter': cfg.get('nightly_filter'),
            'tours': cfg.get('tours', False),
            'mark_layer': cfg.get('mark_layer', True),
            'status_badges': cfg.get('status_badges', []),
            'dispatch_targets': cfg.get('dispatch_targets', {}),
        }
    return out


def get_dispatch_config(route_path, workspace=DEFAULT_WORKSPACE):
    """Per-page dispatch routing. Default target is cc-dispatch (Dee).
    Supports exact match and wildcard '*' match."""
    ws = get_workspace(workspace)
    targets = ws.get('dispatch_targets') or {}
    # Try exact match first, then fallback to wildcard
    cfg = targets.get(route_path) or targets.get('*') or {}
    target = cfg.get('target', 'dee')
    button = cfg.get('button', 'Send to Grok' if target == 'cursor' else 'File feedback' if target == 'rsi' else 'Send to Dee')
    return {'target': target, 'button': button}


def get_workspace(slug):
    workspaces = load_workspaces()
    if slug not in workspaces:
        raise NotFoundError(f'workspace:{slug}')
    ws = workspaces[slug]
    os.makedirs(ws['feedback_dir'], exist_ok=True)
    return ws


def discover_nightly_reports(name_filter=None):
    """Return {slug: absolute_path} for .nightly-*/NIGHTLY-REPORT.md worktrees.
    If name_filter (regex string) is given, only include slugs matching it."""
    out = {}
    if not os.path.isdir(PROJECTS_ROOT):
        return out
    pattern = re.compile(name_filter, re.IGNORECASE) if name_filter else None
    for name in os.listdir(PROJECTS_ROOT):
        if name.startswith('.nightly-'):
            candidate = os.path.join(PROJECTS_ROOT, name, 'NIGHTLY-REPORT.md')
            if os.path.isfile(candidate):
                slug = name[len('.nightly-'):]
                if pattern and not pattern.search(slug):
                    continue
                out[slug] = candidate
    return out


# --- Path resolution / security -----------------------------------------

class NotFoundError(Exception):
    pass


def resolve_page(route_path, workspace=DEFAULT_WORKSPACE):
    """route_path like 'estate/foo/bar.md' or 'nightly/izzy'. Returns absolute fs path.
    Raises NotFoundError if outside whitelist or missing.
    """
    ws = get_workspace(workspace)
    route_path = route_path.strip('/')
    if not route_path:
        route_path = ws['home']

    parts = route_path.split('/', 1)
    if len(parts) != 2:
        raise NotFoundError(route_path)
    prefix, rest = parts

    if prefix == NIGHTLY_PREFIX and ws['nightly']:
        reports = discover_nightly_reports(ws.get('nightly_filter'))
        slug = rest
        if slug not in reports:
            raise NotFoundError(route_path)
        return reports[slug]

    root = None
    for p, r in ws['roots']:
        if p == prefix:
            root = r
            break
    if root is None:
        raise NotFoundError(route_path)

    # Prevent path traversal
    rest = unquote(rest)
    candidate = os.path.normpath(os.path.join(root, rest))
    if not candidate.startswith(os.path.normpath(root) + os.sep) and candidate != os.path.normpath(root):
        raise NotFoundError(route_path)
    if not os.path.isfile(candidate):
        raise NotFoundError(route_path)
    if not candidate.endswith('.md'):
        raise NotFoundError(route_path)
    return candidate


def fs_path_to_route(fs_path, workspace=DEFAULT_WORKSPACE):
    """Best-effort reverse mapping for link rewriting within the same root.
    Works for ANY file under a whitelisted root, not just .md — callers decide
    whether the target is renderable (.md -> /page/) or raw-servable (/raw/)."""
    ws = get_workspace(workspace)
    fs_path = os.path.normpath(fs_path)
    for p, r in ws['roots']:
        r = os.path.normpath(r)
        if fs_path.startswith(r + os.sep):
            rel = os.path.relpath(fs_path, r)
            return f'{p}/{rel}'
    return None


_LEXICON_CACHE = {'mtime': None, 'index': None}


def get_lexicon_index():
    """Load + parse the SOMA Lexicon, cached by mtime so an edit to the source
    file is picked up on the next request without a server restart (same
    "reload fresh, no restart needed" contract as workspaces.json)."""
    try:
        mtime = os.path.getmtime(LEXICON_MD_PATH)
    except OSError:
        return {'by_slug': {}, 'by_alias': {}}
    if _LEXICON_CACHE['mtime'] != mtime:
        with open(LEXICON_MD_PATH, 'r', encoding='utf-8') as f:
            text = f.read()
        _LEXICON_CACHE['index'] = build_lexicon_index(text)
        _LEXICON_CACHE['mtime'] = mtime
    return _LEXICON_CACHE['index']


def lexicon_entry_popover_html(entry, url_prefix, lexicon_route):
    """Popover content for a lexicon-backed term-link: gloss in bold, then the
    first sentence of "What we mean.", then a "full entry" link to the real
    lexicon page — only if that page is whitelisted (resolvable) in the
    current workspace; otherwise the bold gloss + sentence still render, just
    without the deep link."""
    gloss_html = f'<p><strong>{_esc(entry["gloss"])}</strong></p>'
    sentence_html = f'<p>{_esc(entry["first_sentence"])}</p>'
    link_html = ''
    if lexicon_route:
        href = f'{url_prefix}/page/{lexicon_route}#{entry["anchor"]}'
        link_html = (
            f'<p class="term-popover-full">'
            f'<a href="{_esc_attr(href)}" class="internal-link" target="_blank" rel="noopener">full entry &rarr;</a>'
            f'</p>'
        )
    return gloss_html + sentence_html + link_html


def resolve_raw(route_path, workspace=DEFAULT_WORKSPACE):
    """Like resolve_page but for non-.md files under a whitelisted root (read-only
    static serve, e.g. LEDGER.csv). Raises NotFoundError if outside whitelist or missing."""
    ws = get_workspace(workspace)
    route_path = route_path.strip('/')
    parts = route_path.split('/', 1)
    if len(parts) != 2:
        raise NotFoundError(route_path)
    prefix, rest = parts
    root = None
    for p, r in ws['roots']:
        if p == prefix:
            root = r
            break
    if root is None:
        raise NotFoundError(route_path)
    rest = unquote(rest)
    candidate = os.path.normpath(os.path.join(root, rest))
    if not candidate.startswith(os.path.normpath(root) + os.sep) and candidate != os.path.normpath(root):
        raise NotFoundError(route_path)
    if not os.path.isfile(candidate):
        raise NotFoundError(route_path)
    return candidate


_RAW_MIME = {
    '.csv': 'text/csv', '.txt': 'text/plain', '.json': 'application/json',
    '.pdf': 'application/pdf', '.png': 'image/png', '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.svg': 'image/svg+xml',
    '.log': 'text/plain', '.yaml': 'text/plain', '.yml': 'text/plain',
    # video (verification videos convention, WQ-93): without an explicit MIME
    # these fell through to application/octet-stream -> browser downloads
    # instead of playing inline. No Range support here (whole-file read) —
    # fine for 1-2 min verification clips, progressive playback works.
    '.mp4': 'video/mp4', '.webm': 'video/webm', '.mov': 'video/quicktime',
    '.vtt': 'text/vtt', '.mp3': 'audio/mpeg',
}


def page_slug(route_path):
    """Filesystem-safe slug for sidecar filenames."""
    return re.sub(r'[^a-zA-Z0-9._-]+', '_', route_path.strip('/'))


# --- Link rewriting -------------------------------------------------------
#
# render_inline() calls link_resolver(href) -> (href_out, kind) where kind is one of:
#   'internal'    -> in-app /page/ route, works, rendered with .internal-link
#   'external'    -> normal http(s)/mailto link, target=_blank, .external-link
#   'raw'         -> whitelisted non-.md file, served read-only via /raw/, .internal-link
#   'unavailable' -> looks local but isn't resolvable/servable, .unavailable-link,
#                    href='#unavailable', title attr explains why
#
# mdblocks.render_inline still treats any non-'external' kind as "internal" for its
# own class selection, so make_link_resolver returns (href_out, is_internal_bool) to
# match that contract, PLUS stashes the kind/title via a closure-local dict keyed by
# the outgoing href so render_block_html/CSS can special-case 'unavailable' — see
# LinkResolution below. Simpler: just fold kind straight into the returned class by
# having link_resolver return a 3rd element consumed only when present.

class LinkKind:
    INTERNAL = 'internal-link'
    EXTERNAL = 'external-link'
    UNAVAILABLE = 'unavailable-link'


def workspace_url_prefix(workspace):
    """'' for the default workspace (bare /page/..., backward compatible with the
    one sidecar file that predates workspaces), '/w/<slug>' otherwise."""
    return '' if workspace == DEFAULT_WORKSPACE else f'/w/{workspace}'


def make_link_resolver(current_fs_path, current_route, workspace=DEFAULT_WORKSPACE):
    current_dir = os.path.dirname(current_fs_path)
    url_prefix = workspace_url_prefix(workspace)

    def resolver(href):
        raw_href = href
        if href.startswith(('http://', 'https://', 'mailto:')):
            return href, LinkKind.EXTERNAL, None
        if href.startswith('#'):
            # in-page anchor fragment on the current doc — always safe, no file resolution.
            return href, LinkKind.INTERNAL, None

        frag = ''
        if '#' in href:
            href, frag = href.split('#', 1)
            frag = '#' + frag

        if not href:
            # bare '#anchor' link (handled above) or empty — treat as same-page anchor.
            target = current_route and f'{url_prefix}/page/{current_route}{frag}' or frag
            return target, LinkKind.INTERNAL, None

        target_fs = os.path.normpath(os.path.join(current_dir, href))

        if target_fs.endswith('.md'):
            if os.path.isfile(target_fs):
                route = fs_path_to_route(target_fs, workspace)
                if route:
                    return f'{url_prefix}/page/{route}{frag}', LinkKind.INTERNAL, None
                # exists on disk but outside every whitelisted root
                return '#unavailable', LinkKind.UNAVAILABLE, f'Outside review whitelist: {raw_href}'
            return '#unavailable', LinkKind.UNAVAILABLE, f'File not found: {raw_href}'

        # Non-.md local path: serve read-only via /raw/ if it exists under a
        # whitelisted root; otherwise mark unavailable (never silently dead-link).
        if os.path.isfile(target_fs):
            route = fs_path_to_route(target_fs, workspace)
            if route:
                return f'{url_prefix}/raw/{route}', LinkKind.INTERNAL, None
            return '#unavailable', LinkKind.UNAVAILABLE, f'Outside review whitelist: {raw_href}'
        if not href.startswith(('/', '..')) and '://' not in href:
            # looks like a relative local path but the file doesn't exist
            return '#unavailable', LinkKind.UNAVAILABLE, f'File not found: {raw_href}'
        # anything else (absolute fs paths, unrecognized schemes) — treat as external-ish,
        # don't try to resolve, don't claim it's internal.
        return href, LinkKind.EXTERNAL, None

    return resolver


# --- Comment sidecar storage ------------------------------------------

_process_lock = threading.Lock()


def sidecar_path(route_path, workspace=DEFAULT_WORKSPACE):
    ws = get_workspace(workspace)
    return os.path.join(ws['feedback_dir'], page_slug(route_path) + '.jsonl')


def block_map_path(route_path, workspace=DEFAULT_WORKSPACE):
    ws = get_workspace(workspace)
    return os.path.join(ws['feedback_dir'], page_slug(route_path) + '.blocks.json')


def _read_comments_unlocked(path):
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, 'r', encoding='utf-8') as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                sys.stderr.write(f'[comments] malformed row {path}:{line_number}: {exc}\n')
    return out


def read_comments(route_path, workspace=DEFAULT_WORKSPACE):
    path = sidecar_path(route_path, workspace)
    with blockmap.file_lock(path, exclusive=False):
        return _read_comments_unlocked(path)


def _write_all_comments_unlocked(path, comments):
    payload = ''.join(json.dumps(c, ensure_ascii=False) + '\n' for c in comments).encode('utf-8')
    blockmap.atomic_write(path, payload)


def write_all_comments(route_path, comments, workspace=DEFAULT_WORKSPACE):
    path = sidecar_path(route_path, workspace)
    with blockmap.file_lock(path):
        _write_all_comments_unlocked(path, comments)


def append_comment(route_path, comment, workspace=DEFAULT_WORKSPACE):
    path = sidecar_path(route_path, workspace)
    with blockmap.file_lock(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        needs_guard = False
        if os.path.isfile(path) and os.path.getsize(path):
            with open(path, 'rb') as existing:
                existing.seek(-1, os.SEEK_END)
                needs_guard = existing.read(1) != b'\n'
        with open(path, 'ab') as f:
            if needs_guard:
                f.write(b'\n')
            f.write((json.dumps(comment, ensure_ascii=False) + '\n').encode('utf-8'))
            f.flush()
            os.fsync(f.fileno())


def update_comment(route_path, comment_id, patch, workspace=DEFAULT_WORKSPACE):
    path = sidecar_path(route_path, workspace)
    with blockmap.file_lock(path):
        comments = _read_comments_unlocked(path)
        found = False
        for c in comments:
            if c.get('id') == comment_id:
                c.update(patch)
                found = True
        if found:
            _write_all_comments_unlocked(path, comments)
        return found


def read_public_films():
    if not os.path.isfile(PUBLIC_FILMS_PATH):
        return {}
    try:
        with open(PUBLIC_FILMS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def write_public_films(data):
    os.makedirs(os.path.dirname(PUBLIC_FILMS_PATH), exist_ok=True)
    tmp = PUBLIC_FILMS_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write('\n')
    os.replace(tmp, PUBLIC_FILMS_PATH)


def update_public_film(testid, is_public):
    with _process_lock:
        data = read_public_films()
        data[testid] = bool(is_public)
        write_public_films(data)
        return data


# --- HTML rendering --------------------------------------------------------

PAGE_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0;
       display: flex; min-height: 100vh; background: #0f1216; color: #e6e6e6; }
a { color: #7db8ff; }
a.unavailable-link { color: #6a7280; text-decoration: line-through; cursor: not-allowed; }
a.external-link::after { content: " \2197"; font-size: 11px; opacity: .6; }
.sidebar { width: 260px; flex-shrink: 0; background: #14181f; padding: 20px 16px;
           border-right: 1px solid #262b33; position: sticky; top: 0; height: 100vh; overflow-y: auto; }
.sidebar h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .08em; color: #8a93a3; margin: 18px 0 8px; }
.sidebar h2:first-child { margin-top: 0; }
.workspace-switcher { display: flex; flex-wrap: wrap; gap: 4px; margin: 14px 0 4px; }
.workspace-switcher a { padding: 3px 8px; border-radius: 12px; font-size: 11px; text-decoration: none;
                          background: #1c2230; color: #8a93a3; border: 1px solid #262b33; }
.workspace-switcher a.active { background: #22344a; color: #9cc7ff; border-color: #2a5adf; }
.workspace-switcher a:hover { color: #cbd3e0; }
.sidebar a { display: block; padding: 6px 8px; border-radius: 6px; text-decoration: none; font-size: 14px; color: #cbd3e0; }
.sidebar a:hover { background: #1e2430; }
.sidebar a.active { background: #22344a; color: #9cc7ff; }
.main { flex: 1; max-width: 900px; margin: 0 auto; padding: 40px 48px 120px; }
.main h1 { font-size: 28px; }
.main h1, .main h2, .main h3 { line-height: 1.3; }
.block-wrap { position: relative; padding: 4px 10px 4px 14px; border-radius: 8px; margin: 2px -10px; }
.block-wrap:hover { background: #171c24; }
.block-wrap:hover .comment-affordance { opacity: 1; }
.comment-affordance { position: absolute; right: -34px; top: 4px; opacity: 0; transition: opacity .15s;
                       width: 26px; height: 26px; border-radius: 6px; background: #22283333; border: 1px solid #33394a;
                       color: #9cc7ff; font-size: 13px; cursor: pointer; display: flex; align-items: center; justify-content: center; }
.comment-affordance:hover { background: #2a3345; }
.block-wrap.has-comments { border-left: 3px solid #4f8cff; }
.comment-count-pill { position: absolute; left: -28px; top: 6px; font-size: 11px; background: #2a3345; color: #9cc7ff;
                       border-radius: 10px; padding: 1px 6px; }
table { border-collapse: collapse; width: 100%; margin: 10px 0; }
th, td { border: 1px solid #2b3140; padding: 6px 10px; text-align: left; font-size: 14px; }
th { background: #171c24; }
code { background: #1c2230; padding: 1px 5px; border-radius: 4px; font-size: 13px; }
pre code { display: block; padding: 12px; overflow-x: auto; }
blockquote { border-left: 3px solid #3a4152; margin: 8px 0; padding: 4px 14px; color: #b7bfcc; }
hr { border: none; border-top: 1px solid #2b3140; margin: 24px 0; }
.comment-box { display: none; margin: 6px 0 14px; padding: 10px; background: #171c24; border: 1px solid #2b3140;
               border-radius: 8px; }
.comment-box.open { display: block; }
.comment-box textarea { width: 100%; min-height: 60px; background: #0f1216; color: #e6e6e6; border: 1px solid #2b3140;
                          border-radius: 6px; padding: 8px; font-family: inherit; font-size: 14px; }
.comment-box button { margin-top: 6px; background: #2a5adf; color: white; border: none; border-radius: 6px;
                        padding: 6px 14px; font-size: 13px; cursor: pointer; }
.comment-box button:hover { background: #3a6aef; }
.comment-thread { margin: 6px 0 14px; }
.comment-item { background: #171c24; border: 1px solid #2b3140; border-radius: 8px; padding: 8px 12px; margin: 6px 0; font-size: 13px; }
.comment-item .meta { color: #8a93a3; font-size: 11px; margin-bottom: 4px; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 10px; text-transform: uppercase;
         letter-spacing: .04em; font-weight: 600; }
.badge-queued { background: #4a3a1f; color: #f0c674; }
.badge-seen { background: #1f3a4a; color: #74c6f0; }
.badge-in-progress { background: #2a2a4a; color: #a89cf0; }
.badge-done { background: #1f4a2a; color: #7ee08a; }
.page-discussion { margin-top: 60px; border-top: 1px solid #2b3140; padding-top: 20px; }
.page-discussion h2 { font-size: 18px; }
.top-actions { position: sticky; top: 0; background: #0f1216ee; backdrop-filter: blur(6px); padding: 10px 0;
               display: flex; justify-content: flex-end; gap: 10px; z-index: 5; margin-bottom: 10px; }
.top-actions button { background: #1c2634; color: #9cc7ff; border: 1px solid #2b3140; border-radius: 6px;
                        padding: 6px 12px; font-size: 13px; cursor: pointer; }
.top-actions button:hover { background: #24314a; }
.toast { position: fixed; bottom: 20px; right: 20px; background: #1c2634; border: 1px solid #2b3140;
         padding: 10px 16px; border-radius: 8px; font-size: 13px; color: #cbd3e0; opacity: 0; transition: opacity .2s; }
.toast.show { opacity: 1; }
.notfound { padding: 60px; text-align: center; }

/* --- edit-as-comment --- */
.block-body.edit-hint { cursor: text; }
.block-body.edit-hint:hover { outline: 1px dashed #33394a; outline-offset: 3px; border-radius: 4px; }
.block-edit-textarea { width: 100%; min-height: 48px; background: #10151d; color: #e6e6e6;
                        border: 1px solid #4f8cff; border-radius: 6px; padding: 8px; font-family: inherit;
                        font-size: 14px; line-height: 1.5; resize: vertical; }
.edit-hint-label { display: none; font-size: 11px; color: #6a7280; margin-top: 2px; }
.block-wrap.edit-eligible:hover .edit-hint-label { display: block; }
.diff-view { font-size: 13px; font-family: ui-monospace, Menlo, monospace; white-space: pre-wrap;
             line-height: 1.5; word-break: break-word; }
.diff-del { background: #4a1f24; color: #f0a3ab; text-decoration: line-through; padding: 0 2px; border-radius: 2px; }
.diff-ins { background: #1f4a2a; color: #9cf0ac; padding: 0 2px; border-radius: 2px; }
.comment-item.is-edit { border-left: 3px solid #a89cf0; }
.unresolved-marks { margin: 32px 0 18px; padding: 18px; border: 1px solid #a56b35;
  border-radius: 10px; background: rgba(165,107,53,.09); }
.unresolved-marks h2 { margin-top: 0; color: #efb36f; }
.unresolved-explainer { color: #aeb6c3; margin-top: -4px; }
.unresolved-item { margin: 12px 0; padding: 12px; border-left: 3px solid #d89452;
  background: rgba(10,13,18,.45); }
.unresolved-quote { margin: 0 0 8px; padding: 8px 10px; white-space: pre-wrap;
  color: #e4d1b9; background: rgba(0,0,0,.22); border-radius: 5px; }
.unresolved-location { color: #8f98a8; font-size: 11px; margin-bottom: 6px; }
.comment-item .comment-actions { float: right; display: flex; gap: 6px; }
.comment-item .comment-actions button { background: none; border: none; color: #8a93a3; cursor: pointer;
                                          font-size: 12px; padding: 0 3px; }
.comment-item .comment-actions button:hover { color: #e6e6e6; }
.comment-item.deleted { opacity: .45; }
.comment-item .edit-inline-textarea { width: 100%; min-height: 44px; background: #0f1216; color: #e6e6e6;
                                        border: 1px solid #2b3140; border-radius: 6px; padding: 6px;
                                        font-family: inherit; font-size: 13px; margin-top: 4px; }
.mic-btn { background: #1c2634; border: 1px solid #2b3140; border-radius: 6px; padding: 6px 10px;
           font-size: 14px; cursor: pointer; margin-top: 6px; margin-right: 6px; }
.mic-btn:hover { background: #24314a; }
.mic-btn.recording { background: #4a1f24; border-color: #f0a3ab; animation: pulse 1s infinite; }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .5; } }
.comment-item.is-verdict { border-left: 3px solid #e6c26a; }
.verdict-row { display: inline-flex; gap: 4px; align-items: center; flex-wrap: wrap; }
.verdict-btn { background: #1c2634; border: 1px solid #2b3140; border-radius: 6px; padding: 3px 9px;
               font-size: 11px; cursor: pointer; color: #c7ccd6; }
.verdict-btn:hover { background: #24314a; color: #e6e6e6; }
.verdict-btn.verdict-keep:hover { border-color: #9cf0ac; color: #9cf0ac; }
.verdict-btn.verdict-restart:hover { border-color: #7ec6f0; color: #7ec6f0; }
.verdict-btn.verdict-cancel:hover { border-color: #f0a3ab; color: #f0a3ab; }
.verdict-btn.verdict-later:hover { border-color: #e6c26a; color: #e6c26a; }
.verdict-status { font-size: 11px; color: #8a93a3; margin-left: 4px; }
.badge-verdict-keep { background: #1f4a2a; color: #9cf0ac; }
.badge-verdict-restart { background: #1c3a4a; color: #7ec6f0; }
.badge-verdict-cancel { background: #4a1f24; color: #f0a3ab; }
.badge-verdict-later { background: #4a3f1f; color: #e6c26a; }
.review-row { display: inline-flex; gap: 4px; align-items: center; flex-wrap: wrap; margin-top: 6px; }
.review-btn { background: #1c2634; border: 1px solid #2b3140; border-radius: 6px; padding: 4px 10px;
  font-size: 12px; color: #b8c0cc; cursor: pointer; }
.review-btn:hover { background: #24314a; color: #e6e6e6; }
.review-btn.review-agree:hover { border-color: #9cf0ac; color: #9cf0ac; }
.review-btn.review-disagree:hover { border-color: #f0a3ab; color: #f0a3ab; }
.review-btn.review-discuss:hover { border-color: #7ec6f0; color: #7ec6f0; }
.review-btn.review-defer:hover { border-color: #e6c26a; color: #e6c26a; }
.review-status { font-size: 11px; color: #8a93a3; margin-left: 4px; }
.block-wrap.cursor-sent { display: none; }
.cursor-sent-banner { background: #1a2e1f; border: 1px solid #2d5a38; border-radius: 8px;
  padding: 10px 14px; margin-bottom: 16px; font-size: 13px; color: #9cf0ac; }
.cursor-sent-banner a { color: #7ec6f0; }
.review-row.is-sent .review-btn { display: none; }
.review-row.is-sent .review-status { color: #9cf0ac; font-weight: 600; }
.badge-verdict-agree { background: #1f4a2a; color: #9cf0ac; }
.badge-verdict-disagree { background: #4a1f24; color: #f0a3ab; }
.badge-verdict-discuss { background: #1c3a4a; color: #7ec6f0; }
.badge-verdict-defer { background: #4a3f1f; color: #e6c26a; }
.film-player { width: 100%; max-width: 720px; border-radius: 8px; background: #000;
               display: block; margin: 6px 0; }
.public-film-control { display: inline-flex; align-items: center; gap: 8px; margin: 4px 0 12px;
                       padding: 7px 10px; border-radius: 8px; border: 1px solid #2b3140;
                       background: #141922; color: #cbd3e0; font-size: 13px; cursor: pointer; }
.public-film-control:hover { border-color: #3a465a; background: #171e29; }
.public-film-control input { width: 15px; height: 15px; accent-color: #4f8cff; }
.public-film-control input:disabled + span { color: #6a7280; }
.film-error { color: #f0a3ab; background: #2a1418; border: 1px solid #4a1f24;
              border-radius: 6px; padding: 8px 12px; font-size: 13px; }
.film-placeholder { color: #e6c26a; background: #2a2410; border: 1px solid #4a4020;
                     border-radius: 6px; padding: 8px 12px; font-size: 13px; }
.screening-hint { font-size: 12px; color: #8a93a3; font-style: italic; margin: -4px 0 12px; }
.screening-group h2 { margin-top: 32px; border-bottom: 1px solid #262b33; padding-bottom: 6px; }
.film-meta { font-size: 12px; color: #8a93a3; margin: 2px 0 8px; }
.film-blurb { font-size: 14px; color: #c7ccd6; margin: 4px 0 10px; }
details.verification-group summary { cursor: pointer; color: #8a93a3; font-size: 13px;
                                      margin: 20px 0 8px; }
html { scroll-behavior: smooth; }
.main h1[id], .main h2[id], .main h3[id], .main h4[id], .main h5[id], .main h6[id],
.main li[id] { scroll-margin-top: 64px; }
a.term-link { color: #a3c9ff; text-decoration: underline dotted; cursor: help; }
a.term-link:hover, a.term-link:focus { color: #cfe3ff; }
.term-popover { position: absolute; z-index: 500; max-width: 340px; background: #161c27;
                border: 1px solid #37415a; border-radius: 8px; padding: 10px 12px;
                font-size: 13px; line-height: 1.45; color: #dbe2f0; box-shadow: 0 6px 24px rgba(0,0,0,.45);
                pointer-events: auto; }
.term-popover .term-popover-title { font-weight: 600; color: #a3c9ff; margin-bottom: 4px;
                                     font-size: 12px; text-transform: uppercase; letter-spacing: .03em; }
.term-popover a.term-link { color: #cfe3ff; }
"""

MARK_LAYER_CSS = r"""
body.mark-layer {
  --ml-paper:#FCFCFA; --ml-panel:#F3F3EF; --ml-ink:#1A1C22; --ml-graphite:#63666F;
  --ml-rule:#DBDCD6; --ml-rule-soft:#E8E8E3; --ml-read:#EFEFE9;
  --ml-blue:#27508C; --ml-blue-soft:#E7EDF6;
  --ml-agree:#3B7358; --ml-clarify:#9A6A14; --ml-rewrite:#27508C;
  --ml-strike:#AE352F; --ml-note:#5A4A8A; --ml-ruling:#27508C;
  --ml-ack:#63666F; --ml-focus:#27508C;
  background:var(--ml-paper); color:var(--ml-ink);
}
@media (prefers-color-scheme: dark) {
  body.mark-layer {
    --ml-paper:#16171C; --ml-panel:#1E2027; --ml-ink:#E5E4DE; --ml-graphite:#9A9DA6;
    --ml-rule:#2E313A; --ml-rule-soft:#24272E; --ml-read:#1F2129;
    --ml-blue:#8FB3E0; --ml-blue-soft:#1C2532;
    --ml-agree:#7FBE9C; --ml-clarify:#D8AC5A; --ml-rewrite:#8FB3E0;
    --ml-strike:#E08984; --ml-note:#B0A0DA; --ml-ruling:#8FB3E0;
    --ml-ack:#9A9DA6; --ml-focus:#8FB3E0;
  }
}
body.mark-layer .sidebar { background:var(--ml-panel); border-color:var(--ml-rule); }
/* Inline code, tables, quotes and rules inherit the v1 dark palette above; on the
   light paper that rendered every code span as an unreadable dark bar (found
   2026-09-02 on the scheduled-jobs review, 100+ spans). Re-theme them with the
   mark-layer tokens so both colour schemes stay legible. */
body.mark-layer code { background:var(--ml-panel); color:var(--ml-ink); }
body.mark-layer pre code { background:var(--ml-panel); }
body.mark-layer th, body.mark-layer td { border-color:var(--ml-rule); }
body.mark-layer th { background:var(--ml-panel); }
body.mark-layer blockquote { border-left-color:var(--ml-rule); color:var(--ml-graphite); }
body.mark-layer hr { border-top-color:var(--ml-rule); }
body.mark-layer .block-wrap:hover { background:var(--ml-read); }
body.mark-layer .main {
  max-width:47rem; padding:3.25rem 1.5rem 12rem;
  font-family:Spectral,Georgia,serif; font-size:17px; line-height:1.65;
  -webkit-font-smoothing:antialiased;
}
body.mark-layer .top-actions, body.mark-layer .page-discussion,
body.mark-layer .comment-affordance, body.mark-layer .comment-box,
body.mark-layer .comment-thread, body.mark-layer .edit-hint-label,
body.mark-layer .document-title-block { display:none !important; }
.review-doc-header { border-bottom:1px solid var(--ml-rule); padding-bottom:1.6rem; }
.review-eyebrow { font-family:"IBM Plex Sans",system-ui,sans-serif; font-size:.688rem;
  letter-spacing:.14em; text-transform:uppercase; color:var(--ml-graphite); font-weight:600; }
.review-doc-header h1 { font-size:2.3rem; line-height:1.12; font-weight:600;
  margin:.55rem 0 .6rem; letter-spacing:-.015em; text-wrap:balance; }
.review-doc-header .review-sub { color:var(--ml-graphite); font-size:1rem; margin:0; max-width:36rem; }
.review-gears { display:flex; flex-wrap:wrap; gap:.5rem; margin-top:1.3rem; }
body.mark-layer .block-wrap { position:relative; padding:.8rem 0 .8rem 2.2rem;
  border-radius:0; border-bottom:1px solid var(--ml-rule-soft); margin:0; }
body.mark-layer .block-wrap:hover { background:transparent; }
body.mark-layer .block-wrap[data-kind="heading"] { border-bottom:none; padding-bottom:.1rem; }
body.mark-layer .block-wrap[data-kind="heading"] h1,
body.mark-layer .block-wrap[data-kind="heading"] h2,
body.mark-layer .block-wrap[data-kind="heading"] h3,
body.mark-layer .block-wrap[data-kind="heading"] h4 {
  font-family:"IBM Plex Sans",system-ui,sans-serif; font-size:.75rem; line-height:1.4;
  letter-spacing:.14em; text-transform:uppercase; font-weight:600;
  color:var(--ml-blue); margin:2rem 0 0;
}
body.mark-layer .block-body p { margin:0; }
body.mark-layer .block-body.edit-hint { cursor:default; }
body.mark-layer .block-body.edit-hint:hover { outline:none; }
body.mark-layer .block-wrap.mark-decision .block-body > p,
body.mark-layer .block-wrap.mark-decision .block-body > blockquote {
  border-left:2px solid var(--ml-blue); padding-left:1rem; margin-left:-1rem;
}
.ml-gutter { position:absolute; left:0; top:.85rem; width:1.7rem;
  font-family:"IBM Plex Mono",ui-monospace,monospace; font-size:.9rem; line-height:1.25;
  display:flex; flex-direction:column; gap:.05rem; }
.ml-decision-tag { font-family:"IBM Plex Sans",system-ui,sans-serif; font-size:.625rem;
  letter-spacing:.1em; text-transform:uppercase; font-weight:600; color:var(--ml-blue);
  background:var(--ml-blue-soft); padding:.14rem .4rem; display:inline-block; margin-bottom:.4rem; }
.mark-sentence, .mark-block-unit { border-radius:1px; cursor:pointer; }
.mark-sentence.ml-seen, .mark-block-unit.ml-seen { background:var(--ml-read); }
.mark-sentence.ml-current, .mark-block-unit.ml-current {
  background:var(--ml-blue-soft); box-shadow:0 0 0 2px var(--ml-blue-soft); }
.mark-sentence.ml-struck, .mark-block-unit.ml-struck {
  color:var(--ml-strike); text-decoration:line-through; }
.mark-sentence del { color:var(--ml-strike); text-decoration-thickness:1px; }
.mark-sentence ins { color:var(--ml-rewrite); text-decoration:none; border-bottom:1px solid var(--ml-rewrite); }
.ml-glyph { font-family:"IBM Plex Mono",ui-monospace,monospace; font-size:.78em;
  vertical-align:.15em; margin-right:.15em; letter-spacing:-.02em; }
.ml-hint { font-family:"IBM Plex Sans",system-ui,sans-serif; font-size:.66rem;
  letter-spacing:.08em; text-transform:uppercase; color:var(--ml-graphite); margin-top:.45rem; }
.ml-hint b { color:var(--ml-ink); font-weight:600; }
.ml-marks { display:flex; flex-direction:column; margin-top:.6rem; }
.ml-mark { border-top:1px solid var(--ml-rule); padding:.55rem 0 .5rem;
  font-family:"IBM Plex Sans",system-ui,sans-serif; font-size:.85rem; line-height:1.5; }
.ml-mark .ml-who { font-size:.625rem; letter-spacing:.1em; text-transform:uppercase;
  font-weight:600; display:flex; align-items:baseline; gap:.5rem; }
.ml-mark .ml-body { margin-top:.25rem; white-space:pre-wrap; }
.ml-mark .ml-quote { font-family:Spectral,Georgia,serif; font-size:.9rem;
  color:var(--ml-graphite); border-left:2px solid var(--ml-rule);
  padding-left:.6rem; margin-bottom:.35rem; }
.ml-mark .ml-meta { font-family:"IBM Plex Mono",monospace; font-size:.6rem;
  color:var(--ml-graphite); letter-spacing:0; }
.ml-mark.ml-agree .ml-who { color:var(--ml-agree); }
.ml-mark.ml-clarify .ml-who { color:var(--ml-clarify); }
.ml-mark.ml-rewrite .ml-who { color:var(--ml-rewrite); }
.ml-mark.ml-strike .ml-who { color:var(--ml-strike); }
.ml-mark.ml-note .ml-who { color:var(--ml-note); }
.ml-mark.ml-ruling .ml-who { color:var(--ml-ruling); }
.ml-mark.ml-ack .ml-who { color:var(--ml-ack); }
.ml-drop { background:none; border:none; color:var(--ml-graphite);
  font-family:"IBM Plex Mono",monospace; font-size:.75rem; padding:0 .2rem; margin-left:auto; }
.ml-drop:hover { color:var(--ml-strike); }
.ml-section-end { display:flex; gap:.4rem; flex-wrap:wrap; align-items:center;
  margin-top:.9rem; padding-top:.7rem; border-top:1px solid var(--ml-rule); }
.ml-label { font-family:"IBM Plex Sans",system-ui,sans-serif; font-size:.625rem;
  letter-spacing:.1em; text-transform:uppercase; font-weight:600;
  color:var(--ml-graphite); margin-right:.2rem; }
.ml-rulebar { display:flex; gap:.4rem; flex-wrap:wrap; margin-top:.7rem; align-items:center; }
.ml-act { font-family:"IBM Plex Sans",system-ui,sans-serif; font-size:.7rem;
  letter-spacing:.09em; text-transform:uppercase; font-weight:600; padding:.42rem .85rem;
  border:1px solid var(--ml-ink); background:var(--ml-ink); color:var(--ml-paper); cursor:pointer; }
.ml-act.ml-ghost { background:transparent; color:var(--ml-graphite); border-color:var(--ml-rule); }
.ml-act.ml-ghost:hover { color:var(--ml-ink); border-color:var(--ml-ink); }
.ml-act:focus-visible, .ml-composer textarea:focus-visible { outline:2px solid var(--ml-focus); outline-offset:2px; }
.ml-composer { margin-top:.6rem; display:flex; flex-direction:column; gap:.45rem; }
.ml-composer textarea { font-family:"IBM Plex Sans",system-ui,sans-serif; font-size:.9rem;
  line-height:1.5; background:var(--ml-paper); color:var(--ml-ink);
  border:1px solid var(--ml-rule); padding:.55rem .65rem; resize:vertical; width:100%; min-height:4rem; }
.ml-composer.ml-edit textarea { min-height:5.5rem; font-family:Spectral,Georgia,serif; font-size:1rem; line-height:1.6; }
.ml-composer-row { display:flex; gap:.4rem; align-items:center; flex-wrap:wrap; }
.ml-legend { margin-top:2.25rem; padding-top:1rem; border-top:1px solid var(--ml-rule);
  font-family:"IBM Plex Sans",system-ui,sans-serif; font-size:.74rem; color:var(--ml-graphite);
  display:flex; flex-wrap:wrap; gap:.85rem 1.3rem; }
.ml-legend code { font-family:"IBM Plex Mono",monospace; color:var(--ml-ink); }
.ml-dock { position:fixed; left:260px; right:0; bottom:0; background:var(--ml-panel);
  border-top:1px solid var(--ml-rule); padding:.7rem 1.25rem; z-index:20;
  font-family:"IBM Plex Sans",system-ui,sans-serif; font-size:.78rem; color:var(--ml-graphite); }
.ml-dock-inner { max-width:44rem; margin:0 auto; display:flex; gap:.85rem;
  align-items:center; flex-wrap:wrap; }
.ml-counter { display:flex; align-items:center; gap:.5rem; background:transparent;
  border:1px solid var(--ml-rule); padding:.3rem .6rem; color:var(--ml-ink); }
.ml-counter:hover { border-color:var(--ml-ink); }
.ml-counter .ml-n { font-family:"IBM Plex Mono",monospace; font-weight:500; font-size:.95rem; }
.ml-counter .ml-lb { font-size:.68rem; letter-spacing:.09em; text-transform:uppercase; color:var(--ml-graphite); }
.ml-fill { flex:1; min-width:5rem; height:2px; background:var(--ml-rule); position:relative; }
.ml-fill i { position:absolute; inset:0 auto 0 0; background:var(--ml-clarify); display:block; }
.ml-trace { font-family:"IBM Plex Mono",monospace; font-size:.68rem; letter-spacing:0; }
body.mark-layer .unresolved-marks { border-color:var(--ml-strike); background:transparent; }
@media (max-width:760px) {
  body.mark-layer { display:block; }
  body.mark-layer .sidebar { position:relative; width:100%; height:auto; max-height:12rem; }
  body.mark-layer .main { font-size:16px; }
  .review-doc-header h1 { font-size:1.85rem; }
  .ml-dock { left:0; }
}
"""


PAGE_JS = r"""
const ROUTE = window.__ROUTE__;
const API_BASE = window.__API_BASE__ || '';

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2200);
}

function badgeClass(status) {
  return 'badge badge-' + (status || 'queued');
}

function b64ToUtf8(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new TextDecoder('utf-8').decode(bytes);
}

function escapeHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// Minimal word-level diff (LCS-based) for rendering suggested-edit comments inline.
// Good enough for short-to-medium block text; not meant to compete with a real diff lib.
function wordDiff(before, after) {
  const a = before.split(/(\s+)/);
  const b = after.split(/(\s+)/);
  const dp = Array.from({length: a.length+1}, () => new Array(b.length+1).fill(0));
  for (let i = a.length-1; i>=0; i--) {
    for (let j = b.length-1; j>=0; j--) {
      dp[i][j] = a[i]===b[j] ? dp[i+1][j+1]+1 : Math.max(dp[i+1][j], dp[i][j+1]);
    }
  }
  let i=0, j=0, out=[];
  while (i<a.length && j<b.length) {
    if (a[i]===b[j]) { out.push({t:'eq', v:a[i]}); i++; j++; }
    else if (dp[i+1][j] >= dp[i][j+1]) { out.push({t:'del', v:a[i]}); i++; }
    else { out.push({t:'ins', v:b[j]}); j++; }
  }
  while (i<a.length) { out.push({t:'del', v:a[i]}); i++; }
  while (j<b.length) { out.push({t:'ins', v:b[j]}); j++; }
  return out;
}

function renderDiffHtml(before, after) {
  const parts = wordDiff(before, after);
  return parts.map(p => {
    const esc = escapeHtml(p.v);
    if (p.t === 'del') return `<span class="diff-del">${esc}</span>`;
    if (p.t === 'ins') return `<span class="diff-ins">${esc}</span>`;
    return esc;
  }).join('');
}

async function fetchComments() {
  const res = await fetch(`${API_BASE}/api/comments?page=${encodeURIComponent(ROUTE)}`);
  return res.json();
}

function renderCommentItem(c) {
  const div = document.createElement('div');
  div.className = 'comment-item' + (c.type === 'edit' ? ' is-edit' : '')
                   + (c.type === 'verdict' ? ' is-verdict' : '') + (c.deleted ? ' deleted' : '');
  div.dataset.id = c.id;
  const badge = c.type === 'verdict'
    ? `<span class="badge badge-verdict-${c.verdict}">${c.verdict}</span>`
    : `<span class="${badgeClass(c.status)}">${c.status}</span>`;
  const editedNote = c.edited_at ? ' <span style="color:#6a7280">(edited)</span>' : '';
  const deletedNote = c.deleted ? ' <span style="color:#6a7280">(deleted)</span>' : '';
  let bodyHtml;
  if (c.type === 'edit') {
    bodyHtml = `<div class="diff-view">${renderDiffHtml(c.snapshot || '', c.proposed || '')}</div>`;
  } else if (c.type === 'verdict') {
    bodyHtml = `<div class="comment-text">Verdict: <strong>${c.verdict}</strong>${c.text && c.text !== 'Verdict: ' + c.verdict ? ' — ' + escapeHtml(c.text) : ''}</div>`;
  } else {
    bodyHtml = `<div class="comment-text">${escapeHtml(c.text)}</div>`;
  }
  const canEditDelete = c.author === 'mike' && !c.deleted && c.type !== 'edit' && c.type !== 'verdict';
  const actions = canEditDelete
    ? `<span class="comment-actions"><button class="edit-comment-btn" title="Edit">&#9998;</button><button class="delete-comment-btn" title="Delete">&#128465;</button></span>`
    : '';
  div.innerHTML = `<div class="meta">${actions}${c.author} · ${new Date(c.timestamp).toLocaleString()} · ${badge}${editedNote}${deletedNote}</div>
    ${bodyHtml}`;

  if (canEditDelete) {
    div.querySelector('.edit-comment-btn').addEventListener('click', () => startInlineEdit(div, c));
    div.querySelector('.delete-comment-btn').addEventListener('click', () => deleteComment(div, c));
  }
  return div;
}

function startInlineEdit(div, c) {
  const bodyEl = div.querySelector('.comment-text, .diff-view');
  const ta = document.createElement('textarea');
  ta.className = 'edit-inline-textarea';
  ta.value = c.text;
  bodyEl.replaceWith(ta);
  ta.focus();
  const commit = async () => {
    const text = ta.value.trim();
    if (text && text !== c.text) {
      await fetch(`${API_BASE}/api/comments/update`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ page: ROUTE, id: c.id, text })
      });
      toast('Comment updated.');
    }
    loadThreadsIntoDOM();
  };
  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); commit(); }
    if (e.key === 'Escape') { loadThreadsIntoDOM(); }
  });
  ta.addEventListener('blur', commit);
}

async function deleteComment(div, c) {
  if (!confirm('Delete this comment? (soft-delete, kept in the audit trail)')) return;
  await fetch(`${API_BASE}/api/comments/delete`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ page: ROUTE, id: c.id })
  });
  toast('Comment deleted.');
  loadThreadsIntoDOM();
}

function isCursorDispatched(c) {
  return !!(c && c.cursor_dispatched);
}

async function loadThreadsIntoDOM() {
  const comments = await fetchComments();
  const visible = comments.filter(c => !isCursorDispatched(c));
  const blockElements = Array.from(document.querySelectorAll('.block-wrap'));
  const blockIds = new Set(blockElements.map(el => el.dataset.blockId));
  const anchors = new Set(blockElements.map(el => el.dataset.anchor));
  const byBlockId = {};
  const byAnchor = {};
  const pageLevel = [];
  const unresolved = [];
  for (const c of visible) {
    if (c.unresolved) { unresolved.push(c); continue; }
    if (c.block_id && blockIds.has(c.block_id)) {
      (byBlockId[c.block_id] = byBlockId[c.block_id] || []).push(c);
      continue;
    }
    if (c.anchor && anchors.has(c.anchor)) {
      (byAnchor[c.anchor] = byAnchor[c.anchor] || []).push(c);
      continue;
    }
    if (!c.block_id && !c.anchor && !c.quote) { pageLevel.push(c); continue; }
    unresolved.push(c);
  }

  // Pre-mark verdict-row status from the most recent verdict comment for that
  // row_id (survives page reload — the button state itself isn't persisted,
  // only the comment is, so we reconstruct the "already voted" indicator here).
  const latestVerdictByRow = {};
  for (const c of comments) {
    if (c.type === 'verdict' && c.row_id && !c.deleted) {
      const prev = latestVerdictByRow[c.row_id];
      if (!prev || c.timestamp > prev.timestamp) latestVerdictByRow[c.row_id] = c;
    }
  }
  let sentRecCount = 0;
  document.querySelectorAll('.verdict-row').forEach(rowEl => {
    const rowId = rowEl.dataset.rowId;
    const v = latestVerdictByRow[rowId];
    const statusEl = rowEl.querySelector('.verdict-status');
    if (v && statusEl) statusEl.textContent = `✓ ${v.verdict}`;
  });
  document.querySelectorAll('.review-row').forEach(rowEl => {
    const rowId = rowEl.dataset.rowId;
    const v = latestVerdictByRow[rowId];
    const statusEl = rowEl.querySelector('.review-status');
    const wrap = rowEl.closest('.block-wrap');
    const sent = v && v.verdict === 'agree' && isCursorDispatched(v);
    if (sent) {
      sentRecCount += 1;
      rowEl.classList.add('is-sent');
      if (statusEl) statusEl.textContent = '✓ Sent to Grok';
      if (wrap) wrap.classList.add('cursor-sent');
    } else if (v && statusEl) {
      statusEl.textContent = `✓ ${v.verdict}`;
    }
  });
  let renderedNonDeleted = 0;
  blockElements.forEach(el => {
    const blockId = el.dataset.blockId;
    const anchor = el.dataset.anchor;
    const list = [...(byBlockId[blockId] || []), ...(byAnchor[anchor] || [])]
      .filter(c => !c.deleted || c.author === 'mike');
    const threadEl = el.querySelector('.comment-thread');
    threadEl.innerHTML = '';
    if (list.length) {
      el.classList.add('has-comments');
      let pill = el.querySelector('.comment-count-pill');
      if (!pill) {
        pill = document.createElement('span');
        pill.className = 'comment-count-pill';
        el.appendChild(pill);
      }
      pill.textContent = list.filter(c => !c.deleted).length || list.length;
      renderedNonDeleted += list.filter(c => !c.deleted).length;
      list.sort((a,b) => a.timestamp.localeCompare(b.timestamp))
          .forEach(c => threadEl.appendChild(renderCommentItem(c)));
    } else {
      el.classList.remove('has-comments');
      const pill = el.querySelector('.comment-count-pill');
      if (pill) pill.remove();
    }
  });
  const pageThread = document.getElementById('page-thread-list');
  pageThread.innerHTML = '';
  pageLevel.sort((a,b) => a.timestamp.localeCompare(b.timestamp))
            .forEach(c => pageThread.appendChild(renderCommentItem(c)));

  const unresolvedSection = document.getElementById('unresolved-marks');
  const unresolvedList = document.getElementById('unresolved-thread-list');
  const unresolvedVisible = unresolved.filter(c => !c.deleted || c.author === 'mike');
  unresolvedList.innerHTML = '';
  unresolvedVisible.sort((a,b) => a.timestamp.localeCompare(b.timestamp)).forEach(c => {
    const wrap = document.createElement('div');
    wrap.className = 'unresolved-item';
    const path = Array.isArray(c.heading_path) ? c.heading_path.join(' › ') : '';
    wrap.innerHTML = `<div class="unresolved-location">${escapeHtml(path || 'Original location unavailable')}</div>`
      + `<blockquote class="unresolved-quote">${escapeHtml(c.quote || c.snapshot || '(no source quote)')}</blockquote>`;
    wrap.appendChild(renderCommentItem(c));
    unresolvedList.appendChild(wrap);
  });
  const unresolvedCount = unresolved.filter(c => !c.deleted).length;
  document.getElementById('unresolved-count').textContent = unresolvedCount;
  unresolvedSection.hidden = unresolvedVisible.length === 0;

  const nonDeleted = comments.filter(c => !c.deleted).length;
  const hidden = comments.filter(c => !c.deleted && isCursorDispatched(c)).length;
  const pageCount = pageLevel.filter(c => !c.deleted).length;
  if (renderedNonDeleted + pageCount + unresolvedCount + hidden !== nonDeleted) {
    console.error('mark render invariant failed', {
      renderedNonDeleted, pageCount, unresolvedCount, hidden, nonDeleted
    });
  }

  updateCursorSentBanner(sentRecCount);
}

function updateCursorSentBanner(sentRecCount) {
  if (window.__DISPATCH_TARGET__ !== 'cursor') return;
  let banner = document.getElementById('cursor-sent-banner');
  const flash = sessionStorage.getItem('soma-cursor-sent-flash');
  if (flash) {
    try {
      const data = JSON.parse(flash);
      sessionStorage.removeItem('soma-cursor-sent-flash');
      if (!banner) {
        banner = document.createElement('div');
        banner.id = 'cursor-sent-banner';
        banner.className = 'cursor-sent-banner';
        const actions = document.querySelector('.top-actions');
        if (actions) actions.insertAdjacentElement('afterend', banner);
      }
      const n = data.dispatched_count || 0;
      const card = data.card_path || '';
      banner.innerHTML = `Sent <strong>${n}</strong> item(s) to Grok`
        + (card ? ` · <code>${escapeHtml(card)}</code>` : '')
        + ' — dispatched recommendations are hidden below. Disagree/Defer items stay visible.';
      return;
    } catch (e) { /* ignore */ }
  }
  if (sentRecCount > 0) {
    if (!banner) {
      banner = document.createElement('div');
      banner.id = 'cursor-sent-banner';
      banner.className = 'cursor-sent-banner';
      const actions = document.querySelector('.top-actions');
      if (actions) actions.insertAdjacentElement('afterend', banner);
    }
    banner.textContent = `${sentRecCount} recommendation(s) already sent to Grok (hidden). Open SOMA/cursor-intake/inbox/ for the card.`;
  } else if (banner) {
    banner.remove();
  }
}

function blockPayload(el) {
  if (!el) return {block_id: null, from: null, to: null, quote: null, block_text_sha: null};
  return {
    block_id: el.dataset.blockId,
    from: 0,
    to: null,
    quote: b64ToUtf8(el.dataset.normText),
    block_text_sha: el.dataset.blockSha,
  };
}

async function postComment({anchor, snapshot, text, threadId, type, proposed, row_id, verdict,
                            block_id, from, to, quote, block_text_sha, mark_kind, strength,
                            scope, reason, sent_because}) {
  const res = await fetch(`${API_BASE}/api/comments`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ page: ROUTE, anchor, snapshot, text, thread_id: threadId || null,
                            type: type || 'comment', proposed, row_id, verdict,
                            block_id, from, to, quote, block_text_sha, mark_kind, strength,
                            scope, reason, sent_because })
  });
  if (!res.ok) throw new Error('post failed: ' + res.status);
  const created = await res.json();
  // Hook point for overlays (Quinn tour verdict capture listens for this):
  // fired after ANY successful comment save with the created row as detail.
  try { document.dispatchEvent(new CustomEvent('soma-comment-saved', { detail: created })); } catch (e) {}
  return created;
}

// --- Voice-in (Web Speech API, Chrome/webkit only; hides gracefully elsewhere) ---
const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;

function wireMic(btn, textarea) {
  if (!SpeechRec) { btn.style.display = 'none'; return; }
  let rec = null;
  let recording = false;
  btn.addEventListener('click', () => {
    if (recording) { rec && rec.stop(); return; }
    rec = new SpeechRec();
    rec.lang = 'en-US';
    rec.interimResults = false;
    rec.continuous = false;
    rec.onstart = () => { recording = true; btn.classList.add('recording'); };
    rec.onerror = () => { recording = false; btn.classList.remove('recording'); };
    rec.onend = () => { recording = false; btn.classList.remove('recording'); };
    rec.onresult = (e) => {
      let transcript = '';
      for (let i = 0; i < e.results.length; i++) transcript += e.results[i][0].transcript;
      const sep = textarea.value && !textarea.value.endsWith(' ') ? ' ' : '';
      textarea.value = textarea.value + sep + transcript;
      textarea.focus();
    };
    rec.start();
  });
}

// --- Edit-as-comment: click a block body -> textarea with raw markdown source.
// On blur, if changed, POST a {type: "edit"} comment (snapshot=before, proposed=after).
// The underlying .md file is never touched here — this only proposes.
function wireEditableBlocks() {
  document.querySelectorAll('.block-wrap.edit-eligible').forEach(el => {
    const body = el.querySelector('.block-body');
    body.classList.add('edit-hint');
    body.addEventListener('click', (e) => {
      if (e.target.closest('a')) return; // don't hijack link clicks
      if (body.querySelector('textarea')) return; // already editing
      enterEditMode(el, body);
    });
  });
}

function enterEditMode(el, body) {
  const before = b64ToUtf8(el.dataset.source);
  const originalHtml = body.innerHTML;
  const ta = document.createElement('textarea');
  ta.className = 'block-edit-textarea';
  ta.value = before;
  body.innerHTML = '';
  body.appendChild(ta);
  ta.style.height = Math.max(48, ta.scrollHeight) + 'px';
  ta.focus();

  let settled = false;
  const settle = async (commit) => {
    if (settled) return;
    settled = true;
    const after = ta.value;
    if (commit && after.trim() !== before.trim()) {
      await postComment({
        anchor: el.dataset.anchor, snapshot: before, proposed: after,
        text: 'Suggested edit', type: 'edit', ...blockPayload(el),
      });
      toast('Edit proposed — saved as a comment.');
      loadThreadsIntoDOM();
    }
    body.innerHTML = originalHtml;
  };

  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      // Cmd/Ctrl+Enter while editing text: commit the edit immediately.
      e.preventDefault();
      settle(true);
      return;
    }
    if (e.key === 'Escape') { e.preventDefault(); settle(false); return; }
  });
  ta.addEventListener('blur', () => settle(true));
}

// Enter at the end of a (non-edit-mode) block, or Cmd/Enter anywhere in the block,
// opens the inline comment box right there (separate from edit-as-comment above,
// which triggers on click-into-the-body-text).
function wireEnterOpensComment() {
  document.querySelectorAll('.block-wrap').forEach(el => {
    const body = el.querySelector('.block-body');
    if (!body) return;
    body.addEventListener('keydown', (e) => {
      if (body.querySelector('textarea')) return; // in edit mode, let that handler own Enter
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        const box = el.querySelector('.comment-box');
        box.classList.add('open');
        box.querySelector('textarea').focus();
      }
    });
  });
}

function wireBlockAffordances() {
  document.querySelectorAll('.block-wrap').forEach(el => {
    const btn = el.querySelector('.comment-affordance');
    const box = el.querySelector('.comment-box');
    const ta = box.querySelector('textarea');
    wireMic(box.querySelector('.mic-btn'), ta);
    btn.addEventListener('click', () => {
      box.classList.toggle('open');
      if (box.classList.contains('open')) ta.focus();
    });
    const save = async () => {
      const text = ta.value.trim();
      if (!text) return;
      await postComment({ anchor: el.dataset.anchor, snapshot: el.dataset.snapshot, text,
                          ...blockPayload(el) });
      ta.value = '';
      box.classList.remove('open');
      toast('Comment saved.');
      loadThreadsIntoDOM();
    };
    box.querySelector('.save-btn').addEventListener('click', save);
    ta.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); save(); }
      if (e.key === 'Escape') { box.classList.remove('open'); }
    });
  });

  const pageBox = document.getElementById('page-comment-box');
  if (pageBox) {
    const ta = pageBox.querySelector('textarea');
    wireMic(pageBox.querySelector('.mic-btn'), ta);
    const save = async () => {
      const text = ta.value.trim();
      if (!text) return;
      await postComment({ anchor: null, snapshot: '(page-level)', text,
                          block_id: null, from: null, to: null, quote: null, block_text_sha: null });
      ta.value = '';
      toast('Comment saved.');
      loadThreadsIntoDOM();
    };
    pageBox.querySelector('.save-btn').addEventListener('click', save);
    ta.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); save(); }
    });
  }

  const sendBtn = document.getElementById('send-to-dee');
  const dispatchButtonLabel = window.__DISPATCH_BUTTON__ || 'Send to Dee';
  if (sendBtn) {
    sendBtn.textContent = dispatchButtonLabel;
    sendBtn.addEventListener('click', async () => {
      sendBtn.disabled = true;
      sendBtn.textContent = 'Sending...';
      try {
        const res = await fetch(`${API_BASE}/api/dispatch`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ page: ROUTE })
        });
        const data = await res.json();
        if (res.ok) {
          if (data.target === 'cursor') {
            sessionStorage.setItem('soma-cursor-sent-flash', JSON.stringify({
              card_path: data.card_path,
              dispatched_count: data.dispatched_count || 0
            }));
            location.reload();
            return;
          }
          if (data.target === 'rsi') {
            const count = data.dispatched_count || 0;
            toast(`Filed ${count} feedback card${count !== 1 ? 's' : ''} · team will see it on the board`);
            setTimeout(() => location.reload(), 800);
            return;
          }
          toast('Dispatched: ' + data.task_name);
        } else {
          toast('Dispatch failed: ' + (data.error || res.status));
        }
      } catch (e) {
        toast('Dispatch error: ' + e.message);
      }
      sendBtn.disabled = false;
      sendBtn.textContent = dispatchButtonLabel;
    });
  }

  const regenBtn = document.getElementById('regenerate-board');
  if (regenBtn) {
    regenBtn.addEventListener('click', async () => {
      regenBtn.disabled = true;
      regenBtn.textContent = 'Regenerating...';
      try {
        const res = await fetch(`${API_BASE}/api/board/regenerate`, { method: 'POST' });
        const data = await res.json();
        if (res.ok) {
          toast('Board + Portfolio regenerated — reloading.');
          setTimeout(() => location.reload(), 600);
        } else {
          toast('Regenerate failed: ' + (data.error || res.status));
        }
      } catch (e) {
        toast('Regenerate error: ' + e.message);
      }
      regenBtn.disabled = false;
      regenBtn.textContent = 'Regenerate board';
    });
  }
}

// --- Portfolio verdict buttons (keep/restart/cancel/later) -----------------
// Buttons are rendered server-side inside a table cell by mdblocks.py's
// _VERDICT_TOKEN_RE ([[VERDICT:<row-id>]] -> .verdict-row span with 4
// .verdict-btn children), emitted by v2/generate_portfolio.py. Clicking posts
// a {type:"verdict", verdict, row_id} comment anchored to the containing
// block (so it shows up in that block's comment thread like anything else).
function wireVerdictButtons() {
  document.querySelectorAll('.verdict-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const rowId = btn.dataset.rowId;
      const verdict = btn.dataset.verdict;
      const wrap = btn.closest('.block-wrap');
      const anchor = wrap ? wrap.dataset.anchor : null;
      const snapshot = wrap ? wrap.dataset.snapshot : rowId;
      const statusEl = btn.closest('.verdict-row').querySelector('.verdict-status');
      btn.disabled = true;
      try {
        await postComment({ anchor, snapshot, text: `Verdict: ${verdict}`, type: 'verdict', row_id: rowId, verdict,
                            ...blockPayload(wrap) });
      } catch (e) {
        toast('Verdict post failed: ' + e.message);
        btn.disabled = false;
        return;
      }
      if (statusEl) statusEl.textContent = `✓ ${verdict}`;
      toast(`Verdict recorded: ${verdict} (${rowId})`);
      loadThreadsIntoDOM();
      btn.disabled = false;
    });
  });
}

// --- Recommendation review buttons (agree/disagree/discuss/defer) -----------
function wireReviewButtons() {
  document.querySelectorAll('.review-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const rowId = btn.dataset.rowId;
      const verdict = btn.dataset.review;
      const wrap = btn.closest('.block-wrap');
      const anchor = wrap ? wrap.dataset.anchor : null;
      const snapshot = wrap ? wrap.dataset.snapshot : rowId;
      const statusEl = btn.closest('.review-row').querySelector('.review-status');
      if (verdict === 'discuss' && wrap) {
        const box = wrap.querySelector('.comment-box');
        const ta = box && box.querySelector('textarea');
        if (box && ta) {
          box.classList.add('open');
          ta.placeholder = 'Your thoughts on this recommendation…';
          ta.focus();
        }
      }
      btn.disabled = true;
      try {
        await postComment({
          anchor,
          snapshot,
          text: `Review: ${verdict}`,
          type: 'verdict',
          row_id: rowId,
          verdict,
          ...blockPayload(wrap)
        });
      } catch (e) {
        toast('Review post failed: ' + e.message);
        btn.disabled = false;
        return;
      }
      if (statusEl) statusEl.textContent = `✓ ${verdict}`;
      if (verdict === 'agree' && window.__DISPATCH_TARGET__ === 'cursor') {
        toast(`Agreed — staged for Grok (${rowId}). Click Send to Grok when ready.`);
      } else {
        toast(`Recorded: ${verdict} (${rowId})`);
      }
      loadThreadsIntoDOM();
      btn.disabled = false;
    });
  });
}

function wirePublicFilmToggles() {
  document.querySelectorAll('.public-film-toggle').forEach(input => {
    input.addEventListener('change', async () => {
      const testid = input.dataset.filmTestid;
      const nextValue = input.checked;
      input.disabled = true;
      try {
        const res = await fetch(`${API_BASE}/api/public-film`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ testid, public: nextValue })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || res.status);
        toast(nextValue ? 'Film marked public.' : 'Film removed from public list.');
      } catch (e) {
        input.checked = !nextValue;
        toast('Public-film update failed: ' + e.message);
      }
      input.disabled = false;
    });
  });
}

// --- Term tooltips: hover/tap a .term-link to see its definition inline,
// without leaving the page. `title` attr (set server-side) covers plain
// non-JS hover; this adds a richer popover whose content is rendered HTML
// (mdblocks.render_inline output), so a definition that itself links to
// another term shows its own popover on hover too (recursion via the same
// event-delegated listener, since popovers are appended to the live DOM).
function wireTermPopovers() {
  const DEFS = window.__TERM_DEFS__ || {};
  let popEl = null;
  let hideTimer = null;

  function hidePopover() {
    if (popEl) { popEl.remove(); popEl = null; }
  }

  function scheduleHide() {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hidePopover, 220);
  }

  function cancelHide() {
    clearTimeout(hideTimer);
  }

  function showPopover(anchor) {
    const slug = anchor.dataset.termSlug;
    const entry = DEFS[slug];
    if (!entry) return;
    hidePopover();
    const pop = document.createElement('div');
    pop.className = 'term-popover';
    pop.innerHTML = `<div class="term-popover-title">${entry.term.replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]))}</div>${entry.html}`;
    pop.addEventListener('mouseenter', cancelHide);
    pop.addEventListener('mouseleave', scheduleHide);
    document.body.appendChild(pop);
    const r = anchor.getBoundingClientRect();
    const top = window.scrollY + r.bottom + 6;
    let left = window.scrollX + r.left;
    const maxLeft = window.scrollX + document.documentElement.clientWidth - pop.offsetWidth - 12;
    if (left > maxLeft) left = Math.max(8, maxLeft);
    pop.style.top = top + 'px';
    pop.style.left = left + 'px';
    popEl = pop;
  }

  document.addEventListener('mouseover', (e) => {
    const a = e.target.closest('a.term-link');
    if (!a) return;
    cancelHide();
    showPopover(a);
  });
  document.addEventListener('mouseout', (e) => {
    const a = e.target.closest('a.term-link');
    if (!a) return;
    scheduleHide();
  });
  // Tap-to-show on touch devices: first tap opens the popover and suppresses
  // the jump-to-anchor navigation; a second tap (or tapping elsewhere) lets
  // the click through / dismisses it.
  document.addEventListener('click', (e) => {
    const a = e.target.closest('a.term-link');
    if (!a) {
      hidePopover();
      return;
    }
    if (!('ontouchstart' in window)) return; // desktop: hover already handled it, let the jump happen
    if (popEl && a.dataset.termSlug === (popEl.dataset.forSlug || '')) return; // second tap: let it navigate
    e.preventDefault();
    showPopover(a);
    popEl.dataset.forSlug = a.dataset.termSlug;
  });
}

document.addEventListener('DOMContentLoaded', () => {
  wireTermPopovers();
  if (window.__MARK_LAYER__ && typeof window.initMarkLayer === 'function') {
    wireVerdictButtons();
    wireReviewButtons();
    wirePublicFilmToggles();
    window.initMarkLayer();
    return;
  }
  wireBlockAffordances();
  wireEditableBlocks();
  wireEnterOpensComment();
  wireVerdictButtons();
  wireReviewButtons();
  wirePublicFilmToggles();
  loadThreadsIntoDOM();
});
"""


MARK_LAYER_JS = r"""
window.initMarkLayer = function initMarkLayer() {
  const K = {
    agree:{glyph:'✓', label:'Agree', weight:1, color:'agree'},
    clarify:{glyph:'?', label:'Clarify', weight:3, color:'clarify'},
    rewrite:{glyph:'✎', label:'Rewrite', weight:2, color:'rewrite'},
    strike:{glyph:'✗', label:'Strike', weight:2, color:'strike'},
    note:{glyph:'✎', label:'Note', weight:2, color:'note'},
    ack:{glyph:'•', label:'Acknowledged', weight:.5, color:'ack'},
    ruling:{glyph:'§', label:'Ruling', weight:9, color:'ruling'}
  };
  const SEND_AT = 9;
  const IDLE_MS = 60000;
  const DWELL_READ = 1200;
  const STORAGE_KEY = `soma-mark-layer:${API_BASE}:${ROUTE}`;
  const blocks = Array.from(document.querySelectorAll('.block-wrap'));
  const dock = document.getElementById('mark-layer-dock-inner');
  let serverRows = [];
  let pending = [];
  let reading = {dwell:{}, furthest:null, last:null};
  let current = null;
  let composing = null;
  let editing = null;
  let sending = false;
  let lastActivity = Date.now();
  let lastTick = Date.now();

  function uid() { return `ml_${Math.random().toString(36).slice(2,8)}${Date.now().toString(36).slice(-5)}`; }
  function plain(html) { const d=document.createElement('div'); d.innerHTML=html; return d.textContent || ''; }
  function decode(value) { try { return b64ToUtf8(value || ''); } catch (_) { return ''; } }
  function encode(value) {
    const bytes=new TextEncoder().encode(value||'');let binary='';
    bytes.forEach(byte=>{binary+=String.fromCharCode(byte);});
    return btoa(binary);
  }
  function hydrateRichUnits() {
    blocks.forEach(wrap => {
      const encoded=wrap.dataset.listUnits;
      if(!encoded)return;
      let listUnits=[];
      try{listUnits=JSON.parse(decode(encoded));}catch(_){return;}
      const items=Array.from(wrap.querySelectorAll('.block-body li'));
      listUnits.forEach((unit,index)=>{
        const item=items[index];if(!item)return;
        item.classList.add('mark-block-unit');
        item.dataset.from=unit.from;
        item.dataset.to=unit.to;
        item.dataset.quote=encode(unit.quote||'');
      });
    });
  }
  function units() { return Array.from(document.querySelectorAll('.mark-sentence,.mark-block-unit')); }
  function unitMeta(el) {
    const wrap = el.closest('.block-wrap');
    const hasRange = el.dataset.from != null && el.dataset.to != null;
    const from = hasRange ? Number(el.dataset.from) : 0;
    const to = hasRange ? Number(el.dataset.to) : null;
    const quote = hasRange ? decode(el.dataset.quote) : decode(wrap.dataset.normText);
    return { el, wrap, blockId:wrap.dataset.blockId, from, to, quote,
      key:`${wrap.dataset.blockId}:${from}:${to == null ? '' : to}` };
  }
  function metas() { return units().map(unitMeta); }
  function currentMeta() { return metas().find(m => m.key === current) || null; }
  function dwell(key) { return reading.dwell[key] || 0; }
  function urgency() { return pending.reduce((sum,m) => sum + ((K[m.kind]||K.note).weight * (m.strength||1)), 0); }
  function allMarks() { return serverRows.map(rowToMark).concat(pending); }
  function rowToMark(c) {
    let kind = 'note';
    if (c.type === 'mark' && K[c.mark_kind]) kind = c.mark_kind;
    else if (c.type === 'edit') kind = 'rewrite';
    else if (c.type === 'verdict') kind = 'ruling';
    return {
      id:c.id, blockId:c.block_id || null, from:c.from, to:c.to,
      quote:c.quote || c.snapshot || '', kind, author:c.author || 'mike',
      body:c.text || '', before:c.before || c.snapshot || '', after:c.proposed || '',
      reason:c.reason || '', strength:c.strength || 1, scope:c.scope || null,
      sent:true, status:c.status || 'queued', deleted:!!c.deleted, unresolved:!!c.unresolved,
      threadId:c.thread_id
    };
  }
  function markAt(meta) {
    return allMarks().filter(m => !m.deleted && !m.unresolved && m.blockId === meta.blockId
      && Number(m.from || 0) === meta.from
      && ((m.to == null && meta.to == null) || Number(m.to) === meta.to));
  }
  function blockMarks(blockId) { return allMarks().filter(m => !m.deleted && !m.unresolved && m.blockId === blockId); }
  function sectionMarks(sectionId) {
    return allMarks().filter(m => !m.deleted && !m.unresolved && m.blockId === sectionId && m.scope === 'section');
  }
  function save() {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify({pending, reading, current, composing, editing})); } catch (_) {}
  }
  function restore() {
    try {
      const data = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
      pending = Array.isArray(data.pending) ? data.pending : [];
      reading = data.reading && data.reading.dwell ? data.reading : reading;
      current = data.current || reading.last || null;
      composing = data.composing || null;
      editing = data.editing || null;
    } catch (_) {}
  }
  function color(kind) { return `var(--ml-${(K[kind]||K.note).color})`; }
  function glyph(mark) {
    const def = K[mark.kind] || K.note;
    return `<span class="ml-glyph ml-ui" style="color:${color(mark.kind)}" title="${escapeHtml(def.label)}">${def.glyph}${mark.strength>1?mark.strength:''}</span>`;
  }

  function render() {
    document.querySelectorAll('.ml-ui').forEach(el => el.remove());
    metas().forEach(meta => {
      if (meta.el.dataset.mlOriginal == null) meta.el.dataset.mlOriginal = meta.el.innerHTML;
      meta.el.innerHTML = meta.el.dataset.mlOriginal;
      meta.el.classList.remove('ml-current','ml-seen','ml-struck');
      if (dwell(meta.key) >= DWELL_READ) meta.el.classList.add('ml-seen');
      if (meta.key === current) meta.el.classList.add('ml-current');
      const marks = markAt(meta);
      const rewrite = marks.filter(m => m.kind === 'rewrite' && m.after).pop();
      if (rewrite) meta.el.innerHTML = `<del>${meta.el.dataset.mlOriginal}</del> <ins>${escapeHtml(rewrite.after)}</ins>`;
      if (marks.some(m => m.kind === 'strike')) meta.el.classList.add('ml-struck');
      if (marks.length) meta.el.insertAdjacentHTML('beforebegin', marks.map(glyph).join(''));
    });

    blocks.forEach(wrap => {
      if (wrap.classList.contains('document-title-block')) return;
      const marks = blockMarks(wrap.dataset.blockId);
      const blockLevel = marks.filter(m => m.to == null || m.scope === 'section');
      if (blockLevel.length) {
        const gutter = document.createElement('div');
        gutter.className = 'ml-gutter ml-ui';
        gutter.innerHTML = blockLevel.map(glyph).join('');
        wrap.appendChild(gutter);
      }
      if (wrap.dataset.decision === '1') renderDecision(wrap, marks);
      if (marks.length) renderMarkRows(wrap, marks);
      if (currentMeta() && currentMeta().wrap === wrap) {
        const hint = document.createElement('div');
        hint.className = 'ml-hint ml-ui';
        hint.innerHTML = '<b>A</b> agree · <b>?</b> clarify · <b>E</b> edit · <b>X</b> clear &amp; rewrite · <b>S</b> ack · <b>N</b> note';
        wrap.appendChild(hint);
      }
    });
    renderSectionEnds();
    renderUnresolved();
    if (editing) mountEditor();
    else if (composing) mountComposer();
    renderDock();
  }

  function renderDecision(wrap, marks) {
    const ruling = marks.filter(m => m.kind === 'ruling').pop();
    const tag = document.createElement('span');
    tag.className = 'ml-decision-tag ml-ui';
    tag.textContent = ruling ? (ruling.body || 'Ruled') : 'Needs your ruling';
    wrap.querySelector('.block-body').insertAdjacentElement('beforebegin', tag);
    if (!ruling) {
      const bar = document.createElement('div');
      bar.className = 'ml-rulebar ml-ui';
      bar.innerHTML = '<span class="ml-label">Your ruling</span>'+
        `<button class="ml-act" data-ml-rule="Ratified" data-block="${wrap.dataset.blockId}">Ratify</button>`+
        `<button class="ml-act ml-ghost" data-ml-rule="Not yet" data-block="${wrap.dataset.blockId}">Not yet</button>`+
        `<button class="ml-act ml-ghost" data-ml-rule="Rejected" data-block="${wrap.dataset.blockId}">Reject</button>`;
      wrap.appendChild(bar);
    }
  }

  function renderMarkRows(wrap, marks) {
    const host = document.createElement('div');
    host.className = 'ml-marks ml-ui';
    host.innerHTML = marks.filter(m => m.scope !== 'section').map(m => {
      const def = K[m.kind] || K.note;
      let body = m.body || '';
      if (m.kind === 'rewrite') body = `→ ${m.after || body}${m.reason ? `\nR: ${m.reason}` : ''}`;
      if (m.kind === 'strike') body = m.reason ? `struck\nR: ${m.reason}` : 'struck';
      const quote = (m.to != null && m.quote && ['clarify','rewrite','strike','note'].includes(m.kind))
        ? `<div class="ml-quote">on “${escapeHtml(m.quote)}”</div>` : '';
      return `<div class="ml-mark ml-${m.kind}">${quote}<div class="ml-who">${escapeHtml(m.author || 'Mike')} · ${def.label}`+
        `${m.strength>1?` ×${m.strength}`:''}<span class="ml-meta">${m.sent?(m.status||'sent'):'queued'}</span>`+
        `<button class="ml-drop" data-ml-drop="${m.id}" data-pending="${m.sent?'0':'1'}" title="Remove">×</button></div>`+
        `${body?`<div class="ml-body">${escapeHtml(body)}</div>`:''}</div>`;
    }).join('');
    wrap.appendChild(host);
  }

  function renderSectionEnds() {
    const grouped = new Map();
    blocks.forEach(w => {
      const id = w.dataset.sectionId;
      if (!id || w.classList.contains('document-title-block')) return;
      if (!grouped.has(id)) grouped.set(id, []);
      grouped.get(id).push(w);
    });
    grouped.forEach((sectionBlocks, sectionId) => {
      const last = sectionBlocks.filter(w => w.dataset.kind !== 'heading').pop();
      if (!last) return;
      const agreed = sectionMarks(sectionId).filter(m => m.kind === 'agree').pop();
      const bar = document.createElement('div');
      bar.className = 'ml-section-end ml-ui';
      bar.innerHTML = agreed
        ? `<span class="ml-label" style="color:var(--ml-agree)">✓ Section agreed</span><span>${escapeHtml(agreed.body||'')}</span>`
        : `<span class="ml-label">This section</span><button class="ml-act" data-ml-secagree="${sectionId}">Agree with all of it</button>`+
          `<button class="ml-act ml-ghost" data-ml-secwalk="${sectionId}">Review sentence by sentence</button>`;
      last.appendChild(bar);
    });
  }

  function renderUnresolved() {
    const section=document.getElementById('unresolved-marks');
    const list=document.getElementById('unresolved-thread-list');
    const count=document.getElementById('unresolved-count');
    if(!section||!list||!count)return;
    const rows=serverRows.map(rowToMark).filter(m=>m.unresolved&&!m.deleted);
    count.textContent=rows.length;
    section.hidden=!rows.length;
    list.innerHTML=rows.map(m=>`<div class="unresolved-item"><div class="unresolved-location">Original location unavailable</div>`+
      `<blockquote class="unresolved-quote">${escapeHtml(m.quote||m.before||'(no source quote)')}</blockquote>`+
      `<div class="ml-who">${escapeHtml(m.author||'Mike')} · ${(K[m.kind]||K.note).label}</div>`+
      `${m.body?`<div class="ml-body">${escapeHtml(m.body)}</div>`:''}</div>`).join('');
  }

  function currentHost() { const m=currentMeta(); return m && m.wrap; }
  function mountComposer() {
    const host = currentHost(); if (!host) return;
    const c = document.createElement('div'); c.className='ml-composer ml-ui';
    c.innerHTML = '<textarea aria-label="Note" placeholder="What about this sentence? Cmd/Control+Enter sends it."></textarea>'+
      '<div class="ml-composer-row"><button class="ml-act" data-ml-save-note>Save note</button>'+
      '<button class="ml-act ml-ghost" data-ml-cancel>Cancel</button></div>';
    host.appendChild(c);
    const ta=c.querySelector('textarea'); ta.value=composing.body||''; ta.focus();
    ta.addEventListener('input',()=>{ composing.body=ta.value; lastActivity=Date.now(); save(); });
    ta.addEventListener('keydown',e=>{
      if (e.key==='Enter'&&(e.metaKey||e.ctrlKey)){e.preventDefault();saveNote();}
      if (e.key==='Escape'){e.preventDefault();composing=null;save();render();}
    });
  }
  function saveNote() {
    if (composing && (composing.body||'').trim()) add({kind:'note',body:composing.body.trim()});
    composing=null; save(); render();
  }
  function mountEditor() {
    const host=currentHost(); if (!host) return;
    const c=document.createElement('div'); c.className='ml-composer ml-edit ml-ui';
    c.innerHTML='<textarea aria-label="Rewrite this sentence" placeholder="Rewrite it. Start a line with R: to give the reason."></textarea>'+
      '<div class="ml-composer-row"><button class="ml-act" data-ml-save-edit>Save rewrite</button>'+
      '<button class="ml-act ml-ghost" data-ml-strike>Strike without replacing</button>'+
      '<button class="ml-act ml-ghost" data-ml-cancel>Cancel</button></div>';
    host.appendChild(c);
    const ta=c.querySelector('textarea'); ta.value=editing.text||''; ta.focus(); ta.setSelectionRange(ta.value.length,ta.value.length);
    ta.addEventListener('input',()=>{editing.text=ta.value;lastActivity=Date.now();save();});
    ta.addEventListener('keydown',e=>{
      if(e.key==='Enter'&&(e.metaKey||e.ctrlKey)){e.preventDefault();saveEdit();}
      if(e.key==='Escape'){e.preventDefault();editing=null;save();render();}
    });
  }
  function saveEdit() {
    if (!editing) return;
    let after=(editing.text||'').trim(); let reason=''; const keep=[];
    after.split('\n').forEach(line=>{/^\s*R:/i.test(line)?reason+=(reason?' ':'')+line.replace(/^\s*R:\s*/i,''):keep.push(line);});
    after=keep.join('\n').trim();
    if (!after) add({kind:'strike',reason});
    else if (after !== editing.before) add({kind:'rewrite',before:editing.before,after,reason});
    editing=null; save(); render();
  }

  function add(extra, explicitMeta) {
    const meta=explicitMeta||currentMeta(); if (!meta && !extra.pageLevel) return;
    if (extra.kind==='agree' && meta) {
      const existing=pending.find(m=>m.kind==='agree'&&m.blockId===meta.blockId&&m.from===meta.from&&m.to===meta.to);
      if(existing){existing.strength=Math.min(3,(existing.strength||1)+1);touch();render();return;}
    }
    pending.push(Object.assign({id:uid(),blockId:meta?meta.blockId:null,from:meta?meta.from:null,to:meta?meta.to:null,
      quote:meta?meta.quote:null,author:'Mike',strength:1,scope:null,body:'',before:'',after:'',reason:'',sent:false,
      ts:new Date().toISOString()},extra));
    touch(); render();
  }
  function touch(){lastActivity=Date.now();save();maybeSend();}
  function startEdit(clear){const meta=currentMeta();if(!meta)return;const before=plain(meta.el.dataset.mlOriginal||meta.el.innerHTML);
    editing={before,text:clear?'':before};composing=null;save();render();}

  function renderDock() {
    if (!dock) return;
    const count=pending.length, score=urgency(), pct=Math.min(100,Math.round(score/SEND_AT*100));
    const seen=metas().filter(m=>dwell(m.key)>=DWELL_READ).length, total=metas().length;
    const unread=blocks.filter(w=>w.dataset.decision==='1').filter(w=>{
      const us=metas().filter(m=>m.wrap===w); return !us.some(m=>dwell(m.key)>=DWELL_READ);
    }).length;
    let bits=!count?'<span>Nothing queued.</span>':`<button class="ml-counter" id="ml-send"><span class="ml-n">${count}</span>`+
      `<span class="ml-lb">${count===1?'mark':'marks'} — send</span></button><span class="ml-fill"><i style="width:${pct}%"></i></span>`+
      `<span>${sending?'sending':`auto-sends at ${SEND_AT}`}</span>`;
    bits+=`<span class="ml-trace">read ${seen}/${total}${unread?` · ${unread} ruling${unread===1?'':'s'} unread`:''}</span>`;
    dock.innerHTML=bits;
    const send=document.getElementById('ml-send'); if(send)send.addEventListener('click',()=>sendPending('you'));
  }

  async function sendPending(why) {
    if(sending||!pending.length)return;
    sending=true;renderDock();let sent=0;
    for(const mark of [...pending]){
      const wrap=mark.blockId?document.querySelector(`.block-wrap[data-block-id="${CSS.escape(mark.blockId)}"]`):null;
      try{
        await postComment({anchor:wrap?wrap.dataset.anchor:null,snapshot:mark.before||mark.quote||'(page-level mark)',
          text:mark.body||`${(K[mark.kind]||K.note).label}${mark.reason?`: ${mark.reason}`:''}`,
          type:'mark',proposed:mark.after||undefined,block_id:mark.blockId,from:mark.from,to:mark.to,
          quote:mark.quote,block_text_sha:wrap?wrap.dataset.blockSha:null,mark_kind:mark.kind,
          strength:mark.strength||1,scope:mark.scope,reason:mark.reason||undefined,sent_because:why});
        pending=pending.filter(m=>m.id!==mark.id);sent++;save();
      }catch(err){toast(`Could not send; ${pending.length} mark${pending.length===1?' is':'s are'} safe in this browser.`);break;}
    }
    if(sent && window.__HAS_DISPATCH__){
      try{await fetch(`${API_BASE}/api/dispatch`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({page:ROUTE})});}
      catch(_){toast('Marks were saved; dispatch can be retried from the page.');}
    }
    serverRows=await fetchComments();sending=false;render();
    if(sent)toast(`Sent ${sent} mark${sent===1?'':'s'} as one review turn.`);
  }
  function maybeSend(){if(!sending&&urgency()>=SEND_AT)sendPending('threshold');}
  setInterval(()=>{
    if(!sending&&pending.length&&!composing&&!editing&&Date.now()-lastActivity>IDLE_MS)sendPending('idle');
    renderDock();
  },5000);
  document.addEventListener('visibilitychange',()=>{
    if(document.hidden&&!sending&&pending.length&&!composing&&!editing)sendPending('left the page');
  });

  function pick() {
    const y=window.innerHeight*.38;let best=null,bestDistance=Infinity;
    metas().forEach(meta=>{const r=meta.el.getBoundingClientRect();if(r.bottom<0||r.top>window.innerHeight)return;
      const distance=Math.abs((r.top+r.bottom)/2-y);if(distance<bestDistance){bestDistance=distance;best=meta;}});
    if(best&&best.key!==current){current=best.key;reading.last=current;const all=metas().map(m=>m.key);
      if(all.indexOf(current)>all.indexOf(reading.furthest||''))reading.furthest=current;
      save();if(!editing&&!composing)render();else renderDock();}
  }
  function tick(){const now=Date.now(),dt=now-lastTick;lastTick=now;if(!document.hidden&&current&&dt<2000)reading.dwell[current]=dwell(current)+dt;requestAnimationFrame(tick);}
  function jump(){render();const meta=currentMeta();if(meta)meta.el.scrollIntoView({block:'center',behavior:'smooth'});}

  document.addEventListener('click',async e=>{
    const drop=e.target.closest('[data-ml-drop]');
    if(drop){
      if(drop.dataset.pending==='1'){pending=pending.filter(m=>m.id!==drop.dataset.mlDrop);save();render();}
      else if(confirm('Remove this mark? It remains in the audit trail as deleted.')){
        await fetch(`${API_BASE}/api/comments/delete`,{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({page:ROUTE,id:drop.dataset.mlDrop})});serverRows=await fetchComments();render();
      }return;
    }
    if(e.target.closest('[data-ml-save-note]')){saveNote();return;}
    if(e.target.closest('[data-ml-save-edit]')){saveEdit();return;}
    if(e.target.closest('[data-ml-strike]')){add({kind:'strike'},currentMeta());editing=null;save();render();return;}
    if(e.target.closest('[data-ml-cancel]')){composing=null;editing=null;save();render();return;}
    const rule=e.target.closest('[data-ml-rule]');
    if(rule){const wrap=document.querySelector(`.block-wrap[data-block-id="${CSS.escape(rule.dataset.block)}"]`);
      const meta={blockId:wrap.dataset.blockId,from:0,to:null,quote:decode(wrap.dataset.normText),wrap};
      add({kind:'ruling',body:rule.dataset.mlRule},meta);return;}
    const agree=e.target.closest('[data-ml-secagree]');
    if(agree){const heading=document.querySelector(`.block-wrap[data-block-id="${CSS.escape(agree.dataset.mlSecagree)}"]`);
      const sectionUnits=metas().filter(m=>m.wrap.dataset.sectionId===agree.dataset.mlSecagree);
      const covered=sectionUnits.filter(m=>!markAt(m).length).length;
      add({kind:'agree',scope:'section',body:`Agreed with the section — ${covered} unmarked sentence${covered===1?'':'s'} covered.`},
        {blockId:heading.dataset.blockId,from:0,to:null,quote:decode(heading.dataset.normText),wrap:heading});return;}
    const walk=e.target.closest('[data-ml-secwalk]');
    if(walk){const first=metas().find(m=>m.wrap.dataset.sectionId===walk.dataset.mlSecwalk);
      if(first){current=first.key;composing=null;editing=null;jump();}return;}
    if(e.target.closest('button,a,input,textarea,select'))return;
    const unit=e.target.closest('.mark-sentence,.mark-block-unit');
    if(unit){current=unitMeta(unit).key;composing=null;editing=null;save();render();}
  });
  document.addEventListener('dblclick',e=>{const unit=e.target.closest('.mark-sentence,.mark-block-unit');if(!unit)return;
    current=unitMeta(unit).key;startEdit(false);});
  document.addEventListener('keydown',e=>{
    if(['TEXTAREA','INPUT'].includes(e.target.tagName)||e.metaKey||e.ctrlKey||e.altKey)return;
    const all=metas(),index=all.findIndex(m=>m.key===current),key=e.key.toLowerCase();
    if(key==='j'||key==='k'){e.preventDefault();const next=key==='j'?Math.min(index+1,all.length-1):Math.max(index-1,0);
      current=(all[next]||all[0]).key;composing=null;editing=null;jump();return;}
    if(e.key==='Escape'){composing=null;editing=null;save();render();return;}
    if(!currentMeta())return;
    if(key==='a'){e.preventDefault();add({kind:'agree'});return;}
    if(e.key==='?'||key==='/'){e.preventDefault();add({kind:'clarify',body:`Unclear: “${plain(currentMeta().el.dataset.mlOriginal||currentMeta().el.innerHTML).slice(0,120)}”`});return;}
    if(key==='e'){e.preventDefault();startEdit(false);return;}
    if(key==='x'){e.preventDefault();startEdit(true);return;}
    if(key==='s'){e.preventDefault();add({kind:'ack'});return;}
    if(key==='n'){e.preventDefault();composing={body:''};editing=null;save();render();}
  });
  let scrollFrame=null;window.addEventListener('scroll',()=>{if(scrollFrame)return;scrollFrame=requestAnimationFrame(()=>{scrollFrame=null;pick();});},{passive:true});

  document.getElementById('ml-synthesis')?.addEventListener('click',()=>add({kind:'note',pageLevel:true,
    body:'Synthesis pass: re-read the whole document as it now stands and tell me what it says.'},null));
  document.getElementById('ml-reframe')?.addEventListener('click',()=>add({kind:'note',pageLevel:true,
    body:'Step back and reframe: read everything at once and propose a different frame, not a patch.'},null));
  document.getElementById('ml-jump-new')?.addEventListener('click',()=>{
    const target=document.querySelector('.block-wrap.mark-decision,.unresolved-marks:not([hidden]),.block-wrap.has-comments');
    if(target)target.scrollIntoView({block:'center',behavior:'smooth'});
  });
  document.getElementById('ml-regenerate')?.addEventListener('click',async e=>{
    e.target.disabled=true;e.target.textContent='Regenerating…';
    const res=await fetch(`${API_BASE}/api/board/regenerate`,{method:'POST'});
    if(res.ok)location.reload();else{toast('Regeneration failed.');e.target.disabled=false;e.target.textContent='Regenerate board';}
  });

  hydrateRichUnits();
  restore();
  fetchComments().then(rows=>{serverRows=rows;render();pick();tick();}).catch(()=>{render();pick();tick();});
};
"""


def render_workspace_switcher(current_workspace):
    workspaces = load_workspaces()
    opts = []
    for slug, cfg in workspaces.items():
        prefix = workspace_url_prefix(slug)
        cls = ' class="active"' if slug == current_workspace else ''
        opts.append(f'<a href="{prefix}/page/{cfg["home"]}"{cls}>{_html.escape(cfg["label"])}</a>')
    return f'<div class="workspace-switcher">{"".join(opts)}</div>'


def render_sidebar(current_route, workspace=DEFAULT_WORKSPACE):
    ws = get_workspace(workspace)
    url_prefix = workspace_url_prefix(workspace)
    reports = discover_nightly_reports(ws.get('nightly_filter')) if ws['nightly'] else {}
    items = []
    items.append(f'<h2>{_html.escape(ws["label"])}</h2>')
    for label, route in ws['nav']:
        cls = ' class="active"' if route == current_route else ''
        items.append(f'<a href="{url_prefix}/page/{route}"{cls}>{render_inline(label)}</a>')
    if reports:
        items.append('<h2>Nightly Reports</h2>')
        for slug in sorted(reports):
            route = f'{NIGHTLY_PREFIX}/{slug}'
            cls = ' class="active"' if route == current_route else ''
            items.append(f'<a href="{url_prefix}/page/{route}"{cls}>{render_inline(slug)}</a>')
    return '\n'.join(items)


# --- Workqueue status chips (flag-gated: workspaces.json::<ws>.status_badges) ---
#
# Render-time decoration only — the markdown file stays the single source of
# truth. An item block (one-paragraph-per-item, IDs like WQ-n / G1.n / P-n)
# carrying an in-row `**DONE 2026-07-03**` marker gets a small green DONE chip;
# `**IN-FLIGHT**` (the marker the maintenance passes use for worker-running
# items, e.g. WQ-67 pre-completion) gets an amber IN-FLIGHT chip. Everything
# else: no chip. Palette matches the existing comment-status badges
# (.badge-done green / .badge-queued amber) so the surface speaks one visual
# language. CSS is injected only on flagged pages (like tour_head), so
# non-flagged pages render byte-identically.

_WQ_ITEM_RE = re.compile(r'^\*\*(?:WQ-\d|G\d+\.\d|P-\d)')
_WQ_DONE_RE = re.compile(r'\*\*DONE(?:\s+\d{4}-\d{2}-\d{2})?\*\*')
_WQ_INFLIGHT_RE = re.compile(r'\*\*IN-FLIGHT(?:\s+\d{4}-\d{2}-\d{2})?\*\*')

STATUS_CHIP_CSS = """
.wq-chip { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 10px;
           text-transform: uppercase; letter-spacing: .04em; font-weight: 700;
           margin-right: 8px; vertical-align: 2px; }
.wq-chip-done { background: #1f4a2a; color: #7ee08a; }
.wq-chip-in-flight { background: #4a3a1f; color: #f0c674; }
"""


def wq_status_chip(block):
    """'done' | 'in-flight' | None for a workqueue item block. Only paragraph
    blocks that start with an item ID are eligible — prose that merely mentions
    the markers (footer notes, Done-today list) never gets a chip."""
    if block['kind'] != 'paragraph':
        return None
    text = block['text']
    if not _WQ_ITEM_RE.match(text):
        return None
    if _WQ_DONE_RE.search(text):
        return 'done'
    if _WQ_INFLIGHT_RE.search(text):
        return 'in-flight'
    return None


_SENTENCE_ABBREVIATIONS = {
    'mr', 'mrs', 'ms', 'dr', 'prof', 'sr', 'jr', 'st', 'vs', 'etc',
    'e.g', 'i.e', 'no', 'fig', 'rev', 'sept', 'jan', 'feb', 'mar', 'apr',
    'jun', 'jul', 'aug', 'oct', 'nov', 'dec',
}


def sentence_ranges(text):
    """Split normalized block text into conservative sentence ranges.

    Offsets are Unicode code-point offsets into ``blockmap.norm(text)`` — the
    same coordinate system validated_binding() persists.  We deliberately keep
    abbreviations and decimal/version dots together; a false negative merely
    produces a slightly larger addressable unit, while a false positive makes a
    sentence fragment feel broken in the reading instrument.
    """
    normalized = blockmap.norm(text)
    if not normalized:
        return []
    ranges = []
    start = 0
    for match in re.finditer(r'[.!?](?:[\"\u201d\u2019)\]]+)?(?=\s+|$)', normalized):
        end = match.end()
        punct_at = match.start()
        if normalized[punct_at] == '.':
            prefix = normalized[start:punct_at]
            token_match = re.search(r'([A-Za-z](?:[A-Za-z.]*)?)$', prefix)
            token = (token_match.group(1).lower().rstrip('.') if token_match else '')
            if token in _SENTENCE_ABBREVIATIONS or (len(token) == 1 and token.isalpha()):
                continue
            if (punct_at > 0 and punct_at + 1 < len(normalized)
                    and normalized[punct_at - 1].isdigit()
                    and normalized[punct_at + 1].isdigit()):
                continue
        quote = normalized[start:end].strip()
        if quote:
            quote_start = normalized.find(quote, start, end)
            ranges.append((quote_start, quote_start + len(quote), quote))
        start = end
        while start < len(normalized) and normalized[start].isspace():
            start += 1
    if start < len(normalized):
        quote = normalized[start:].strip()
        if quote:
            quote_start = normalized.find(quote, start)
            ranges.append((quote_start, quote_start + len(quote), quote))
    return ranges or [(0, len(normalized), normalized)]


def list_item_ranges(text):
    """Return exact normalized ranges for each visible Markdown list item.

    Lists remain one durable block, while each rendered ``li`` becomes an
    independently addressable review unit inside that block.
    """
    normalized = blockmap.norm(text)
    ranges = []
    cursor = 0
    for line in text.splitlines():
        match = re.match(r'^\s*(?:[-*+]|\d+\.)\s+(.*)$', line)
        if not match:
            continue
        quote = blockmap.norm(match.group(1))
        if not quote:
            continue
        start = normalized.find(quote, cursor)
        if start < 0:
            start = normalized.find(quote)
        if start < 0:
            continue
        end = start + len(quote)
        ranges.append((start, end, quote))
        cursor = end
    return ranges


def mark_layer_inner(block, link_resolver=None, terms=None, lexicon=None, auto_lexicon=False):
    """Render prose as sentence-addressable spans without disturbing rich blocks.

    Each sentence is a separate render_inline() call, but auto-lexicon linking's
    "first occurrence" rule is scoped to the whole BLOCK (paragraph), not the
    sentence — a single `auto_seen` set is created once here and threaded into
    every sentence's render_inline() call so occurrence-tracking spans the block.
    """
    kind = block['kind']
    if kind == 'list':
        block['mark_layer_list_units'] = [
            {'from': start, 'to': end, 'quote': quote}
            for start, end, quote in list_item_ranges(block['text'])
        ]
        return block['html'], bool(block['mark_layer_list_units'])
    if kind not in ('paragraph', 'blockquote'):
        return block['html'], False
    pieces = []
    auto_seen = set()
    for start, end, quote in sentence_ranges(block['text']):
        quote_b64 = __import__('base64').b64encode(quote.encode('utf-8')).decode('ascii')
        pieces.append(
            f'<span class="mark-sentence" data-from="{start}" data-to="{end}" '
            f'data-quote="{quote_b64}">'
            f'{render_inline(quote, link_resolver, terms=terms, lexicon=lexicon, auto_lexicon=auto_lexicon, auto_seen=auto_seen)}'
            f'</span>'
        )
    content = ' '.join(pieces)
    tag = 'blockquote' if kind == 'blockquote' else 'p'
    return f'<{tag}>{content}</{tag}>', True


def render_block_html(block, route_path, status_chip=None, link_resolver=None, terms=None,
                       lexicon=None, auto_lexicon=False):
    kind = block['kind']
    anchor = block['anchor']
    block_id = _html_attr_escape(block['id'])
    block_sha = _html_attr_escape(blockmap.block_text_sha(block))
    snapshot = _html_attr_escape(block['snapshot'])
    inner, has_sentence_units = mark_layer_inner(
        block, link_resolver, terms=terms, lexicon=lexicon,
        auto_lexicon=auto_lexicon and kind != 'heading',
    )
    # Raw markdown source, base64'd, so the client can swap rendered HTML for an
    # editable <textarea> pre-filled with the exact source text (edit-as-comment,
    # see v2/CLAUDE.md "CM6 vs contenteditable" note). Base64 sidesteps any HTML/JS
    # string-escaping edge cases in doc text (backticks, quotes, newlines).
    import base64 as _b64
    source_b64 = _b64.b64encode(block['text'].encode('utf-8')).decode('ascii')
    norm_b64 = _b64.b64encode(blockmap.norm(block['text']).encode('utf-8')).decode('ascii')
    list_units = block.get('mark_layer_list_units') or []
    list_units_b64 = _b64.b64encode(
        json.dumps(list_units, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    ).decode('ascii') if list_units else ''
    # code/table/film blocks are excluded from click-to-edit — their raw source has
    # internal structure (fences, pipes, JSON) that's easy to corrupt via a flat
    # textarea edit and low-value to inline-edit anyway; they still get the comment
    # affordance (film comments are exactly the "notes to the videographer" mechanism).
    editable = kind not in ('code', 'table', 'film')
    edit_cls = ' edit-eligible' if editable else ''
    title_cls = (' document-title-block' if kind == 'heading' and block.get('level') == 1
                 and block.get('index') == 0 else '')
    decision = bool(block.get('mark_layer_decision'))
    decision_cls = ' mark-decision' if decision else ''
    section_id = _html_attr_escape(block.get('mark_layer_section_id') or '')
    section_title = _html_attr_escape(block.get('mark_layer_section_title') or '')
    if status_chip:
        # Inline chip at the head of the item's first line. Item blocks are
        # paragraphs, so splice inside the opening <p>; anchors/snapshots/
        # data-source are untouched (chip is presentation-only).
        label = status_chip.upper()
        chip_html = f'<span class="wq-chip wq-chip-{status_chip}">{label}</span>'
        if inner.startswith('<p>'):
            inner = '<p>' + chip_html + inner[len('<p>'):]
        else:
            inner = chip_html + inner
    unit_cls = ' mark-block-unit' if not has_sentence_units and kind != 'heading' else ''
    list_units_attr = f' data-list-units="{list_units_b64}"' if list_units_b64 else ''
    return f'''<div class="block-wrap{edit_cls}{title_cls}{decision_cls}" data-block-id="{block_id}" data-block-sha="{block_sha}" data-norm-text="{norm_b64}" data-anchor="{anchor}" data-snapshot="{snapshot}" data-kind="{kind}" data-source="{source_b64}" data-section-id="{section_id}" data-section-title="{section_title}" data-decision="{'1' if decision else '0'}"{list_units_attr}>
  <button class="comment-affordance" title="Comment on this block (Enter)">+</button>
  <div class="block-body{unit_cls}"{' tabindex="0"' if editable else ''}>{inner}</div>
  <div class="comment-box">
    <textarea placeholder="Comment on this block... (Enter to save, Shift+Enter for newline)"></textarea>
    <button class="mic-btn" type="button" title="Dictate">&#127908;</button>
    <button class="save-btn" type="button">Save comment</button>
  </div>
  <div class="comment-thread"></div>
</div>'''


def _html_attr_escape(s):
    return (s.replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;').replace('>', '&gt;'))


def reconcile_parsed_page(route_path, workspace, src_bytes, blocks):
    """Attach durable ids and reconcile sidecar rows under cross-process locks."""
    map_path = block_map_path(route_path, workspace)
    comments_path = sidecar_path(route_path, workspace)
    with blockmap.file_lock(map_path):
        with blockmap.file_lock(comments_path):
            old_map = blockmap.load_map(map_path)
            comments = _read_comments_unlocked(comments_path)
            new_map, updated_comments, report = blockmap.reconcile(
                old_map, src_bytes, blocks, comments
            )
            if report.get('changed') or old_map is None:
                blockmap.save_map(map_path, new_map)
            if updated_comments != comments:
                _write_all_comments_unlocked(comments_path, updated_comments)

    render_records = new_map.get('blocks') or []
    if len(render_records) != len(blocks):
        render_records = [
            {'id': 'transient_' + hashlib.sha256(
                src_bytes + str(index).encode('ascii')
            ).hexdigest()[:24], 'text': blockmap.norm(block['text'])}
            for index, block in enumerate(blocks)
        ]
    for block, record in zip(blocks, render_records):
        block['id'] = record['id']
        block['norm_text'] = record['text']
    return new_map, report


def current_page_blocks(route_path, workspace=DEFAULT_WORKSPACE):
    fs_path = resolve_page(route_path, workspace)
    with open(fs_path, 'rb') as handle:
        src_bytes = handle.read()
    _title, blocks = parse_markdown(src_bytes.decode('utf-8'))
    mapping, report = reconcile_parsed_page(route_path, workspace, src_bytes, blocks)
    return src_bytes, blocks, mapping, report


class BindingConflict(Exception):
    pass


def validated_binding(route_path, workspace, candidate):
    """Validate client/thread anchoring against the current parse.

    A stale id is accepted only when the carried quote resolves exactly and
    uniquely.  The returned fields are safe to persist as a schema-v2 mark.
    """
    src_bytes, blocks, mapping, report = current_page_blocks(route_path, workspace)
    has_location = any(candidate.get(key) is not None
                       for key in ('block_id', 'anchor', 'quote'))
    if not has_location:
        return {
            'schema': 2, 'block_id': None, 'from': None, 'to': None,
            'quote': None, 'origin_quote': None, 'block_text_sha': None,
            'heading_path': None, 'source_sha': blockmap.source_sha256(src_bytes),
            'unresolved': False,
        }
    if report.get('blocked'):
        raise BindingConflict('source parse is temporarily unsafe: ' + report['blocked'])

    old_by_id = {}
    for row in list(mapping.get('blocks') or []) + list(mapping.get('retired') or []):
        if row.get('id'):
            old_by_id[row['id']] = row
    outcome = blockmap.resolve(candidate, blocks, old_by_id)
    if outcome['status'] != 'bound':
        raise BindingConflict(outcome['reason'])
    block = outcome['block']
    start, end = outcome['from'], outcome['to']
    text = blockmap.norm(block['text'])
    quote = text if end is None else text[start:end]
    supplied_id = candidate.get('block_id')
    fields = {
        'schema': 2,
        'block_id': block['id'],
        'from': start,
        'to': end,
        'quote': quote,
        'origin_quote': candidate.get('origin_quote') or quote,
        'block_text_sha': blockmap.block_text_sha(block),
        'heading_path': list(block.get('heading_path') or []),
        'source_sha': blockmap.source_sha256(src_bytes),
        'unresolved': False,
        'quote_verified_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    if supplied_id and supplied_id != block['id']:
        fields['reanchored'] = True
    if candidate.get('from') is not None and candidate.get('to') is not None:
        if (candidate.get('from'), candidate.get('to')) != (start, end):
            fields['offsets_repaired'] = True
    return fields


def render_page(route_path, workspace=DEFAULT_WORKSPACE):
    ws = get_workspace(workspace)
    url_prefix = workspace_url_prefix(workspace)
    fs_path = resolve_page(route_path, workspace)
    with open(fs_path, 'rb') as f:
        src_bytes = f.read()
    src = src_bytes.decode('utf-8')
    resolver = make_link_resolver(fs_path, route_path, workspace)
    terms_out = {}
    lexicon = get_lexicon_index()
    auto_lexicon_page, _ = strip_auto_lexicon_marker(src)
    title, blocks = parse_markdown(src, link_resolver=resolver, terms_out=terms_out, lexicon=lexicon)
    _new_map, reconcile_report = reconcile_parsed_page(
        route_path, workspace, src_bytes, blocks
    )
    title = title or route_path

    # Sentence-level review rides on the durable block map.  A heading starts a
    # section; prose below it inherits that heading's stable id.  "Needs Mike" /
    # ruling-shaped sections become explicit decision blocks, matching the
    # artifact's rule that a ruling is never inferred from an agree mark.
    current_section_id = ''
    current_section_title = ''
    decision_section = False
    heading_decision_terms = re.compile(
        r'\b(needs mike|needs your ruling|decision(?:s)?(?: needed)?|ruling(?:s)?|approval(?:s)?)\b',
        re.I,
    )
    block_decision_terms = re.compile(
        r"\b(needs mike|needs your ruling|mike(?:\u2019s|'s)? (?:decision|ruling|approval))\b",
        re.I,
    )
    for block in blocks:
        if block['kind'] == 'heading':
            current_section_id = block['id']
            current_section_title = blockmap.norm(block['text'])
            decision_section = bool(heading_decision_terms.search(current_section_title))
        block['mark_layer_section_id'] = current_section_id
        block['mark_layer_section_title'] = current_section_title
        block['mark_layer_decision'] = (
            block['kind'] != 'heading'
            and (decision_section or bool(block_decision_terms.search(blockmap.norm(block['text'])[:160])))
        )

    badges_on = route_path in (ws.get('status_badges') or [])
    blocks_html = '\n'.join(
        render_block_html(
            b, route_path,
            status_chip=(wq_status_chip(b) if badges_on else None),
            link_resolver=resolver,
            terms=terms_out,
            lexicon=lexicon,
            auto_lexicon=auto_lexicon_page,
        )
        for b in blocks)

    # Slim map for the hover-popover JS: slug -> rendered definition html (which may
    # itself contain nested .term-link anchors — recursion handled by mdblocks.py's
    # extract_terms()/render_inline(), this is just the transport to the client).
    # Lexicon entries are namespaced `lex-<slug>` (never collides with a page-local
    # slug) and only included if this page actually rendered a term-link to them —
    # built AFTER blocks_html so a page with zero lexicon references embeds an
    # empty lexicon slice, same as before this feature existed (byte-identical
    # __TERM_DEFS__ for every unflagged, lexicon-free page).
    term_defs = {slug: {'term': v['term'], 'html': v['html']} for slug, v in terms_out.items()}
    referenced_lex_slugs = set(re.findall(r'data-term-slug="lex-([a-z0-9-]+)"', blocks_html))
    if referenced_lex_slugs:
        lexicon_route = fs_path_to_route(LEXICON_MD_PATH, workspace) if os.path.isfile(LEXICON_MD_PATH) else None
        for slug in referenced_lex_slugs:
            entry = lexicon['by_slug'].get(slug)
            if entry:
                term_defs[f'lex-{slug}'] = {
                    'term': entry['term'],
                    'html': lexicon_entry_popover_html(entry, url_prefix, lexicon_route),
                }
    term_defs_json = json.dumps(term_defs)

    # trailing newline keeps flagged-page head formatting tidy; empty string on
    # non-flagged pages keeps their rendered HTML byte-identical to pre-feature.
    chip_head = f'<style>{STATUS_CHIP_CSS}</style>\n' if badges_on else ''

    # Every route in a workspace's own roots (or its nightly reports) is dispatchable.
    root_prefixes = tuple(f'{p}/' for p, _ in ws['roots'])
    has_dispatch = route_path.startswith(root_prefixes) or (ws['nightly'] and route_path.startswith(f'{NIGHTLY_PREFIX}/'))
    dispatch_cfg = get_dispatch_config(route_path, workspace)
    dispatch_button = dispatch_cfg['button'] if has_dispatch else ''
    # "Regenerate board" appears on the Board and Portfolio pages themselves
    # (both are generated by the same nightly job, so one button covers both).
    is_board_or_portfolio = route_path in ('estate/BOARD.md', 'estate/PORTFOLIO.md')

    # Quinn tours (feature-flagged): only when the workspace opts in AND this
    # route is tour-bearing (completions index / completion page). tours.py
    # returns None everywhere else, so no other page gains a byte of change.
    tour_head, tour_body = '', ''
    if ws.get('tours'):
        try:
            assets = tour_engine.tour_page_assets(route_path, workspace_url_prefix(workspace))
            if assets:
                tour_head, tour_body = assets
        except Exception as e:  # noqa: BLE001 — a tour bug must never break page render
            sys.stderr.write(f'[tours] asset build failed for {route_path}: {e}\n')

    # Tour-bearing pages keep their purpose-built Quinn review flow.  Every
    # other document uses the sentence-level mark layer by default; workspaces
    # can opt out while migrating with `"mark_layer": false`.
    mark_layer = bool(ws.get('mark_layer', True)) and not bool(tour_body)
    body_class = ' class="mark-layer"' if mark_layer else ''
    mark_fonts = ('<link rel="preconnect" href="https://fonts.googleapis.com">\n'
                  '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
                  '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
                  'family=Spectral:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&'
                  'family=IBM+Plex+Sans:wght@400;500;600&display=swap">') if mark_layer else ''
    review_header = ''
    mark_legend = ''
    mark_dock = ''
    if mark_layer:
        eyebrow = f'{ws["label"]} · {os.path.basename(route_path)}'
        regen = ('<button class="ml-act ml-ghost" id="ml-regenerate">Regenerate board</button>'
                 if is_board_or_portfolio else '')
        review_header = f'''<header class="review-doc-header">
    <div class="review-eyebrow">{_html.escape(eyebrow)}</div>
    <h1>{_html.escape(title)}</h1>
    <p class="review-sub">Scroll and the current sentence highlights. <b>A</b> agree · <b>?</b> clarify · <b>E</b> edit · <b>X</b> clear &amp; rewrite · <b>S</b> acknowledge · <b>N</b> note. Marks gather at the bottom and travel as review turns.</p>
    <div class="review-gears">
      <button class="ml-act ml-ghost" id="ml-synthesis">Ask for a synthesis pass</button>
      <button class="ml-act ml-ghost" id="ml-reframe">Step back and reframe</button>
      <button class="ml-act ml-ghost" id="ml-jump-new">Jump to what’s new</button>
      {regen}
    </div>
  </header>'''
        mark_legend = '''<div class="ml-legend">
    <span><code>A</code> agree (press again for emphasis)</span><span><code>?</code> clarify</span>
    <span><code>E</code> edit</span><span><code>X</code> clear &amp; rewrite</span>
    <span><code>S</code> acknowledge</span><span><code>N</code> note</span>
    <span><code>J/K</code> next/previous sentence</span>
  </div>'''
        mark_dock = '<div class="ml-dock" id="mark-layer-dock"><div class="ml-dock-inner" id="mark-layer-dock-inner"></div></div>'

    html_doc = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html.escape(title)} — soma-review</title>
{mark_fonts}
<style>{PAGE_CSS}{MARK_LAYER_CSS if mark_layer else ''}</style>
{chip_head}{tour_head}
</head>
<body{body_class}>
<nav class="sidebar">
  <a href="{url_prefix}/page/{ws['home']}" style="font-weight:700;font-size:15px;color:#e6e6e6;">soma-review</a>
  {render_workspace_switcher(workspace)}
  {render_sidebar(route_path, workspace)}
</nav>
<main class="main">
  {review_header}
  <div class="top-actions">
    {f'<button id="send-to-dee">{_html.escape(dispatch_button)}</button>' if has_dispatch else ''}
    {'<button id="regenerate-board">Regenerate board</button>' if is_board_or_portfolio else ''}
  </div>
  {blocks_html}
  {mark_legend}
  <section class="unresolved-marks" id="unresolved-marks" hidden>
    <h2>Unresolved marks (<span id="unresolved-count">0</span>)</h2>
    <p class="unresolved-explainer">Their source text moved or disappeared. Nothing was discarded.</p>
    <div id="unresolved-thread-list"></div>
  </section>
  <div class="page-discussion">
    <h2>Page discussion</h2>
    <div id="page-thread-list"></div>
    <div class="comment-box open" id="page-comment-box">
      <textarea placeholder="General comment about this page..."></textarea>
      <button class="mic-btn" type="button" title="Dictate">&#127908;</button>
      <button class="save-btn" type="button">Save comment</button>
    </div>
  </div>
</main>
<div class="toast" id="toast"></div>
<script>window.__ROUTE__ = {json.dumps(route_path)}; window.__API_BASE__ = {json.dumps(url_prefix)};
window.__DISPATCH_TARGET__ = {json.dumps(dispatch_cfg['target'])};
window.__DISPATCH_BUTTON__ = {json.dumps(dispatch_cfg['button'])};
window.__MARK_LAYER__ = {json.dumps(mark_layer)}; window.__HAS_DISPATCH__ = {json.dumps(has_dispatch)};
window.__TERM_DEFS__ = {term_defs_json};</script>
<script>{PAGE_JS}</script>
{f'<script>{MARK_LAYER_JS}</script>' if mark_layer else ''}
{tour_body}
{mark_dock}
</body>
</html>"""
    return html_doc


def render_404(route_path, workspace=DEFAULT_WORKSPACE):
    try:
        ws = get_workspace(workspace)
    except NotFoundError:
        workspace = DEFAULT_WORKSPACE
        ws = get_workspace(workspace)
    url_prefix = workspace_url_prefix(workspace)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Not found — soma-review</title>
<style>{PAGE_CSS}</style></head>
<body>
<nav class="sidebar">
  <a href="{url_prefix}/page/{ws['home']}" style="font-weight:700;font-size:15px;color:#e6e6e6;">soma-review</a>
  {render_workspace_switcher(workspace)}
  {render_sidebar('', workspace)}
</nav>
<main class="main">
  <div class="notfound">
    <h1>404</h1>
    <p>No such page: <code>{_html.escape(route_path)}</code></p>
    <p><a href="{url_prefix}/page/{ws['home']}">&larr; Back home</a></p>
  </div>
</main>
</body></html>"""


# --- Dispatch --------------------------------------------------------------

def load_dispatch_template():
    if os.path.isfile(DISPATCH_PROMPT_TEMPLATE):
        with open(DISPATCH_PROMPT_TEMPLATE, 'r', encoding='utf-8') as f:
            return f.read()
    return "Read the sidecar JSONL for page {page} and act on each comment."


def run_cursor_dispatch(route_path, workspace=DEFAULT_WORKSPACE):
    """Bundle staged review work for Grok in Cursor — not cc-dispatch."""
    comments = read_comments(route_path, workspace)
    prefix = workspace_url_prefix(workspace)

    def persist(updated):
        write_all_comments(route_path, updated, workspace)

    rel_path, err, count = cursor_intake.file_intake_card(route_path, comments, persist, prefix)
    if err:
        raise RuntimeError(err)
    return rel_path, count


def run_rsi_dispatch(route_path, workspace=DEFAULT_WORKSPACE):
    """File queued review comments as RSI Development Requests (workspace-scoped app routing)."""
    comments = read_comments(route_path, workspace)

    # Use workspace as app (playmaker -> 'playmaker', estate -> 'estate', etc.)
    app_override = workspace if workspace != DEFAULT_WORKSPACE else None

    # File one DR per queued comment
    count = 0
    for comment in comments:
        if comment.get('status') not in ('queued', 'seen'):
            continue
        if comment.get('type') == 'edit':
            continue  # Skip edit-type comments

        narrative = comment.get('text', '')
        dr_result = file_development_request(
            route_path,
            narrative,
            workspace,
            app_override=app_override
        )
        if not dr_result.get('error'):
            count += 1
            # Mark comment seen after filing
            comment['status'] = 'seen'

    if count > 0:
        write_all_comments(route_path, comments, workspace)

    return {
        'target': 'rsi',
        'dispatched_count': count,
        'task_name': None,
        'pid': None,
    }


def run_dispatch(route_path, workspace=DEFAULT_WORKSPACE):
    cfg = get_dispatch_config(route_path, workspace)
    if cfg['target'] == 'cursor':
        card_path, dispatched_count = run_cursor_dispatch(route_path, workspace)
        return {
            'target': 'cursor',
            'card_path': card_path,
            'dispatched_count': dispatched_count,
            'task_name': None,
            'pid': None,
        }

    if cfg['target'] == 'rsi':
        result = run_rsi_dispatch(route_path, workspace)
        return result

    ws = get_workspace(workspace)
    fs_path = resolve_page(route_path, workspace)
    sidecar = sidecar_path(route_path, workspace)
    slug = page_slug(route_path)
    # cc-dispatch appends its own .md to build the report filename; strip any
    # .md already in the slug so we don't end up with foo.md.md-shaped names.
    task_slug = re.sub(r'\.md$', '', slug)
    ws_infix = '' if workspace == DEFAULT_WORKSPACE else f'{workspace}-'
    task_name = f'review-comments-{ws_infix}{task_slug}'[:80]
    template = load_dispatch_template()
    # api_base includes the workspace URL prefix so the dispatched worker's
    # POST {api_base}/api/comments/reply calls land in the right workspace's sidecar
    # (the plain /api/... routes default to the estate workspace).
    prompt = template.format(
        page=route_path,
        page_fs_path=fs_path,
        sidecar_path=sidecar,
        api_base=f'http://localhost:8090{workspace_url_prefix(workspace)}',
    )
    prompt_file = os.path.join(ws['feedback_dir'], f'.dispatch-prompt-{slug}.md')
    with open(prompt_file, 'w', encoding='utf-8') as f:
        f.write(prompt)

    cc_dispatch_bin = os.path.expanduser('~/.local/bin/cc-dispatch')
    if not os.path.isfile(cc_dispatch_bin):
        cc_dispatch_bin = 'cc-dispatch'  # rely on PATH
    cmd = [cc_dispatch_bin, task_name, prompt_file]
    # launchd's default PATH is /usr/bin:/bin:/usr/sbin:/sbin — no homebrew.
    # cc-dispatch's own runner.sh shells out to bare `python3`, which under
    # that minimal PATH resolves to Xcode's /usr/bin/python3 (3.9.6, no
    # PEP 604 `X | None` support) and crashes. Explicitly widen PATH so the
    # child inherits homebrew's python3.12/3.14 the way an interactive shell
    # would. Same class of bug the launchd plist itself avoids by using an
    # explicit interpreter path.
    env = dict(os.environ)
    homebrew_bins = '/opt/homebrew/bin:/opt/homebrew/sbin'
    env['PATH'] = f"{homebrew_bins}:{env.get('PATH', '/usr/bin:/bin:/usr/sbin:/sbin')}"
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        cwd=PROJECTS_ROOT,
        env=env,
    )
    return {'target': 'dee', 'task_name': task_name, 'pid': proc.pid, 'card_path': None}


# --- Board / Portfolio regeneration -----------------------------------------

GENERATE_BOARD_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'generate_board.py')
GENERATE_PORTFOLIO_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'generate_portfolio.py')


RSI_INCOMING_DIR = os.path.join(PROJECTS_ROOT, 'SOMA', 'rsi', 'requests', 'incoming')
RSI_ROUTER = os.path.join(PROJECTS_ROOT, 'SOMA', 'tools', 'rsi', 'route_requests.py')
_PROJECT_META_RE = re.compile(r'\*\*Project:\*\*\s*([^·\n]+)')
_ROUTER_CARD_RE = re.compile(r'->\s*board/inbox/(\S+)')


def file_development_request(page, narrative, workspace=DEFAULT_WORKSPACE, app_override=None):
    """Quinn-tour "recommend changes" relay: write a Development Request
    (schema v1, rsi/README.md) into SOMA/rsi/requests/incoming/ and run the
    router synchronously (admin -> board card, idempotent per request-id).
    app comes from the completion page's `**Project:**` meta field, or app_override parameter.
    Returns {'file', 'app', 'card'?} or {'error': ...} — never raises."""
    try:
        fs_path = resolve_page(page, workspace)
        app = app_override or ''
        if not app:
            try:
                with open(fs_path, 'r', encoding='utf-8') as f:
                    m = _PROJECT_META_RE.search(f.read())
                if m:
                    # "playmaker + SOMA" / "SOMA (tools/monitoring)" -> first clean
                    # project token (the app field feeds card filenames downstream).
                    app = m.group(1).split('+')[0].strip().strip('*_ ').lower()
                    app = re.match(r'[a-z0-9_.-]*', app).group(0).rstrip('.-')
            except OSError:
                pass
        app = app or 'estate'
        req = {
            'app': app,
            'route': '/page/' + page,
            'reporter': {'role': 'admin', 'id': 'mike'},
            'intent': 'idea',
            'narrative': narrative,
            'evidence': ['_estate/completions/' + os.path.basename(fs_path)],
        }
        os.makedirs(RSI_INCOMING_DIR, exist_ok=True)
        slug = re.sub(r'[^a-z0-9]+', '-', os.path.basename(fs_path).lower()).strip('-')[:50]
        fname = 'verdict-%s-%s.json' % (slug, time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()))
        dr_path = os.path.join(RSI_INCOMING_DIR, fname)
        with open(dr_path, 'w', encoding='utf-8') as f:
            json.dump(req, f, indent=2)
        # Route it now. Same launchd-PATH gotcha as run_dispatch: pin the
        # interpreter explicitly rather than trusting the inherited PATH.
        py = sys.executable or '/opt/homebrew/bin/python3'
        proc = subprocess.run([py, RSI_ROUTER, 'route'],
                              capture_output=True, text=True, timeout=30)
        out = {'file': 'SOMA/rsi/requests/incoming/' + fname, 'app': app}
        m = _ROUTER_CARD_RE.search(proc.stdout or '')
        if proc.returncode == 0 and m:
            out['card'] = m.group(1)
        elif proc.returncode != 0:
            out['error'] = ('router exited %d: %s'
                            % (proc.returncode, (proc.stderr or proc.stdout or '')[-500:]))
        return out
    except Exception as e:  # noqa: BLE001 — verdict write must never 500 on DR relay
        return {'error': str(e)}


def run_board_regenerate():
    """Blocking (both generators run in well under a second — see their own
    module docstrings for the stream list). Runs board then portfolio; returns
    {ok, board_stdout, portfolio_stdout} or raises on a hard failure (a script
    crashing entirely, not an individual source being unavailable — the
    generators already degrade gracefully per-source)."""
    py = sys.executable or '/opt/homebrew/bin/python3'
    results = {}
    for key, script in (('board', GENERATE_BOARD_SCRIPT), ('portfolio', GENERATE_PORTFOLIO_SCRIPT)):
        proc = subprocess.run(
            [py, script],
            capture_output=True, text=True, timeout=60, cwd=PROJECTS_ROOT,
        )
        results[key] = {
            'rc': proc.returncode,
            'stdout': proc.stdout,
            'stderr': proc.stderr,
        }
        if proc.returncode != 0:
            raise RuntimeError(f'{key} generator exited {proc.returncode}: {proc.stderr[-2000:]}')
    return results


# --- HTTP handler ------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = 'soma-review/2.0'

    def log_message(self, fmt, *args):
        sys.stderr.write('%s - - [%s] %s\n' % (self.address_string(), self.log_date_time_string(), fmt % args))

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body, status=200):
        b = body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _read_json_body(self):
        length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(length) if length else b'{}'
        try:
            return json.loads(raw.decode('utf-8'))
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _split_workspace(path):
        """'/w/<slug>/rest/of/path' -> ('<slug>', '/rest/of/path'). Bare paths
        ('/page/...', '/api/...', etc, no /w/ prefix) -> (DEFAULT_WORKSPACE, path)
        unchanged, so every existing bookmark/sidecar keeps working."""
        if path.startswith('/w/'):
            rest = path[len('/w/'):]
            parts = rest.split('/', 1)
            workspace = parts[0]
            remainder = '/' + parts[1] if len(parts) > 1 else '/'
            return workspace, remainder
        return DEFAULT_WORKSPACE, path

    def do_GET(self):
        parsed = urlparse(self.path)
        workspace, path = self._split_workspace(parsed.path)
        qs = parse_qs(parsed.query)

        if path == '/':
            try:
                ws = get_workspace(workspace)
            except NotFoundError:
                self._send_html('<h1>404</h1><p>Unknown workspace.</p>', status=404)
                return
            self.send_response(302)
            self.send_header('Location', f'{workspace_url_prefix(workspace)}/page/{ws["home"]}')
            self.end_headers()
            return

        if path.startswith('/page/'):
            route_path = path[len('/page/'):]
            try:
                self._send_html(render_page(route_path, workspace))
            except NotFoundError:
                self._send_html(render_404(route_path, workspace), status=404)
            return

        if path.startswith('/raw/'):
            route_path = path[len('/raw/'):]
            try:
                fs_path = resolve_raw(route_path, workspace)
            except NotFoundError:
                self._send_html('<h1>404</h1><p>Not found or outside whitelist.</p>', status=404)
                return
            ext = os.path.splitext(fs_path)[1].lower()
            mime = _RAW_MIME.get(ext, 'application/octet-stream')
            with open(fs_path, 'rb') as f:
                body = f.read()
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Content-Disposition', f'inline; filename="{os.path.basename(fs_path)}"')
            self.end_headers()
            self.wfile.write(body)
            return

        if path == '/api/comments':
            page = qs.get('page', [''])[0]
            try:
                if page:
                    current_page_blocks(page, workspace)
                self._send_json(read_comments(page, workspace))
            except NotFoundError:
                self._send_json({'error': 'unknown workspace'}, status=404)
            return

        if path == '/api/workspaces':
            workspaces = load_workspaces()
            self._send_json({
                slug: {'label': cfg['label'], 'home': cfg['home'],
                       'url_prefix': workspace_url_prefix(slug)}
                for slug, cfg in workspaces.items()
            })
            return

        if path == '/healthz':
            self._send_json({'ok': True, 'ts': time.time()})
            return

        self._send_html('<h1>404</h1>', status=404)

    def do_POST(self):
        parsed = urlparse(self.path)
        workspace, path = self._split_workspace(parsed.path)

        if path == '/api/comments':
            data = self._read_json_body()
            page = data.get('page', '')
            ctype = data.get('type', 'comment')
            if ctype not in ('comment', 'edit', 'verdict', 'mark'):
                self._send_json({'error': 'invalid type'}, status=400)
                return
            if ctype == 'mark':
                mark_kind = data.get('mark_kind')
                if mark_kind not in ('agree', 'clarify', 'rewrite', 'strike',
                                     'note', 'ack', 'ruling'):
                    self._send_json({'error': 'invalid mark_kind'}, status=400)
                    return
                if not page:
                    self._send_json({'error': 'page required for mark'}, status=400)
                    return
                text = (data.get('text') or '').strip() or mark_kind.title()
                try:
                    strength = max(0.5, min(3.0, float(data.get('strength', 1))))
                except (TypeError, ValueError):
                    self._send_json({'error': 'invalid mark strength'}, status=400)
                    return
            elif ctype == 'edit':
                # Edit-as-comment: {type: "edit", anchor, snapshot (before), proposed (after)}.
                # `text` is auto-derived (short label) if not supplied so existing render
                # paths that expect a `text` field still have something sane to show.
                proposed = data.get('proposed')
                if not page or proposed is None:
                    self._send_json({'error': 'page and proposed required for edit'}, status=400)
                    return
                text = (data.get('text') or '').strip() or '(proposed edit)'
            elif ctype == 'verdict':
                # Portfolio triage: {type: "verdict", verdict: keep|restart|cancel|later,
                # row_id, anchor?, snapshot?}. No new storage — same JSONL sidecar, just
                # a comment carrying a `verdict` field; the client badges it distinctly.
                # Completion review (Quinn tours) adds approve|recommend-changes:
                # approve = off the review inbox; recommend-changes additionally files
                # an RSI Development Request (see below) that routes to a board card.
                verdict = data.get('verdict')
                if not page or verdict not in ('keep', 'restart', 'cancel', 'later',
                                               'approve', 'recommend-changes',
                                               'agree', 'disagree', 'discuss', 'defer'):
                    self._send_json({'error': 'page and a valid verdict required'}, status=400)
                    return
                row_id = (data.get('row_id') or '').strip()
                text = (data.get('text') or '').strip() or f'Verdict: {verdict}'
            else:
                text = (data.get('text') or '').strip()
                if not page or not text:
                    self._send_json({'error': 'page and text required'}, status=400)
                    return
            try:
                binding = validated_binding(page, workspace, data)
            except NotFoundError:
                self._send_json({'error': 'unknown page'}, status=404)
                return
            except BindingConflict as exc:
                self._send_json({'error': str(exc), 'unresolved': True}, status=409)
                return
            comment_id = str(uuid.uuid4())
            thread_id = data.get('thread_id') or comment_id
            comment = {
                'id': comment_id,
                'page': page,
                'type': ctype,
                'anchor': data.get('anchor'),
                'snapshot': data.get('snapshot', ''),
                'author': data.get('author', 'mike'),
                'text': text,
                'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                'status': 'queued',
                'thread_id': thread_id,
                'deleted': False,
                **binding,
            }
            if ctype == 'edit':
                comment['proposed'] = data.get('proposed')
            if ctype == 'mark':
                comment['mark_kind'] = mark_kind
                comment['strength'] = strength
                if data.get('scope') in ('section', 'page'):
                    comment['scope'] = data.get('scope')
                if data.get('reason'):
                    comment['reason'] = str(data.get('reason'))
                if data.get('proposed') is not None:
                    comment['proposed'] = data.get('proposed')
                if data.get('sent_because'):
                    comment['sent_because'] = str(data.get('sent_because'))
            if ctype == 'verdict':
                comment['verdict'] = data.get('verdict')
                comment['row_id'] = row_id
            append_comment(page, comment, workspace)
            if ctype == 'verdict' and data.get('verdict') == 'agree':
                if get_dispatch_config(page, workspace)['target'] == 'cursor':
                    cursor_intake.refresh_staged_manifest(
                        page,
                        read_comments(page, workspace),
                        workspace_url_prefix(workspace),
                    )
            if ctype == 'verdict' and data.get('verdict') == 'recommend-changes':
                # RSI loop relay: a recommend-changes verdict on a completion page
                # becomes a Development Request (reporter.role=admin) in
                # SOMA/rsi/requests/incoming/, immediately routed (admin -> board
                # card) by route_requests.py. Best-effort: a routing failure is
                # reported in the response, never blocks the verdict itself.
                comment['_dr'] = file_development_request(page, text, workspace)
            self._send_json(comment, status=201)
            return

        if path == '/api/public-film':
            data = self._read_json_body()
            testid = (data.get('testid') or '').strip()
            if not testid:
                self._send_json({'error': 'testid required'}, status=400)
                return
            if 'public' not in data:
                self._send_json({'error': 'public required'}, status=400)
                return
            manifest = update_public_film(testid, bool(data.get('public')))
            self._send_json({'ok': True, 'testid': testid, 'public': manifest[testid]})
            return

        if path == '/api/comments/status':
            data = self._read_json_body()
            page = data.get('page', '')
            comment_id = data.get('id', '')
            status = data.get('status', '')
            if status not in ('queued', 'seen', 'in-progress', 'done'):
                self._send_json({'error': 'invalid status'}, status=400)
                return
            ok = update_comment(page, comment_id, {'status': status}, workspace)
            if not ok:
                self._send_json({'error': 'comment not found'}, status=404)
                return
            self._send_json({'ok': True})
            return

        if path == '/api/comments/update':
            data = self._read_json_body()
            page = data.get('page', '')
            comment_id = data.get('id', '')
            text = (data.get('text') or '').strip()
            if not (page and comment_id and text):
                self._send_json({'error': 'page, id, text required'}, status=400)
                return
            existing = [c for c in read_comments(page, workspace) if c.get('id') == comment_id]
            if not existing:
                self._send_json({'error': 'comment not found'}, status=404)
                return
            if existing[0].get('author') != 'mike':
                self._send_json({'error': 'only mike-authored comments are editable via this endpoint'}, status=403)
                return
            ok = update_comment(page, comment_id, {
                'text': text,
                'edited_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            }, workspace)
            if not ok:
                self._send_json({'error': 'comment not found'}, status=404)
                return
            self._send_json({'ok': True})
            return

        if path == '/api/comments/delete':
            data = self._read_json_body()
            page = data.get('page', '')
            comment_id = data.get('id', '')
            if not (page and comment_id):
                self._send_json({'error': 'page and id required'}, status=400)
                return
            existing = [c for c in read_comments(page, workspace) if c.get('id') == comment_id]
            if not existing:
                self._send_json({'error': 'comment not found'}, status=404)
                return
            if existing[0].get('author') != 'mike':
                self._send_json({'error': 'only mike-authored comments are deletable via this endpoint'}, status=403)
                return
            # Soft-delete: keep the row (audit trail survives) but flag it and let the
            # UI hide/gray it out. deleted_at recorded for the same reason.
            ok = update_comment(page, comment_id, {
                'deleted': True,
                'deleted_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            }, workspace)
            if not ok:
                self._send_json({'error': 'comment not found'}, status=404)
                return
            self._send_json({'ok': True})
            return

        if path == '/api/comments/reply':
            data = self._read_json_body()
            page = data.get('page', '')
            thread_id = data.get('thread_id', '')
            text = (data.get('text') or '').strip()
            author = data.get('author', 'claude')
            if not (page and thread_id and text):
                self._send_json({'error': 'page, thread_id, text required'}, status=400)
                return
            try:
                resolve_page(page, workspace)
            except NotFoundError:
                self._send_json({'error': 'unknown page'}, status=404)
                return
            reply_id = str(uuid.uuid4())
            # Re-resolve the thread root against the current parse.  A reply never
            # blindly inherits a stale anchor.
            existing = [c for c in read_comments(page, workspace) if c.get('thread_id') == thread_id]
            root = existing[0] if existing else {}
            anchor = root.get('anchor')
            snapshot = root.get('snapshot', '')
            try:
                binding = validated_binding(page, workspace, root)
            except BindingConflict as exc:
                binding = {
                    'schema': 2,
                    'block_id': root.get('block_id'),
                    'from': root.get('from'),
                    'to': root.get('to'),
                    'quote': root.get('quote') or root.get('snapshot'),
                    'origin_quote': root.get('origin_quote') or root.get('quote') or root.get('snapshot'),
                    'block_text_sha': root.get('block_text_sha'),
                    'heading_path': root.get('heading_path'),
                    'source_sha': root.get('source_sha'),
                    'unresolved': True,
                    'unresolved_reason': str(exc),
                }
            comment = {
                'id': reply_id,
                'page': page,
                'type': 'comment',
                'anchor': anchor,
                'snapshot': snapshot,
                'author': author,
                'text': text,
                'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                'status': data.get('status', 'seen'),
                'thread_id': thread_id,
                'deleted': False,
                **binding,
            }
            append_comment(page, comment, workspace)
            self._send_json(comment, status=201)
            return

        if path == '/api/dispatch':
            data = self._read_json_body()
            page = data.get('page', '')
            try:
                resolve_page(page, workspace)
            except NotFoundError:
                self._send_json({'error': 'unknown page'}, status=404)
                return
            try:
                result = run_dispatch(page, workspace)
            except Exception as e:  # noqa: BLE001
                self._send_json({'error': str(e)}, status=500)
                return
            self._send_json({'ok': True, **result})
            return

        if path == '/api/board/regenerate':
            try:
                results = run_board_regenerate()
            except Exception as e:  # noqa: BLE001
                self._send_json({'error': str(e)}, status=500)
                return
            self._send_json({'ok': True, **results})
            return

        self._send_json({'error': 'not found'}, status=404)


def main():
    port = int(os.environ.get('SOMA_REVIEW_PORT', '8090'))
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    home = get_workspace(DEFAULT_WORKSPACE)['home']
    print(f'soma-review v2 listening on http://localhost:{port}/page/{home}')
    server.serve_forever()


if __name__ == '__main__':
    main()
