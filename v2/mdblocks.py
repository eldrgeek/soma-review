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


def _esc_attr(s):
    """Escape for use inside a double-quoted HTML attribute (quotes included —
    _esc() above deliberately leaves quotes alone for text-node use, but an
    embedded `"` in an attribute value truncates it early)."""
    return _html.escape(s, quote=True)


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


_WIDGET_ATTR_RE = re.compile(r'(\w+)=(\S+)')


def parse_widget_attrs(lang_rest):
    """Parse the ` kind=passive name=inline-html`-shaped attribute tail after
    the ```widget fence marker, per the three-kind contract in
    `SOMA/shared-cognition/marked-document-widgets.md` (Mike, 2026-09-03,
    written in parallel with this build). Bare ```widget (no attributes) is
    kept working and defaults to kind=passive name=inline-html, so the simple
    form this revision ships stays valid syntax once demo/active kinds land."""
    attrs = dict(_WIDGET_ATTR_RE.findall(lang_rest or ''))
    return {
        'kind': attrs.get('kind', 'passive'),
        'name': attrs.get('name', 'inline-html'),
    }


def render_widget_block(raw_html, kind='passive', name='inline-html'):
    """Render a ```widget fenced block. This build implements the PASSIVE kind
    only (Mike's spec, 2026-09-03, item 6): "completely passive and only
    graphical" — no reads, no writes, no network. `demo` and `active` are
    specified (see the design doc referenced above) but not built here; a
    widget declaring either renders a clearly-labeled not-yet-supported
    placeholder instead of silently misbehaving or crashing page render.

    A passive/inline-html widget's fence body is raw HTML, sandboxed in an
    iframe via `srcdoc` with `sandbox="allow-scripts"` only — no
    allow-same-origin, no allow-forms, no allow-popups, no network egress —
    so it can animate or compute for display but cannot read the parent
    document, phone home, or navigate anything. Height defaults to a
    reasonable card size and can be overridden with a first line
    `<!-- height: 320 -->` in the fence body.
    """
    if kind != 'passive':
        return (
            f'<div class="widget-unsupported">Widget kind &ldquo;{_esc(kind)}&rdquo; '
            f'(name: {_esc(name)}) is not yet supported — this build ships the '
            f'passive kind only. See '
            f'<code>SOMA/shared-cognition/marked-document-widgets.md</code>.</div>'
        )
    if name != 'inline-html':
        return (
            f'<div class="widget-unsupported">Widget registry lookups '
            f'(name=&ldquo;{_esc(name)}&rdquo;) are not yet supported — only '
            f'name=inline-html renders in this build.</div>'
        )
    height = 220
    body = raw_html
    m = re.match(r'\s*<!--\s*height:\s*(\d+)\s*-->\s*\n?', raw_html)
    if m:
        height = max(60, min(2000, int(m.group(1))))
        body = raw_html[m.end():]
    srcdoc = _esc_attr(body)
    return (
        f'<div class="widget-block-frame">'
        f'<iframe class="widget-block" sandbox="allow-scripts" '
        f'referrerpolicy="no-referrer" loading="lazy" '
        f'style="width:100%;height:{height}px;border:0;display:block;" '
        f'srcdoc="{srcdoc}"></iframe>'
        f'</div>'
    )


_INLINE_LINK_RE = re.compile(r'\[([^\]]*)\]\(([^)]+)\)')
_INLINE_CODE_RE = re.compile(r'`([^`]+)`')
_BOLD_RE = re.compile(r'\*\*([^*]+)\*\*')
_ITALIC_RE = re.compile(r'(?<!\*)\*([^*]+)\*(?!\*)')
_UNDERSCORE_ITALIC_RE = re.compile(r'(?<!\w)_(?!_)(.+?)(?<!_)_(?!\w)')
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


def slugify(text):
    """GitHub-style heading slug: strip markdown markup, lowercase, spaces->hyphens,
    drop everything else that isn't a word char or hyphen. Used for heading `id`s,
    `#term-<slug>` anchors, and their cross-reference matching — same function for
    all three so a heading and a link to it always agree on the id."""
    # Strip markdown-link syntax down to the label, then markup chars.
    t = _INLINE_LINK_RE.sub(r'\1', text)
    t = re.sub(r'[`*_]', '', t)
    t = t.lower().strip()
    t = re.sub(r'[^\w\s-]', '', t)
    t = re.sub(r'[\s]+', '-', t)
    t = re.sub(r'-+', '-', t).strip('-')
    return t or 'section'


_TERM_ITEM_RE = re.compile(r'^\*\*(.+?)\*\*\s*[—–-]\s*(.*)$')


def _strip_markdown_to_text(text):
    """Best-effort plain-text rendering of a markdown fragment for use in a `title`
    attribute (no HTML tags allowed in title text — attributes only, no nested markup)."""
    t = _INLINE_LINK_RE.sub(r'\1', text)
    t = _INLINE_CODE_RE.sub(r'\1', t)
    t = _BOLD_RE.sub(r'\1', t)
    t = _ITALIC_RE.sub(r'\1', t)
    t = _UNDERSCORE_ITALIC_RE.sub(r'\1', t)
    return t.strip()


def _first_sentence(text):
    plain = _strip_markdown_to_text(text)
    m = re.search(r'^(.{1,400}?[.!?])(\s|$)', plain)
    return m.group(1) if m else plain


_LEXICON_HEADING_RE = re.compile(r'^###\s+(.+?)\s*\xb7\s*(.*)$')
_AUTO_LEXICON_MARKER_RE = re.compile(r'[ \t]*<!--\s*auto-lexicon\s*-->[ \t]*\n?')
_FRONT_MATTER_RE = re.compile(r'\A---[ \t]*\n(.*?)\n---[ \t]*\n', re.S)
_FRONT_MATTER_FLAG_RE = re.compile(r'^\s*auto-lexicon\s*:\s*(true|yes|on)\s*$', re.I | re.M)
_LEADING_FRONT_MATTER_RE = re.compile(
    r'\A\s*(?:<!--.*?-->\s*)*---[ \t]*\n(.*?)\n---[ \t]*\n', re.S
)


def strip_front_matter(src):
    """Remove a leading YAML-ish front-matter block (`---\\nkey: value\\n---\\n`)
    from markdown before block parsing, regardless of a leading HTML comment
    before it (e.g. the `<!-- auto-lexicon -->` opt-in marker, or any other
    comment). Front matter across the estate is metadata for THIS app
    (`server.py::compute_level` / `compute_default_view` read `level:`/
    `view:` keys) — it was never meant to render as document text. Before
    this, any page with a leading front-matter block rendered it as a literal
    `<hr>`/paragraph/`<hr>` sandwich in the body (the mdp-proposal.md leak,
    found 2026-09-03: `level: "meta: how MDP works" view: "v3"` showing under
    the title). Call this AFTER `strip_auto_lexicon_marker` so its own
    flag-scoped front-matter handling still runs first; this catches whatever
    front matter remains (plain front matter with no auto-lexicon
    association at all, or front matter that only had its marker comment
    stripped). A doc with no leading front-matter block is returned
    byte-identical."""
    m = _LEADING_FRONT_MATTER_RE.match(src)
    if m:
        return src[m.end():]
    return src


def stripped_src_and_prefix_len(src):
    """Mirror `parse_markdown`'s own leading-metadata stripping (NFC normalize,
    then `strip_auto_lexicon_marker`, then `strip_front_matter`) and additionally
    return how many leading characters of the NORMALIZED source were removed.

    Used by merge-on-accept (`server.py::apply_block_merge`) to translate a
    block's `line_start`/`line_end` (recorded against the stripped, line-split
    source `parse_markdown` actually iterates) back into an absolute character
    offset in the real file text, so an inline edit/replace mark can be applied
    to the file on disk without re-deriving the block's location from its
    rendered `text` (which for paragraphs is space-joined and has already lost
    the original line breaks).

    Assumes — as every real page does today — that all removed material sits
    at the very front of the document; a marker or front-matter block
    appearing only after real content is out of scope (documented limitation,
    matches `strip_front_matter`'s own docstring: it is a *leading*-block
    stripper).
    """
    normalized = unicodedata.normalize('NFC', src)
    _, after_marker = strip_auto_lexicon_marker(normalized)
    after_fm = strip_front_matter(after_marker)
    prefix_len = len(normalized) - len(after_fm)
    return normalized, after_fm, prefix_len


_SENTENCE_ABBREVIATIONS = {
    'e.g', 'i.e', 'etc', 'vs', 'mr', 'mrs', 'dr', 'ms', 'jr', 'sr',
    'inc', 'ltd', 'no', 'st', 'approx', 'fig', 'vol', 'op', 'cf',
}


def segment_sentences(text):
    """Split `text` into sentences on `.`/`?`/`!` followed by whitespace-or-end
    (MDP mechanics item C, 2026-09-03: "unit of editing is the SENTENCE, not
    the block/paragraph"). A simple rule, not a full NLP sentence-boundary
    detector: an ending punctuation run is a boundary unless the word
    immediately before it is a known abbreviation (`e.g.`, `i.e.`, `etc.`,
    `Mr.`, ...) or a single capital letter (an initial, e.g. "J. Smith").

    Returns a list of `(start, end, sentence_text)` character spans that
    exactly tile `text` with no gaps or overlaps — `''.join(t for _, _, t in
    segment_sentences(text)) == text` always holds, which is what lets caller
    code (server.py's sentence-level change/revert) splice a replacement in
    by character offset and trust the rest of the block is untouched.
    """
    if not text:
        return []
    n = len(text)
    starts = [0]
    i = 0
    while i < n:
        ch = text[i]
        if ch in '.!?':
            j = i
            while j < n and text[j] in '.!?':
                j += 1
            k = i
            # Walk back through letters AND internal periods so a multi-dot
            # abbreviation like "e.g." is seen as one token ("e.g") rather
            # than being checked one dot at a time (which would see "e" then
            # "g" and miss the abbreviation on its second, real, boundary).
            while k > 0 and (text[k - 1].isalnum() or text[k - 1] == '.'):
                k -= 1
            word = text[k:i].strip('.')
            is_abbrev = (
                word.lower() in _SENTENCE_ABBREVIATIONS
                or (len(word) == 1 and word.isupper())
            )
            boundary_ok = j >= n or text[j] in ' \t\n'
            if boundary_ok and not is_abbrev:
                starts.append(j)
            i = j
            continue
        i += 1
    starts.append(n)
    starts = sorted(set(s for s in starts if 0 <= s <= n))
    spans = []
    for a, b in zip(starts, starts[1:]):
        if a == b:
            continue
        spans.append((a, b, text[a:b]))
    return spans


def block_source_span(raw_src, block):
    """Locate a block's exact original source slice in `raw_src` (the real file
    content, as read from disk) using the block's `line_start`/`line_end`
    (0-indexed, end-exclusive, set by `parse_markdown` on every block).

    Returns `(normalized_src, start_char, end_char, exact_text)` where
    `normalized_src` is `raw_src` after NFC normalization (the coordinate
    space `start_char`/`end_char` are in — see `stripped_src_and_prefix_len`),
    and `exact_text` is `normalized_src[start_char:end_char]` — the block's
    real original text INCLUDING any internal newlines (unlike the block's
    `text` field, which is space-joined for paragraphs).

    Raises KeyError if the block carries no `line_start`/`line_end` (e.g. a
    block dict built by a caller other than `parse_markdown`).
    """
    line_start = block['line_start']
    line_end = block['line_end']
    normalized, stripped, prefix_len = stripped_src_and_prefix_len(raw_src)
    lines = stripped.split('\n')
    exact_text = '\n'.join(lines[line_start:line_end])
    start_char = prefix_len + sum(len(l) + 1 for l in lines[:line_start])
    end_char = start_char + len(exact_text)
    return normalized, start_char, end_char, exact_text


def strip_auto_lexicon_marker(src):
    """Detect the per-page auto-lexicon opt-in and remove the marker from the
    rendered source so it never shows up as visible text (same treatment normal
    markdown renderers give front matter).

    Two forms, either enables it: a front-matter block containing `auto-lexicon:
    true` (stripped only when that key is present, so an ordinary `---` divider
    at the top of a doc is never touched), or an `<!-- auto-lexicon -->` HTML
    comment anywhere in the document (stripped in place).

    Returns (auto_lexicon: bool, cleaned_src). A doc with neither marker returns
    (False, src) UNCHANGED — this is what keeps every pre-existing page
    byte-identical: the opt-in is off by default and costs nothing until used.
    """
    auto = False
    fm = _FRONT_MATTER_RE.match(src)
    if fm and _FRONT_MATTER_FLAG_RE.search(fm.group(1)):
        auto = True
        src = src[fm.end():]
    if _AUTO_LEXICON_MARKER_RE.search(src):
        auto = True
        src = _AUTO_LEXICON_MARKER_RE.sub('', src, count=1)
    return auto, src


def build_lexicon_index(md_text):
    """Parse the SOMA Lexicon's markdown into a lookup index.

    Entries are `### Term · gloss` headings followed eventually by a
    `**What we mean.**` paragraph (some entries have a `**Plain reading.**`
    paragraph first — skipped, not the definition). Aliases: when the heading's
    TERM segment (the part before `·`) lists multiple names separated by
    ` / ` (e.g. `Pulse / Pulse Core`, `SCS / SAIS`, `$ARR / DARR`), each name is
    a separate alias for the same entry, all resolving to one slug (the first
    alias, slugified).

    Returns {'by_slug': {slug: entry}, 'by_alias': {alias_lower: slug}} where
    entry = {'term', 'slug', 'gloss', 'first_sentence', 'aliases', 'anchor'}.
    `anchor` is the id the real rendered lexicon page gives this heading
    (slugify() of the FULL heading text, matching parse_markdown's own
    heading-id scheme) — used for the "full entry" deep link, distinct from
    `slug` (identifier keyed off the primary alias only, used for lookup).
    """
    lines = md_text.split('\n')
    n = len(lines)
    by_slug = {}
    by_alias = {}
    i = 0
    while i < n:
        m = _LEXICON_HEADING_RE.match(lines[i].strip())
        if not m:
            i += 1
            continue
        term_part, gloss = m.group(1).strip(), m.group(2).strip()
        anchor = slugify(f'{term_part} \xb7 {gloss}')
        aliases = [a.strip() for a in term_part.split(' / ') if a.strip()]
        if not aliases:
            i += 1
            continue
        primary = aliases[0]
        slug = slugify(primary)
        # Find the "What we mean." paragraph: scan forward until the next
        # heading, collecting paragraph text, and take the first one that
        # starts with that marker (skips a leading "Plain reading." aside).
        i += 1
        def_raw = ''
        para = []
        while i < n and not re.match(r'^#{1,6}\s+', lines[i].strip()):
            stripped = lines[i].strip()
            if stripped == '':
                joined = ' '.join(para).strip()
                if joined.lower().startswith('**what we mean.**'):
                    def_raw = joined[len('**what we mean.**'):].strip()
                    break
                para = []
            else:
                para.append(stripped)
            i += 1
        else:
            joined = ' '.join(para).strip()
            if joined.lower().startswith('**what we mean.**'):
                def_raw = joined[len('**what we mean.**'):].strip()
        if not def_raw:
            def_raw = gloss  # fallback: no "What we mean." paragraph found
        entry = {
            'term': primary, 'slug': slug, 'gloss': gloss,
            'first_sentence': _first_sentence(def_raw), 'aliases': aliases,
            'anchor': anchor,
        }
        by_slug[slug] = entry
        for alias in aliases:
            by_alias[alias.lower()] = slug
        # continue outer scan from wherever the inner walk left off (don't
        # re-advance past a heading line we need the outer loop to see)
    return {'by_slug': by_slug, 'by_alias': by_alias}


def _lookup_lexicon(key, lexicon):
    """Resolve `key` (a slug, an alias, or free text) against a lexicon index.
    Tries, in order: exact slug, lowercase alias, slugify(key)."""
    if not lexicon or not key:
        return None
    key = key.strip()
    if not key:
        return None
    if key in lexicon['by_slug']:
        return lexicon['by_slug'][key]
    alias_slug = lexicon['by_alias'].get(key.lower())
    if alias_slug:
        return lexicon['by_slug'][alias_slug]
    guess = slugify(key)
    if guess in lexicon['by_slug']:
        return lexicon['by_slug'][guess]
    return None


def _find_lexicon_by_label(label, lexicon):
    """Same fuzzy label match as _find_term_by_label(), against the lexicon's
    alias set instead of a page-local terms table."""
    if not lexicon or not label:
        return None
    label_l = label.strip().lower()
    if not label_l:
        return None
    if label_l in lexicon['by_alias']:
        return lexicon['by_slug'][lexicon['by_alias'][label_l]]
    for alias_l, slug in lexicon['by_alias'].items():
        if label_l in alias_l or alias_l in label_l:
            return lexicon['by_slug'][slug]
    return None


def extract_terms(src, link_resolver=None, lexicon=None):
    """Scan raw markdown for a "Terms" heading (any level, exact text match,
    case-insensitive) and the bullet-list definitions under it, of the form
    `**term** — definition`. Returns {slug: {'term', 'slug', 'def_raw',
    'first_sentence', 'html'}} keyed by slugify(term). `html` is the definition
    rendered through render_inline with this same terms map threaded in, so a
    definition that itself links to another term produces a nested term-link
    (recursion) — safe because it only needs the *existence* of other slugs/terms,
    not their own rendered html, to resolve a nested reference.
    """
    lines = src.split('\n')
    n = len(lines)
    i = 0
    terms_level = None
    terms = {}
    while i < n:
        m = re.match(r'^(#{1,6})\s+(.*)$', lines[i].strip())
        if m and m.group(2).strip().lower() == 'terms':
            terms_level = len(m.group(1))
            i += 1
            break
        i += 1
    if terms_level is None:
        return {}

    while i < n:
        stripped = lines[i].strip()
        hm = re.match(r'^(#{1,6})\s+', stripped)
        if hm and len(hm.group(1)) <= terms_level:
            break  # next sibling/ancestor heading ends the Terms section
        item_m = re.match(r'^\s*([-*+])\s+(.*)$', lines[i])
        if item_m:
            item_lines = [item_m.group(2)]
            i += 1
            while i < n and lines[i].strip() != '' and not re.match(r'^\s*([-*+]|\d+\.)\s+', lines[i]) \
                    and not re.match(r'^#{1,6}\s+', lines[i].strip()):
                item_lines.append(lines[i].strip())
                i += 1
            raw = ' '.join(item_lines)
            term_m = _TERM_ITEM_RE.match(raw)
            if term_m:
                term_label, def_raw = term_m.group(1).strip(), term_m.group(2).strip()
                slug = slugify(term_label)
                terms[slug] = {'term': term_label, 'slug': slug, 'def_raw': def_raw}
            continue
        i += 1

    # Second pass: render each definition's html + first-sentence now that the full
    # term/slug table exists (needed so nested `#terms`/`#term-x` links inside a
    # definition resolve correctly).
    for entry in terms.values():
        entry['first_sentence'] = _first_sentence(entry['def_raw'])
        entry['html'] = render_inline(entry['def_raw'], link_resolver, terms=terms, lexicon=lexicon)
    return terms


def _find_term_by_label(label, terms):
    """Match a link's visible label text against the terms table. Exact match wins;
    otherwise accept a term that contains the label or a label that contains the
    term (case-insensitive) — e.g. link text "Path 0" matches the defined term
    "Path 0 / Path A / Path B"."""
    if not terms:
        return None
    label_l = label.strip().lower()
    if not label_l:
        return None
    for entry in terms.values():
        if entry['term'].strip().lower() == label_l:
            return entry
    for entry in terms.values():
        term_l = entry['term'].strip().lower()
        if label_l in term_l or term_l in label_l:
            return entry
    return None


def render_inline(text, link_resolver=None, terms=None, lexicon=None, auto_lexicon=False, auto_seen=None,
                  auto_local_terms=False):
    """Render inline markdown (links, bold, italic, code) to HTML.
    link_resolver(href) -> href_out, is_internal
    `terms`, if given, is the {slug: {...}} table from extract_terms(); links whose
    target resolves to `#terms` (matched by label) or `#term-<slug>` (matched by
    slug) render as `.term-link`s carrying `data-def`/`data-term-slug` for the
    hover-popover JS, plus a plain `title` fallback for non-JS hover.

    `lexicon`, if given, is a {'by_slug', 'by_alias'} index from
    build_lexicon_index() — the SOMA Lexicon fallback. It resolves three things
    a page's own `terms` table can't: (1) a `#terms`/`#term-<slug>` link the
    page-local table has no entry for, (2) the lightweight `lexicon:<term>` href
    scheme, (3) a `#lex-<slug>` anchor. Page-local terms always win when both
    would match. Lexicon term-links are namespaced `lex-<slug>` in
    `data-term-slug` (vs. a bare `<slug>` for page-local terms) so the two
    tables never collide in the client's `__TERM_DEFS__` map.

    `auto_lexicon`, if true (and `lexicon` given), additionally wraps the FIRST
    occurrence of each lexicon term found in this call's plain text as a
    term-link — this is the opt-in automatic-linking mode (see
    strip_auto_lexicon_marker()). Caller is responsible for never passing
    auto_lexicon=True for headings or Terms-section content (this function has
    no notion of "which block/section is this").

    `auto_seen`, if given, is a `set()` the caller owns and reuses across
    multiple render_inline() calls that together make up one logical paragraph
    (e.g. the Mark Layer's one-call-per-sentence rendering) -- without it,
    "first occurrence" would reset per call instead of per paragraph. Pass the
    SAME set instance across those calls; this function only ever adds to it.
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
        is_lexicon_scheme = href.startswith('lexicon:')
        href_out, cls, extra = ('#lex-pending', 'internal-link', '') if is_lexicon_scheme else _resolve(href)
        term_entry = None
        frag = None
        if not is_lexicon_scheme:
            if '#' in href_out:
                frag = href_out.rsplit('#', 1)[1].lower()
            elif '#' in href:
                frag = href.rsplit('#', 1)[1].lower()
        if terms:
            if frag == 'terms':
                term_entry = _find_term_by_label(label, terms)
            elif frag and frag.startswith('term-'):
                term_entry = terms.get(frag[len('term-'):])
        lex_entry = None
        if term_entry is None and lexicon:
            if is_lexicon_scheme:
                key = href[len('lexicon:'):]
                lex_entry = _lookup_lexicon(key, lexicon) or _find_lexicon_by_label(label, lexicon)
            elif frag == 'terms':
                lex_entry = _find_lexicon_by_label(label, lexicon)
            elif frag and frag.startswith('term-'):
                lex_entry = _lookup_lexicon(frag[len('term-'):], lexicon)
            elif frag and frag.startswith('lex-'):
                lex_entry = _lookup_lexicon(frag[len('lex-'):], lexicon)
        if term_entry:
            # Always an in-page fragment jump — never target=_blank even if the
            # generic resolver would have called this an external link.
            def_esc = _esc_attr(term_entry['first_sentence'])
            return stash(
                f'<a href="{_esc(href_out)}" class="term-link" data-term-slug="{_esc_attr(term_entry["slug"])}"'
                f' data-def="{def_esc}" title="{def_esc}">{label_html}</a>'
            )
        if lex_entry:
            def_esc = _esc_attr(lex_entry['first_sentence'])
            return stash(
                f'<a href="#lex-{_esc_attr(lex_entry["slug"])}" class="term-link"'
                f' data-term-slug="lex-{_esc_attr(lex_entry["slug"])}"'
                f' data-def="{def_esc}" title="{def_esc}">{label_html}</a>'
            )
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

    # Opt-in automatic lexicon linking: wrap the FIRST occurrence of each
    # lexicon term still present as plain text (code spans, existing links, and
    # bare URLs are already stashed to \x00N\x00 placeholders above, so this
    # regex can never reach inside them). Multi-word aliases match case-
    # insensitively; single-word aliases (capitalized coinages like SOMA,
    # Yeshie, Pulse) match case-sensitively, so lowercase everyday uses of the
    # same word are left alone.
    #
    # A page's OWN Terms section links at first use unconditionally (Mike's
    # ruling, 2026-09-03: "define every term the page uses — Terms section,
    # linked at first use"). Before 2026-09-04 the alternation was built from
    # lexicon aliases only, so a page-local term was linked only where it
    # happened to collide with a lexicon alias: `mdp-agreed-model.md` defined
    # 21 terms and linked 1. Page-local labels are now their own aliases, and
    # they are matched case-INSENSITIVELY even when single-word, because a
    # page-local term is a coinage this page is defining ("trunk", "bracket",
    # "fold") rather than an estate-wide capitalized name — the case-sensitive
    # rule below exists to protect everyday words from the LEXICON, and a page
    # that wrote the definition itself has already opted in.
    lexicon_aliases = (
        {alias for slug, entry in lexicon['by_slug'].items() for alias in entry['aliases']}
        if (auto_lexicon and lexicon and lexicon.get('by_alias')) else set()
    )
    # A local label may name several forms ("Watch-only / armed", "Path 0 / Path A"),
    # the same ' / ' convention build_lexicon_index() uses for lexicon aliases. Split
    # it, or the alternation only ever matches the literal joined string, which
    # appears nowhere — so the terms with the MOST names got zero links.
    # All-digit labels are dropped: the alternation runs while stashed code spans and
    # links are still \x00N\x00 placeholders, and \x00 is a non-word char, so a bare
    # number would match a placeholder INDEX and shred the output.
    local_aliases = set()
    if auto_local_terms:
        for e in (terms or {}).values():
            for alias in str(e.get('term') or '').split(' / '):
                alias = alias.strip()
                if alias and not alias.isdigit():
                    local_aliases.add(alias)
    if lexicon_aliases or local_aliases:
        all_aliases = sorted(lexicon_aliases | local_aliases, key=len, reverse=True)
        if all_aliases:
            parts = []
            for alias in all_aliases:
                esc = re.escape(alias)
                if alias in local_aliases:
                    # `\b` treats `-` as a boundary, so `\bround\b` matches inside
                    # "round-trip" and hovers a review-round definition over a
                    # serializer test. A page-local coinage links only as a free
                    # word, never as a limb of a hyphenated compound.
                    parts.append(f'(?i:(?<![\\w-]){esc}(?![\\w-]))')
                elif ' ' in alias:
                    parts.append(f'(?i:\\b{esc}\\b)')
                else:
                    parts.append(f'\\b{esc}\\b')
            auto_re = re.compile('|'.join(parts))
            seen_slugs = auto_seen if auto_seen is not None else set()

            def auto_sub(m):
                matched = m.group(0)
                # Page-local Terms still win, same rule as the explicit
                # #terms/#term-<slug> fallback above: if this page defines its
                # own meaning for the matched word, link to that instead of
                # the lexicon's.
                # EXACT (case-insensitive) label match only. _find_term_by_label's
                # containment fallback is correct for an explicit `[label](#terms)`
                # link and wrong here: the lexicon alias "bracketed assent" — the
                # phrase Mike actually ruled on — contains the local label
                # "bracket", so containment handed the wrong definition to the
                # longer, more specific phrase.
                matched_l = matched.strip().lower()
                local_entry = next(
                    (e for e in (terms or {}).values()
                     if any(a.strip().lower() == matched_l
                            for a in str(e.get('term') or '').split(' / '))),
                    None,
                )
                if local_entry:
                    seen_key = f'local:{local_entry["slug"]}'
                    if seen_key in seen_slugs:
                        return matched
                    seen_slugs.add(seen_key)
                    def_esc = _esc_attr(local_entry['first_sentence'])
                    return stash(
                        f'<a href="#term-{_esc_attr(local_entry["slug"])}" class="term-link"'
                        f' data-term-slug="{_esc_attr(local_entry["slug"])}"'
                        f' data-def="{def_esc}" title="{def_esc}">{_esc(matched)}</a>'
                    )
                entry = _lookup_lexicon(matched, lexicon)
                if entry is None or entry['slug'] in seen_slugs:
                    return matched
                seen_slugs.add(entry['slug'])
                def_esc = _esc_attr(entry['first_sentence'])
                return stash(
                    f'<a href="#lex-{_esc_attr(entry["slug"])}" class="term-link"'
                    f' data-term-slug="lex-{_esc_attr(entry["slug"])}"'
                    f' data-def="{def_esc}" title="{def_esc}">{_esc(matched)}</a>'
                )

            text = auto_re.sub(auto_sub, text)

    # Escape whatever plain text remains (placeholders are \x00N\x00 — no HTML-special
    # chars, so escaping is a no-op on them and safe to run after stashing).
    text = _esc(text)
    text = _BOLD_RE.sub(r'<strong>\1</strong>', text)
    text = _ITALIC_RE.sub(r'<em>\1</em>', text)
    text = _UNDERSCORE_ITALIC_RE.sub(r'<em>\1</em>', text)

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


def parse_markdown(src, link_resolver=None, terms_out=None, lexicon=None):
    """Return (title, blocks) where blocks is a list of dicts (see module docstring).

    `terms_out`, if passed a dict, is populated with the page's {slug: {term, html,
    first_sentence}} terms table (see extract_terms()) — used by server.py to embed a
    JSON map for the hover-popover JS. Optional and backward-compatible: existing
    callers that only unpack `(title, blocks)` are unaffected.

    `lexicon`, if given, is the SOMA Lexicon index from build_lexicon_index(). It is
    always available as a fallback for `#terms`/`#term-<slug>`/`lexicon:<term>`/
    `#lex-<slug>` links regardless of the auto-lexicon opt-in below — that opt-in
    only controls the SEPARATE automatic-first-occurrence-linking behavior.
    Automatic linking additionally requires the page to opt in via a front-matter
    `auto-lexicon: true` flag or an `<!-- auto-lexicon -->` marker comment (see
    strip_auto_lexicon_marker()) — default off, so no pre-existing page changes a
    byte until it opts in. Even when opted in, headings and the page's own Terms
    section are never auto-linked.
    """
    src = unicodedata.normalize('NFC', src)
    auto_lexicon_flag, src = strip_auto_lexicon_marker(src)
    src = strip_front_matter(src)
    lines = src.split('\n')
    blocks = []
    heading_stack = []  # list of (level, text)
    i = 0
    n = len(lines)
    idx = 0
    title = None
    terms = extract_terms(src, link_resolver, lexicon=lexicon)
    if terms_out is not None:
        terms_out.update(terms)
    used_ids = set()
    terms_section_level = [None]  # boxed so the closure below can mutate it

    def auto_lex_now():
        """Whether the block currently being rendered may be auto-linked: opt-in
        flag set, a lexicon is available, and we're not inside the page's own
        Terms section. Headings never call this — they're excluded unconditionally
        at the call site."""
        return bool(auto_lexicon_flag and lexicon and terms_section_level[0] is None)

    def auto_local_now():
        """Whether the block currently being rendered may auto-link the page's OWN
        Terms entries. Unlike auto_lex_now() this needs no opt-in marker — a page
        that wrote a Terms section has already opted in by writing it (Mike,
        2026-09-03: terms are "linked at first use") — but it obeys the same
        rule that the Terms section never links itself."""
        return bool(terms and terms_section_level[0] is None)

    def make_id(candidate):
        base = candidate
        n_dupe = 2
        while candidate in used_ids:
            candidate = f'{base}-{n_dupe}'
            n_dupe += 1
        used_ids.add(candidate)
        return candidate

    def claim_id(id_str):
        """Register a fixed id (term-<slug> anchors) that must match exactly what
        the terms table expects for `#term-<slug>` links to resolve — never
        renamed on collision, just reserved so a later heading doesn't reuse it."""
        used_ids.add(id_str)
        return id_str

    def heading_path():
        return [t for (_, t) in heading_stack]

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if stripped == '':
            i += 1
            continue

        # Line-span of the block about to be parsed, in terms of `lines` (the
        # stripped/normalized source split on '\n') — recorded on every block
        # below as `line_start`/`line_end` (end exclusive) so file-writing code
        # (merge-on-accept, server.py) can locate and replace a block's exact
        # original source lines without re-deriving them from the rendered
        # `text` field (which for paragraphs is space-joined and has already
        # lost the original line breaks).
        blk_line_start = i

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
            elif lang == 'widget' or lang.startswith('widget '):
                w_attrs = parse_widget_attrs(lang[len('widget'):])
                html_body = render_widget_block(raw, kind=w_attrs['kind'], name=w_attrs['name'])
                kind = 'widget'
            else:
                html_body = f'<pre><code class="lang-{_esc(lang)}">{_esc(raw)}</code></pre>'
                kind = 'code'
            blocks.append({
                'line_start': blk_line_start, 'line_end': i,
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
            # Track whether we're inside the page's own "Terms" section (any
            # level, exact case-insensitive match, same rule extract_terms()
            # uses) so prose blocks below know to skip auto-linking there.
            if text.strip().lower() == 'terms':
                terms_section_level[0] = level
            elif terms_section_level[0] is not None and level <= terms_section_level[0]:
                terms_section_level[0] = None
            heading_id = make_id(slugify(text))
            html_body = f'<h{level} id="{heading_id}">{render_inline(text, link_resolver, terms=terms, lexicon=lexicon)}</h{level}>'
            i += 1
            blocks.append({
                'line_start': blk_line_start, 'line_end': i,
                'kind': 'heading', 'level': level, 'heading_path': heading_path()[:-1],
                'index': idx, 'text': text, 'html': html_body, 'heading_id': heading_id,
            })
            idx += 1
            continue

        # Horizontal rule
        if re.match(r'^(-{3,}|\*{3,}|_{3,})$', stripped):
            i += 1
            blocks.append({
                'line_start': blk_line_start, 'line_end': i,
                'kind': 'hr', 'level': None, 'heading_path': heading_path(),
                'index': idx, 'text': '---', 'html': '<hr/>',
            })
            idx += 1
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
            _al = auto_lex_now()
            _alt = auto_local_now()
            thead = ''.join(
                f'<th>{render_inline(c, link_resolver, terms=terms, lexicon=lexicon, auto_lexicon=_al, auto_local_terms=_alt)}</th>'
                for c in header_cells
            )
            tbody = ''.join(
                '<tr>' + ''.join(
                    f'<td>{render_inline(c, link_resolver, terms=terms, lexicon=lexicon, auto_lexicon=_al, auto_local_terms=_alt)}</td>'
                    for c in row
                ) + '</tr>'
                for row in rows
            )
            html_body = f'<table><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table>'
            blocks.append({
                'line_start': blk_line_start, 'line_end': i,
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
            html_body = (
                f'<blockquote>'
                f'{render_inline(raw, link_resolver, terms=terms, lexicon=lexicon, auto_lexicon=auto_lex_now(), auto_local_terms=auto_local_now())}'
                f'</blockquote>'
            )
            blocks.append({
                'line_start': blk_line_start, 'line_end': i,
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
            html_body = _render_list(
                item_lines, link_resolver, terms=terms, claim_id=claim_id,
                lexicon=lexicon, auto_lexicon=auto_lex_now(), auto_local_terms=auto_local_now(),
            )
            blocks.append({
                'line_start': blk_line_start, 'line_end': i,
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
        html_body = f'<p>{render_inline(raw, link_resolver, terms=terms, lexicon=lexicon, auto_lexicon=auto_lex_now(), auto_local_terms=auto_local_now())}</p>'
        blocks.append({
            'line_start': blk_line_start, 'line_end': i,
            'kind': 'paragraph', 'level': None, 'heading_path': heading_path(),
            'index': idx, 'text': raw, 'html': html_body,
        })
        idx += 1

    for b in blocks:
        b['anchor'] = _make_anchor(b['heading_path'], b['index'], b['text'])
        b['snapshot'] = b['text'][:80]
        b['norm_text'] = norm(b['text'])

    return title, blocks


def _render_list(item_lines, link_resolver, terms=None, claim_id=None, lexicon=None, auto_lexicon=False,
                 auto_local_terms=False):
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
                out[-1] = out[-1][:-5] + ' ' + render_inline(
                    line.strip(), link_resolver, terms=terms, lexicon=lexicon, auto_lexicon=auto_lexicon,
                    auto_local_terms=auto_local_terms,
                ) + '</li>'
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
        # A definition-list-shaped item ("**term** — definition") gets an
        # addressable `id="term-<slug>"` so `#term-<slug>` links resolve here,
        # same slug function extract_terms() used to build the terms table.
        li_id_attr = ''
        term_m = _TERM_ITEM_RE.match(content)
        if term_m and claim_id is not None:
            term_id = claim_id(f'term-{slugify(term_m.group(1).strip())}')
            li_id_attr = f' id="{term_id}"'
        out.append(
            f'<li{li_id_attr}>'
            f'{render_inline(content, link_resolver, terms=terms, lexicon=lexicon, auto_lexicon=auto_lexicon, auto_local_terms=auto_local_terms)}'
            f'</li>'
        )
        prev_indent = indent_level
    while len(stack_tags) > 1:
        out.append(f'</{stack_tags.pop()}>')
    out.append(f'</{tag}>')
    return ''.join(out)
