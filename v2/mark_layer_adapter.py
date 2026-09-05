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

Deliberately not wired into any live server route yet — same staged state
the JS adapter itself documents ("Not consumed anywhere yet"). Wiring
`server.py`'s actual block-parse response to use this shape is the next,
larger step (changes the wire format multiple live consumers read) and is
out of scope for this pass.

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

**Still open: the JS adapter (`fromProseMarkdown`) was not given the same
fix in this pass** — it is the "share one engine" side that would need to
recompute the same `sha1(kind:text)[:10]` scheme in TypeScript for the two
engines' ids to actually agree on identical input, which is a real
prerequisite for the eventual client-side diffing this fix names, and is
called out explicitly rather than assumed done because both files use the
word "parity."

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
layered on top to break ties is itself position-derived. **Not exploitable
today** because nothing consumes this module's output yet (see "Deliberately
not wired" above) — but whoever wires the real cross-edit diffing use case
(as opposed to idempotent re-parse of one static document, e.g. on server
restart) MUST solve real disambiguation first, or two runs of identical
content anywhere in the same document will falsely appear to move/change
across every edit that happens to touch an EARLIER occurrence of that text.

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
