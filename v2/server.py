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

PROJECTS_ROOT = os.path.expanduser('~/Projects')
FEEDBACK_DIR = os.path.join(PROJECTS_ROOT, '_estate', 'review-feedback')
DISPATCH_PROMPT_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dispatch-prompt-template.md')
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')

os.makedirs(FEEDBACK_DIR, exist_ok=True)

# --- Whitelisted roots -------------------------------------------------
# Each entry: (route_prefix, absolute_fs_root)
WHITELIST_ROOTS = [
    ('estate', os.path.join(PROJECTS_ROOT, '_estate')),
    ('business-ops', os.path.join(PROJECTS_ROOT, 'business-ops')),
    ('soma', os.path.join(PROJECTS_ROOT, 'SOMA')),
]

# Nightly worktree reports get their own synthetic route: nightly/<worktree-name>
NIGHTLY_PREFIX = 'nightly'


def discover_nightly_reports():
    """Return {slug: absolute_path} for .nightly-*/NIGHTLY-REPORT.md worktrees."""
    out = {}
    if not os.path.isdir(PROJECTS_ROOT):
        return out
    for name in os.listdir(PROJECTS_ROOT):
        if name.startswith('.nightly-'):
            candidate = os.path.join(PROJECTS_ROOT, name, 'NIGHTLY-REPORT.md')
            if os.path.isfile(candidate):
                slug = name[len('.nightly-'):]
                out[slug] = candidate
    return out


# Nav sidebar entries: (label, page_path_within_app)
# page_path is the route path used by resolve_page(), e.g. "estate/MORNING-REVIEW-2026-07-02.md"
NAV_ENTRIES = [
    ('Morning Review', 'estate/MORNING-REVIEW-2026-07-02.md'),
    ('Productivity Opportunities', 'estate/PRODUCTIVITY-OPPORTUNITIES-2026-07-02.md'),
    ('Business Plan', 'business-ops/BUSINESS-PLAN-2026-07.md'),
    ('Doc-Proofing Plan', 'estate/DOC-PROOFING-PLAN-2026-07-01.md'),
    ('Overnight Manifest', 'estate/OVERNIGHT-2026-07-01.md'),
    ('SOMA App Standard', 'soma/SOMA-APP-STANDARD.md'),
    ('Vision Interview', 'estate/audit-2026-07/VISION-INTERVIEW-2026-07-01.md'),
]

HOME_PAGE = 'estate/MORNING-REVIEW-2026-07-02.md'


# --- Path resolution / security -----------------------------------------

class NotFoundError(Exception):
    pass


def resolve_page(route_path):
    """route_path like 'estate/foo/bar.md' or 'nightly/izzy'. Returns absolute fs path.
    Raises NotFoundError if outside whitelist or missing.
    """
    route_path = route_path.strip('/')
    if not route_path:
        route_path = HOME_PAGE

    parts = route_path.split('/', 1)
    if len(parts) != 2:
        raise NotFoundError(route_path)
    prefix, rest = parts

    if prefix == NIGHTLY_PREFIX:
        reports = discover_nightly_reports()
        slug = rest
        if slug not in reports:
            raise NotFoundError(route_path)
        return reports[slug]

    root = None
    for p, r in WHITELIST_ROOTS:
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


def fs_path_to_route(fs_path):
    """Best-effort reverse mapping for link rewriting within the same root."""
    fs_path = os.path.normpath(fs_path)
    for p, r in WHITELIST_ROOTS:
        r = os.path.normpath(r)
        if fs_path.startswith(r + os.sep):
            rel = os.path.relpath(fs_path, r)
            return f'{p}/{rel}'
    return None


def page_slug(route_path):
    """Filesystem-safe slug for sidecar filenames."""
    return re.sub(r'[^a-zA-Z0-9._-]+', '_', route_path.strip('/'))


# --- Link rewriting -------------------------------------------------------

def make_link_resolver(current_fs_path, current_route):
    current_dir = os.path.dirname(current_fs_path)

    def resolver(href):
        if href.startswith(('http://', 'https://', 'mailto:', '#')):
            return href, False
        # strip a leading backtick-wrapped or trailing anchor fragment
        frag = ''
        if '#' in href:
            href, frag = href.split('#', 1)
            frag = '#' + frag
        if not href:
            return current_route and f'/page/{current_route}{frag}' or frag, True
        target_fs = os.path.normpath(os.path.join(current_dir, href))
        if os.path.isfile(target_fs) and target_fs.endswith('.md'):
            route = fs_path_to_route(target_fs)
            if route:
                return f'/page/{route}{frag}', True
        # Not resolvable / not whitelisted / not markdown -> leave as inert text-ish link
        # but still make it non-navigating-away by disabling href if it looks local.
        if href.endswith('.md'):
            return '#unresolved', True
        return href, False

    return resolver


# --- Comment sidecar storage ------------------------------------------

_lock = threading.Lock()


def sidecar_path(route_path):
    return os.path.join(FEEDBACK_DIR, page_slug(route_path) + '.jsonl')


def read_comments(route_path):
    path = sidecar_path(route_path)
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


def write_all_comments(route_path, comments):
    path = sidecar_path(route_path)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        for c in comments:
            f.write(json.dumps(c, ensure_ascii=False) + '\n')
    os.replace(tmp, path)


def append_comment(route_path, comment):
    with _lock:
        path = sidecar_path(route_path)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(comment, ensure_ascii=False) + '\n')


def update_comment(route_path, comment_id, patch):
    with _lock:
        comments = read_comments(route_path)
        found = False
        for c in comments:
            if c.get('id') == comment_id:
                c.update(patch)
                found = True
        if found:
            write_all_comments(route_path, comments)
        return found


# --- HTML rendering --------------------------------------------------------

PAGE_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0;
       display: flex; min-height: 100vh; background: #0f1216; color: #e6e6e6; }
a { color: #7db8ff; }
.sidebar { width: 260px; flex-shrink: 0; background: #14181f; padding: 20px 16px;
           border-right: 1px solid #262b33; position: sticky; top: 0; height: 100vh; overflow-y: auto; }
.sidebar h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .08em; color: #8a93a3; margin: 18px 0 8px; }
.sidebar h2:first-child { margin-top: 0; }
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
"""

PAGE_JS = r"""
const ROUTE = window.__ROUTE__;

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2200);
}

function badgeClass(status) {
  return 'badge badge-' + (status || 'queued');
}

async function fetchComments() {
  const res = await fetch(`/api/comments?page=${encodeURIComponent(ROUTE)}`);
  return res.json();
}

function renderCommentItem(c) {
  const div = document.createElement('div');
  div.className = 'comment-item';
  div.innerHTML = `<div class="meta">${c.author} · ${new Date(c.timestamp).toLocaleString()} · <span class="${badgeClass(c.status)}">${c.status}</span></div>
    <div>${c.text.replace(/</g,'&lt;')}</div>`;
  return div;
}

async function loadThreadsIntoDOM() {
  const comments = await fetchComments();
  const byAnchor = {};
  const pageLevel = [];
  for (const c of comments) {
    if (!c.anchor) { pageLevel.push(c); continue; }
    (byAnchor[c.anchor] = byAnchor[c.anchor] || []).push(c);
  }
  document.querySelectorAll('.block-wrap').forEach(el => {
    const anchor = el.dataset.anchor;
    const list = byAnchor[anchor] || [];
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
      pill.textContent = list.length;
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

async function postComment({anchor, snapshot, text, threadId}) {
  const res = await fetch('/api/comments', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ page: ROUTE, anchor, snapshot, text, thread_id: threadId || null })
  });
  if (!res.ok) throw new Error('post failed: ' + res.status);
  return res.json();
}

function wireBlockAffordances() {
  document.querySelectorAll('.block-wrap').forEach(el => {
    const btn = el.querySelector('.comment-affordance');
    const box = el.querySelector('.comment-box');
    btn.addEventListener('click', () => {
      box.classList.toggle('open');
      if (box.classList.contains('open')) box.querySelector('textarea').focus();
    });
    box.querySelector('button').addEventListener('click', async () => {
      const ta = box.querySelector('textarea');
      const text = ta.value.trim();
      if (!text) return;
      await postComment({ anchor: el.dataset.anchor, snapshot: el.dataset.snapshot, text });
      ta.value = '';
      box.classList.remove('open');
      toast('Comment saved.');
      loadThreadsIntoDOM();
    });
  });

  const pageBox = document.getElementById('page-comment-box');
  if (pageBox) {
    pageBox.querySelector('button').addEventListener('click', async () => {
      const ta = pageBox.querySelector('textarea');
      const text = ta.value.trim();
      if (!text) return;
      await postComment({ anchor: null, snapshot: '(page-level)', text });
      ta.value = '';
      toast('Comment saved.');
      loadThreadsIntoDOM();
    });
  }

  const sendBtn = document.getElementById('send-to-dee');
  if (sendBtn) {
    sendBtn.addEventListener('click', async () => {
      sendBtn.disabled = true;
      sendBtn.textContent = 'Dispatching...';
      try {
        const res = await fetch('/api/dispatch', {
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
}

document.addEventListener('DOMContentLoaded', () => {
  wireBlockAffordances();
  loadThreadsIntoDOM();
});
"""


def render_sidebar(current_route):
    reports = discover_nightly_reports()
    items = []
    items.append('<h2>Estate</h2>')
    for label, route in NAV_ENTRIES:
        cls = ' class="active"' if route == current_route else ''
        items.append(f'<a href="/page/{route}"{cls}>{render_inline(label)}</a>')
    if reports:
        items.append('<h2>Nightly Reports</h2>')
        for slug in sorted(reports):
            route = f'{NIGHTLY_PREFIX}/{slug}'
            cls = ' class="active"' if route == current_route else ''
            items.append(f'<a href="/page/{route}"{cls}>{render_inline(slug)}</a>')
    return '\n'.join(items)


def render_block_html(block, route_path):
    kind = block['kind']
    anchor = block['anchor']
    snapshot = _html_attr_escape(block['snapshot'])
    inner = block['html']
    # heading blocks: keep affordance but don't nest interactive stuff awkwardly
    return f'''<div class="block-wrap" data-anchor="{anchor}" data-snapshot="{snapshot}">
  <button class="comment-affordance" title="Comment on this block">+</button>
  {inner}
  <div class="comment-box">
    <textarea placeholder="Comment on this block..."></textarea>
    <button>Save comment</button>
  </div>
  <div class="comment-thread"></div>
</div>'''


def _html_attr_escape(s):
    return (s.replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;').replace('>', '&gt;'))


def render_page(route_path):
    fs_path = resolve_page(route_path)
    with open(fs_path, 'r', encoding='utf-8') as f:
        src = f.read()
    resolver = make_link_resolver(fs_path, route_path)
    title, blocks = parse_markdown(src, link_resolver=resolver)
    title = title or route_path

    blocks_html = '\n'.join(render_block_html(b, route_path) for b in blocks)

    has_dispatch = route_path.startswith(('estate/', 'nightly/', 'soma/', 'business-ops/'))

    html_doc = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html.escape(title)} — soma-review</title>
<style>{PAGE_CSS}</style>
</head>
<body>
<nav class="sidebar">
  <a href="/page/{HOME_PAGE}" style="font-weight:700;font-size:15px;color:#e6e6e6;">soma-review</a>
  {render_sidebar(route_path)}
</nav>
<main class="main">
  <div class="top-actions">
    {'<button id="send-to-dee">Send to Dee</button>' if has_dispatch else ''}
  </div>
  {blocks_html}
  <div class="page-discussion">
    <h2>Page discussion</h2>
    <div id="page-thread-list"></div>
    <div class="comment-box open" id="page-comment-box">
      <textarea placeholder="General comment about this page..."></textarea>
      <button>Save comment</button>
    </div>
  </div>
</main>
<div class="toast" id="toast"></div>
<script>window.__ROUTE__ = {json.dumps(route_path)};</script>
<script>{PAGE_JS}</script>
</body>
</html>"""
    return html_doc


def render_404(route_path):
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Not found — soma-review</title>
<style>{PAGE_CSS}</style></head>
<body>
<nav class="sidebar">
  <a href="/page/{HOME_PAGE}" style="font-weight:700;font-size:15px;color:#e6e6e6;">soma-review</a>
  {render_sidebar('')}
</nav>
<main class="main">
  <div class="notfound">
    <h1>404</h1>
    <p>No such page: <code>{_html.escape(route_path)}</code></p>
    <p><a href="/page/{HOME_PAGE}">&larr; Back to Morning Review</a></p>
  </div>
</main>
</body></html>"""


# --- Dispatch --------------------------------------------------------------

def load_dispatch_template():
    if os.path.isfile(DISPATCH_PROMPT_TEMPLATE):
        with open(DISPATCH_PROMPT_TEMPLATE, 'r', encoding='utf-8') as f:
            return f.read()
    return "Read the sidecar JSONL for page {page} and act on each comment."


def run_dispatch(route_path):
    fs_path = resolve_page(route_path)
    sidecar = sidecar_path(route_path)
    slug = page_slug(route_path)
    # cc-dispatch appends its own .md to build the report filename; strip any
    # .md already in the slug so we don't end up with foo.md.md-shaped names.
    task_slug = re.sub(r'\.md$', '', slug)
    task_name = f'review-comments-{task_slug}'[:80]
    template = load_dispatch_template()
    prompt = template.format(
        page=route_path,
        page_fs_path=fs_path,
        sidecar_path=sidecar,
        api_base='http://localhost:8090',
    )
    prompt_file = os.path.join(FEEDBACK_DIR, f'.dispatch-prompt-{slug}.md')
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

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == '/' :
            self.send_response(302)
            self.send_header('Location', f'/page/{HOME_PAGE}')
            self.end_headers()
            return

        if path.startswith('/page/'):
            route_path = path[len('/page/'):]
            try:
                self._send_html(render_page(route_path))
            except NotFoundError:
                self._send_html(render_404(route_path), status=404)
            return

        if path == '/api/comments':
            page = qs.get('page', [''])[0]
            self._send_json(read_comments(page))
            return

        if path == '/healthz':
            self._send_json({'ok': True, 'ts': time.time()})
            return

        self._send_html('<h1>404</h1>', status=404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == '/api/comments':
            data = self._read_json_body()
            page = data.get('page', '')
            text = (data.get('text') or '').strip()
            if not page or not text:
                self._send_json({'error': 'page and text required'}, status=400)
                return
            try:
                resolve_page(page)
            except NotFoundError:
                self._send_json({'error': 'unknown page'}, status=404)
                return
            comment_id = str(uuid.uuid4())
            thread_id = data.get('thread_id') or comment_id
            comment = {
                'id': comment_id,
                'page': page,
                'anchor': data.get('anchor'),
                'snapshot': data.get('snapshot', ''),
                'author': data.get('author', 'mike'),
                'text': text,
                'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                'status': 'queued',
                'thread_id': thread_id,
            }
            append_comment(page, comment)
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
            ok = update_comment(page, comment_id, {'status': status})
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
                resolve_page(page)
            except NotFoundError:
                self._send_json({'error': 'unknown page'}, status=404)
                return
            reply_id = str(uuid.uuid4())
            # anchor/snapshot copied from the first comment in the thread, if found
            existing = [c for c in read_comments(page) if c.get('thread_id') == thread_id]
            anchor = existing[0]['anchor'] if existing else None
            snapshot = existing[0]['snapshot'] if existing else ''
            comment = {
                'id': reply_id,
                'page': page,
                'anchor': anchor,
                'snapshot': snapshot,
                'author': author,
                'text': text,
                'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                'status': data.get('status', 'seen'),
                'thread_id': thread_id,
            }
            append_comment(page, comment)
            self._send_json(comment, status=201)
            return

        if path == '/api/dispatch':
            data = self._read_json_body()
            page = data.get('page', '')
            try:
                resolve_page(page)
            except NotFoundError:
                self._send_json({'error': 'unknown page'}, status=404)
                return
            try:
                task_name, pid = run_dispatch(page)
            except Exception as e:  # noqa: BLE001
                self._send_json({'error': str(e)}, status=500)
                return
            self._send_json({'ok': True, 'task_name': task_name, 'pid': pid})
            return

        self._send_json({'error': 'not found'}, status=404)


def main():
    port = int(os.environ.get('SOMA_REVIEW_PORT', '8090'))
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(f'soma-review v2 listening on http://localhost:{port}/page/{HOME_PAGE}')
    server.serve_forever()


if __name__ == '__main__':
    main()
