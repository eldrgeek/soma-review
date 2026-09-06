"""Python-side emitter of the shared mark-layer node/fragment model.

SOMA agreed model item 6a: "Playmaker and soma-review are to share one
engine, Playmaker's, with soma-review supplying blocks and the mark record."
Playmaker's JS side (`playmaker/src/mark-layer-engine/adapters/proseMarkdown.ts`)
already defines the target shape (`MarkLayerNode`/`MarkLayerFragment`,
`playmaker/src/mark-layer-engine/types.ts`) and a `fromProseMarkdown` adapter
that ported soma-review's OWN `segment_sentences`/`blockmap.norm` into JS.
This module is the missing other half: soma-review emitting that same
node/fragment shape natively, in Python, from the sentence-segmentation logic
it already owns (`mdblocks.segment_sentences`, `blockmap.norm`) — the
prerequisite named in `playmaker/docs/MARK-LAYER-ENGINE.md` "Next" item 1
("soma-review consumption") before the live block parser can be rewired to
produce it.

Write-side beside-step (2026-09-06): `POST /api/comments` type=mark now
calls `attach_mark_layer_node_ids` so a new mark can carry the matching
node id. UI may display/jump via that id; the live comment/mark *read*
path still uses the old block-parser / `mark_layer_inner` / v3 path — this
module is not the default renderer.
Wiring `server.py`'s actual block-parse response to emit this shape is the
cutover (agreed-model 6a + item 15), still open.

**Fixed 2026-09-05 (mission-1, same day as the module's first slice): node
ids are now content-derived, not a call-scoped counter.** `_next_id` used to
draw from a module-global `itertools.count`, so calling `to_mark_layer_nodes`
twice on identical text minted different ids both times — the same flaw the
JS `fromProseMarkdown` still has (`let counter = 0` at module scope; **not
yet ported to JS**, named below rather than silently left inconsistent).
This repo already paid for the general shape of this mistake once (Anchoring
v2 replaced renderer-order comment anchors with stable content-hash block
ids after non-stable ids broke reconciliation across re-renders). Every id is
now `{prefix}-{sha1(kind:text)[:10]}`, with a `-{n}` suffix appended only for
the 2nd/3rd/... node sharing the exact same `(prefix, text)` within one call
— same content, unchanged position among its duplicates, same id across
repeated calls on identical text; ids stay unique within a single call even
when the same sentence/paragraph/blank text repeats verbatim (an empty line
plus a doc with two literally identical sentences are both real cases in
this corpus). This is the property `to_mark_layer_nodes` must have before
any client-side diffing (unchanged text between two parses of the same
document maps to the same node id) — see
`test_repeated_calls_on_identical_text_reuse_ids` and
`test_duplicate_text_within_one_call_gets_distinct_ids`.

**Closed the same day (mission-1, second pass): the JS adapter
(`fromProseMarkdown`, `playmaker/src/mark-layer-engine/adapters/proseMarkdown.ts`)
now ports the identical `sha1(prefix:text)[:10]` scheme** (a vendored,
synchronous SHA-1 — this file runs in the browser, so neither Node's
`crypto` nor Web Crypto's async `subtle.digest` fit a sync parse). Verified
with a pinned cross-engine test (`tests/mark-layer-engine-extraction.test.ts`)
that runs both engines on `'Alpha is first. Beta is second.'` and asserts
the literal id strings match — not just that both use sha1, but that they
mint the SAME ids on the SAME input.

**Named, not fixed, by the same pass (Skip's adversarial review, 2026-09-05):
the fix above is real but narrower than "stable for diffing" implies — it
only holds for re-parsing an UNMODIFIED document.** The occurrence-index
disambiguation (`-{n}` suffix) is assigned by each duplicate's position
*among its own duplicates in document order*, not by anything intrinsic to
that node. Concretely: if a document has two paragraphs with identical text
("Ready.") at positions 1 and 5, they get ids `pmpara-X` and `pmpara-X-1`.
Insert a THIRD identical "Ready." paragraph before position 1, and the old
`pmpara-X` (unchanged text, unchanged position relative to itself) becomes
the second occurrence and gets reassigned to `pmpara-X-1`; the old
`pmpara-X-1` shifts to `pmpara-X-2`. A client diffing v1 against v2 by id
would read two untouched nodes as deleted-and-recreated, purely because an
unrelated sibling with the same text was inserted earlier in the doc. This
is the same failure SHAPE Anchoring v2 exists to prevent (`blockmap.py`),
one layer down: content-hash ids are stable, but the occurrence COUNTER
layered on top to break ties is itself position-derived. **UI can display/jump via a stored id** (additive chip +
`jumpToMarkLayerNode`); the default create/render path is still the old
block-parser fields — but whoever wires the real cross-edit
diffing use case (as opposed to idempotent re-parse of one static document,
e.g. on server restart) MUST solve real disambiguation first, or two runs
of identical content anywhere in the same document will falsely appear to
move/change across every edit that happens to touch an EARLIER occurrence
of that text.

**Known gap, still flagged rather than fixed:**

1. **A paragraph node's own fragment text is the raw block; its sentence
   children's fragment texts tile `norm(block)`, not the raw block.** For a
   paragraph with a soft-wrap newline or doubled internal whitespace, the
   paragraph node's text and the join of its sentence children's texts are
   different strings (both correct in their own coordinate space — the
   paragraph anchors to soma-review's block source, the sentences anchor to
   the `blockmap.norm()` space `sentence_ranges` computes offsets into — but
   a caller that assumes a paragraph node's text always equals the
   concatenation of its own children would be wrong). See
   `test_paragraph_and_sentence_children_use_different_coordinate_spaces`.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from mdblocks import norm, segment_sentences

_HEADING_RE = re.compile(r'^#{1,6}\s+')


def _content_id(seen: dict[str, int], prefix: str, text: str) -> str:
    """A content-derived id: same `(prefix, text)` always hashes to the same
    base id, so unchanged content maps to the same id across calls. `seen`
    (fresh per `to_mark_layer_nodes` call) disambiguates a `(prefix, text)`
    pair that repeats verbatim within the same call — an empty-line `blank`
    node or two literally identical sentences are both real cases — by
    appending `-{occurrence index}` to every repeat after the first, so ids
    stay unique within a call without giving up cross-call stability for the
    common (non-duplicated) case."""
    digest = hashlib.sha1(f'{prefix}:{text}'.encode('utf-8')).hexdigest()[:10]
    key = f'{prefix}:{digest}'
    occurrence = seen.get(key, 0)
    seen[key] = occurrence + 1
    base = f'{prefix}-{digest}'
    return base if occurrence == 0 else f'{base}-{occurrence}'


def _paragraph_node(seen: dict[str, int], text: str) -> dict[str, Any]:
    node_id = _content_id(seen, 'pmpara', text)
    return {
        'id': node_id,
        'kind': 'paragraph',
        'fragments': [{'id': _content_id(seen, f'{node_id}-frag', text), 'text': text}],
    }


def _blank_node(seen: dict[str, int], text: str) -> dict[str, Any]:
    node_id = _content_id(seen, 'pmln', text)
    return {
        'id': node_id,
        'kind': 'blank',
        'fragments': [{'id': _content_id(seen, f'{node_id}-frag', text), 'text': text}],
    }


def _sentence_nodes(seen: dict[str, int], paragraph_text: str) -> list[dict[str, Any]]:
    """One `sentence` node per sentence in `paragraph_text`, offsets in
    code points into `norm(paragraph_text)` — the same coordinate space
    `blockmap`/`server.sentence_ranges` already anchor marks to, and the
    one the JS adapter's `attrs.offset` was fixed to agree with (2026-09-05,
    `docs/MARK-LAYER-ENGINE.md` "Next" item 1, offset-space closure)."""
    normalized = norm(paragraph_text)
    nodes = []
    offset = 0
    for _start, _end, text in segment_sentences(normalized):
        node_id = _content_id(seen, 'pmsent', text)
        nodes.append({
            'id': node_id,
            'kind': 'sentence',
            'fragments': [{'id': _content_id(seen, f'{node_id}-frag', text), 'text': text}],
            'attrs': {'offset': offset},
        })
        offset += len(text)
    return nodes


def _node_text(node: dict[str, Any]) -> str:
    return ''.join(str(frag.get('text') or '') for frag in (node.get('fragments') or []))


def _paragraph_groups(nodes: list[dict[str, Any]]) -> list[tuple[dict[str, Any] | None, list[dict[str, Any]]]]:
    """Walk the adapter's flat list into (paragraph, following sentences) groups.

    `to_mark_layer_nodes` emits a paragraph, then its sentence siblings, then
    a blank (or the next paragraph). Grouping lets a mark's `snapshot` (the
    block text) scope which duplicate sentence we attach when the same quote
    appears more than once.
    """
    groups: list[tuple[dict[str, Any] | None, list[dict[str, Any]]]] = []
    paragraph: dict[str, Any] | None = None
    sentences: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal paragraph, sentences
        if paragraph is not None or sentences:
            groups.append((paragraph, sentences))
        paragraph = None
        sentences = []

    for node in nodes:
        kind = node.get('kind')
        if kind == 'blank':
            flush()
            continue
        if kind == 'paragraph':
            flush()
            paragraph = node
            continue
        if kind == 'sentence':
            sentences.append(node)
    flush()
    return groups


def _unique_node_id(nodes: list[dict[str, Any]]) -> str | None:
    """Single node id if every node shares one; else None (ambiguous or empty)."""
    ids = [node.get('id') for node in nodes if node.get('id')]
    if not ids:
        return None
    first = ids[0]
    return first if all(nid == first for nid in ids) else None


def match_mark_layer_nodes(
    nodes: list[dict[str, Any]],
    *,
    quote: str | None = None,
    snapshot: str | None = None,
) -> list[dict[str, Any]]:
    """Best-matching MarkLayerNode(s) for a mark's quote (and optional snapshot).

    Prefer an exact sentence-text match (whitespace-stripped — adapter
    sentence fragments keep the leading space `segment_sentences` tiles with,
    while live mark quotes come from `sentence_ranges` which strips), then an
    exact paragraph match, then containment. When `snapshot` uniquely names a
    paragraph group, matching is scoped to that group so a repeated sentence
    attaches the occurrence in that block.

    A tier that still has more than one distinct node id is a miss, not a
    first-hit attach. Containment (`needle in text or text in needle`) is
    the last tier and is also unique-only: a wrong node id is silent and
    permanent on the sidecar (Skip 2026-09-06 nit 1). Returns [] when
    nothing uniquely matches.
    """
    needle = (quote or '').strip() or (snapshot or '').strip()
    if not needle or not nodes:
        return []

    groups = _paragraph_groups(nodes)
    snapshot_stripped = (snapshot or '').strip()
    scoped = groups
    if snapshot_stripped:
        narrowed = [
            (para, sents) for para, sents in groups
            if para is not None and _node_text(para).strip() == snapshot_stripped
        ]
        if narrowed:
            scoped = narrowed
        # else: snapshot did not narrow; keep all groups and require a
        # unique match below so we never first-hit attach.

    exact_sentences: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    exact_paragraphs: list[dict[str, Any]] = []
    contain_sentences: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    contain_paragraphs: list[dict[str, Any]] = []

    for para, sents in scoped:
        for sent in sents:
            text = _node_text(sent).strip()
            if not text:
                continue
            if text == needle:
                exact_sentences.append((sent, para))
            elif needle in text or text in needle:
                contain_sentences.append((sent, para))
        if para is not None:
            text = _node_text(para).strip()
            if not text:
                continue
            if text == needle:
                exact_paragraphs.append(para)
            elif needle in text or text in needle:
                contain_paragraphs.append(para)

    def with_parent(sent: dict[str, Any], para: dict[str, Any] | None) -> list[dict[str, Any]]:
        out = [sent]
        if para is not None and para.get('id') and para.get('id') != sent.get('id'):
            out.append(para)
        return out

    if exact_sentences:
        sent, para = exact_sentences[0]
        if _unique_node_id([s for s, _p in exact_sentences]) == sent.get('id'):
            return with_parent(sent, para)
        return []
    if exact_paragraphs:
        para = exact_paragraphs[0]
        if _unique_node_id(exact_paragraphs) == para.get('id'):
            return [para]
        return []
    if contain_sentences:
        sent, para = contain_sentences[0]
        if _unique_node_id([s for s, _p in contain_sentences]) == sent.get('id'):
            return with_parent(sent, para)
        return []
    if contain_paragraphs:
        para = contain_paragraphs[0]
        if _unique_node_id(contain_paragraphs) == para.get('id'):
            return [para]
        return []
    return []


def attach_mark_layer_node_ids(record: dict[str, Any], page_src: str) -> dict[str, Any]:
    """Additively stamp `mark_layer_node_id` / `mark_layer_node_ids` on a mark.

    Best-effort and never load-bearing: any adapter failure, empty node list,
    or unmatched quote leaves `record` unchanged (no new keys). Does not
    raise. Existing anchor/quote/snapshot/block_id fields are not touched.
    """
    try:
        if not isinstance(record, dict) or not isinstance(page_src, str):
            return record
        matched = match_mark_layer_nodes(
            to_mark_layer_nodes(page_src),
            quote=record.get('quote'),
            snapshot=record.get('snapshot'),
        )
        ids = [node['id'] for node in matched if node.get('id')]
        if not ids:
            return record
        record['mark_layer_node_id'] = ids[0]
        record['mark_layer_node_ids'] = ids
    except Exception:  # noqa: BLE001 — attach is beside-only; never fail a write
        return record
    return record


def to_mark_layer_nodes(md: str) -> list[dict[str, Any]]:
    """Parse `md` into the shared node/fragment model: one `paragraph` node
    per blank-line-separated block (headings kept whole, not sentence-split),
    each non-heading paragraph further split into `sentence` nodes, and blank
    separators preserved as their own `blank` nodes — mirrors
    `fromProseMarkdown` in `playmaker/src/mark-layer-engine/adapters/proseMarkdown.ts`
    node-for-node, so the two engines can agree on the shape of the mark
    record (SOMA agreed model item 6a). Ids are content-derived (`_content_id`)
    and therefore stable across repeated calls on the same document — see the
    module docstring's 2026-09-05 fix note."""
    nodes: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    blocks = re.split(r'(\n{2,})', md)
    for block in blocks:
        if not block:
            continue
        if re.fullmatch(r'\n{2,}', block):
            nodes.append(_blank_node(seen, block))
            continue
        if _HEADING_RE.match(block):
            nodes.append(_paragraph_node(seen, block))
            continue
        nodes.append(_paragraph_node(seen, block))
        nodes.extend(_sentence_nodes(seen, block))
    return nodes
