"""
mdblocks.py — a small, dependency-free Markdown -> block-list parser + HTML renderer.

Not a full CommonMark implementation. Handles the subset the estate's docs actually
use: ATX headings, paragraphs, unordered/ordered lists (incl. nested by indent),
GFM-style pipe tables, fenced code blocks, blockquotes, hr, and inline emphasis/
code/links. Good enough for rendering + block-level comment anchoring.

Each parsed block gets:
  - kind: heading|paragraph|list|table|code|blockquote|hr|film (film: Screening
    Room video-player block, a ```film fenced JSON payload — see render_film_block())
  - level: heading level (1-6) if kind == heading
  - heading_path: list of heading texts above this block (breadcrumb), most-recent-first ancestry
  - index: 0-based index of this block within the whole document
  - text: raw source text of the block (for snapshotting / anchor stability)
  - html: rendered HTML for the block body (no wrapping comment affordance — caller wraps)
  - anchor: stable-ish id derived from heading_path + index
"""

import re
import html as _html
import hashlib
import json
import os
import unicodedata

from blockmap import norm


PROJECTS_ROOT = os.path.expanduser('~/Projects')
PUBLIC_FILMS_PATH = os.path.join(PROJECTS_ROOT, '_estate', 'public-films.json')


def _esc(s):
    return _html.escape(s, quote=False)


def _load_public_films():
    if not os.path.isfile(PUBLIC_FILMS_PATH):
        return {}
    try:
        with open(PUBLIC_FILMS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def render_film_block(raw_json):
    """Render a ```film fenced block (Screening Room, soma-review) — a JSON payload
    describing one video into an HTML5 <video> player with poster + optional captions
    track. Kept in mdblocks.py (not server.py) so it flows through the same
    anchor/snapshot/edit-source machinery every other block kind gets for free.

    Payload keys: src (required, a /raw/... URL already resolved by the caller),
    poster (optional /raw/... URL), vtt (optional /raw/... URL), duration (optional
    display string), data-testid (optional, for verification-video selectors).
    Malformed JSON renders a visible error block instead of crashing page render.
    """
    try:
        d = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError) as e:
        return f'<div class="film-error">Malformed film block: {_esc(str(e))}</div>'
    if d.get('placeholder'):
        note = _esc(d.get('note', 'Placeholder — no video yet.'))
        return f'<div class="film-placeholder">{note}</div>'
    src = d.get('src', '')
    if not src:
        return '<div class="film-error">film block missing "src"</div>'
    poster = d.get('poster')
    vtt = d.get('vtt')
    testid = d.get('testid', '')
    poster_attr = f' poster="{_esc(poster)}"' if poster else ''
    testid_attr = f' data-testid="{_esc(testid)}"' if testid else ''
    track_html = f'<track kind="captions" src="{_esc(vtt)}" default>' if vtt else ''
    public_control = ''
    if testid:
        checked_attr = ' checked' if _load_public_films().get(testid) is True else ''
        public_control = (
            '<label class="public-film-control">'
            f'<input type="checkbox" class="public-film-toggle" data-testid="{_esc(testid)}"'
            f' data-film-testid="{_esc(testid)}"{checked_attr}>'
            '<span>Make public &mdash; feature on mikewolf.com</span>'
            '</label>'
        )
    return (
        f'<video class="film-player" controls preload="none"{poster_attr}{testid_attr}>'
        f'<source src="{_esc(src)}" type="video/mp4">{track_html}'
        f'Your browser does not support HTML5 video.'
        f'</video>{public_control}'
    )


_INLINE_LINK_RE = re.compile(r'\[([^\]]*)\]\(([^)]+)\)')
_INLINE_CODE_RE = re.compile(r'`([^`]+)`')
_BOLD_RE = re.compile(r'\*\*([^*]+)\*\*')
_ITALIC_RE = re.compile(r'(?<!\*)\*([^*]+)\*(?!\*)')
# Bare autolinks: a raw http(s) URL not already inside [label](...) markdown-link syntax.
# Stops at whitespace or a small set of trailing punctuation/markup chars so it doesn't
# swallow a following '**', ')', or sentence punctuation.
_BARE_URL_RE = re.compile(r'(https?://[^\s<>\[\]()]+?)(?=[.,;:!?]?(?:\*\*)?(?:\s|$))')

# Portfolio verdict token: [[VERDICT:<row-id>]], emitted by v2/generate_portfolio.py
# in the rightmost cell of any table row that needs a keep/restart/cancel/later
# call. Rendered as four buttons wired by PAGE_JS's wireVerdictButtons() — each
# POSTs a {type:"verdict", verdict:..., row_id:...} comment to the existing
# comment API (no new storage). Matched before bold/italic so the brackets don't
# get mistaken for anything else.
_VERDICT_TOKEN_RE = re.compile(r'\[\[VERDICT:([a-zA-Z0-9_-]+)\]\]')
# Recommendation review token: [[REVIEW:<row-id>]] — agree/disagree/discuss/defer
# for structured review pages (e.g. fleet-tooling audits). Same comment API as
# VERDICT; distinct button set wired by PAGE_JS's wireReviewButtons().
_REVIEW_TOKEN_RE = re.compile(r'\[\[REVIEW:([a-zA-Z0-9_-]+)\]\]')


def render_inline(text, link_resolver=None):
    """Render inline markdown (links, bold, italic, code) to HTML.
    link_resolver(href) -> href_out, is_internal
    """
    # Protect inline code spans first so we don't mangle markup inside them.
    placeholders = []

    def stash(html_out):
        placeholders.append(html_out)
        return f'\x00{len(placeholders) - 1}\x00'

    def stash_code(m):
        return stash(f'<code>{_esc(m.group(1))}</code>')

    text = _INLINE_CODE_RE.sub(stash_code, text)

    def _resolve(href):
        """Normalize link_resolver's return to (href_out, css_class, extra_attrs).
        link_resolver may return a 2-tuple (href, is_internal_bool) — legacy/simple
        callers — or a 3-tuple (href, kind_str, title_or_None) for finer control
        (see server.py::LinkKind). Falls back to plain external link if no resolver."""
        if link_resolver is None:
            return href, 'external-link', ' target="_blank" rel="noopener"'
        result = link_resolver(href)
        if len(result) == 2:
            href_out, internal = result
            cls = 'internal-link' if internal else 'external-link'
            extra = '' if internal else ' target="_blank" rel="noopener"'
            return href_out, cls, extra
        href_out, kind, title = result
        extra = ' target="_blank" rel="noopener"' if kind == 'external-link' else ''
        if title:
            extra += f' title="{_esc(title)}"'
        return href_out, kind, extra

    def link_sub(m):
        label, href = m.group(1), m.group(2)
        label_html = _esc(label)
        href_out, cls, extra = _resolve(href)
        return stash(f'<a href="{_esc(href_out)}" class="{cls}"{extra}>{label_html}</a>')

    def verdict_sub(m):
        row_id = _esc(m.group(1))
        buttons = ''.join(
            f'<button type="button" class="verdict-btn verdict-{v}" data-verdict="{v}" data-row-id="{row_id}">{label}</button>'
            for v, label in (('keep', 'Keep'), ('restart', 'Restart'), ('cancel', 'Cancel'), ('later', 'Later'))
        )
        return stash(f'<span class="verdict-row" data-row-id="{row_id}">{buttons}<span class="verdict-status"></span></span>')

    def review_sub(m):
        row_id = _esc(m.group(1))
        buttons = ''.join(
            f'<button type="button" class="review-btn review-{v}" data-review="{v}" data-row-id="{row_id}">{label}</button>'
            for v, label in (
                ('agree', 'Agree'),
                ('disagree', 'Disagree'),
                ('discuss', 'Discuss'),
                ('defer', 'Defer'),
            )
        )
        return stash(
            f'<span class="review-row" data-row-id="{row_id}">{buttons}'
            f'<span class="review-status"></span></span>'
        )

    # Verdict/review tokens BEFORE markdown-link stashing (tokens never contain '[label](href)'
    # syntax so order vs. links doesn't matter for correctness, but doing it first keeps
    # the intent — "this is UI, not a link" — clearest).
    text = _VERDICT_TOKEN_RE.sub(verdict_sub, text)
    text = _REVIEW_TOKEN_RE.sub(review_sub, text)

    # Stash markdown-form links [label](href) BEFORE bare-URL autolinking, so a URL
    # used as the href of a real markdown link is never double-linked.
    text = _INLINE_LINK_RE.sub(link_sub, text)

    def bare_url_sub(m):
        href = m.group(1)
        href_out, cls, extra = _resolve(href)
        return stash(f'<a href="{_esc(href_out)}" class="{cls}"{extra}>{_esc(href)}</a>')

    text = _BARE_URL_RE.sub(bare_url_sub, text)

    # Escape whatever plain text remains (placeholders are \x00N\x00 — no HTML-special
    # chars, so escaping is a no-op on them and safe to run after stashing).
    text = _esc(text)
    text = _BOLD_RE.sub(r'<strong>\1</strong>', text)
    text = _ITALIC_RE.sub(r'<em>\1</em>', text)

    # Placeholders can nest (a link's stashed HTML can itself contain a code-span
    # placeholder token, e.g. `[`file.md`](file.md)`), so a single forward pass isn't
    # enough — a placeholder substituted late may re-introduce an earlier token into
    # `text`. Re-scan until no more \x00N\x00 tokens remain (bounded by placeholder count).
    for _ in range(len(placeholders) + 1):
        if '\x00' not in text:
            break
        for i, ph in enumerate(placeholders):
            text = text.replace(f'\x00{i}\x00', ph)
    return text


def _make_anchor(heading_path, index, text):
    key = '>'.join(heading_path) + f'#{index}:' + text[:40]
    h = hashlib.sha1(key.encode('utf-8')).hexdigest()[:10]
    return f'b{index}-{h}'


def parse_markdown(src, link_resolver=None):
    """Return (title, blocks) where blocks is a list of dicts (see module docstring)."""
    src = unicodedata.normalize('NFC', src)
    lines = src.split('\n')
    blocks = []
    heading_stack = []  # list of (level, text)
    i = 0
    n = len(lines)
    idx = 0
    title = None

    def heading_path():
        return [t for (_, t) in heading_stack]

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if stripped == '':
            i += 1
            continue

        # Fenced code block (special-cased 'film' language: Screening Room video
        # player block — see render_film_block(). Same fence syntax so anchor/
        # snapshot/edit-source machinery is unchanged; only the rendered HTML differs.)
        if stripped.startswith('```'):
            fence = stripped[:3]
            lang = stripped[3:].strip()
            code_lines = []
            i += 1
            while i < n and not lines[i].strip().startswith('```'):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            raw = '\n'.join(code_lines)
            if lang == 'film':
                html_body = render_film_block(raw)
                kind = 'film'
            else:
                html_body = f'<pre><code class="lang-{_esc(lang)}">{_esc(raw)}</code></pre>'
                kind = 'code'
            blocks.append({
                'kind': kind, 'level': None, 'heading_path': heading_path(),
                'index': idx, 'text': raw, 'html': html_body,
            })
            idx += 1
            continue

        # ATX heading
        m = re.match(r'^(#{1,6})\s+(.*)$', stripped)
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
            if title is None and level == 1:
                title = text
            # pop stack to this level
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, text))
            html_body = f'<h{level}>{render_inline(text, link_resolver)}</h{level}>'
            blocks.append({
                'kind': 'heading', 'level': level, 'heading_path': heading_path()[:-1],
                'index': idx, 'text': text, 'html': html_body,
            })
            idx += 1
            i += 1
            continue

        # Horizontal rule
        if re.match(r'^(-{3,}|\*{3,}|_{3,})$', stripped):
            blocks.append({
                'kind': 'hr', 'level': None, 'heading_path': heading_path(),
                'index': idx, 'text': '---', 'html': '<hr/>',
            })
            idx += 1
            i += 1
            continue

        # Table (GFM pipe table): header line, separator line, body lines
        if '|' in stripped and i + 1 < n and re.match(r'^\s*\|?[\s:\-|]+\|?\s*$', lines[i + 1]) and '-' in lines[i + 1]:
            header_cells = [c.strip() for c in stripped.strip('|').split('|')]
            i += 2
            rows = []
            while i < n and '|' in lines[i].strip() and lines[i].strip() != '':
                rows.append([c.strip() for c in lines[i].strip().strip('|').split('|')])
                i += 1
            raw_lines = [line] + [f'row: {r}' for r in rows]
            thead = ''.join(f'<th>{render_inline(c, link_resolver)}</th>' for c in header_cells)
            tbody = ''.join(
                '<tr>' + ''.join(f'<td>{render_inline(c, link_resolver)}</td>' for c in row) + '</tr>'
                for row in rows
            )
            html_body = f'<table><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table>'
            blocks.append({
                'kind': 'table', 'level': None, 'heading_path': heading_path(),
                'index': idx, 'text': '\n'.join(raw_lines), 'html': html_body,
            })
            idx += 1
            continue

        # Blockquote
        if stripped.startswith('>'):
            quote_lines = []
            while i < n and lines[i].strip().startswith('>'):
                quote_lines.append(re.sub(r'^\s*>\s?', '', lines[i]))
                i += 1
            raw = '\n'.join(quote_lines)
            html_body = f'<blockquote>{render_inline(raw, link_resolver)}</blockquote>'
            blocks.append({
                'kind': 'blockquote', 'level': None, 'heading_path': heading_path(),
                'index': idx, 'text': raw, 'html': html_body,
            })
            idx += 1
            continue

        # List (unordered - * + / ordered 1.)
        if re.match(r'^\s*([-*+]|\d+\.)\s+', line):
            item_lines = []
            while i < n and (re.match(r'^\s*([-*+]|\d+\.)\s+', lines[i]) or
                              (lines[i].strip() != '' and lines[i].startswith((' ', '\t')) and item_lines)):
                item_lines.append(lines[i])
                i += 1
            raw = '\n'.join(item_lines)
            html_body = _render_list(item_lines, link_resolver)
            blocks.append({
                'kind': 'list', 'level': None, 'heading_path': heading_path(),
                'index': idx, 'text': raw, 'html': html_body,
            })
            idx += 1
            continue

        # Paragraph: gather consecutive non-blank, non-special lines
        para_lines = []
        while i < n and lines[i].strip() != '' and not re.match(r'^(#{1,6})\s+', lines[i].strip()) \
                and not lines[i].strip().startswith('```') \
                and not re.match(r'^\s*([-*+]|\d+\.)\s+', lines[i]) \
                and not lines[i].strip().startswith('>') \
                and not re.match(r'^(-{3,}|\*{3,}|_{3,})$', lines[i].strip()):
            para_lines.append(lines[i].strip())
            i += 1
        raw = ' '.join(para_lines)
        html_body = f'<p>{render_inline(raw, link_resolver)}</p>'
        blocks.append({
            'kind': 'paragraph', 'level': None, 'heading_path': heading_path(),
            'index': idx, 'text': raw, 'html': html_body,
        })
        idx += 1

    for b in blocks:
        b['anchor'] = _make_anchor(b['heading_path'], b['index'], b['text'])
        b['snapshot'] = b['text'][:80]
        b['norm_text'] = norm(b['text'])

    return title, blocks


def _render_list(item_lines, link_resolver):
    # Simple flat rendering with indent-based nesting by leading whitespace count // 2.
    ordered = bool(re.match(r'^\s*\d+\.\s+', item_lines[0])) if item_lines else False
    tag = 'ol' if ordered else 'ul'
    out = [f'<{tag}>']
    depth = 0
    stack_tags = [tag]
    prev_indent = 0
    for line in item_lines:
        m = re.match(r'^(\s*)([-*+]|\d+\.)\s+(.*)$', line)
        if not m:
            # continuation line of previous item (soft wrap)
            if out and out[-1].endswith('</li>'):
                out[-1] = out[-1][:-5] + ' ' + render_inline(line.strip(), link_resolver) + '</li>'
            continue
        indent, marker, content = m.groups()
        indent_level = len(indent) // 2
        is_ordered = marker[0].isdigit()
        this_tag = 'ol' if is_ordered else 'ul'
        if indent_level > prev_indent:
            out.append(f'<{this_tag}>')
            stack_tags.append(this_tag)
        elif indent_level < prev_indent:
            for _ in range(prev_indent - indent_level):
                if len(stack_tags) > 1:
                    out.append(f'</{stack_tags.pop()}>')
        out.append(f'<li>{render_inline(content, link_resolver)}</li>')
        prev_indent = indent_level
    while len(stack_tags) > 1:
        out.append(f'</{stack_tags.pop()}>')
    out.append(f'</{tag}>')
    return ''.join(out)
