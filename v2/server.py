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
from mdblocks import parse_markdown, render_inline  # noqa: E402
import tours as tour_engine  # noqa: E402  (Quinn tours of completed jobs — see tours.py)

PROJECTS_ROOT = os.path.expanduser('~/Projects')
DISPATCH_PROMPT_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dispatch-prompt-template.md')
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
WORKSPACES_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'workspaces.json')

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
        }
    return out


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

_lock = threading.Lock()


def sidecar_path(route_path, workspace=DEFAULT_WORKSPACE):
    ws = get_workspace(workspace)
    return os.path.join(ws['feedback_dir'], page_slug(route_path) + '.jsonl')


def read_comments(route_path, workspace=DEFAULT_WORKSPACE):
    path = sidecar_path(route_path, workspace)
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def write_all_comments(route_path, comments, workspace=DEFAULT_WORKSPACE):
    path = sidecar_path(route_path, workspace)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        for c in comments:
            f.write(json.dumps(c, ensure_ascii=False) + '\n')
    os.replace(tmp, path)


def append_comment(route_path, comment, workspace=DEFAULT_WORKSPACE):
    with _lock:
        path = sidecar_path(route_path, workspace)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(comment, ensure_ascii=False) + '\n')


def update_comment(route_path, comment_id, patch, workspace=DEFAULT_WORKSPACE):
    with _lock:
        comments = read_comments(route_path, workspace)
        found = False
        for c in comments:
            if c.get('id') == comment_id:
                c.update(patch)
                found = True
        if found:
            write_all_comments(route_path, comments, workspace)
        return found


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

async function loadThreadsIntoDOM() {
  const comments = await fetchComments();
  const byAnchor = {};
  const pageLevel = [];
  for (const c of comments) {
    if (!c.anchor) { pageLevel.push(c); continue; }
    (byAnchor[c.anchor] = byAnchor[c.anchor] || []).push(c);
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
  document.querySelectorAll('.verdict-row').forEach(rowEl => {
    const rowId = rowEl.dataset.rowId;
    const v = latestVerdictByRow[rowId];
    const statusEl = rowEl.querySelector('.verdict-status');
    if (v && statusEl) statusEl.textContent = `✓ ${v.verdict}`;
  });
  document.querySelectorAll('.block-wrap').forEach(el => {
    const anchor = el.dataset.anchor;
    const list = (byAnchor[anchor] || []).filter(c => !c.deleted || c.author === 'mike');
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
}

async function postComment({anchor, snapshot, text, threadId, type, proposed, row_id, verdict}) {
  const res = await fetch(`${API_BASE}/api/comments`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ page: ROUTE, anchor, snapshot, text, thread_id: threadId || null,
                            type: type || 'comment', proposed, row_id, verdict })
  });
  if (!res.ok) throw new Error('post failed: ' + res.status);
  return res.json();
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
        text: 'Suggested edit', type: 'edit',
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
      await postComment({ anchor: el.dataset.anchor, snapshot: el.dataset.snapshot, text });
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
      await postComment({ anchor: null, snapshot: '(page-level)', text });
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
  if (sendBtn) {
    sendBtn.addEventListener('click', async () => {
      sendBtn.disabled = true;
      sendBtn.textContent = 'Dispatching...';
      try {
        const res = await fetch(`${API_BASE}/api/dispatch`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ page: ROUTE })
        });
        const data = await res.json();
        if (res.ok) {
          toast('Dispatched: ' + data.task_name);
        } else {
          toast('Dispatch failed: ' + (data.error || res.status));
        }
      } catch (e) {
        toast('Dispatch error: ' + e.message);
      }
      sendBtn.disabled = false;
      sendBtn.textContent = 'Send to Dee';
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
        await postComment({ anchor, snapshot, text: `Verdict: ${verdict}`, type: 'verdict', row_id: rowId, verdict });
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

document.addEventListener('DOMContentLoaded', () => {
  wireBlockAffordances();
  wireEditableBlocks();
  wireEnterOpensComment();
  wireVerdictButtons();
  loadThreadsIntoDOM();
});
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


def render_block_html(block, route_path):
    kind = block['kind']
    anchor = block['anchor']
    snapshot = _html_attr_escape(block['snapshot'])
    inner = block['html']
    # Raw markdown source, base64'd, so the client can swap rendered HTML for an
    # editable <textarea> pre-filled with the exact source text (edit-as-comment,
    # see v2/CLAUDE.md "CM6 vs contenteditable" note). Base64 sidesteps any HTML/JS
    # string-escaping edge cases in doc text (backticks, quotes, newlines).
    import base64 as _b64
    source_b64 = _b64.b64encode(block['text'].encode('utf-8')).decode('ascii')
    # code/table blocks are excluded from click-to-edit — their raw source has
    # internal structure (fences, pipes) that's easy to corrupt via a flat textarea
    # edit and low-value to inline-edit anyway; they still get the comment affordance.
    editable = kind not in ('code', 'table')
    edit_cls = ' edit-eligible' if editable else ''
    return f'''<div class="block-wrap{edit_cls}" data-anchor="{anchor}" data-snapshot="{snapshot}" data-kind="{kind}" data-source="{source_b64}">
  <button class="comment-affordance" title="Comment on this block (Enter)">+</button>
  <div class="block-body"{' tabindex="0"' if editable else ''}>{inner}</div>
  <div class="comment-box">
    <textarea placeholder="Comment on this block... (Enter to save, Shift+Enter for newline)"></textarea>
    <button class="mic-btn" type="button" title="Dictate">&#127908;</button>
    <button class="save-btn" type="button">Save comment</button>
  </div>
  <div class="comment-thread"></div>
</div>'''


def _html_attr_escape(s):
    return (s.replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;').replace('>', '&gt;'))


def render_page(route_path, workspace=DEFAULT_WORKSPACE):
    ws = get_workspace(workspace)
    url_prefix = workspace_url_prefix(workspace)
    fs_path = resolve_page(route_path, workspace)
    with open(fs_path, 'r', encoding='utf-8') as f:
        src = f.read()
    resolver = make_link_resolver(fs_path, route_path, workspace)
    title, blocks = parse_markdown(src, link_resolver=resolver)
    title = title or route_path

    blocks_html = '\n'.join(render_block_html(b, route_path) for b in blocks)

    # Every route in a workspace's own roots (or its nightly reports) is dispatchable.
    root_prefixes = tuple(f'{p}/' for p, _ in ws['roots'])
    has_dispatch = route_path.startswith(root_prefixes) or (ws['nightly'] and route_path.startswith(f'{NIGHTLY_PREFIX}/'))
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

    html_doc = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html.escape(title)} — soma-review</title>
<style>{PAGE_CSS}</style>
{tour_head}
</head>
<body>
<nav class="sidebar">
  <a href="{url_prefix}/page/{ws['home']}" style="font-weight:700;font-size:15px;color:#e6e6e6;">soma-review</a>
  {render_workspace_switcher(workspace)}
  {render_sidebar(route_path, workspace)}
</nav>
<main class="main">
  <div class="top-actions">
    {'<button id="send-to-dee">Send to Dee</button>' if has_dispatch else ''}
    {'<button id="regenerate-board">Regenerate board</button>' if is_board_or_portfolio else ''}
  </div>
  {blocks_html}
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
<script>window.__ROUTE__ = {json.dumps(route_path)}; window.__API_BASE__ = {json.dumps(url_prefix)};</script>
<script>{PAGE_JS}</script>
{tour_body}
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


def run_dispatch(route_path, workspace=DEFAULT_WORKSPACE):
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
    return task_name, proc.pid


# --- Board / Portfolio regeneration -----------------------------------------

GENERATE_BOARD_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'generate_board.py')
GENERATE_PORTFOLIO_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'generate_portfolio.py')


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
            if ctype not in ('comment', 'edit', 'verdict'):
                self._send_json({'error': 'invalid type'}, status=400)
                return
            if ctype == 'edit':
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
                verdict = data.get('verdict')
                if not page or verdict not in ('keep', 'restart', 'cancel', 'later'):
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
                resolve_page(page, workspace)
            except NotFoundError:
                self._send_json({'error': 'unknown page'}, status=404)
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
            }
            if ctype == 'edit':
                comment['proposed'] = data.get('proposed')
            if ctype == 'verdict':
                comment['verdict'] = data.get('verdict')
                comment['row_id'] = row_id
            append_comment(page, comment, workspace)
            self._send_json(comment, status=201)
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
            # anchor/snapshot copied from the first comment in the thread, if found
            existing = [c for c in read_comments(page, workspace) if c.get('thread_id') == thread_id]
            anchor = existing[0]['anchor'] if existing else None
            snapshot = existing[0]['snapshot'] if existing else ''
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
                task_name, pid = run_dispatch(page, workspace)
            except Exception as e:  # noqa: BLE001
                self._send_json({'error': str(e)}, status=500)
                return
            self._send_json({'ok': True, 'task_name': task_name, 'pid': pid})
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
