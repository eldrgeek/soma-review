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

**Known gaps, flagged rather than fixed (Skip's adversarial pass, 2026-09-05,
same day as this module):**

1. **Node ids are not stable across calls.** `_next_id` draws from a
   module-global `itertools.count`, so calling `to_mark_layer_nodes` twice on
   identical text mints different ids both times — the same flaw the JS
   `fromProseMarkdown` has (`let counter = 0` at module scope), so this is
   parity, not a Python-only regression. This repo already paid for the
   general shape of this mistake once (Anchoring v2 replaced renderer-order
   comment anchors with stable content-hash block ids after non-stable ids
   broke reconciliation across re-renders). Any future live route wiring
   MUST replace this with a stable, content-derived id scheme before a
   client does same-input-same-id diffing against it — do not ship the
   route wiring with this counter still in place.
2. **A paragraph node's own fragment text is the raw block; its sentence
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

import itertools
import re
from typing import Any

from mdblocks import norm, segment_sentences

_HEADING_RE = re.compile(r'^#{1,6}\s+')

_counter = itertools.count(1)


def _next_id(prefix: str) -> str:
    return f'{prefix}-{next(_counter)}'


def _paragraph_node(text: str) -> dict[str, Any]:
    return {
        'id': _next_id('pmpara'),
        'kind': 'paragraph',
        'fragments': [{'id': _next_id('frag'), 'text': text}],
    }


def _blank_node(text: str) -> dict[str, Any]:
    return {
        'id': _next_id('pmln'),
        'kind': 'blank',
        'fragments': [{'id': _next_id('frag'), 'text': text}],
    }


def _sentence_nodes(paragraph_text: str) -> list[dict[str, Any]]:
    """One `sentence` node per sentence in `paragraph_text`, offsets in
    code points into `norm(paragraph_text)` — the same coordinate space
    `blockmap`/`server.sentence_ranges` already anchor marks to, and the
    one the JS adapter's `attrs.offset` was fixed to agree with (2026-09-05,
    `docs/MARK-LAYER-ENGINE.md` "Next" item 1, offset-space closure)."""
    normalized = norm(paragraph_text)
    nodes = []
    offset = 0
    for _start, _end, text in segment_sentences(normalized):
        nodes.append({
            'id': _next_id('pmsent'),
            'kind': 'sentence',
            'fragments': [{'id': _next_id('frag'), 'text': text}],
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
    record (SOMA agreed model item 6a)."""
    nodes: list[dict[str, Any]] = []
    blocks = re.split(r'(\n{2,})', md)
    for block in blocks:
        if not block:
            continue
        if re.fullmatch(r'\n{2,}', block):
            nodes.append(_blank_node(block))
            continue
        if _HEADING_RE.match(block):
            nodes.append(_paragraph_node(block))
            continue
        nodes.append(_paragraph_node(block))
        nodes.extend(_sentence_nodes(block))
    return nodes
