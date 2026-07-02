"""
mdblocks.py — a small, dependency-free Markdown -> block-list parser + HTML renderer.

Not a full CommonMark implementation. Handles the subset the estate's docs actually
use: ATX headings, paragraphs, unordered/ordered lists (incl. nested by indent),
GFM-style pipe tables, fenced code blocks, blockquotes, hr, and inline emphasis/
code/links. Good enough for rendering + block-level comment anchoring.

Each parsed block gets:
  - kind: heading|paragraph|list|table|code|blockquote|hr
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


def _esc(s):
    return _html.escape(s, quote=False)


_INLINE_LINK_RE = re.compile(r'\[([^\]]*)\]\(([^)]+)\)')
_INLINE_CODE_RE = re.compile(r'`([^`]+)`')
_BOLD_RE = re.compile(r'\*\*([^*]+)\*\*')
_ITALIC_RE = re.compile(r'(?<!\*)\*([^*]+)\*(?!\*)')


def render_inline(text, link_resolver=None):
    """Render inline markdown (links, bold, italic, code) to HTML.
    link_resolver(href) -> href_out, is_internal
    """
    # Protect inline code spans first so we don't mangle markup inside them.
    placeholders = []

    def stash_code(m):
        placeholders.append(f'<code>{_esc(m.group(1))}</code>')
        return f'\x00{len(placeholders) - 1}\x00'

    text = _INLINE_CODE_RE.sub(stash_code, text)

    def link_sub(m):
        label, href = m.group(1), m.group(2)
        label_html = _esc(label)
        if link_resolver:
            href_out, internal = link_resolver(href)
        else:
            href_out, internal = href, False
        cls = ' class="internal-link"' if internal else ' class="external-link" target="_blank" rel="noopener"'
        return f'<a href="{_esc(href_out)}"{cls}>{label_html}</a>'

    text = _esc(text)
    # _esc already ran; but we escaped before substituting links so re-do link matching
    # on the escaped text is wrong because []( ) survive escaping fine (no special html chars).
    text = _INLINE_LINK_RE.sub(link_sub, text)
    text = _BOLD_RE.sub(r'<strong>\1</strong>', text)
    text = _ITALIC_RE.sub(r'<em>\1</em>', text)

    for i, ph in enumerate(placeholders):
        text = text.replace(f'\x00{i}\x00', ph)
    return text


def _make_anchor(heading_path, index, text):
    key = '>'.join(heading_path) + f'#{index}:' + text[:40]
    h = hashlib.sha1(key.encode('utf-8')).hexdigest()[:10]
    return f'b{index}-{h}'


def parse_markdown(src, link_resolver=None):
    """Return (title, blocks) where blocks is a list of dicts (see module docstring)."""
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

        # Fenced code block
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
            html_body = f'<pre><code class="lang-{_esc(lang)}">{_esc(raw)}</code></pre>'
            blocks.append({
                'kind': 'code', 'level': None, 'heading_path': heading_path(),
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
