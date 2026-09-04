#!/usr/bin/env python3
"""
soma-review v2 — local interactive review server.

Serves whitelisted estate markdown as linked in-app pages with anchored,
persistent comments. Stdlib only (http.server + json), no external deps.

See soma-review/CLAUDE.md and README.md for the API and sidecar format.
"""
import copy
import html as _html
import json
import os
import re
import subprocess
import sys
import time
import uuid
import hashlib
import datetime
import calendar
import difflib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mdblocks import (  # noqa: E402
    parse_markdown, render_inline, build_lexicon_index, strip_auto_lexicon_marker,
    _esc, _esc_attr, _first_sentence, block_source_span, segment_sentences,
)
import blockmap  # noqa: E402
from blockmap import norm  # noqa: E402
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
.widget-block-frame{margin:12px 0;border:1px solid #2a2d34;border-radius:8px;overflow:hidden;background:#0f1115;}
.widget-unsupported{margin:12px 0;padding:10px 14px;border:1px dashed #5a4626;border-radius:8px;background:#211b12;color:#e0b463;font-size:13px;}
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

  let done = false;
  // MDP mechanics item B (corrected 2026-09-03): leaving a changed sentence
  // writes the trunk file AT ONCE (server-side, POST /api/comments type
  // 'edit' — see do_POST) and commits; this is never a "suggested"/queued
  // edit sitting apart from the document. A stale before-text (someone else
  // changed this block since you started editing) refuses with 409 — caught
  // here so the textarea's content isn't silently lost.
  const commitChange = async (commit) => {
    if (done) return;
    done = true;
    const after = ta.value;
    if (commit && after.trim() !== before.trim()) {
      try {
        await postComment({
          anchor: el.dataset.anchor, snapshot: before, proposed: after,
          text: 'Sentence change', type: 'edit', ...blockPayload(el),
        });
        toast('Change applied and committed.');
        loadThreadsIntoDOM();
      } catch (err) {
        alert('Change refused — this text changed on disk since you started editing. Reload and try again.');
        done = false;
        return;
      }
    }
    body.innerHTML = originalHtml;
  };

  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      // Cmd/Ctrl+Enter while editing text: commit the change immediately.
      e.preventDefault();
      commitChange(true);
      return;
    }
    if (e.key === 'Escape') { e.preventDefault(); commitChange(false); return; }
  });
  ta.addEventListener('blur', () => commitChange(true));
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


# --- v3 view: Playmaker-model mark layer (2026-09-03) ------------------------
# Ships alongside the classic sentence-dwell mark layer above, behind the
# `?view=v3` / localStorage toggle rendered by render_view_toggle(). See
# v2/CLAUDE.md "v3 view" section (added by this change) for the full design
# note. VIEW_TOGGLE_CSS/VIEW_TOGGLE_JS are the only pieces included on every
# page (classic included) — everything else here is embedded only when
# view == 'v3', so classic pages pay only the toggle's cost.

VIEW_TOGGLE_CSS = """
.view-toggle{display:flex;align-items:center;gap:6px;margin:10px 0 4px;font-size:11px;}
.view-toggle-label{color:#8a8f98;text-transform:uppercase;letter-spacing:.04em;font-size:10px;}
.view-toggle-btn{padding:3px 8px;border-radius:5px;border:1px solid #333;color:#9aa0a8;text-decoration:none;background:#1c1f26;}
.view-toggle-btn.active{background:#2f6feb;border-color:#2f6feb;color:#fff;}
.view-toggle-btn:hover{border-color:#2f6feb;}
"""

VIEW_TOGGLE_JS = r"""
(function(){
  var KEY = 'soma-review-view';
  document.querySelectorAll('[data-view-toggle]').forEach(function(a){
    a.addEventListener('click', function(){
      try { localStorage.setItem(KEY, a.dataset.viewToggle); } catch(_) {}
    });
  });
  // Returning-user redirect: if this URL carries no explicit ?view= and the
  // browser remembers a v3 preference, hop to it once. Never fires the other
  // direction (v3 -> classic) unprompted — classic is always reachable via
  // the explicit toggle link or ?view=classic.
  try {
    var url = new URL(window.location.href);
    if (!url.searchParams.has('view') && localStorage.getItem(KEY) === 'v3') {
      url.searchParams.set('view', 'v3');
      window.location.replace(url.toString());
    }
  } catch(_) {}
})();
"""

# Mark-kind vocabulary shown in the v3 filter list and mark rows. Reuses the
# same kind ids already ratified for the classic mark layer's K table (agree/
# clarify/rewrite/strike/note/ack/ruling — the Playmaker source/decision kinds
# this whole feature is modeled on) plus the two kinds unique to the
# non-dwell, always-editable v3 surface: `comment` (a plain block comment) and
# `edit` (a block-level tracked change, `wireEditableBlocks()`'s contenteditable
# diff). `verdict` covers Board/Portfolio rows. A mark made in the classic
# dwell view lands in the same sidecar and appears here too (D1: marks anchor
# to the model, never to DOM position).
V3_CSS = r"""
body.v3-view .sidebar{position:relative;transition:width .12s ease;}
body.v3-view .sidebar.v3-sidebar-closed{width:0!important;min-width:0;padding:0;overflow:hidden;border:0;}
.v3-sidebar-resize{position:absolute;top:0;right:-3px;width:6px;height:100%;cursor:col-resize;z-index:5;}
.v3-sidebar-resize:hover,.v3-sidebar-resize.dragging{background:#2f6feb55;}
.v3-sidebar-toggle-open{display:none;position:fixed;top:10px;left:10px;z-index:40;background:#1c1f26;color:#e6e6e6;border:1px solid #333;border-radius:6px;padding:4px 8px;cursor:pointer;font-size:12px;}
body.v3-view .sidebar.v3-sidebar-closed ~ .v3-sidebar-toggle-open,
body.v3-view .sidebar.v3-sidebar-closed + .v3-sidebar-toggle-open{display:block;}
.v3-header{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:10px 0 14px;border-bottom:1px solid #2a2d34;margin-bottom:16px;}
.v3-level-pill{font-size:10px;text-transform:uppercase;letter-spacing:.05em;padding:3px 8px;border-radius:20px;background:#3a2f1c;color:#e0b463;border:1px solid #5a4626;}
.v3-level-pill.is-object{background:#1c2a3a;color:#63a0e0;border-color:#264a5a;}
.v3-header h1{margin:0;font-size:20px;flex:1 1 auto;}
.v3-marks-btn{background:#1c1f26;color:#e6e6e6;border:1px solid #333;border-radius:6px;padding:6px 12px;font-size:13px;cursor:pointer;}
.v3-marks-btn .v3-marks-count{display:inline-block;min-width:18px;padding:0 5px;margin-left:6px;border-radius:9px;background:#2f6feb;color:#fff;font-size:11px;text-align:center;}
.v3-panel{position:fixed;top:0;right:-380px;width:360px;height:100%;background:#14161b;border-left:1px solid #2a2d34;box-shadow:-6px 0 24px rgba(0,0,0,.4);z-index:50;transition:right .18s ease;display:flex;flex-direction:column;}
.v3-panel.open{right:0;}
.v3-panel-head{padding:14px 16px;border-bottom:1px solid #2a2d34;display:flex;align-items:center;justify-content:space-between;}
.v3-panel-head h2{margin:0;font-size:15px;}
.v3-panel-close{background:none;border:0;color:#9aa0a8;font-size:18px;cursor:pointer;}
.v3-filter-row{display:flex;flex-wrap:wrap;gap:5px;padding:10px 16px;border-bottom:1px solid #2a2d34;}
.v3-filter-chip{font-size:11px;padding:3px 8px;border-radius:12px;border:1px solid #333;background:#1c1f26;color:#9aa0a8;cursor:pointer;}
.v3-filter-chip.active{background:#2f6feb;border-color:#2f6feb;color:#fff;}
.v3-mark-list{overflow-y:auto;flex:1 1 auto;padding:6px 10px;}
.v3-mark-row{padding:9px 10px;margin:4px 0;border-radius:8px;border:1px solid #23262d;cursor:pointer;background:#1a1c22;}
.v3-mark-row:hover{border-color:#2f6feb;}
.v3-mark-row .v3-mr-top{display:flex;align-items:center;gap:6px;font-size:11px;color:#8a8f98;}
.v3-mr-kind{font-weight:600;color:#e6e6e6;}
.v3-mr-status{margin-left:auto;padding:1px 6px;border-radius:8px;font-size:10px;text-transform:uppercase;}
.v3-mr-status.open{background:#3a2f1c;color:#e0b463;}
.v3-mr-status.resolved{background:#1c3a24;color:#63e089;}
.v3-mr-status.stale{background:#3a1c1c;color:#e06363;}
.v3-mr-quote{font-size:12px;color:#c8ccd2;margin-top:4px;font-style:italic;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;}
.v3-panel-empty{padding:20px 16px;color:#8a8f98;font-size:13px;}
.v3-dialog-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:60;display:flex;align-items:center;justify-content:center;}
.v3-dialog{width:460px;max-width:92vw;max-height:80vh;overflow-y:auto;background:#181b21;border:1px solid #2a2d34;border-radius:10px;padding:18px;}
.v3-dialog h3{margin:0 0 8px;font-size:15px;}
.v3-dialog .v3-dialog-quote{font-style:italic;color:#c8ccd2;border-left:3px solid #2f6feb;padding-left:10px;margin:8px 0;}
.v3-dialog-actions{display:flex;gap:8px;margin-top:14px;}
.v3-dialog-actions button{padding:6px 12px;border-radius:6px;border:1px solid #333;background:#1c1f26;color:#e6e6e6;cursor:pointer;}
.v3-dialog-actions button.primary{background:#2f6feb;border-color:#2f6feb;}
.v3-dialog-close{position:absolute;top:10px;right:14px;background:none;border:0;color:#9aa0a8;font-size:18px;cursor:pointer;}
.block-wrap.v3-has-marks{border-left:3px solid #e0b463;}
.block-wrap.v3-has-marks.v3-all-resolved{border-left-color:#63e089;}
.block-wrap.v3-has-marks .comment-count-pill{cursor:pointer;}
.v3-giveup-btn{margin-left:4px;}
.v3-done-btn{display:block;margin:28px 0 20px;}
.v3-done-btn:disabled,.v3-giveup-btn:disabled{opacity:.6;cursor:default;}
.v3-terms-heading{padding:10px 10px 4px;font-size:11px;color:#8a8f98;text-transform:uppercase;letter-spacing:.04em;border-top:1px solid #23262d;margin-top:6px;}
.v3-term-row{padding:9px 10px;margin:4px 0;border-radius:8px;border:1px solid #23262d;cursor:pointer;background:#1a1c22;}
.v3-term-row:hover{border-color:#2f6feb;}
/* Term-link visual is a v3-only, filter-gated affordance (spec item 3): off
   by default, hover/tooltip capability (wired in PAGE_JS, shared with
   classic) never depends on this class toggle. Higher specificity than the
   shared `a.term-link` rule in PAGE_CSS, so it wins without !important and
   classic (no body.v3-view) is untouched. */
body.v3-view a.term-link{color:inherit;text-decoration:none;cursor:text;}
body.v3-view.v3-terms-on a.term-link{color:#a3c9ff;text-decoration:underline dotted;cursor:help;}
/* Decision cards (task spec item 2iv), note-card fold, pointer links, and the
   one-action "back" chip (2026-09-03 rebuild, see mdp-proposal.md). */
.v3-alt{border:1px solid #2a2d34;border-radius:8px;padding:10px;margin:8px 0;background:#181b21;}
.v3-alt-default{border-color:#2f6feb;}
.v3-alt-label{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#8a8f98;margin-bottom:4px;}
.v3-alt-text{font-size:13px;color:#e6e6e6;margin-bottom:8px;}
.v3-history{margin-top:10px;border-top:1px solid #2a2d34;padding-top:6px;}
.v3-history-row{font-size:12px;color:#c8ccd2;padding:3px 0;}
.v3-pointer-link{color:#a3c9ff;text-decoration:underline dotted;}
.v3-fold-toggle{background:none;border:0;color:#8a8f98;cursor:pointer;font-size:11px;padding:0 4px 0 0;}
.v3-mark-row.v3-folded{opacity:.75;}
.v3-back-chip{display:none;order:-1;}
/* Generated Terms view (D, 2026-09-03): a page's ## Terms section body is
   replaced at render time (server.py::render_generated_terms_section) with
   one entry per term, definition + back-linked uses. */
.v3-terms-generated{display:flex;flex-direction:column;gap:14px;}
.v3-term-entry{border-top:1px solid #23262e;padding-top:10px;}
.v3-term-entry:first-child{border-top:0;padding-top:0;}
.v3-term-entry h4{margin:0 0 4px;font-size:14px;color:#e6e6e6;}
.v3-term-uses{font-size:12px;color:#8a8f98;margin-top:4px;}
.v3-term-use-link{color:#a3c9ff;text-decoration:none;margin-right:4px;}
.v3-term-use-link:hover{text-decoration:underline;}
/* Inline edit/replace-mark diffs (Mike's rule, 2026-09-03: "Wordsmithing
   shows as additions and deletions" IN THE TEXT, like Playmaker). Real
   <del>/<ins> elements, not span-only, so the diff is real document
   structure, not decoration — see v3PaintInlineDiffs() in V3_JS. Scoped
   under body.v3-view so classic pages (which never gain the
   v3-inline-diff class) are byte-identical. */
body.v3-view .block-body.v3-inline-diff{cursor:pointer;}
body.v3-view .v3-inline-diff del{background:#4a1f24;color:#f0a3ab;text-decoration:line-through;padding:0 2px;border-radius:2px;}
body.v3-view .v3-inline-diff ins{background:#1f4a2a;color:#9cf0ac;text-decoration:none;padding:0 2px;border-radius:2px;}
"""

V3_JS = r"""
(function(){
  const API_BASE = window.__API_BASE__;
  const ROUTE = window.__ROUTE__;
  const KIND_META = {
    comment:{label:'Comment', glyph:'💬'},
    edit:{label:'Edit', glyph:'✎'},
    agree:{label:'Agree', glyph:'✓'},
    clarify:{label:'Clarify', glyph:'?'},
    rewrite:{label:'Rewrite', glyph:'✎'},
    strike:{label:'Strike', glyph:'✗'},
    note:{label:'Note', glyph:'✎'},
    ack:{label:'Ack', glyph:'•'},
    ruling:{label:'Ruling', glyph:'§'},
    verdict:{label:'Verdict', glyph:'§'},
    decision:{label:'Decision', glyph:'§'},
    replace:{label:'Replace', glyph:'⇄'}
  };
  // A `replace` mark is stored as `type:"edit"` (so it rides the existing
  // snapshot/proposed diff machinery — see `renderDiffHtml` in PAGE_JS, shared
  // scope) plus `replace:true` for panel labeling only (Mike, 2026-09-03: "I
  // would like a card that replaces my statement with your restatement").
  function kindOf(c){
    if (c.type === 'mark') return c.mark_kind || 'note';
    if (c.type === 'verdict') return 'verdict';
    if (c.type === 'edit') return c.replace ? 'replace' : 'edit';
    return c.type || 'comment';
  }
  function escapeHtml(s){ return (s||'').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch])); }
  // Word-level diff for INLINE in-body rendering of open edit/replace marks
  // (Mike's rule, 2026-09-03: "Wordsmithing shows as additions and deletions"
  // IN THE TEXT, like Playmaker — not just inside the mark's own dialog).
  // Deliberately a separate local copy of the LCS diff in PAGE_JS's
  // wordDiff/renderDiffHtml (same algorithm) rather than a shared function:
  // PAGE_JS ships unmodified on every page including classic, and this v3-only
  // v3RenderDiffHtml emits real <del>/<ins> elements (not the classic
  // sidebar's <span class="diff-del/ins">) so v3-view's document text reads
  // as literal struck/added content — see v3PaintInlineDiffs() below.
  function v3WordDiff(before, after){
    const a = (before||'').split(/(\s+)/);
    const b = (after||'').split(/(\s+)/);
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
  function v3RenderDiffHtml(before, after){
    return v3WordDiff(before, after).map(p => {
      const esc = escapeHtml(p.v);
      if (p.t === 'del') return `<del>${esc}</del>`;
      if (p.t === 'ins') return `<ins>${esc}</ins>`;
      return esc;
    }).join('');
  }
  // Pointers render as real links (task spec item 2ii): a minimal
  // `[label](url)` -> `<a>` pass, applied to already-escaped text (so it must
  // run on the escaped string and re-open only the `()[]` sequences it
  // matches itself — safe because escapeHtml never produces literal `[`/`]`).
  function mdLinkify(escaped){
    // Supports an optional title `[label](href "first line of target")` —
    // pointers "carry the first line of their target beside them" (task spec
    // item 10): for cross-file pointers the note text is authored with that
    // title inline (no fetch possible from a note); for in-page `#anchor`
    // pointers `wirePointerTooltips()` fills it in live from the live DOM.
    return (escaped || '').replace(/\[([^\]]+)\]\(([^)\s"]+)(?:\s+&quot;([^&]*)&quot;)?\)/g, (_, label, href, title) => {
      const external = /^https?:\/\//.test(href);
      const t = title ? ` title="${title}"` : '';
      return `<a href="${href}"${external ? ' target="_blank" rel="noopener"' : ''} class="v3-pointer-link"${t}>${label}</a>`;
    });
  }
  function isReplyRow(m){ return !!(m.thread_id && m.thread_id !== m.id); }
  function repliesFor(m, rows){
    const tid = m.thread_id || m.id;
    return (rows || allRows).filter(r => r.id !== m.id && (r.thread_id === tid));
  }

  let allRows = [];
  let activeFilters = new Set(Object.keys(KIND_META));
  let panelOpen = false;
  let termsOn = false;

  // --- Read-state tracking (2026-09-03 spec item 1/2): per-block reading
  // progress, client-tracked as the reader scrolls. States advance
  // unreached -> skipped (entered viewport, left before 1.5s) -> read
  // (dwelled >=1.5s) -> interacted (a mark exists on the block — distinct
  // from "read" per Mike: "I can also skip over ... needs to be distinguished
  // from reading"). Feeds `last_read_block` and `read_states` on the
  // give-up/done reader-signal marks below.
  const blockStates = {};
  let blockEls = [];
  let deepestReadIndex = -1;
  let readObserver = null;
  const readTimers = new Map();

  function bumpDeepest(id){
    const idx = blockEls.findIndex(el => el.dataset.blockId === id);
    if (idx > deepestReadIndex) deepestReadIndex = idx;
  }

  function initReadTracking(){
    blockEls = Array.from(document.querySelectorAll('.block-wrap[data-block-id]'));
    blockEls.forEach(el => { blockStates[el.dataset.blockId] = blockStates[el.dataset.blockId] || 'unreached'; });
    if (!('IntersectionObserver' in window)) return;
    readObserver = new IntersectionObserver((entries) => {
      entries.forEach(entry => {
        const id = entry.target.dataset.blockId;
        if (entry.isIntersecting) {
          if (blockStates[id] === 'unreached') blockStates[id] = 'skipped';
          if (!readTimers.has(id)) {
            const t = setTimeout(() => {
              readTimers.delete(id);
              if (blockStates[id] !== 'interacted') blockStates[id] = 'read';
              bumpDeepest(id);
            }, 1500);
            readTimers.set(id, t);
          }
        } else {
          const t = readTimers.get(id);
          if (t) { clearTimeout(t); readTimers.delete(id); }
        }
      });
    }, {threshold: 0.5});
    blockEls.forEach(el => readObserver.observe(el));
  }

  function markInteractedFromRows(){
    allRows.forEach(m => {
      if (m.block_id && blockStates[m.block_id] !== undefined) {
        blockStates[m.block_id] = 'interacted';
        bumpDeepest(m.block_id);
      }
    });
  }

  function deepestReadBlockId(){
    return (deepestReadIndex >= 0 && blockEls[deepestReadIndex]) ? blockEls[deepestReadIndex].dataset.blockId : null;
  }

  function currentReadStates(){
    const out = {};
    blockEls.forEach(el => { out[el.dataset.blockId] = blockStates[el.dataset.blockId] || 'unreached'; });
    return out;
  }

  async function sendReaderSignal(signal, btn){
    if (!btn || btn.dataset.recorded === '1') return;
    btn.dataset.recorded = '1';
    const payload = {
      page: ROUTE, type: 'mark', mark_kind: 'reader-signal', signal,
      last_read_block: deepestReadBlockId(),
    };
    if (signal === 'done') payload.read_states = currentReadStates();
    try {
      await fetch(`${API_BASE}/api/comments`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
      btn.textContent = 'Recorded';
      btn.disabled = true;
    } catch(_) {
      btn.dataset.recorded = '';
    }
  }

  // --- Terms filter (spec item 3): off by default. ON makes term-links
  // visually marked (CSS class toggle, no DOM change) and lists every
  // occurrence in the panel so a reader can walk the document term by term.
  function decodeNormText(el){
    try { return atob((el && el.dataset.normText) || ''); } catch(_) { return ''; }
  }
  function firstSentenceOf(text){
    const m = text.match(/^[\s\S]*?[.!?](?=\s|$)/);
    return (m ? m[0] : text).trim().slice(0, 200);
  }
  function collectTermOccurrences(){
    return Array.from(document.querySelectorAll('a.term-link')).map(a => {
      const wrap = a.closest('.block-wrap[data-block-id]');
      return {
        term: a.textContent,
        blockId: wrap ? wrap.dataset.blockId : null,
        firstSentence: firstSentenceOf(decodeNormText(wrap)),
      };
    });
  }

  function currentShaMap(){
    const map = {};
    document.querySelectorAll('.block-wrap[data-block-id]').forEach(el => { map[el.dataset.blockId] = el.dataset.blockSha; });
    return map;
  }

  function decorate(rows){
    const shas = currentShaMap();
    const decorated = rows.filter(c => !c.deleted).map(c => {
      const kind = kindOf(c);
      const resolved = c.status === 'done';
      const stale = !!(c.block_id && c.block_text_sha && shas[c.block_id] && shas[c.block_id] !== c.block_text_sha);
      return Object.assign({}, c, {kind, resolved, stale});
    });
    // Fold (task spec item 5, Mike 2026-09-03: "an agreed restate card
    // COLLAPSES to one line ... apply generally: once a mark is agreed, it
    // folds"). A note/restate/decision card is "agreed" once its thread has a
    // reply whose text starts with Agree/Ratify (the answer-button copy below
    // writes exactly that prefix) — cheap, robust to the free-text reply
    // model this API already uses instead of a new structured "answer" field.
    decorated.forEach(m => {
      if (isReplyRow(m)) return;
      if (!(m.kind === 'note' || m.kind === 'restate' || m.kind === 'decision')) return;
      const replies = repliesFor(m, decorated);
      m.folded = replies.some(r => /^(agree|ratify)/i.test(r.text || ''));
    });
    return decorated;
  }

  async function fetchMarks(){
    const res = await fetch(`${API_BASE}/api/comments?page=${encodeURIComponent(ROUTE)}`);
    const raw = await res.json();
    // Reader-signal marks (give-up/done) are page-level telemetry, not review
    // marks — keep them out of the marks panel/badge/gutter entirely.
    allRows = decorate(raw.filter(c => !(c.type === 'mark' && c.mark_kind === 'reader-signal')));
    markInteractedFromRows();
    return allRows;
  }

  function paintBlockIndicators(){
    const byBlock = {};
    allRows.forEach(m => { if (m.block_id) (byBlock[m.block_id] = byBlock[m.block_id] || []).push(m); });
    document.querySelectorAll('.block-wrap[data-block-id]').forEach(el => {
      const marks = byBlock[el.dataset.blockId] || [];
      el.classList.toggle('v3-has-marks', marks.length > 0);
      el.classList.toggle('v3-all-resolved', marks.length > 0 && marks.every(m => m.resolved));
      const pill = el.querySelector('.comment-count-pill');
      if (pill && marks.length && !pill.dataset.v3Wired) {
        pill.dataset.v3Wired = '1';
        pill.addEventListener('click', (e) => {
          e.stopPropagation();
          openDialog(marks.filter(m => !m.resolved)[0] || marks[0]);
        });
      }
    });
  }

  // --- Inline edit/replace-mark diffs (task, 2026-09-03): every OPEN
  // edit/replace mark renders as struck/added text directly in its block's
  // body — not just inside the mark's own dialog. Resolving (Accept/Reject
  // via openDialog's extraActions below) clears the inline diff for the rest
  // of THIS session; the underlying .md is never touched, so a fresh load
  // always starts from the real base text plus whatever marks are still
  // open — the DOM is the decision record, not the file.
  function v3PaintInlineDiffs(){
    const byBlock = {};
    allRows.forEach(m => {
      if (!m.block_id || m.resolved) return;
      if (m.kind !== 'edit' && m.kind !== 'replace') return;
      (byBlock[m.block_id] = byBlock[m.block_id] || []).push(m);
    });
    document.querySelectorAll('.block-wrap[data-block-id]').forEach(el => {
      const body = el.querySelector('.block-body');
      if (!body) return;
      const marks = byBlock[el.dataset.blockId];
      if (marks && marks.length) {
        // Most-recently-created open edit/replace mark wins if more than one
        // targets the same block — real corpus (mdp-proposal.md) never has
        // this, but the tie-break keeps the surface sane if it ever does.
        const mark = marks.slice().sort((a,b) => (a.timestamp||'').localeCompare(b.timestamp||'')).pop();
        if (!body.dataset.v3OrigBody) {
          try { body.dataset.v3OrigBody = btoa(unescape(encodeURIComponent(body.innerHTML))); } catch(_) {}
        }
        body.innerHTML = v3RenderDiffHtml(mark.snapshot || '', mark.proposed || '');
        body.classList.add('v3-inline-diff');
        body.dataset.v3MarkId = mark.id;
      } else if (body.classList.contains('v3-inline-diff')) {
        if (body.dataset.v3OrigBody) {
          try { body.innerHTML = decodeURIComponent(escape(atob(body.dataset.v3OrigBody))); } catch(_) {}
          delete body.dataset.v3OrigBody;
        }
        body.classList.remove('v3-inline-diff');
        delete body.dataset.v3MarkId;
      }
    });
  }

  // Clicking an inline diff opens that mark's dialog instead of entering
  // click-to-edit mode (wireEditableBlocks in PAGE_JS, shared with classic).
  // Registered on the block-wrap (an ANCESTOR of .block-body) in the CAPTURE
  // phase so it runs before the body's own bubble-phase click-to-edit
  // listener, regardless of script load order; wired once per block-wrap,
  // reads current diff state live so it needs no re-wiring on refresh.
  function v3WireInlineDiffClicks(){
    document.querySelectorAll('.block-wrap[data-block-id]').forEach(el => {
      if (el.dataset.v3DiffClickWired) return;
      el.dataset.v3DiffClickWired = '1';
      el.addEventListener('click', (e) => {
        const body = el.querySelector('.block-body');
        if (!body || !body.classList.contains('v3-inline-diff')) return;
        if (!body.contains(e.target)) return; // let other affordances (pill, etc.) behave normally
        e.preventDefault();
        e.stopPropagation();
        const mark = allRows.find(r => r.id === body.dataset.v3MarkId);
        if (mark) openDialog(mark);
      }, true);
    });
  }

  // --- One-action return (task spec item 3, Mike 2026-09-03: "Terms should
  // have a way to go back to where you were with one click or one key or one
  // action"). Generalized to every in-page jump (term row, mark row's "Go to
  // block", any `#anchor` pointer link a note/decision card renders) — not
  // terms-only, since the same disorientation applies to any jump. `Escape`
  // and the header "← back" chip are the two one-action returns; either pops
  // the same stack, so they're interchangeable, never additive.
  const backStack = [];
  function showBackChip(){
    let chip = document.getElementById('v3-back-chip');
    if (!chip) {
      chip = document.createElement('button');
      chip.id = 'v3-back-chip';
      chip.className = 'v3-marks-btn v3-back-chip';
      chip.title = 'Back to where you were (or press Escape)';
      chip.textContent = '← back';
      chip.addEventListener('click', goBack);
      const header = document.querySelector('.v3-header');
      if (header) header.insertBefore(chip, header.firstChild);
    }
    chip.style.display = 'inline-block';
  }
  function hideBackChip(){
    const chip = document.getElementById('v3-back-chip');
    if (chip) chip.style.display = 'none';
  }
  function goBack(){
    const pos = backStack.pop();
    if (pos != null) window.scrollTo({top: pos, behavior:'smooth'});
    if (!backStack.length) hideBackChip();
  }
  function jumpToBlock(blockId){
    if (!blockId) return;
    const el = document.querySelector(`.block-wrap[data-block-id="${CSS.escape(blockId)}"]`);
    if (!el) return;
    backStack.push(window.scrollY);
    showBackChip();
    el.scrollIntoView({block:'center', behavior:'smooth'});
  }
  function jumpToAnchor(id){
    const target = document.getElementById(id) || document.querySelector(`.block-wrap[data-block-id="${CSS.escape(id)}"]`);
    if (!target) return false;
    backStack.push(window.scrollY);
    showBackChip();
    target.scrollIntoView({block:'center', behavior:'smooth'});
    return true;
  }
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && backStack.length) { e.preventDefault(); goBack(); }
  });
  // Deep link from the "Waiting on you" inbox: `#b:<block-id>` lands the reader
  // on the block carrying the oldest open ask, not at the top of the document.
  (function(){
    const h = window.location.hash || '';
    if (!h.startsWith('#b:')) return;
    const blockId = decodeURIComponent(h.slice(3));
    // Instant, not smooth: an incoming deep link should land, not animate past
    // the reader. And it must keep landing: the v3 view decorates blocks after
    // first paint (mark rails, edit chips, term links), which moves everything
    // below by hundreds of pixels — a single jump computed at `load` measured
    // 1193px off in testing. So re-jump until the target's position holds still
    // for two consecutive checks, up to 3s, and give up the moment the reader
    // scrolls or types, because from then on the position is theirs, not ours.
    // The v3 view re-lays-out the document after first paint (mark rails, edit
    // chips, term links, block decoration). Measured: the page was 8418px tall
    // at `load` and 6328px once settled, so a single jump left the target
    // 1193px above the viewport — a reader following an inbox link would land
    // in blank space. So: re-jump on every body resize until the height holds
    // still, and surrender the moment the reader scrolls or types, because from
    // then on the scroll position is theirs, not ours.
    // The v3 view keeps re-laying-out the document for seconds after first
    // paint (mark rails, edit chips, term links, block decoration). Measured on
    // MAC-STEWARD: 8418px tall at `load`, 6328px once settled. A single jump —
    // or a jump that stops as soon as the height looks stable — left the target
    // 1193px (then 507px) above the viewport, i.e. the reader following an
    // inbox link landed in blank space. Height stability is not a reliable
    // settle signal here because it plateaus mid-way. So: re-jump on a fixed
    // cadence for five seconds and surrender the instant the reader scrolls or
    // types, because from then on the scroll position is theirs, not ours.
    // The v3 view keeps mutating the document for many seconds after `load`
    // (mark rails, edit chips, term links, block decoration) and every mutation
    // above the target moves it. Measured on MAC-STEWARD: 8418px tall at load,
    // 6328px settled — a single jump landed 1193px off, a 5s fixed cadence
    // still 507px off, because the last shrink happened after the cadence
    // stopped. So follow the DOM instead of the clock: re-jump on any mutation
    // (debounced), for up to 15s, and surrender the instant the reader scrolls
    // or types, because from then on the scroll position is theirs, not ours.
    // Landing a deep link in this view is harder than it looks: the page keeps
    // re-laying-out for seconds after `load` (mark rails, edit chips, term
    // links), and not every reflow comes from a DOM mutation we can observe.
    // Measured on MAC-STEWARD: 8418px tall at load, 6328px settled. A single
    // jump landed 1193px above the target; a 5s fixed cadence and a
    // MutationObserver both still landed 507px off, because the last shrink
    // came after they stopped. So pin instead of jump: every animation frame
    // for eight seconds, re-centre the target if it has drifted more than a few
    // pixels. Self-correcting whatever the cause of the reflow. The reader
    // takes the position back the instant they scroll, touch, click or type.
    let surrendered = false;
    const surrender = () => { surrendered = true; };
    const pin = (deadline) => {
      if (surrendered || Date.now() > deadline) return;
      const el = document.querySelector(`.block-wrap[data-block-id="${CSS.escape(blockId)}"]`);
      if (el) {
        const want = (window.innerHeight - el.getBoundingClientRect().height) / 2;
        if (Math.abs(el.getBoundingClientRect().top - want) > 4) {
          el.scrollIntoView({block:'center', behavior:'auto'});
        }
      }
      requestAnimationFrame(() => pin(deadline));
    };
    window.addEventListener('load', () => {
      ['wheel','touchstart','keydown','mousedown'].forEach(
        ev => window.addEventListener(ev, surrender, {once:true, passive:true}));
      pin(Date.now() + 8000);
    });
  })();
  document.addEventListener('click', (e) => {
    // Generated Terms view "used N times" back-links (D, 2026-09-03): model-
    // anchored (block id + occurrence), handled before the generic `#anchor`
    // delegate below since these carry a literal `href="#"` (no fragment id).
    const useLink = e.target.closest('.v3-term-use-link');
    if (useLink) {
      e.preventDefault();
      jumpToBlock(useLink.dataset.blockId);
      return;
    }
    const a = e.target.closest('a[href^="#"]');
    if (!a) return;
    const id = decodeURIComponent(a.getAttribute('href').slice(1));
    if (jumpToAnchor(id)) e.preventDefault();
  });
  function wirePointerTooltips(){
    document.querySelectorAll('a.term-link, a.v3-pointer-link[href^="#"]').forEach(a => {
      if (a.title) return;
      const id = decodeURIComponent(a.getAttribute('href').slice(1));
      const target = document.getElementById(id) || document.querySelector(`.block-wrap[data-block-id="${CSS.escape(id)}"]`);
      if (!target) return;
      const raw = target.dataset && target.dataset.normText ? decodeNormText(target) : (target.textContent || '');
      const sentence = firstSentenceOf(raw.trim());
      if (sentence) a.title = sentence;
    });
  }

  function updateCountBadge(){
    const btn = document.getElementById('v3-marks-btn');
    if (!btn) return;
    const open = allRows.filter(m => !m.resolved).length;
    btn.querySelector('.v3-marks-count').textContent = open;
  }

  const expandedFold = new Set();

  function renderPanel(){
    const list = document.getElementById('v3-mark-list');
    if (!list) return;
    // Replies (thread_id set but not the root id — decision answers, note
    // agree/not-yet responses) are history attached to their root mark's
    // dialog, not separate top-level rows in the list (spec: decisions are
    // "separately marked" per-block, not per-answer).
    const rows = allRows.filter(m => activeFilters.has(m.kind) && !isReplyRow(m));
    const terms = termsOn ? collectTermOccurrences() : [];
    if (!rows.length && !terms.length) {
      list.innerHTML = '<div class="v3-panel-empty">No marks of the selected type(s).</div>';
      return;
    }
    rows.sort((a,b) => (a.timestamp||'').localeCompare(b.timestamp||''));
    let markHtml = rows.map(m => {
      const meta = KIND_META[m.kind] || KIND_META.note;
      const status = m.stale ? 'stale' : (m.resolved ? 'resolved' : 'open');
      const quote = m.quote || m.snapshot || m.text || '(page-level)';
      const folded = m.folded && !expandedFold.has(m.id);
      const chevron = m.folded ? `<button class="v3-fold-toggle" data-fold-id="${m.id}">${folded ? '▸' : '▾'}</button>` : '';
      const quoteHtml = folded ? '' : `<div class="v3-mr-quote">${mdLinkify(escapeHtml(quote))}</div>`;
      return `<div class="v3-mark-row${folded ? ' v3-folded' : ''}" data-mark-id="${m.id}">
        <div class="v3-mr-top">${chevron}<span class="v3-mr-kind">${meta.glyph} ${meta.label}</span>
          <span class="v3-mr-status ${status}">${status}</span></div>
        ${quoteHtml}
      </div>`;
    }).join('');
    let termsHtml = '';
    if (termsOn) {
      termsHtml = `<div class="v3-terms-heading">Terms &middot; ${terms.length} occurrence${terms.length===1?'':'s'}</div>` +
        (terms.length ? terms.map(t => `<div class="v3-term-row" data-block-id="${escapeHtml(t.blockId||'')}">
          <div class="v3-mr-top"><span class="v3-mr-kind">${escapeHtml(t.term)}</span></div>
          <div class="v3-mr-quote">${escapeHtml(t.firstSentence)}</div>
        </div>`).join('') : '<div class="v3-panel-empty">No terms on this page.</div>');
    }
    list.innerHTML = markHtml + termsHtml;
    list.querySelectorAll('.v3-mark-row').forEach(row => {
      row.addEventListener('click', (e) => {
        if (e.target.closest('.v3-fold-toggle')) return;
        const m = allRows.find(r => r.id === row.dataset.markId);
        if (m) openDialog(m);
      });
    });
    list.querySelectorAll('.v3-fold-toggle').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const id = btn.dataset.foldId;
        if (expandedFold.has(id)) expandedFold.delete(id); else expandedFold.add(id);
        renderPanel();
      });
    });
    list.querySelectorAll('.v3-term-row').forEach(row => {
      row.addEventListener('click', () => jumpToBlock(row.dataset.blockId));
    });
  }

  function renderFilters(){
    const row = document.getElementById('v3-filter-row');
    if (!row) return;
    const kindChips = Object.keys(KIND_META).map(k =>
      `<button class="v3-filter-chip${activeFilters.has(k)?' active':''}" data-kind="${k}">${KIND_META[k].label}</button>`
    ).join('');
    row.innerHTML = kindChips +
      `<button class="v3-filter-chip${termsOn?' active':''}" id="v3-terms-chip" data-terms-toggle>Terms</button>`;
    row.querySelectorAll('.v3-filter-chip[data-kind]').forEach(chip => {
      chip.addEventListener('click', () => {
        const k = chip.dataset.kind;
        if (activeFilters.has(k)) activeFilters.delete(k); else activeFilters.add(k);
        chip.classList.toggle('active');
        renderPanel();
      });
    });
    const termsChip = row.querySelector('#v3-terms-chip');
    termsChip.addEventListener('click', () => {
      termsOn = !termsOn;
      document.body.classList.toggle('v3-terms-on', termsOn);
      termsChip.classList.toggle('active', termsOn);
      renderPanel();
    });
  }

  function openPanel(){
    panelOpen = true;
    document.getElementById('v3-panel').classList.add('open');
    renderFilters(); renderPanel();
  }
  function closePanel(){ panelOpen = false; document.getElementById('v3-panel').classList.remove('open'); }

  // --- Resolve-advances-in-document-order (MDP mechanics item C, 2026-09-03):
  // after Accept/Reject/Agree/Ratify, the NEXT open mark in document order
  // (block position on the page, then creation order for co-located marks)
  // has its dialog opened and is scrolled into view; at the true end, focus
  // moves to "I'm done" instead. Document order (not filter/timestamp order,
  // which is what the pre-existing generic Resolve button used) — cards and
  // inline edit/replace marks share this one path.
  function blockDomIndex(blockId){
    if (!blockId) return Number.MAX_SAFE_INTEGER;
    const nodes = Array.from(document.querySelectorAll('.block-wrap[data-block-id]'));
    const idx = nodes.findIndex(el => el.dataset.blockId === blockId);
    return idx === -1 ? Number.MAX_SAFE_INTEGER : idx;
  }
  function nextOpenMark(afterId){
    const ordered = allRows.filter(r => !isReplyRow(r)).slice().sort((a, b) => {
      const ba = blockDomIndex(a.block_id), bb = blockDomIndex(b.block_id);
      if (ba !== bb) return ba - bb;
      return (a.timestamp || '').localeCompare(b.timestamp || '');
    });
    const idx = ordered.findIndex(r => r.id === afterId);
    for (let i = idx + 1; i < ordered.length; i++) if (!ordered[i].resolved) return ordered[i];
    return null; // no wraparound — the end of the document ends the walk
  }
  function advanceAfterResolve(m){
    const next = nextOpenMark(m.id);
    if (next) {
      jumpToBlock(next.block_id);
      openDialog(allRows.find(r => r.id === next.id) || next);
    } else {
      document.getElementById('v3-done-btn')?.focus();
    }
  }

  // --- Settle / Revert (items B/C, corrected 2026-09-03): an edit/replace
  // mark's change was already written to the trunk file the moment it was
  // created (server-side — see ctype=='edit' in do_POST). Settle just
  // resolves the mark (no file write); Revert writes the mark's `before`
  // text back into the trunk, committed. Both apply the server's freshly
  // re-rendered block HTML in place (v3ApplyMergedBlockHtml keeps the
  // existing .block-wrap/.block-body ELEMENTS so their already-bound
  // listeners — comment affordance, click-to-edit — survive; only the
  // wrapper's data-* attributes and the body's innerHTML are refreshed) —
  // no full page reload. The diff-overlay bookkeeping is explicitly cleared
  // here rather than left to v3PaintInlineDiffs' own cache-restore path:
  // that path caches whatever the block-body showed the FIRST time a diff
  // was painted, which under this model is already the post-change text,
  // not genuinely "before" — trusting it after a revert would silently show
  // the wrong sentence.
  function v3ApplyMergedBlockHtml(wrap, html){
    if (!wrap || !html) return;
    const tmp = document.createElement('div');
    tmp.innerHTML = html;
    const fresh = tmp.firstElementChild;
    if (!fresh) return;
    Object.keys(fresh.dataset).forEach(k => { wrap.dataset[k] = fresh.dataset[k]; });
    const freshBody = fresh.querySelector('.block-body');
    const body = wrap.querySelector('.block-body');
    if (freshBody && body) {
      body.innerHTML = freshBody.innerHTML;
      body.classList.remove('v3-inline-diff');
      delete body.dataset.v3OrigBody;
      delete body.dataset.v3MarkId;
    }
  }
  async function settleOrRevertMark(m, action){
    const res = await fetch(`${API_BASE}/api/marks/merge`, {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({page: ROUTE, id: m.id, action, author: 'mike'})});
    let data = {};
    try { data = await res.json(); } catch(_) {}
    if (!res.ok) { alert(data.error || (action + ' failed')); return false; }
    if (data.html) {
      const wrap = document.querySelector(`.block-wrap[data-block-id="${CSS.escape(m.block_id)}"]`);
      v3ApplyMergedBlockHtml(wrap, data.html);
    }
    await fetchMarks();
    paintBlockIndicators(); v3PaintInlineDiffs(); v3WireInlineDiffClicks(); updateCountBadge();
    if (panelOpen) renderPanel();
    return true;
  }

  async function setStatus(m, status){
    await fetch(`${API_BASE}/api/comments/status`, {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({page: ROUTE, id: m.id, status})});
    await fetchMarks();
    paintBlockIndicators(); v3PaintInlineDiffs(); updateCountBadge(); if (panelOpen) renderPanel();
  }

  // Answers to decision/note cards are plain replies into the mark's own
  // thread (existing `/api/comments/reply`, no new endpoint) — the reply text
  // IS the recorded answer ("Ratify: A", "Agree and unpack", "Not yet"), and
  // the root mark's own `status` (existing `/api/comments/status`) is what
  // drives open/resolved so the panel/badge machinery needs no new field.
  async function postAnswer(m, text, status){
    await fetch(`${API_BASE}/api/comments/reply`, {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({page: ROUTE, thread_id: m.thread_id || m.id, text, author: 'mike', status: 'seen'})});
    await setStatus(m, status);
  }

  function openDialog(m){
    if (!m) return;
    document.querySelectorAll('.v3-dialog-backdrop').forEach(el => el.remove());
    const meta = KIND_META[m.kind] || KIND_META.note;
    const backdrop = document.createElement('div');
    backdrop.className = 'v3-dialog-backdrop';
    const status = m.stale ? 'stale' : (m.resolved ? 'resolved' : 'open');
    let diff;
    let extraActions = '';
    if (m.kind === 'decision') {
      const dmeta = m.meta || {};
      const alts = dmeta.alternatives || [];
      diff = `<div class="v3-dialog-quote">${mdLinkify(escapeHtml(dmeta.prompt || m.text || ''))}</div>` +
        alts.map(a => `<div class="v3-alt${a.key === dmeta.default ? ' v3-alt-default' : ''}">
            <div class="v3-alt-label">${escapeHtml(a.key)}${a.key === dmeta.default ? ' — recommended' : ''}</div>
            <div class="v3-alt-text">${mdLinkify(escapeHtml(a.text || ''))}</div>
            <button class="primary" data-v3-ratify="${escapeHtml(a.key)}">Ratify ${escapeHtml(a.key)}</button>
          </div>`).join('');
      extraActions = `<button data-v3-not-yet>Not yet</button><button data-v3-reject>Reject</button>`;
    } else if (m.type === 'edit') {
      diff = `<div class="v3-dialog-quote"><del>${escapeHtml(m.snapshot||'')}</del></div><div class="v3-dialog-quote"><ins>${mdLinkify(escapeHtml(m.proposed||''))}</ins></div>`;
    } else {
      diff = `<div class="v3-dialog-quote">${mdLinkify(escapeHtml(m.quote || m.snapshot || m.text || ''))}</div>`;
    }
    if (m.kind === 'note' || m.kind === 'restate') {
      extraActions = `<button class="primary" data-v3-agree>Agree</button>
        <button data-v3-agree-unpack>Agree and unpack</button>
        <button data-v3-not-yet>Not yet</button>`;
    }
    // Edit/replace marks opened from their inline diff (v3PaintInlineDiffs):
    // the trunk file already holds the proposed text (written at change
    // time — item B). Settle resolves the mark and clears the del/ins
    // overlay (trunk unchanged). Revert writes `before` back into the trunk
    // (committed) and resolves the mark the other way. Edit re-opens the
    // block's own click-to-edit textarea (enterEditMode, PAGE_JS, shared
    // with classic) so Mike can change it again before deciding.
    if ((m.kind === 'edit' || m.kind === 'replace') && !m.resolved) {
      extraActions = `<button class="primary" data-v3-accept>Settle</button>
        <button data-v3-reject-edit>Revert</button>
        <button data-v3-edit-more>Edit</button>`;
    }
    const replies = repliesFor(m).sort((a,b) => (a.timestamp||'').localeCompare(b.timestamp||''));
    const historyHtml = replies.length ? `<div class="v3-history"><div class="v3-terms-heading">History</div>` +
      replies.map(r => `<div class="v3-history-row">${escapeHtml(r.author||'mike')}: ${escapeHtml(r.text||'')}</div>`).join('') +
      `</div>` : '';
    backdrop.innerHTML = `<div class="v3-dialog">
      <button class="v3-dialog-close" data-v3-close>&times;</button>
      <h3>${meta.glyph} ${meta.label} &middot; <span class="v3-mr-status ${status}">${status}</span></h3>
      ${diff}
      ${m.text && m.type !== 'edit' && m.kind !== 'decision' ? `<div>${mdLinkify(escapeHtml(m.text))}</div>` : ''}
      ${historyHtml}
      <div style="color:#8a8f98;font-size:11px;margin-top:6px;">${escapeHtml(m.author||'mike')} &middot; block ${escapeHtml(m.block_id||'(page-level)')}${m.stale?' &middot; source text has changed since this mark was made':''}</div>
      <div class="v3-dialog-actions">
        ${extraActions}
        ${!extraActions ? (m.resolved ? '<button data-v3-reopen>Reopen</button>' : '<button class="primary" data-v3-resolve>Resolve</button>') : ''}
        <button data-v3-scroll>Go to block</button>
      </div>
    </div>`;
    document.body.appendChild(backdrop);
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) backdrop.remove(); });
    backdrop.querySelector('[data-v3-close]').addEventListener('click', () => backdrop.remove());
    backdrop.querySelector('[data-v3-scroll]')?.addEventListener('click', () => {
      jumpToBlock(m.block_id);
    });
    backdrop.querySelector('[data-v3-resolve]')?.addEventListener('click', async () => {
      await setStatus(m, 'done');
      backdrop.remove();
      advanceAfterResolve(m);
    });
    backdrop.querySelector('[data-v3-reopen]')?.addEventListener('click', async () => {
      await setStatus(m, 'queued');
      backdrop.remove();
    });
    backdrop.querySelectorAll('[data-v3-ratify]').forEach(btn => {
      btn.addEventListener('click', async () => {
        await postAnswer(m, `Ratify: Alternative ${btn.dataset.v3Ratify}`, 'done');
        backdrop.remove();
        advanceAfterResolve(m);
      });
    });
    backdrop.querySelector('[data-v3-not-yet]')?.addEventListener('click', async () => {
      await postAnswer(m, 'Not yet', 'queued');
      backdrop.remove();
    });
    backdrop.querySelector('[data-v3-reject]')?.addEventListener('click', async () => {
      await postAnswer(m, 'Reject', 'done');
      backdrop.remove();
      advanceAfterResolve(m);
    });
    backdrop.querySelector('[data-v3-agree]')?.addEventListener('click', async () => {
      await postAnswer(m, 'Agree', 'done');
      backdrop.remove();
      advanceAfterResolve(m);
    });
    backdrop.querySelector('[data-v3-agree-unpack]')?.addEventListener('click', async () => {
      // "Agree and unpack" (task spec item 1): records assent AND flags the
      // card for a depth-two expansion next round — same answer mechanism,
      // distinguished only by the reply text the harvest reads.
      await postAnswer(m, 'Agree and unpack — flagged for depth-two expansion next round', 'done');
      backdrop.remove();
      advanceAfterResolve(m);
    });
    backdrop.querySelector('[data-v3-accept]')?.addEventListener('click', async () => {
      // Settle (item B): trunk already holds the right text — resolves the
      // mark and redraws the block without the del/ins overlay.
      const ok = await settleOrRevertMark(m, 'settle');
      backdrop.remove();
      if (ok) advanceAfterResolve(m);
    });
    backdrop.querySelector('[data-v3-reject-edit]')?.addEventListener('click', async () => {
      // Revert (item B): writes `before` back into the trunk file,
      // committed — see apply_sentence_revert.
      const ok = await settleOrRevertMark(m, 'revert');
      backdrop.remove();
      if (ok) advanceAfterResolve(m);
    });
    backdrop.querySelector('[data-v3-edit-more]')?.addEventListener('click', () => {
      backdrop.remove();
      jumpToBlock(m.block_id);
      const wrap = document.querySelector(`.block-wrap[data-block-id="${CSS.escape(m.block_id)}"]`);
      const body = wrap && wrap.querySelector('.block-body');
      if (wrap && body && typeof enterEditMode === 'function') enterEditMode(wrap, body);
    });
  }

  function wireSidebar(){
    const sidebar = document.querySelector('.sidebar');
    if (!sidebar) return;
    const openBtn = document.createElement('button');
    openBtn.className = 'v3-sidebar-toggle-open';
    openBtn.textContent = '≡ Menu';
    sidebar.insertAdjacentElement('afterend', openBtn);
    const closeBtn = document.createElement('button');
    closeBtn.textContent = '×';
    closeBtn.title = 'Close sidebar';
    closeBtn.style.cssText = 'position:absolute;top:8px;right:12px;background:none;border:0;color:#9aa0a8;cursor:pointer;font-size:16px;z-index:6;';
    sidebar.insertBefore(closeBtn, sidebar.firstChild);
    const resize = document.createElement('div');
    resize.className = 'v3-sidebar-resize';
    sidebar.appendChild(resize);

    let closed = false, width = 220;
    try {
      closed = localStorage.getItem('soma-review-sidebar-closed') === '1';
      width = parseInt(localStorage.getItem('soma-review-sidebar-width') || '220', 10) || 220;
    } catch(_) {}
    sidebar.style.width = width + 'px';
    if (closed) sidebar.classList.add('v3-sidebar-closed');

    closeBtn.addEventListener('click', () => {
      sidebar.classList.add('v3-sidebar-closed');
      try { localStorage.setItem('soma-review-sidebar-closed', '1'); } catch(_) {}
    });
    openBtn.addEventListener('click', () => {
      sidebar.classList.remove('v3-sidebar-closed');
      try { localStorage.setItem('soma-review-sidebar-closed', '0'); } catch(_) {}
    });
    let dragging = false;
    resize.addEventListener('mousedown', (e) => { dragging = true; resize.classList.add('dragging'); e.preventDefault(); });
    document.addEventListener('mousemove', (e) => {
      if (!dragging) return;
      const w = Math.max(140, Math.min(480, e.clientX));
      sidebar.style.width = w + 'px';
    });
    document.addEventListener('mouseup', () => {
      if (!dragging) return;
      dragging = false; resize.classList.remove('dragging');
      try { localStorage.setItem('soma-review-sidebar-width', parseInt(sidebar.style.width, 10)); } catch(_) {}
    });
  }

  function wireHeader(){
    const main = document.querySelector('.main');
    if (!main) return;
    const header = document.createElement('header');
    header.className = 'v3-header';
    const level = window.__LEVEL_LABEL__ || 'object';
    const isObject = level === 'object';
    header.innerHTML = `<span class="v3-level-pill${isObject?' is-object':''}">${escapeHtml(level)}</span>
      <h1>${escapeHtml(document.title.replace(/ .. soma-review$/, ''))}</h1>
      <button class="v3-marks-btn" id="v3-marks-btn">Marks<span class="v3-marks-count">0</span></button>
      <button class="v3-marks-btn v3-giveup-btn" id="v3-giveup-btn" title="Tell the origin where the document lost you">I've given up</button>`;
    main.insertBefore(header, main.firstChild);
    header.querySelector('#v3-marks-btn').addEventListener('click', () => panelOpen ? closePanel() : openPanel());
    header.querySelector('#v3-giveup-btn').addEventListener('click', (e) => sendReaderSignal('gave-up', e.currentTarget));
  }

  function wirePanel(){
    const panel = document.createElement('div');
    panel.className = 'v3-panel';
    panel.id = 'v3-panel';
    panel.innerHTML = `<div class="v3-panel-head"><h2>Marks</h2><button class="v3-panel-close" id="v3-panel-close">&times;</button></div>
      <div class="v3-filter-row" id="v3-filter-row"></div>
      <div class="v3-mark-list" id="v3-mark-list"></div>`;
    document.body.appendChild(panel);
    panel.querySelector('#v3-panel-close').addEventListener('click', closePanel);
  }

  function wireEndOfDoc(){
    // End-of-document "I'm done" (spec item 1) — inserted client-side right
    // before the page-discussion section, which every workspace page renders
    // unconditionally, so this never depends on doc content/length.
    const anchor = document.querySelector('.page-discussion') || document.getElementById('unresolved-marks');
    if (!anchor || !anchor.parentNode) return;
    const btn = document.createElement('button');
    btn.className = 'v3-marks-btn v3-done-btn';
    btn.id = 'v3-done-btn';
    btn.textContent = "I'm done";
    anchor.parentNode.insertBefore(btn, anchor);
    btn.addEventListener('click', (e) => sendReaderSignal('done', e.currentTarget));
  }

  document.addEventListener('DOMContentLoaded', async () => {
    document.body.classList.add('v3-view');
    wireSidebar();
    wireHeader();
    wirePanel();
    wireEndOfDoc();
    initReadTracking();
    await fetchMarks();
    paintBlockIndicators();
    v3PaintInlineDiffs();
    v3WireInlineDiffClicks();
    updateCountBadge();
    wirePointerTooltips();
    // Re-decorate whenever the existing comment plumbing reloads threads
    // (new comment/edit saved, reply posted) so the panel/badge/gutter stay live.
    // This is also how a Mike-typed edit (enterEditMode -> postComment,
    // PAGE_JS) picks up its own inline diff: postComment fires
    // soma-comment-saved on save, same as any other mark.
    document.addEventListener('soma-comment-saved', async () => {
      await fetchMarks(); paintBlockIndicators(); v3PaintInlineDiffs(); v3WireInlineDiffClicks(); updateCountBadge(); wirePointerTooltips(); if (panelOpen) renderPanel();
    });
  });
})();
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
    waiting_total = sum(r['on_mike'] for r in collect_waiting()
                        if not _waiting_is_stale(r['last_ts']))
    badge = (f' <span style="background:#4a3a1f;color:#f0c674;border-radius:9px;'
             f'padding:0 6px;font-size:10px;font-weight:700;">{waiting_total}</span>'
             if waiting_total else '')
    items.append(f'<a href="{url_prefix}/waiting" style="font-weight:600;">&#9203; Waiting on you{badge}</a>')
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


def _auto_local_for(block, terms_out):
    """Whether THIS block may auto-link the page's own Terms entries.

    Mirrors mdblocks.parse_markdown's `auto_local_now()` on the mark-layer
    render path, which re-renders prose sentence by sentence and so bypasses the
    paragraph HTML parse_markdown produced. Before 2026-09-04 that path dropped
    page-local term links from every paragraph on a v3 page: only list blocks,
    which keep their parsed html verbatim, kept theirs. A page's own Terms
    section still never links itself.
    """
    if not terms_out:
        return False
    # blockmap.norm() NFC-normalizes and collapses whitespace; it does NOT
    # lowercase. Every page writes `## Terms` with a capital T, so comparing the
    # normed title against the lowercase literal was True everywhere and this
    # guard never fired once: the Terms section's own prose auto-linked to
    # bullets two lines below it. Caught by the adversarial pass, not by the
    # suite, because the suite pinned the OTHER copy of this rule
    # (mdblocks.auto_local_now), which tracks heading LEVEL and was correct.
    title = blockmap.norm(block.get('mark_layer_section_title') or '').strip().lower()
    return title != 'terms'


def mark_layer_inner(block, link_resolver=None, terms=None, lexicon=None, auto_lexicon=False,
                     auto_local_terms=False):
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
            f'{render_inline(quote, link_resolver, terms=terms, lexicon=lexicon, auto_lexicon=auto_lexicon, auto_seen=auto_seen, auto_local_terms=auto_local_terms)}'
            f'</span>'
        )
    content = ' '.join(pieces)
    tag = 'blockquote' if kind == 'blockquote' else 'p'
    return f'<{tag}>{content}</{tag}>', True


def render_block_html(block, route_path, status_chip=None, link_resolver=None, terms=None,
                       lexicon=None, auto_lexicon=False, auto_local_terms=False):
    kind = block['kind']
    anchor = block['anchor']
    block_id = _html_attr_escape(block['id'])
    block_sha = _html_attr_escape(blockmap.block_text_sha(block))
    snapshot = _html_attr_escape(block['snapshot'])
    inner, has_sentence_units = mark_layer_inner(
        block, link_resolver, terms=terms, lexicon=lexicon,
        auto_lexicon=auto_lexicon and kind != 'heading',
        auto_local_terms=auto_local_terms and kind != 'heading',
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
    editable = kind not in ('code', 'table', 'film', 'widget')
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


# --- Sentence-level change / settle / revert (MDP mechanics items B/C,
# corrected by Mike 2026-09-03 evening): a mark is a branch of a SENTENCE
# (not a block/paragraph), and an edit/replace mark is never "queued" or
# "pending" — the moment any author (Mike typing and leaving a sentence, or
# Claude posting an edit/replace mark) changes it, the trunk file is updated
# AT ONCE to the new text and committed. `mark['snapshot']` (before) /
# `mark['proposed']` (the text now actually in the trunk) together ARE the
# open-change record; del/ins painting (v3PaintInlineDiffs, client-side,
# unchanged by this) means "here is an open change", not "here is a pending
# suggestion". Settle resolves the mark with no file write (the trunk
# already holds the right text). Revert writes `before` back into the trunk,
# committed, and resolves the mark the other way. No model inference
# anywhere in this path: `proposed`/`snapshot` are copied verbatim, byte for
# byte, in both directions. -------------------------------------------------

class MergeConflict(Exception):
    """Raised when the sentence/block text actually on disk no longer matches
    what a change or revert expects — refuse loudly rather than silently
    clobbering whatever is there now."""


def _git_repo_root(fs_path):
    """Return the git repo root containing fs_path, or None if it isn't
    inside one (e.g. an ad-hoc directory with no .git — merge still writes
    the file in that case, just skips the commit step)."""
    try:
        out = subprocess.run(
            ['git', '-C', os.path.dirname(fs_path), 'rev-parse', '--show-toplevel'],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _git_commit_file(repo_root, fs_path, message):
    """`git add` + `git commit` the one file, trailer `Seat: dee` per estate
    convention. Returns the new commit sha, or None if there was nothing to
    commit (e.g. the write happened to reproduce byte-identical content) or
    git isn't available/configured for commits in this repo — a merge that
    changed the file on disk still counts as applied; the commit is a
    best-effort audit trail on top of that, not the definition of success."""
    # realpath both sides: `git rev-parse --show-toplevel` resolves symlinks
    # (macOS /var -> /private/var is the concrete case that bit this in
    # testing), but fs_path may still carry the symlinked form — a mismatch
    # here makes git report the file as "outside repository".
    rel = os.path.relpath(os.path.realpath(fs_path), os.path.realpath(repo_root))
    try:
        add = subprocess.run(['git', '-C', repo_root, 'add', rel],
                              capture_output=True, text=True, timeout=10)
        if add.returncode != 0:
            return None, add.stderr.strip()
        commit = subprocess.run(
            ['git', '-C', repo_root, 'commit', '-m', message, '--trailer', 'Seat: dee'],
            capture_output=True, text=True, timeout=10,
        )
        if commit.returncode != 0:
            # "nothing to commit" is not an error — the write may have been a
            # no-op (proposed == current text) or an earlier merge already
            # committed the identical bytes on retry.
            if 'nothing to commit' in (commit.stdout + commit.stderr).lower():
                return None, None
            return None, commit.stderr.strip()
        sha = subprocess.run(['git', '-C', repo_root, 'rev-parse', 'HEAD'],
                              capture_output=True, text=True, timeout=10)
        return (sha.stdout.strip() or None), None
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)


def _locate_change_span(route_path, workspace, block_id, expected_text):
    """Locate the exact span in the real file that currently holds
    `expected_text` — either the WHOLE block (the common case: this doc's
    blocks are one sentence each, and classic whole-block edit-as-comment
    always submits the full block text) or, if `expected_text` is a strict
    sub-sentence of a multi-sentence block, that one sentence
    (`mdblocks.segment_sentences`, item C: "unit of editing is the
    sentence"). Returns `(fs_path, normalized_src, start, end, block,
    sentence_index_or_None)`. Raises MergeConflict if `expected_text` matches
    neither the whole block nor exactly one of its sentences — the caller's
    idea of "before" no longer corresponds to anything really on disk."""
    fs_path = resolve_page(route_path, workspace)
    _src_bytes, blocks, _mapping, _report = current_page_blocks(route_path, workspace)
    block = next((b for b in blocks if b['id'] == block_id), None)
    if block is None:
        raise MergeConflict(f'block {block_id} no longer exists on this page')
    with open(fs_path, 'r', encoding='utf-8') as f:
        raw_text = f.read()
    normalized, block_start, block_end, exact_text = block_source_span(raw_text, block)
    if norm(exact_text) == norm(expected_text or ''):
        return fs_path, normalized, block_start, block_end, block, None
    spans = segment_sentences(exact_text)
    for idx, (s, e, sent_text) in enumerate(spans):
        if norm(sent_text) == norm(expected_text or ''):
            return fs_path, normalized, block_start + s, block_start + e, block, idx
    raise MergeConflict(
        f'block {block_id} text on disk no longer matches the expected before-text — refusing change'
    )


def _rerender_block(route_path, workspace, fs_path, new_src, block_id, old_block):
    """Re-render one block fresh from `new_src` (the file content just
    written). Reconciliation (inside current_page_blocks) carries the
    block's id forward across the text change when possible; falls back to
    matching on the same line_start if the id genuinely changed."""
    _src2, new_blocks, _map2, _report2 = current_page_blocks(route_path, workspace)
    new_block = next((b for b in new_blocks if b['id'] == block_id), None)
    if new_block is None:
        new_block = next((b for b in new_blocks if b.get('line_start') == old_block.get('line_start')), None)
    if new_block is None:
        return {'block_id': block_id, 'html': None}
    ws = get_workspace(workspace)
    resolver = make_link_resolver(fs_path, route_path, workspace)
    lexicon = get_lexicon_index()
    auto_lexicon_page, _ = strip_auto_lexicon_marker(new_src)
    terms_out = {}
    parse_markdown(new_src, link_resolver=resolver, terms_out=terms_out, lexicon=lexicon)
    badges_on = route_path in (ws.get('status_badges') or [])
    html = render_block_html(
        new_block, route_path,
        status_chip=(wq_status_chip(new_block) if badges_on else None),
        link_resolver=resolver, terms=terms_out, lexicon=lexicon,
        auto_lexicon=auto_lexicon_page,
        auto_local_terms=_auto_local_for(new_block, terms_out),
    )
    return {'block_id': new_block['id'], 'html': html}


def apply_sentence_change(route_path, workspace, mark, author_label='claude'):
    """The trunk write for a NEW open change (item B): called once, at the
    moment a type=='edit' mark is created (POST /api/comments — see do_POST),
    for every author alike. Writes `mark['proposed']` into the exact span
    that currently holds `mark['snapshot']` (hash-guarded — MergeConflict if
    the sentence/block has drifted), commits `mdp: change <id> on <page>
    (<author>)`. The mark itself is NOT resolved by this — it stays open
    (del/ins showing) until Settle or Revert."""
    block_id = mark.get('block_id')
    proposed = mark.get('proposed')
    if not block_id:
        raise MergeConflict('mark carries no block_id')
    if proposed is None:
        raise MergeConflict('mark carries no proposed text')
    fs_path, normalized, start, end, block, sentence_index = _locate_change_span(
        route_path, workspace, block_id, mark.get('snapshot')
    )
    new_src = normalized[:start] + proposed + normalized[end:]
    with open(fs_path, 'w', encoding='utf-8') as f:
        f.write(new_src)
    commit_sha = None
    commit_err = None
    repo_root = _git_repo_root(fs_path)
    if repo_root:
        message = f"mdp: change {mark.get('id')} on {route_path} ({author_label})"
        commit_sha, commit_err = _git_commit_file(repo_root, fs_path, message)
    result = _rerender_block(route_path, workspace, fs_path, new_src, block_id, block)
    result['commit'] = commit_sha
    result['commit_error'] = commit_err
    result['sentence_index'] = sentence_index
    return result


def apply_sentence_settle(route_path, workspace, mark):
    """Settle: the trunk already holds the right text (it was written at
    change time) — no file write, no commit, only the mark is resolved. Still
    returns a fresh re-render of the block (cheap — no write involved) so the
    client can clear the del/ins overlay by swapping in server-authoritative
    plain HTML rather than trusting a client-side page-load-time DOM cache
    (which, under this model, never actually held the true "before" text)."""
    fs_path = resolve_page(route_path, workspace)
    with open(fs_path, 'r', encoding='utf-8') as f:
        current_src = f.read()
    _src_bytes, blocks, _mapping, _report = current_page_blocks(route_path, workspace)
    block = next((b for b in blocks if b['id'] == mark.get('block_id')), None)
    result = (_rerender_block(route_path, workspace, fs_path, current_src, mark.get('block_id'), block or {})
              if block else {'block_id': mark.get('block_id'), 'html': None})
    result['commit'] = None
    return result


def apply_sentence_revert(route_path, workspace, mark):
    """Revert: deterministically writes `mark['snapshot']` (before) back into
    the span that currently holds `mark['proposed']` (the open change),
    hash-guarded the same way as a change, commits `mdp: revert <id> on
    <page> (Mike)`, and resolves the mark (`reverted: true` — set by the
    caller, do_POST, alongside status:'done')."""
    block_id = mark.get('block_id')
    before_text = mark.get('snapshot')
    if not block_id:
        raise MergeConflict('mark carries no block_id')
    if before_text is None:
        raise MergeConflict('mark carries no snapshot (before) text to revert to')
    fs_path, normalized, start, end, block, sentence_index = _locate_change_span(
        route_path, workspace, block_id, mark.get('proposed')
    )
    new_src = normalized[:start] + before_text + normalized[end:]
    with open(fs_path, 'w', encoding='utf-8') as f:
        f.write(new_src)
    commit_sha = None
    commit_err = None
    repo_root = _git_repo_root(fs_path)
    if repo_root:
        message = f"mdp: revert {mark.get('id')} on {route_path} (Mike)"
        commit_sha, commit_err = _git_commit_file(repo_root, fs_path, message)
    result = _rerender_block(route_path, workspace, fs_path, new_src, block_id, block)
    result['commit'] = commit_sha
    result['commit_error'] = commit_err
    result['sentence_index'] = sentence_index
    return result


_LEVEL_FRONTMATTER_RE = re.compile(r'^\s*(?:<!--.*?-->\s*)*---\s*\n(.*?)\n---\s*\n', re.S)
_LEVEL_KEY_RE = re.compile(r'^level:\s*(.+?)\s*$', re.M)
_VIEW_KEY_RE = re.compile(r'^view:\s*(.+?)\s*$', re.M)


def compute_default_view(src):
    """Front-matter `view: v3` (task spec item 14, 2026-09-03) forces a page
    to open in the v3 mark layer even with no `?view=` query param — used by
    `mdp-proposal.md` so Mike never lands on classic and has to remember the
    toggle. An explicit `?view=` in the URL always wins over this default;
    see `do_GET`."""
    m = _LEVEL_FRONTMATTER_RE.match(src)
    if m:
        vm = _VIEW_KEY_RE.search(m.group(1))
        if vm and vm.group(1).strip('"\'') == 'v3':
            return 'v3'
    return 'classic'


def compute_level(src, route_path):
    """v3 finding 5 (mdp-as-ui-prior-art.md Part 4): every view carries a level
    label. A page opts in explicitly via a leading YAML-ish front-matter
    `level:` field; absent that, pages living under `SOMA/shared-cognition/`
    default to "meta: the mark layer" (documents ABOUT the review instrument
    itself) and everything else defaults to "object" (a document reviewed BY
    the instrument, not one describing it). Reading front matter here never
    strips it from the source used for block-parsing, so this cannot change a
    single byte of classic rendering."""
    m = _LEVEL_FRONTMATTER_RE.match(src)
    if m:
        km = _LEVEL_KEY_RE.search(m.group(1))
        if km:
            return km.group(1).strip('"\'')
    if route_path.startswith('SOMA/shared-cognition/') or route_path.startswith('soma/shared-cognition/'):
        return 'meta: the mark layer'
    return 'object'


def render_view_toggle(view, route_path, workspace):
    """The one piece of chrome that must appear on BOTH views so Mike can flip
    between them on the same page (spec item 1, 2026-09-03). This is the only
    server-rendered addition to the classic (view=classic, no ?view param)
    page — everything else new about v3 is injected client-side by V3_JS,
    which is only embedded when view == 'v3', so it costs classic zero bytes."""
    classic_active = ' active' if view != 'v3' else ''
    v3_active = ' active' if view == 'v3' else ''
    return f'''<div class="view-toggle" role="group" aria-label="Page view">
    <span class="view-toggle-label">View</span>
    <a class="view-toggle-btn{classic_active}" href="?view=classic" data-view-toggle="classic">Classic</a>
    <a class="view-toggle-btn{v3_active}" href="?view=v3" data-view-toggle="v3">Mark layer</a>
  </div>'''


_TERM_USE_SLUG_RE = re.compile(r'data-term-slug="([a-zA-Z0-9-]+)"')


def _replace_block_body_html(wrapped_html, new_inner_html):
    """Swap a rendered `render_block_html()` wrapper's `.block-body` inner
    content in place, keeping every wrapper attribute (data-block-id, anchor,
    source, comment-box, etc.) untouched — used by the generated Terms view
    (D, below) so the replaced block keeps its real identity for comment/
    edit-mark binding even though its VISIBLE content is server-generated."""
    m = re.search(r'(<div class="block-body[^>]*>)(.*)(</div>\s*<div class="comment-box">)', wrapped_html, re.S)
    if not m:
        return wrapped_html
    return wrapped_html[:m.start()] + m.group(1) + new_inner_html + m.group(3) + wrapped_html[m.end():]


def render_generated_terms_section(blocks, block_htmls, terms_out, lexicon, url_prefix, lexicon_route):
    """v3-only (MDP mechanics, item D, 2026-09-03): a page's `## Terms` section
    renders as a GENERATED view built from the lexicon node store — every
    term-link on the page (page-local `terms_out` table + lexicon auto-links)
    gets one entry with its definition and a "used N times on this page" line
    back-linking to every use. A use's identity is `(block_id,
    occurrence-within-block)` — model-anchored, never a text position, so it
    survives ordinary edits the way every other mark binding does.

    Never touches the `.md` file: only THIS render's HTML for the Terms
    section's body block(s) is replaced (`_replace_block_body_html` keeps the
    wrapper so comment/edit-mark binding on that block id is unaffected). If a
    hand-written Terms section exists, its own entries came from `terms_out`
    already (existing `extract_terms()` behavior — page-local overrides the
    lexicon) and are folded into this same generated view rather than lost.
    A page with no `## Terms` heading, or an empty one, renders unchanged
    (`blocks`/`block_htmls` returned as-is).
    """
    terms_idx = next(
        (i for i, b in enumerate(blocks)
         if b['kind'] == 'heading' and b['text'].strip().lower() == 'terms'),
        None,
    )
    if terms_idx is None:
        return
    heading_level = blocks[terms_idx]['level']
    end_idx = len(blocks)
    for j in range(terms_idx + 1, len(blocks)):
        if blocks[j]['kind'] == 'heading' and blocks[j]['level'] <= heading_level:
            end_idx = j
            break
    body_idxs = list(range(terms_idx + 1, end_idx))
    if not body_idxs:
        return

    # Every term-link use ELSEWHERE on the page, in document order, tagged
    # with (block_id, occurrence-index-within-that-block) — never counts the
    # Terms section's own definitions as a "use" of themselves.
    uses = {}
    for idx, block in enumerate(blocks):
        if idx == terms_idx or idx in body_idxs:
            continue
        counts = {}
        for slug in _TERM_USE_SLUG_RE.findall(block_htmls[idx]):
            occ = counts.get(slug, 0)
            uses.setdefault(slug, []).append((block['id'], occ))
            counts[slug] = occ + 1

    all_slugs = sorted(set(terms_out.keys()) | set(uses.keys()))
    if not all_slugs:
        return

    entries = []
    for slug in all_slugs:
        local = terms_out.get(slug)
        if local:
            term_name, def_html = local['term'], local['html']
        else:
            lex_slug = slug[len('lex-'):] if slug.startswith('lex-') else slug
            lex_entry = (lexicon or {}).get('by_slug', {}).get(lex_slug)
            if not lex_entry:
                continue
            term_name = lex_entry['term']
            def_html = (lexicon_entry_popover_html(lex_entry, url_prefix, lexicon_route) if lexicon_route
                        else f"<p>{_esc_html(lex_entry['gloss'])}</p>")
        term_uses = uses.get(slug, [])
        use_links = ' '.join(
            f'<a href="#" class="v3-term-use-link" data-block-id="{_html_attr_escape(bid)}" '
            f'data-occurrence="{occ}">[{k + 1}]</a>'
            for k, (bid, occ) in enumerate(term_uses)
        )
        n = len(term_uses)
        uses_line = (
            f'<div class="v3-term-uses">Used {n} time{"s" if n != 1 else ""} on this page: {use_links}</div>'
            if n else '<div class="v3-term-uses">Not used elsewhere on this page.</div>'
        )
        entries.append(
            f'<div class="v3-term-entry" id="term-{_html_attr_escape(slug)}">'
            f'<h4>{_html_attr_escape(term_name)}</h4>{def_html}{uses_line}</div>'
        )
    generated_html = '<div class="v3-terms-generated">' + ''.join(entries) + '</div>'

    first = body_idxs[0]
    block_htmls[first] = _replace_block_body_html(block_htmls[first], generated_html)
    for j in body_idxs[1:]:
        block_htmls[j] = _replace_block_body_html(
            block_htmls[j], '<!-- absorbed into generated Terms view above -->'
        )


def _esc_html(s):
    return (s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def render_page(route_path, workspace=DEFAULT_WORKSPACE, view='classic'):
    ws = get_workspace(workspace)
    url_prefix = workspace_url_prefix(workspace)
    fs_path = resolve_page(route_path, workspace)
    with open(fs_path, 'rb') as f:
        src_bytes = f.read()
    src = src_bytes.decode('utf-8')
    # `view=None` means "no explicit ?view= on the URL" — fall back to the
    # page's own front-matter default (compute_default_view), else classic.
    # An explicit view (including `?view=classic` overriding a v3-default
    # page) always wins.
    if view is None:
        view = compute_default_view(src)
    view = 'v3' if view == 'v3' else 'classic'
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
    block_htmls = [
        render_block_html(
            b, route_path,
            status_chip=(wq_status_chip(b) if badges_on else None),
            link_resolver=resolver,
            terms=terms_out,
            lexicon=lexicon,
            auto_lexicon=auto_lexicon_page,
            auto_local_terms=_auto_local_for(b, terms_out),
        )
        for b in blocks]
    if view == 'v3':
        lexicon_route = fs_path_to_route(LEXICON_MD_PATH, workspace) if os.path.isfile(LEXICON_MD_PATH) else None
        render_generated_terms_section(blocks, block_htmls, terms_out, lexicon, url_prefix, lexicon_route)
    blocks_html = '\n'.join(block_htmls)

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
    mark_layer = bool(ws.get('mark_layer', True)) and not bool(tour_body) and view != 'v3'
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

    level_label = compute_level(src, route_path) if view == 'v3' else None

    # Agreed model 12b: the ringer list is part of closing a round, so it is
    # rendered into the page itself rather than left to a client script or to
    # the writer's memory. Failure to build it never breaks the page — a page
    # that cannot show its ringer list says so is better than one that hides
    # the failure by showing nothing.
    ringer_section = ''
    if view == 'v3':
        try:
            ringer_section = render_ringer_section(compute_ringer_list(route_path, workspace))
        except Exception as exc:  # noqa: BLE001 - never 500 a page over the ringer list
            ringer_section = ('<section class="ringer-list" id="ringer-list">'
                              '<h2>Ringer list unavailable</h2><p class="ringer-why">'
                              f'Could not be generated: {_html.escape(str(exc))}. '
                              'Treat this round as unclosed.</p></section>')

    html_doc = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html.escape(title)} — soma-review</title>
{mark_fonts}
<style>{PAGE_CSS}{MARK_LAYER_CSS if mark_layer else ''}{VIEW_TOGGLE_CSS}{V3_CSS if view == 'v3' else ''}{RINGER_CSS if view == 'v3' else ''}</style>
{chip_head}{tour_head}
</head>
<body{body_class}>
<nav class="sidebar">
  <a href="{url_prefix}/page/{ws['home']}" style="font-weight:700;font-size:15px;color:#e6e6e6;">soma-review</a>
  {render_view_toggle(view, route_path, workspace)}
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
  {ringer_section}
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
window.__TERM_DEFS__ = {term_defs_json};
window.__V3_VIEW__ = {json.dumps(view == 'v3')}; window.__LEVEL_LABEL__ = {json.dumps(level_label)};</script>
<script>{PAGE_JS}</script>
{f'<script>{MARK_LAYER_JS}</script>' if mark_layer else ''}
<script>{VIEW_TOGGLE_JS}</script>
{f'<script>{V3_JS}</script>' if view == 'v3' else ''}
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


# --- "Waiting on you" inbox (2026-09-04, COO run) --------------------------
#
# The gap this closes: every marked document lived at its own URL and Mike had
# to be told that URL by whoever wrote it. Docs he had already ruled on and docs
# still holding an unanswered ask looked identical from outside. This route is
# the one address that answers "what is waiting for me" across every workspace.
#
# It is derived, never authored: it reads the same comment sidecars the review
# surface writes (`<feedback_dir>/*.jsonl`), so it cannot drift from the docs.
# A row's true route comes from each record's own `page` field — `page_slug()`
# is lossy (slashes become underscores) and is never reversed here.
#
# Counting rules (deliberate, and the reason the two columns are separate):
#   waiting on you  = open rows NOT authored by mike   -> asks he has not answered
#   waiting on Dee  = open rows authored by mike       -> rulings not yet acted on
#   open            = status not 'done' and not deleted
# `reader-signal` rows are bookkeeping (done / gave-up), never an ask; they are
# excluded from both counts and surfaced as a chip instead.

_WAITING_TERMINAL_STATUS = ('done',)
_WAITING_MIKE_AUTHORS = ('mike', 'mw', 'mike-wolf')


def _waiting_is_open(row):
    if row.get('deleted'):
        return False
    if row.get('mark_kind') == 'reader-signal':
        return False
    return (row.get('status') or 'queued') not in _WAITING_TERMINAL_STATUS


def _waiting_by_mike(row):
    return (row.get('author') or '').strip().lower() in _WAITING_MIKE_AUTHORS


def _waiting_doc_title(route, workspace):
    """First markdown H1 of the doc, else its filename. Best-effort: a doc that
    has moved or been deleted still gets a row (its marks are still real)."""
    try:
        fs_path = resolve_page(route, workspace)
    except Exception:
        return os.path.basename(route), False
    try:
        with open(fs_path, 'r', encoding='utf-8') as f:
            for _ in range(80):
                line = f.readline()
                if not line:
                    break
                if line.startswith('# '):
                    return line[2:].strip(), True
    except OSError:
        return os.path.basename(route), False
    return os.path.basename(route), True


def collect_waiting():
    """One row per document that has any sidecar, across every workspace."""
    rows = []
    for slug in load_workspaces():
        try:
            ws = get_workspace(slug)
        except NotFoundError:
            continue
        feedback_dir = ws['feedback_dir']
        if not os.path.isdir(feedback_dir):
            continue
        for name in sorted(os.listdir(feedback_dir)):
            if not name.endswith('.jsonl'):
                continue
            records = _read_comments_unlocked(os.path.join(feedback_dir, name))
            if not records:
                continue
            route = ''
            for rec in records:
                if rec.get('page'):
                    route = rec['page']
                    break
            if not route:
                continue
            on_mike = [r for r in records if _waiting_is_open(r) and not _waiting_by_mike(r)]
            on_dee = [r for r in records if _waiting_is_open(r) and _waiting_by_mike(r)]
            signals = [r for r in records
                       if r.get('mark_kind') == 'reader-signal' and not r.get('deleted')]
            signals.sort(key=lambda r: r.get('timestamp') or '')
            live = [r for r in records if not r.get('deleted')]
            title, exists = _waiting_doc_title(route, slug)
            first_open = None
            for r in sorted(on_mike, key=lambda r: r.get('timestamp') or ''):
                if r.get('block_id'):
                    first_open = r['block_id']
                    break
            rows.append({
                'workspace': slug,
                'workspace_label': ws['label'],
                'route': route,
                'title': title,
                'exists': exists,
                'on_mike': len(on_mike),
                'on_dee': len(on_dee),
                'total': len(live),
                'last_ts': max((r.get('timestamp') or '' for r in live), default=''),
                'signal': (signals[-1].get('signal') if signals else ''),
                'first_open_block': first_open,
                'kinds': sorted({(r.get('mark_kind') or r.get('type') or 'comment')
                                 for r in on_mike}),
            })
    rows.sort(key=lambda r: (r['on_mike'] == 0, r['on_mike'] == 0 and r['on_dee'] == 0,
                             _waiting_sort_ts(r)), reverse=False)
    return rows


def _waiting_sort_ts(row):
    """Newest first inside each band — negate by string trick: sort ascending on
    the inverted timestamp so the outer tuple can stay a plain ascending sort."""
    ts = row['last_ts'] or ''
    return ''.join(chr(0x10FFFD - ord(c)) if ord(c) < 0x10FFFD else c for c in ts)


def _waiting_is_stale(ts, days=14):
    """An ask nobody has touched in two weeks is reported as stale rather than
    silently counted alongside tonight's work — a stack of dead July asks at the
    top of the inbox is exactly the noise this page exists to remove."""
    if not ts:
        return True
    try:
        then = datetime.datetime.strptime(ts, '%Y-%m-%dT%H:%M:%SZ').replace(
            tzinfo=datetime.timezone.utc)
    except ValueError:
        return False
    return (datetime.datetime.now(datetime.timezone.utc) - then).days >= days


def _waiting_age(ts):
    if not ts:
        return '—'
    try:
        then = datetime.datetime.strptime(ts, '%Y-%m-%dT%H:%M:%SZ').replace(
            tzinfo=datetime.timezone.utc)
    except ValueError:
        return ts
    delta = datetime.datetime.now(datetime.timezone.utc) - then
    mins = int(delta.total_seconds() // 60)
    if mins < 60:
        return f'{max(mins, 0)}m ago'
    if mins < 60 * 48:
        return f'{mins // 60}h ago'
    return f'{mins // (60 * 24)}d ago'


WAITING_CSS = """
.waiting-wrap{max-width:900px;}
.waiting-wrap h1{font-size:22px;margin:0 0 4px;}
.waiting-sub{color:#8a8f98;font-size:13px;margin:0 0 20px;}
.waiting-band{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#8a8f98;
  margin:26px 0 8px;border-top:1px solid #23262d;padding-top:12px;}
.waiting-row{display:block;padding:12px 14px;margin:6px 0;border:1px solid #23262d;
  border-radius:9px;background:#1a1c22;text-decoration:none;color:inherit;}
.waiting-row:hover{border-color:#2f6feb;}
.waiting-row.is-ask{border-left:3px solid #e0b463;}
.waiting-top{display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
.waiting-title{font-weight:600;color:#e6e6e6;font-size:14px;flex:1 1 auto;}
.waiting-ws{font-size:10px;text-transform:uppercase;letter-spacing:.05em;padding:2px 7px;
  border-radius:10px;background:#1c2a3a;color:#63a0e0;border:1px solid #264a5a;}
.waiting-count{font-size:11px;padding:2px 8px;border-radius:10px;}
.waiting-count.mike{background:#3a2f1c;color:#e0b463;}
.waiting-count.dee{background:#1c2a3a;color:#63a0e0;}
.waiting-count.signal{background:#1c3a24;color:#63e089;}
.waiting-count.missing{background:#3a1c1c;color:#e06363;}
.waiting-meta{margin-top:5px;font-size:11px;color:#8a8f98;}
.waiting-empty{color:#8a8f98;font-size:13px;padding:14px 0;}
"""


def render_waiting(workspace=DEFAULT_WORKSPACE):
    try:
        ws = get_workspace(workspace)
    except NotFoundError:
        workspace = DEFAULT_WORKSPACE
        ws = get_workspace(workspace)
    rows = collect_waiting()
    asks = [r for r in rows if r['on_mike']]
    replies = [r for r in rows if not r['on_mike'] and r['on_dee']]
    settled = [r for r in rows if not r['on_mike'] and not r['on_dee']]

    def row_html(r):
        prefix = workspace_url_prefix(r['workspace'])
        frag = f"#b:{r['first_open_block']}" if r['first_open_block'] else ''
        href = f"{prefix}/page/{r['route']}?view=v3{frag}"
        chips = []
        if r['on_mike']:
            chips.append(f'<span class="waiting-count mike">{r["on_mike"]} for you</span>')
        if r['on_dee']:
            chips.append(f'<span class="waiting-count dee">{r["on_dee"]} for Dee</span>')
        if r['signal']:
            chips.append(f'<span class="waiting-count signal">{_html.escape(r["signal"])}</span>')
        if not r['exists']:
            chips.append('<span class="waiting-count missing">doc missing</span>')
        if r['on_mike'] and _waiting_is_stale(r['last_ts']):
            chips.append('<span class="waiting-count missing">stale</span>')
        kinds = ', '.join(r['kinds']) if r['kinds'] else ''
        meta = f"{_html.escape(r['route'])} · last activity {_waiting_age(r['last_ts'])}"
        if kinds:
            meta += f" · {_html.escape(kinds)}"
        return (f'<a class="waiting-row{" is-ask" if r["on_mike"] else ""}" href="{href}">'
                f'<div class="waiting-top">'
                f'<span class="waiting-title">{_html.escape(r["title"])}</span>'
                f'<span class="waiting-ws">{_html.escape(r["workspace_label"])}</span>'
                f'{"".join(chips)}</div>'
                f'<div class="waiting-meta">{meta}</div></a>')

    def band(label, items, empty):
        body = '\n'.join(row_html(r) for r in items) if items \
            else f'<div class="waiting-empty">{empty}</div>'
        return f'<div class="waiting-band">{label}</div>{body}'

    url_prefix = workspace_url_prefix(workspace)
    fresh_asks = [r for r in asks if not _waiting_is_stale(r['last_ts'])]
    total_asks = sum(r['on_mike'] for r in fresh_asks)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Waiting on you — soma-review</title>
<style>{PAGE_CSS}{WAITING_CSS}</style></head>
<body>
<nav class="sidebar">
  <a href="{url_prefix}/waiting" style="font-weight:700;font-size:15px;color:#e6e6e6;">soma-review</a>
  {render_workspace_switcher(workspace)}
  {render_sidebar('', workspace)}
</nav>
<main class="main">
  <div class="waiting-wrap">
    <h1>Waiting on you</h1>
    <p class="waiting-sub">{total_asks} open item(s) across {len(fresh_asks)} document(s) are addressed to
    you, plus {sum(r['on_mike'] for r in asks) - total_asks} on documents nobody has touched in two weeks. Derived live from the review sidecars — nothing here is hand-maintained.
    Each row opens the document in the mark view at the first open item.</p>
    {band('Needs your ruling', asks, 'Nothing is waiting on you.')}
    {band('You ruled — waiting on Dee', replies, 'Nothing outstanding on Dee.')}
    {band('Settled', settled, 'No settled documents yet.')}
  </div>
</main>
</body></html>"""


# --- Ringer list (agreed model 12b, 2026-09-04, COO run) --------------------
#
# Fork Q1 closed as Alternative B (Mike, 2026-09-03): a bracket of assent
# covers everything between two of the reader's own marks, revisions included.
# A revision inside that bracket that the reader never looked at individually
# therefore reaches the trunk WITH his assent and WITHOUT his attention.
# 12b is the counterweight: every round closes with a machine-generated list
# naming each such revision back to him, so the list can never be empty by
# omission — which is exactly the failure a writer-composed list produces
# (the writer omits the revision it is most invested in).
#
# Derived, never authored. Inputs are the same sidecar rows the review surface
# already writes plus the durable block map for document order. Two sources
# feed one list:
#   swallowed  = machine half. A revision (type 'edit', incl. replace) by an
#                author other than the reader, whose block lies inside the
#                reader's bracket, that the reader never marked, settled or
#                reverted himself.
#   flagged    = writer half. Any row carrying `ringer: true` — a sentence the
#                writer believes he should NOT have agreed with. Same list, so
#                one section answers "what did my bracket swallow".
#
# The bracket is deliberately generous (it counts a change against the writer
# whenever there is doubt): its lower edge is the top of the document once any
# reader signal exists, and its upper edge is the deepest of the reader's own
# marks and his last-read block. A revision below the upper edge was inside
# the span he consented to.

RINGER_READER = 'mike'


def _ringer_is_reader(row, reader=RINGER_READER):
    author = (row.get('author') or '').strip().lower()
    if reader == RINGER_READER:
        return author in _WAITING_MIKE_AUTHORS
    return author == reader.strip().lower()


def _ringer_is_revision(row):
    """A revision is a change to the reader's text: an edit mark (the `replace`
    flag is a panel label on the same record, not a different kind). Reader
    signals and plain comments are not revisions."""
    return row.get('type') == 'edit' and not row.get('deleted')


def compute_ringer_list(route_path, workspace=DEFAULT_WORKSPACE, reader=RINGER_READER):
    """The machine half of agreed-model 12b. Returns
    {reader, bracket:{lower,upper,lower_block,upper_block,basis}, ringers:[...],
     swallowed, flagged, revisions, marked_blocks}."""
    all_rows = read_comments(route_path, workspace)
    rows = [c for c in all_rows if not c.get('deleted')]
    # A soft-deleted edit row is NOT a soft-deleted change: apply_sentence_change
    # wrote and committed at create time, and the delete path does not revert.
    # Deleting the row therefore removes the change from every list while
    # leaving it in the trunk — the exact silent-swallow this section exists to
    # prevent — so a deleted edit that carries a commit is listed as withdrawn.
    withdrawn = [c for c in all_rows
                 if c.get('deleted') and c.get('type') == 'edit' and c.get('commit')]
    _src, blocks, _mapping, _report = current_page_blocks(route_path, workspace)
    order = {b['id']: i for i, b in enumerate(blocks)}
    text_of = {b['id']: blockmap.norm(b.get('text') or '') for b in blocks}

    signals = [r for r in rows
               if r.get('type') == 'mark' and r.get('mark_kind') == 'reader-signal'
               and _ringer_is_reader(r, reader)]
    latest_signal = None
    if signals:
        latest_signal = max(enumerate(signals),
                            key=lambda p: (p[1].get('timestamp') or '', p[0]))[1]

    # Blocks the reader touched explicitly. A reader-signal is bookkeeping, not
    # attention on a block, so it never counts as an explicit mark.
    # {position: latest timestamp the reader marked it}. The time dimension is
    # load-bearing: a mark at T1 is not attention on a revision made at T2.
    # Without it, one agree in round one suppresses every later revision to
    # that block, in every round, forever.
    # Keys are (timestamp, append index): sidecar timestamps are
    # second-resolution, so a mark and a revision made in the same second are
    # ordered by the order they were written, the same tie-break
    # `/api/read-state` already uses for two signals in one second.
    seq = {r.get('id'): i for i, r in enumerate(rows)}
    marked = {}
    reached = set()
    answered_threads = set()
    for r in rows:
        if not _ringer_is_reader(r, reader):
            continue
        if r.get('mark_kind') == 'reader-signal':
            continue
        pos = order.get(r.get('block_id'))
        is_reply = bool(r.get('thread_id') and r.get('thread_id') != r.get('id'))
        if is_reply:
            # A reply is re-bound to its thread root's block, so it is attention
            # on THAT thread, not on the block generally: answering a decision
            # card says nothing about a separate revision to the same sentence.
            # It still proves he reached the block, so it moves the bracket edge.
            answered_threads.add(r.get('thread_id'))
            if pos is not None:
                reached.add(pos)
            continue
        if pos is not None:
            reached.add(pos)
            key = (r.get('timestamp') or '', seq.get(r.get('id'), 0))
            if key > marked.get(pos, ('', -1)):
                marked[pos] = key
    # Settling or reverting a revision is attention on that revision even when
    # it leaves no row of its own on the block.
    reader_names = (_WAITING_MIKE_AUTHORS if reader == RINGER_READER
                    else (reader.strip().lower(),))
    resolved_by_reader = {
        r.get('id') for r in rows
        if _ringer_is_revision(r) and (r.get('settled') or r.get('reverted'))
        and (r.get('resolved_by') or '').strip().lower() in reader_names
    }

    upper = max(reached) if reached else -1
    basis = 'marks'
    if latest_signal is not None:
        sig_pos = order.get(latest_signal.get('last_read_block'))
        if sig_pos is not None and sig_pos > upper:
            upper = sig_pos
            basis = 'reader-signal'
    # The lower edge is always the top of the document, not the reader's first
    # mark: reading is top-down, so reaching a mark at block N means passing
    # every block above it. This over-reports rather than under-reports, which
    # is the correct bias for a list whose whole job is to catch what slipped
    # through — a ringer he already knew about costs him a glance, one that was
    # never named costs him the ruling.
    lower = 0
    if not reached and latest_signal is None:
        # Nothing read, nothing marked: there is no bracket, so nothing was
        # swallowed. An empty list here is a true empty, not an omission.
        upper = -1

    def _block_label(pos):
        if pos is None or pos < 0 or pos >= len(blocks):
            return None
        return blocks[pos]['id']

    ringers = []
    for r in rows:
        flagged = bool(r.get('ringer'))
        swallowed = False
        pos = order.get(r.get('block_id'))
        if (_ringer_is_revision(r) and not _ringer_is_reader(r, reader)
                and upper >= 0 and r.get('id') not in resolved_by_reader
                and r.get('id') not in answered_threads
                # A revision whose block no longer exists (pos is None) is
                # listed too: it changed text the reader was reading and now
                # cannot be found on the page, which is the least reviewable
                # state a change can be in, not a reason to drop it.
                and (pos is None or (lower <= pos <= upper
                                     and marked.get(pos, ('', -1))
                                     < ((r.get('timestamp') or ''), seq.get(r.get('id'), 0))))):
            swallowed = True
        if not (flagged or swallowed):
            continue
        ringers.append({
            'id': r.get('id'),
            'block_id': r.get('block_id'),
            'position': pos,
            'author': r.get('author'),
            'timestamp': r.get('timestamp'),
            'why': 'flagged' if flagged else 'swallowed',
            'reason': r.get('ringer_reason') or r.get('reason') or '',
            'before': r.get('snapshot') or '',
            'after': r.get('proposed') or '',
            'replace': bool(r.get('replace')),
            'status': r.get('status') or 'queued',
            'block_text': text_of.get(r.get('block_id'), ''),
        })
    for r in withdrawn:
        pos = order.get(r.get('block_id'))
        ringers.append({
            'id': r.get('id'), 'block_id': r.get('block_id'), 'position': pos,
            'author': r.get('author'), 'timestamp': r.get('timestamp'),
            'why': 'withdrawn',
            'reason': 'The sidecar row was deleted but the change was already committed to the '
                      'trunk, so it left every list while staying in the document.',
            'before': r.get('snapshot') or '', 'after': r.get('proposed') or '',
            'replace': bool(r.get('replace')), 'status': r.get('status') or 'queued',
            'block_text': text_of.get(r.get('block_id'), ''),
        })
    # The sidecar half is complete only if every change came through the review
    # surface. The trunk gap is the second witness for the ones that did not.
    try:
        gap = compute_trunk_gap(route_path, workspace, reader, rows=rows, all_rows=all_rows)
    except Exception as exc:  # noqa: BLE001 - a missing witness is not a clean round
        gap = {'status': 'unavailable', 'reason': str(exc), 'since': None, 'basis': 'none',
               'commits': [], 'unattributed': 0, 'accounted': 0, 'by_reader': 0}
    for c in gap['commits']:
        ringers.append({
            'id': c['sha'] or 'worktree',
            'block_id': None, 'position': None,
            'author': c['author'], 'timestamp': (c['date'] or '')[:19],
            'why': 'unattributed',
            # A stranded surface write IS claimed by a row — the commit failed,
            # that is all. Saying "no row claims it" there would be false, and
            # would send the reader hunting for an author who is on the page.
            'reason': (f"{c['subject']} ({'working tree' if c['uncommitted'] else c['sha']}) — "
                       + ("this changed the document and the trunk keeps no record of it, so it "
                          "cannot be checked against history."
                          if c.get('surface_write') else
                          "this changed the document and no row on this surface claims it, so no "
                          "other list on this page can see it.")
                       + (" Committed under your own git identity, which proves nothing here: "
                          "this machine commits agent work under your name too."
                          if c.get('as_reader') else "")),
            'before': c['before'], 'after': c['after'],
            'replace': False, 'status': 'in-trunk', 'block_text': '',
        })
    ringers.sort(key=lambda x: (x['position'] if x['position'] is not None else 10**6,
                                x['timestamp'] or ''))
    listed_ids = {x['id'] for x in ringers}
    revisions = sum(1 for r in rows if _ringer_is_revision(r) and not _ringer_is_reader(r, reader))
    # Rewriting a block's text can move it, so a revision the reader really did
    # pass can end up below the bracket edge on the CURRENT page and drop out
    # of the machine half. Counted and shown rather than smoothed away: the
    # writer-flagged half is the backstop for exactly this case.
    outside = sum(1 for r in rows
                  if _ringer_is_revision(r) and not _ringer_is_reader(r, reader)
                  and r.get('id') not in listed_ids
                  and (order.get(r.get('block_id')) or 0) > upper >= 0)
    return {
        'page': route_path,
        'workspace': workspace,
        'reader': reader,
        'bracket': {
            'lower': lower if upper >= 0 else None,
            'upper': upper if upper >= 0 else None,
            'lower_block': _block_label(lower) if upper >= 0 else None,
            'upper_block': _block_label(upper) if upper >= 0 else None,
            'basis': basis if upper >= 0 else 'none',
            'blocks_total': len(blocks),
        },
        'ringers': ringers,
        'trunk_gap': gap,
        'unattributed': sum(1 for x in ringers if x['why'] == 'unattributed'),
        'swallowed': sum(1 for x in ringers if x['why'] == 'swallowed'),
        'flagged': sum(1 for x in ringers if x['why'] == 'flagged'),
        'withdrawn': sum(1 for x in ringers if x['why'] == 'withdrawn'),
        'revisions': revisions,
        'outside_bracket': outside,
        'marked_blocks': len(marked),
    }


# --- The trunk gap ---------------------------------------------------------
#
# The machine half above is complete only for changes that left a sidecar row.
# A writer who edits the Markdown file directly — an agent with the Edit tool,
# a `sed` in a dispatched job, a hand fix in an editor — changes the reader's
# text with no row at all, so `compute_ringer_list` cannot see it and the list
# comes back clean. That is the same silent swallow 12b exists to forbid, one
# layer down: the list was complete *if the writer behaved*.
#
# The trunk itself is the second witness. Every sidecar change that lands is
# committed by `_git_commit_file` and records its sha on the row, so the set of
# commits touching the document minus the set of shas the sidecar claims is
# exactly the set of changes made behind the review surface's back. The
# working tree adds one more case: an edit not yet committed at all.
#
# Bias, as everywhere in 12b: over-report. Being shown a change the reader
# already knew about costs him a glance; one that was never named costs him
# the ruling.

_TRUNK_GAP_MAX_COMMITS = 50


def _git_out(repo_root, args, timeout=10):
    try:
        out = subprocess.run(['git', '-C', repo_root] + args,
                             capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if out.returncode != 0:
        return None, (out.stderr or '').strip()
    return out.stdout, None


def _trunk_diff_texts(patch):
    """Collapse a unified diff into (before, after) word streams, so the row can
    show the change itself through the same del/ins renderer the sidecar rows
    use rather than a description of it."""
    before, after = [], []
    for line in (patch or '').splitlines():
        # Header lines are `--- a/x` / `+++ b/x` — WITH a trailing space. Matching
        # on the bare prefix also eats a deleted `---` front-matter fence or
        # horizontal rule, which is exactly the kind of edit that must be seen.
        if line.startswith('--- ') or line.startswith('+++ ') or line in ('---', '+++'):
            continue
        if line.startswith('diff --git') or line.startswith('@@') or line.startswith('index '):
            continue
        if line.startswith('-'):
            before.append(line[1:].strip())
        elif line.startswith('+'):
            after.append(line[1:].strip())
    return (' '.join(x for x in before if x)[:800],
            ' '.join(x for x in after if x)[:800])


_TRUNK_READER_EMAILS = ('mw@mike-wolf.com', 'mike@embeddedsystemsresearch.org')


def _trunk_author_is_reader(name, email, reader=RINGER_READER):
    """Does this commit CLAIM to be the reader's own hand?

    Only a claim, never a verdict: this laptop's `user.name` is Mike Wolf, so
    every agent that commits here commits under his name. Excluding commits by
    "the reader" would therefore suppress almost exactly the set of changes
    this list exists to name — checked against the real
    `soma/shared-cognition/mdp-agreed-model.md`, where 7 of 7 unattributed
    commits carried his identity and none were typed by him. So the flag is
    carried onto the row as a caveat and excludes nothing.
    """
    name = (name or '').strip().lower()
    email = (email or '').strip().lower()
    if reader == RINGER_READER:
        return email in _TRUNK_READER_EMAILS or name in ('mike wolf', 'mike', 'mikewolf')
    return reader.strip().lower() in f'{name} {email}'


def _trunk_residue(added_text, owner_rows, floor=3):
    """What a commit added beyond what its claiming sidecar rows proposed.

    Word-level, order-insensitive and deliberately crude: the question is only
    whether the commit carried text nobody recorded, not what that text means.
    Words the rows proposed are struck out one occurrence at a time; what is
    left is text that entered the document with no row behind it."""
    proposed = []
    for r in owner_rows:
        proposed.extend(blockmap.norm(r.get('proposed') or '').split())
        proposed.extend(blockmap.norm(r.get('snapshot') or '').split())
    pool = {}
    for w in proposed:
        pool[w] = pool.get(w, 0) + 1
    left = []
    for w in blockmap.norm(added_text or '').split():
        if pool.get(w):
            pool[w] -= 1
        else:
            left.append(w)
    # A word or two of residue is diff noise (a moved comma, a re-wrapped line).
    # Three or more is a sentence someone wrote. `floor` drops to 1 on the
    # DELETED side, where the noise argument does not hold: a two-word deletion
    # nobody proposed ("Charlie three.") is a removal from the reader's document,
    # and swallowing it is how a deletion inherits the "this was us" label.
    return ' '.join(left) if len(left) >= floor else ''


def _git_since(stamp):
    """Sidecar stamps are `%Y-%m-%dT%H:%M:%SZ`. Git's approxidate parser does
    not reliably honour a trailing `Z` and will read it as local time, which
    silently shifts the window by the machine's UTC offset — four hours on this
    laptop, i.e. a whole round of changes going unreported. Hand it an explicit
    `+0000` stamp, one second early so a commit made in the same second as the
    round's opening mark falls inside the window."""
    try:
        t = time.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ')
        # timegm, not mktime: mktime reads the struct as local time, and
        # `time.timezone` does not carry DST, so the naive correction is an
        # hour out for half the year.
        return time.strftime('%Y-%m-%d %H:%M:%S +0000',
                             time.gmtime(calendar.timegm(t) - 1))
    except (TypeError, ValueError):
        return None


# One v3 render costs up to 54 git subprocesses and ~0.4s of trunk-gap work, and
# a reader refreshing a page changes none of its inputs. Key the answer on
# everything that can change it — repo HEAD, the working tree's own state, the
# sidecar's mtime and size, and the document's — so a stale entry is only
# possible if git, the sidecar and the file all report unchanged.
_TRUNK_GAP_CACHE = {}
_TRUNK_GAP_CACHE_MAX = 64


def _stat_key(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def compute_trunk_gap(route_path, workspace=DEFAULT_WORKSPACE, reader=RINGER_READER,
                      rows=None, all_rows=None):
    """Changes to the trunk in this round that no sidecar row accounts for.

    Returns {status, reason, since, basis, commits:[...], unattributed,
    accounted, by_reader}. `status` is 'ok', 'no-round' (no round to report on
    yet), 'untracked' (the document is not in a git repo, so the trunk keeps no
    history and this check cannot run), or 'unavailable' (git failed)."""
    empty = {'status': 'no-round', 'reason': '', 'since': None, 'basis': 'none',
             'commits': [], 'unattributed': 0, 'accounted': 0, 'by_reader': 0}
    if all_rows is None:
        all_rows = read_comments(route_path, workspace)
    if rows is None:
        rows = [c for c in all_rows if not c.get('deleted')]

    # The window is this round: from the close of the previous round (the
    # reader's second-most-recent signal) to now. With only one signal or none,
    # the round runs from the first sidecar row on the page — the earliest
    # moment anyone was reviewing this document at all.
    # Only a `done` signal closes a round. `gave-up` is mid-round by design
    # (fork Q2: the click records the block that lost him), so counting it as a
    # round boundary would shrink the window to the part of the round AFTER he
    # gave up and call everything before it checked.
    signals = [r for r in rows
               if r.get('type') == 'mark' and r.get('mark_kind') == 'reader-signal'
               and r.get('signal') == 'done' and _ringer_is_reader(r, reader)]
    signals.sort(key=lambda r: (r.get('timestamp') or ''))
    stamps = [r.get('timestamp') for r in all_rows if r.get('timestamp')]
    if len(signals) >= 2:
        since, basis = signals[-2].get('timestamp'), 'the close of your previous round'
    elif stamps:
        since, basis = min(stamps), 'the first mark on this page'
    else:
        return empty

    try:
        fs_path = resolve_page(route_path, workspace)
    except Exception as exc:  # noqa: BLE001
        return dict(empty, status='unavailable', reason=str(exc))
    repo_root = _git_repo_root(fs_path)
    if not repo_root:
        return dict(empty, status='untracked', since=since, basis=basis,
                    reason='the document is not inside a git repository, so the trunk keeps '
                           'no history to compare the sidecar against')
    rel = os.path.relpath(os.path.realpath(fs_path), os.path.realpath(repo_root))

    head, _head_err = _git_out(repo_root, ['rev-parse', 'HEAD'])
    # `rows`/`all_rows` are parameters, so the sidecar's mtime is not the whole
    # input — a caller that hands in a filtered or synthesized row set computes
    # a different answer. Digest what the answer actually reads off the rows.
    rows_digest = hashlib.sha256('\x1f'.join(
        f"{r.get('commit')}|{r.get('revert_commit')}|{r.get('commit_error')}|"
        f"{r.get('timestamp')}|{r.get('proposed')}|{r.get('snapshot')}|{r.get('author')}"
        for r in all_rows).encode('utf-8')).hexdigest()
    cache_key = (repo_root, rel, route_path, workspace, reader, since,
                 (head or '').strip(), rows_digest, _stat_key(fs_path),
                 _stat_key(sidecar_path(route_path, workspace)))
    # A HEAD that could not be read is a key that does not move when HEAD does.
    # Never serve or store an answer under it.
    cacheable = bool((head or '').strip())
    hit = _TRUNK_GAP_CACHE.get(cache_key) if cacheable else None
    if hit is not None:
        return copy.deepcopy(hit)

    since_arg = _git_since(since)
    if since_arg is None:
        # Guessing here re-introduces the exact four-hour window shift
        # `_git_since` exists to prevent, and it would do it silently.
        return dict(empty, status='unavailable', since=since, basis=basis,
                    reason=f'the round stamp {since!r} is not in the sidecar format, so the '
                           f'window cannot be computed without guessing a timezone')

    # An untracked document has no history to compare against, and `git status`
    # reports `??` for the whole file — so a direct edit to it is invisible to
    # both halves. That is an unverifiable trunk, not a clean one.
    status_out, status_err = _git_out(repo_root, ['--literal-pathspecs', 'status',
                                                  '--porcelain', '--', rel])
    if status_out is None:
        return dict(empty, status='unavailable', since=since, basis=basis,
                    reason=f'git status failed ({status_err or "no reason given"})')
    if status_out.lstrip().startswith('??'):
        return dict(empty, status='untracked', since=since, basis=basis,
                    reason='the document is not tracked by git, so the trunk keeps no history '
                           'to compare the sidecar against')

    # `--full-history`, and the commit date (`%cI`) rather than the author date.
    # Default history simplification drops a merge that is TREESAME to one of
    # its parents and follows that parent instead — so a writer who edits the
    # document on a branch and merges it in after the round opens is invisible:
    # the merge is simplified away and the side commit it points at carries its
    # own older date, outside the window. `--full-history` keeps the merge, and
    # merges are diffed against their first parent below so one that changed
    # nothing along this line of history is dropped again.
    fmt = '%H\x1f%an\x1f%ae\x1f%cI\x1f%s\x1f%P'
    out, err = _git_out(repo_root, ['--literal-pathspecs', 'log', '--full-history',
                                    f'--since={since_arg}',
                                    f'-n{_TRUNK_GAP_MAX_COMMITS + 1}',
                                    f'--format={fmt}', '--', rel])
    if out is None:
        return dict(empty, status='unavailable', since=since, basis=basis, reason=err or 'git failed')
    lines = [l for l in out.splitlines() if l.strip()]
    truncated = len(lines) > _TRUNK_GAP_MAX_COMMITS
    lines = lines[:_TRUNK_GAP_MAX_COMMITS]

    # A sha shorter than 7 characters is not an identifier, it is a prefix that
    # matches one commit in sixteen — a junk value in one row would account for
    # a share of all history.
    claimed = {}
    for r in all_rows:
        for field in ('commit', 'revert_commit'):
            sha = (r.get(field) or '').strip().lower()
            if len(sha) >= 7:
                claimed.setdefault(sha, []).append(r)

    def _claimants(low):
        hit = claimed.get(low)
        if hit:
            return hit
        for c, rows_for in claimed.items():
            if low.startswith(c) or c.startswith(low):
                return rows_for
        return None

    # Two passes on purpose. A merge and the side commit it carried describe one
    # change, and `git log` hands them back newest-first — so whether a row is a
    # duplicate of another cannot be decided until every candidate has been
    # read, and an *accounted* side commit has to be able to account for its own
    # merge (the surface records the side commit's sha, never the merge's).
    records, probe_failures = [], []
    for line in lines:
        parts = line.split('\x1f')
        if len(parts) != 6:
            continue
        sha, name, email, date, subject, parents = parts
        low = sha.lower()
        parent_shas = parents.split()
        is_merge = len(parent_shas) > 1
        if is_merge:
            # `git show` on a merge prints no diff at all by default, so the
            # merge that carried a branch's rewrite into the trunk would read as
            # an empty change. Diff it against its first parent: that is exactly
            # what the merge did to this line of history.
            patch, perr = _git_out(repo_root, ['--literal-pathspecs', 'diff',
                                               parent_shas[0], sha, '--unified=0', '--', rel])
        else:
            patch, perr = _git_out(repo_root, ['--literal-pathspecs', 'show', sha,
                                               '--format=', '--unified=0', '--', rel])
        if patch is None:
            # A subprocess that failed is not a commit that changed nothing.
            # Dropping it here (or listing it with an empty diff) would let a
            # timeout or an index lock print "everything traces to a row".
            probe_failures.append(f'{sha[:12]} ({perr or "no reason given"})')
            continue
        if is_merge and not patch.strip():
            continue  # the merge changed nothing along this line of history
        before, after = _trunk_diff_texts(patch)
        if is_merge:
            subject = f'{subject} — merge, brought this change into the trunk from a branch'
        records.append({'sha': sha, 'name': name, 'email': email, 'date': date,
                        'subject': subject, 'before': before, 'after': after,
                        'is_merge': is_merge, 'owners': _claimants(low)})

    commits, accounted, by_reader, laundered = [], 0, 0, 0
    # Only a fingerprint that is genuinely a duplicate of ANOTHER SHAPE of the
    # same change may suppress a row: a merge against the commit it carried.
    # Deduping non-merge against non-merge would swallow a re-application after
    # a revert (X→Y, Y→X, X→Y) and leave the page describing a document that
    # ends in X while the file holds Y.
    def _fp(rec):
        pair = (blockmap.norm(rec['before'] or ''), blockmap.norm(rec['after'] or ''))
        return pair if any(pair) else None      # an empty diff is not an identity
    merge_fps = {_fp(r) for r in records if r['is_merge']} - {None}
    plain_fps = {_fp(r) for r in records if not r['is_merge']} - {None}
    seen = set()
    for rec in records:
        fp = _fp(rec)
        twin = (fp is not None
                and (fp in plain_fps if rec['is_merge'] else fp in merge_fps))
        if twin and fp in seen:
            continue
        owners = rec['owners']
        before, after, subject = rec['before'], rec['after'], rec['subject']
        if owners is None and rec['is_merge'] and fp in plain_fps:
            # The merge itself is claimed by nobody — the surface records the
            # side commit's sha. Inherit that commit's claim rather than ringing
            # every merged PR at the reader.
            owners = next((r['owners'] for r in records
                           if not r['is_merge'] and _fp(r) == fp and r['owners']), None)
        if owners is not None:
            # `_git_commit_file` runs `git add <file>` — it stages the WHOLE
            # document, not the span it just wrote. So a direct edit left dirty
            # in the working tree is swept into the next surface commit and
            # would be laundered into "accounted for" by its sha. Check the
            # added text against what the claiming rows actually proposed; a
            # residue means this commit carried more than the surface wrote.
            residue = _trunk_residue(after, owners)
            if not residue:
                accounted += 1
                if fp is not None:
                    seen.add(fp)
                continue
            laundered += 1
            subject = (f'{subject} — carried text no row proposed, staged into a recorded '
                       f'commit (git add stages the whole file)')
            after = residue
            before = ''
        if fp is not None:
            seen.add(fp)
        as_reader = _trunk_author_is_reader(rec['name'], rec['email'], reader)
        by_reader += 1 if as_reader else 0
        commits.append({'sha': rec['sha'][:12], 'author': rec['name'], 'date': rec['date'],
                        'subject': subject,
                        'before': before, 'after': after, 'uncommitted': False,
                        'as_reader': as_reader, 'merge': rec['is_merge']})

    # An edit sitting in the working tree is the same gap with the commit step
    # missing — and it is the state a half-finished direct edit is actually in.
    if status_out.strip():
        patch, diff_err = _git_out(repo_root, ['--literal-pathspecs', 'diff', 'HEAD',
                                               '--unified=0', '--', rel])
        if patch is None:
            return dict(empty, status='unavailable', since=since, basis=basis,
                        commits=commits, unattributed=len(commits), accounted=accounted,
                        reason=f'the working tree is dirty and git diff failed '
                               f'({diff_err or "no reason given"}), so an uncommitted direct '
                               f'edit cannot be read')
        before, after = _trunk_diff_texts(patch)
        if (patch or '').strip():
            # Non-empty patch, possibly empty texts: a blank-line-only change
            # merges two paragraphs and is a real edit to the document.
            #
            # One of these is not a stranger's edit: `_git_commit_file` can fail
            # (no committer identity, an index lock, a pre-commit hook), and it
            # used to swallow the error — the row then stored `commit: null`,
            # the file stayed dirty, and every later render reported the
            # surface's own write as an edit of unknown origin that never
            # cleared. Rows now carry `commit_error`; if the uncommitted text is
            # what those rows proposed, say whose it is and why it is loose.
            # Scoped to THIS round: `commit_error` is written once and never
            # cleared, so an unscoped read would let a single failure in a round
            # weeks ago label every later direct edit as this surface's own.
            failed_rows = [r for r in all_rows
                           if (r.get('commit_error') or '').strip()
                           and (r.get('timestamp') or '') >= (since or '')]
            # BOTH sides have to be accounted for. A stranger's deletion has an
            # empty `after`, and `_trunk_residue('')` is empty by arithmetic, not
            # by evidence — checking only the added side would hand the single
            # most destructive edit the "this was us" label at zero words.
            residue_after = _trunk_residue(after, failed_rows) if failed_rows else after
            residue_before = _trunk_residue(before, failed_rows, floor=1) if failed_rows else before
            surface_write = (bool(failed_rows) and not residue_after and not residue_before
                             and bool((after or '').strip()))
            if surface_write:
                why = failed_rows[-1].get('commit_error') or 'no reason recorded'
                subject = ('in the trunk, uncommitted — this surface wrote it and the commit '
                           f'failed ({why}), so the trunk keeps no record of it')
                author = failed_rows[-1].get('author') or 'this surface'
            else:
                # The whole dirty diff is shown, not the residue: trimming it to
                # the unmatched words strikes holes through a stranger's
                # sentence and throws the deleted side away entirely.
                subject = 'uncommitted edit to the trunk, not committed at all'
                author = 'working tree'
                if failed_rows and (residue_after or residue_before):
                    subject += (' — part of this text is a surface write whose commit failed, '
                                'and part is not; the whole dirty diff is shown')
            commits.append({'sha': None, 'author': author, 'date': '',
                            'subject': subject,
                            'before': before, 'after': after, 'uncommitted': True,
                            'surface_write': surface_write,
                            'as_reader': False})

    if probe_failures:
        # Some commit in the window could not be read, so this list is a partial
        # answer. Every other git failure in this function is `unavailable`;
        # these were the two that quietly were not.
        return dict(empty, status='unavailable', since=since, basis=basis,
                    commits=commits, unattributed=len(commits), accounted=accounted,
                    by_reader=by_reader, laundered=laundered, truncated=truncated,
                    window_commits=len(lines),
                    reason=f'{len(probe_failures)} commit(s) in this round could not be read '
                           f'({"; ".join(probe_failures[:3])}), so what is listed below is a '
                           f'partial answer, not a checked round')
    result = {'status': 'ok', 'reason': '', 'since': since, 'basis': basis,
              'commits': commits, 'unattributed': len(commits), 'truncated': truncated,
              'accounted': accounted, 'by_reader': by_reader, 'laundered': laundered,
              'window_commits': len(lines)}
    # Only the 'ok' answer is cached. Every other exit is a failure to look
    # (git unavailable, an untracked document) and must be retried, not pinned.
    if cacheable:
        if len(_TRUNK_GAP_CACHE) >= _TRUNK_GAP_CACHE_MAX:
            _TRUNK_GAP_CACHE.clear()
        _TRUNK_GAP_CACHE[cache_key] = copy.deepcopy(result)
    return result


def _ringer_diff_html(before, after):
    """Word-level del/ins, server side — the same reading the v3 client paints
    inline, so the ringer row shows the change itself and not a description of
    it (agreed model item 3)."""
    a = re.split(r'(\s+)', before or '')
    b = re.split(r'(\s+)', after or '')
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ('equal',):
            out.append(_html.escape(''.join(a[i1:i2])))
            continue
        if tag in ('replace', 'delete'):
            seg = ''.join(a[i1:i2])
            if seg:
                out.append(f'<del>{_html.escape(seg)}</del>')
        if tag in ('replace', 'insert'):
            seg = ''.join(b[j1:j2])
            if seg:
                out.append(f'<ins>{_html.escape(seg)}</ins>')
    return ''.join(out)


RINGER_CSS = """
.ringer-list{margin:34px 0 10px;padding:18px 20px;border:1px solid #3a3222;border-radius:10px;background:#191712;}
.ringer-list h2{margin:0 0 6px;font-size:16px;color:#e8d9a8;}
.ringer-list .ringer-why{margin:0 0 14px;color:#a99c7d;font-size:13px;line-height:1.55;max-width:70ch;}
.ringer-row{border-top:1px solid #2e2a20;padding:12px 0;}
.ringer-row:first-of-type{border-top:none;}
.ringer-tag{display:inline-block;font-size:11px;letter-spacing:.04em;text-transform:uppercase;
  padding:1px 7px;border-radius:9px;border:1px solid #5a4a24;color:#e8d9a8;margin-right:8px;}
.ringer-tag.flagged{border-color:#6a3a3a;color:#f0b8b8;}
.ringer-tag.withdrawn{border-color:#6a3a3a;color:#f0b8b8;}
.ringer-tag.unattributed{border-color:#6a4a2a;color:#f0cf9a;}
.ringer-meta{font-size:12px;color:#8d8470;}
.ringer-diff{margin-top:7px;font-size:14px;line-height:1.6;color:#d8d2c4;}
.ringer-diff del{color:#c98a8a;text-decoration:line-through;}
.ringer-diff ins{color:#9ed49e;text-decoration:none;}
.ringer-row a.ringer-goto{color:#8fb8e0;text-decoration:none;font-size:12px;}
.ringer-empty{color:#8d8470;font-size:13px;}
"""


def render_ringer_section(ringer):
    """Server-rendered so the list exists in the page's HTML whether or not the
    v3 client script ran — a list that only appears when JavaScript succeeds is
    a list that can be empty by omission, which is the thing 12b forbids."""
    b = ringer['bracket']
    if b['upper'] is None:
        why = ('No bracket yet on this page — the reader has neither marked a block nor '
               'signalled where they stopped, so nothing has been swallowed.')
    else:
        why = (f"Fork Q1 is Alternative B: a bracket of assent covers everything between the "
               f"reader’s marks, revisions included. This reader’s bracket runs from the top of "
               f"the document to block {b['upper'] + 1} of {b['blocks_total']} "
               f"(edge set by {b['basis']}), and they marked {ringer['marked_blocks']} block(s) "
               f"inside it. Every revision below is one their bracket swallowed without an "
               f"explicit mark, named back to them. Generated from the sidecar; it cannot be "
               f"short by omission.")
    gap = ringer.get('trunk_gap') or {}
    if gap.get('status') == 'ok':
        if gap['unattributed']:
            why += (f" {gap['unattributed']} change(s) reached the document since "
                    f"{gap['basis']} with no row on this surface at all — a direct file "
                    f"write, invisible to every other list here. They are listed below.")
        elif gap.get('window_commits'):
            why += (f" The trunk was also checked against the sidecar since {gap['basis']}: "
                    f"{gap['accounted']} committed change(s) all trace to a row here, so "
                    f"nothing reached the document behind this surface's back.")
        else:
            why += (f" No commit has touched this file since {gap['basis']}, so the trunk "
                    f"witness had nothing to check — which is not the same as every change "
                    f"tracing to a row.")
        if gap.get('truncated'):
            why += (f" The window held more than {_TRUNK_GAP_MAX_COMMITS} commits and was cut "
                    f"to the newest {_TRUNK_GAP_MAX_COMMITS}; older ones in this round were "
                    f"NOT checked.")
        if gap.get('laundered'):
            why += (f" {gap['laundered']} of them is recorded by a row that does not account "
                    f"for all its text: `git add` stages the whole file, so a direct edit left "
                    f"dirty gets swept into the next recorded commit.")
    elif gap.get('status') in ('untracked', 'unavailable'):
        why += (f" The trunk could NOT be checked against the sidecar — "
                f"{_html.escape(gap.get('reason') or 'git was unavailable')}. A change made "
                f"by writing the file directly would not appear in this list.")
    if ringer.get('outside_bracket'):
        why += (f" {ringer['outside_bracket']} further revision(s) now sit below the bracket "
                f"edge on the current page and are not listed here — a rewritten block can "
                f"move, so the machine half can lose one. The writer-flagged entries are the "
                f"backstop for that.")
    rows_html = []
    for r in ringer['ringers']:
        tag = r['why'] if r['why'] in ('flagged', 'withdrawn', 'unattributed') else 'swallowed'
        label = {'flagged': 'writer-flagged',
                 'withdrawn': 'withdrawn, still in the trunk',
                 'unattributed': 'trunk change with no sidecar row'}.get(r['why'],
                                                                        'bracket swallowed')
        pos = ('in the trunk' if r['why'] == 'unattributed'
               else f"block {r['position'] + 1}" if r['position'] is not None else 'unanchored')
        reason = (f"<div class=\"ringer-meta\">{_html.escape(r['reason'])}</div>"
                  if r['reason'] else '')
        rows_html.append(
            f'<div class="ringer-row" data-ringer-id="{_html.escape(r["id"] or "")}">'
            f'<span class="ringer-tag {tag}">{label}</span>'
            f'<span class="ringer-meta">{pos} · by {_html.escape(r["author"] or "?")}'
            f'{" · replace" if r["replace"] else ""} · {_html.escape((r["timestamp"] or "")[:19])}</span> '
            + (f'<a class="ringer-goto" href="#{_html.escape(r["block_id"])}">go to block</a>'
               if r['block_id'] else '')
            + f'<div class="ringer-diff">{_ringer_diff_html(r["before"], r["after"])}</div>'
            + f'{reason}</div>'
        )
    if not rows_html:
        # Two different empties, and conflating them would be the same
        # over-claim the ringer list exists to prevent: "everything was
        # handled" is a stronger statement than "there was nothing to handle".
        rows_html.append(
            '<p class="ringer-empty">No one revised your text on this page through this '
            'surface, so there was nothing for the bracket to swallow.</p>'
            if not ringer['revisions'] else
            '<p class="ringer-empty">Nothing was swallowed this round: every revision inside '
            'the bracket was marked, settled or reverted by the reader.</p>')
    return (f'<section class="ringer-list" id="ringer-list">'
            f'<h2>Ringer list ({len(ringer["ringers"])})</h2>'
            f'<p class="ringer-why">{why}</p>' + ''.join(rows_html) + '</section>')


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
            # Landing page is the inbox, not a workspace home doc (2026-09-04):
            # "what is waiting for me" is the question the surface should answer
            # first. Every workspace home stays one click away in the sidebar.
            try:
                get_workspace(workspace)
            except NotFoundError:
                self._send_html('<h1>404</h1><p>Unknown workspace.</p>', status=404)
                return
            self.send_response(302)
            self.send_header('Location', f'{workspace_url_prefix(workspace)}/waiting')
            self.end_headers()
            return

        if path == '/home':
            try:
                ws = get_workspace(workspace)
            except NotFoundError:
                self._send_html('<h1>404</h1><p>Unknown workspace.</p>', status=404)
                return
            self.send_response(302)
            self.send_header('Location', f'{workspace_url_prefix(workspace)}/page/{ws["home"]}')
            self.end_headers()
            return

        if path == '/waiting':
            self._send_html(render_waiting(workspace))
            return

        if path == '/api/waiting':
            self._send_json(collect_waiting())
            return

        if path.startswith('/page/'):
            route_path = path[len('/page/'):]
            view = qs.get('view', [None])[0]
            try:
                self._send_html(render_page(route_path, workspace, view=view))
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

        if path == '/api/read-state':
            # Harvest-facing read of the latest reader-signal ('done' or
            # 'gave-up') mark for a page — the deepest reached block plus the
            # full per-block read/skip/interact map (2026-09-03 spec item 4).
            page = qs.get('page', [''])[0]
            if not page:
                self._send_json({'error': 'page required'}, status=400)
                return
            try:
                signals = [
                    c for c in read_comments(page, workspace)
                    if not c.get('deleted') and c.get('type') == 'mark'
                    and c.get('mark_kind') == 'reader-signal'
                ]
            except NotFoundError:
                self._send_json({'error': 'unknown workspace'}, status=404)
                return
            if not signals:
                self._send_json({'found': False})
                return
            # Tie-break on append order (JSONL sidecar order), not just the
            # second-resolution timestamp — two signals in the same second
            # (e.g. a quick gave-up followed immediately by done) must still
            # resolve to whichever was actually written last.
            latest = max(enumerate(signals), key=lambda pair: (pair[1].get('timestamp') or '', pair[0]))[1]
            self._send_json({
                'found': True,
                'signal': latest.get('signal'),
                'last_read_block': latest.get('last_read_block'),
                'read_states': latest.get('read_states') or {},
                'timestamp': latest.get('timestamp'),
                'author': latest.get('author'),
            })
            return

        if path == '/api/ringer':
            # Agreed model 12b, machine half: what did the reader's bracket
            # swallow on this page. JSON twin of the section rendered into the
            # v3 page, so a round can be closed from a script as well as by eye.
            page = qs.get('page', [''])[0]
            if not page:
                self._send_json({'error': 'page required'}, status=400)
                return
            reader = qs.get('reader', [RINGER_READER])[0] or RINGER_READER
            try:
                self._send_json(compute_ringer_list(page, workspace, reader))
            except NotFoundError:
                self._send_json({'error': 'unknown page or workspace'}, status=404)
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
            response_extra_html = None
            if ctype not in ('comment', 'edit', 'verdict', 'mark'):
                self._send_json({'error': 'invalid type'}, status=400)
                return
            if ctype == 'mark':
                mark_kind = data.get('mark_kind')
                # `reader-signal` is not one of the Playmaker-model K kinds — it is
                # the v3 "I've given up" / "I'm done" pair (Mike, 2026-09-03).
                # Same sidecar, same `type: mark` envelope, distinguished by
                # `mark_kind == 'reader-signal'` + a required `signal` field.
                if mark_kind not in ('agree', 'clarify', 'rewrite', 'strike',
                                     'note', 'ack', 'ruling', 'reader-signal',
                                     'decision'):
                    self._send_json({'error': 'invalid mark_kind'}, status=400)
                    return
                if not page:
                    self._send_json({'error': 'page required for mark'}, status=400)
                    return
                if mark_kind == 'reader-signal':
                    reader_signal = data.get('signal')
                    if reader_signal not in ('gave-up', 'done'):
                        self._send_json({'error': 'invalid signal'}, status=400)
                        return
                text = (data.get('text') or '').strip() or (
                    f'Reader signal: {data.get("signal")}' if mark_kind == 'reader-signal'
                    else mark_kind.title()
                )
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
                text = (data.get('text') or '').strip() or '(sentence change)'
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
                if data.get('replace'):
                    comment['replace'] = True
                # Writer half of the ringer list (agreed model 12b): the author
                # of a change can flag it as one the reader should NOT simply
                # agree with. It then appears in the same generated list as the
                # revisions the bracket swallowed, so there is one place to look.
                if data.get('ringer'):
                    comment['ringer'] = True
                    if data.get('reason'):
                        comment['ringer_reason'] = str(data.get('reason'))
                # Sentence-level change is applied to the trunk file AT ONCE
                # (MDP mechanics item B, corrected by Mike 2026-09-03
                # evening): this comment record is the OPEN-CHANGE marker
                # (snapshot=before, proposed=now actually on disk), never a
                # pending suggestion. A stale before-text refuses with 409
                # and writes nothing — neither the file nor this sidecar row.
                try:
                    change_result = apply_sentence_change(page, workspace, comment, author_label=comment['author'])
                except MergeConflict as exc:
                    self._send_json({'error': str(exc)}, status=409)
                    return
                except NotFoundError:
                    self._send_json({'error': 'unknown page'}, status=404)
                    return
                comment['commit'] = change_result.get('commit')
                # A write whose commit failed leaves a dirty working tree and a
                # row claiming no sha. Without this field the trunk witness sees
                # the surface's own write as an edit of unknown origin, forever
                # — the row is the only thing that can say "this one is ours".
                if change_result.get('commit_error'):
                    comment['commit_error'] = change_result['commit_error']
                if change_result.get('sentence_index') is not None:
                    comment['sentence_index'] = change_result['sentence_index']
                # Not persisted to the sidecar (would bloat every row with a
                # full block-html blob) — only returned in THIS response so
                # postComment()'s caller can redraw reactively.
                response_extra_html = change_result.get('html')
            if ctype == 'mark':
                comment['mark_kind'] = mark_kind
                comment['strength'] = strength
                # Generic structured payload for kinds whose panel/dialog rendering
                # needs more than plain text (e.g. `decision`'s alternatives +
                # recommended default). Passed through opaque; the client owns the
                # shape (`{prompt, alternatives:[{key,text}], default}` for
                # decision marks — see V3_JS `KIND_META.decision` handling).
                if isinstance(data.get('meta'), dict):
                    comment['meta'] = data.get('meta')
                if data.get('scope') in ('section', 'page'):
                    comment['scope'] = data.get('scope')
                if data.get('reason'):
                    comment['reason'] = str(data.get('reason'))
                if data.get('proposed') is not None:
                    comment['proposed'] = data.get('proposed')
                if data.get('sent_because'):
                    comment['sent_because'] = str(data.get('sent_because'))
                if mark_kind == 'reader-signal':
                    # Spec literal field names (`kind`, `signal`) alongside the
                    # schema's existing `mark_kind`, so a reader can query either
                    # vocabulary. `last_read_block` = deepest block id the v3
                    # client had marked >=1.5s-dwell 'read' (or 'interacted') at
                    # click time; `read_states` (only meaningful for 'done') is
                    # the full per-block {unreached|skipped|read|interacted} map
                    # the client tracked while scrolling — lets the harvest and
                    # bracketed-assent rule distinguish read from skipped.
                    comment['kind'] = 'reader-signal'
                    comment['signal'] = reader_signal
                    comment['last_read_block'] = data.get('last_read_block')
                    read_states = data.get('read_states')
                    if reader_signal == 'done' and isinstance(read_states, dict):
                        comment['read_states'] = {
                            str(k): str(v) for k, v in read_states.items()
                        }
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
            response = dict(comment)
            if response_extra_html is not None:
                response['html'] = response_extra_html
            self._send_json(response, status=201)
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

        if path == '/api/marks/merge':
            # Settle / Revert (MDP mechanics items B/C, corrected 2026-09-03):
            # {page, id, action: "settle"|"revert"}. An edit/replace mark's
            # change was ALREADY written to the trunk at creation time (see
            # ctype == 'edit' below) — this endpoint only resolves it.
            # Settle: no file write, trunk already correct. Revert:
            # deterministically writes the mark's `snapshot` (before) back
            # into the trunk, committed. Both hash-guard against the file's
            # real current text and refuse (409) on drift.
            data = self._read_json_body()
            page = data.get('page', '')
            mark_id = data.get('id', '')
            action = data.get('action', 'settle')
            # No default of 'mike' here: an unnamed resolver must not be credited
            # as the reader, or the ringer list clears itself. This field is
            # client-asserted exactly like every `author` in this app — it is a
            # record of who claimed to resolve, not an authenticated fact.
            resolver_author = (data.get('author') or '').strip()
            if action not in ('settle', 'revert'):
                self._send_json({'error': 'action must be settle or revert'}, status=400)
                return
            if not (page and mark_id):
                self._send_json({'error': 'page and id required'}, status=400)
                return
            try:
                resolve_page(page, workspace)
            except NotFoundError:
                self._send_json({'error': 'unknown page'}, status=404)
                return
            rows = [c for c in read_comments(page, workspace) if c.get('id') == mark_id]
            if not rows:
                self._send_json({'error': 'mark not found'}, status=404)
                return
            mark = rows[0]
            if mark.get('type') not in ('edit',):
                self._send_json({'error': 'only edit/replace marks can be settled or reverted'}, status=400)
                return
            if mark.get('status') == 'done':
                self._send_json({'error': 'mark already resolved'}, status=409)
                return
            if action == 'settle':
                result = apply_sentence_settle(page, workspace, mark)
                # `resolved_by` is what lets the ringer list tell a revision the
                # reader acted on from one his bracket swallowed silently. It is
                # as trustworthy as any other client-asserted author on this
                # surface, which is to say: a record, not an authentication.
                update_comment(page, mark_id, {'status': 'done', 'settled': True,
                                               'resolved_by': resolver_author}, workspace)
                self._send_json({
                    'ok': True, 'settled': True,
                    'block_id': result['block_id'], 'html': result['html'], 'commit': result['commit'],
                })
                return
            try:
                result = apply_sentence_revert(page, workspace, mark)
            except MergeConflict as exc:
                self._send_json({'error': str(exc)}, status=409)
                return
            except NotFoundError:
                self._send_json({'error': 'unknown page'}, status=404)
                return
            # The revert writes and commits. Without the sha on the row, the
            # trunk witness sees a commit no row claims and rings the reader's
            # own revert back at him as an unrecorded change.
            revert_patch = {'status': 'done', 'reverted': True,
                            'revert_commit': result.get('commit'),
                            'resolved_by': resolver_author}
            if result.get('commit_error'):
                revert_patch['commit_error'] = result['commit_error']
            update_comment(page, mark_id, revert_patch, workspace)
            self._send_json({
                'ok': True, 'reverted': True,
                'block_id': result['block_id'], 'html': result['html'], 'commit': result['commit'],
            })
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
