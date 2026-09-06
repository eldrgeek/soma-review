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

Write-side (2026-09-06, dual-write retired as default): `POST /api/comments`
type=mark treats `mark_layer_node_id` as the sole required anchoring
record for a unique match. `attach_mark_layer_node_ids` prefers a
client-supplied stamp id that still exists in the current parse and
agrees with a supplied quote (Skip 2026-09-06 nit 1), then unique
quote/snapshot match. Default jump/render is that stored id → DOM
`data-mark-layer-node-id` (`MarkLayerDomStamper` walks these nodes in
document order). Text-occurrence lookup remains the counted fallback
when the id or stamp is missing.

Old `block_id` / quote / snapshot are not written on unique-match
creates unless `SOMA_REVIEW_MARK_LAYER_DUAL_WRITE` is explicitly on
(compat bridge, default off). Readers use the stored id when present
and fall back to those fields only for legacy rows. 6a stays open
Item-15 view-diff / parity (`mark_layer_parity.py`) is the named
gate; 6a stays open while the twin emitter is the live bridge.
Twin `-{n}` mint is the permanent Playmaker mint (unique
(kind, text) already has no suffix); identity across duplicate-insert
edits is the remap ledger. Weak-neighbor pairing no longer
position-pairs identical lone paragraphs; unpaired old ids miss.
Later-block stamps are restamped on the same mid-doc edit that remaps
sidecar ids (see `_rerender_block`).

Edit-rebind: `align_mark_layer_nodes` maps previous-parse ids onto the
current parse by unique fingerprint, then neighborhood for duplicates,
so an earlier inserted twin remaps the stored id instead of silently
falling back to text. `rebind_mark_layer_node_ids` applies that map to
sidecar rows. Occurrence-suffix minting in `_content_id` is the
Playmaker twin and is the permanent mint — unique `(kind, text)` in
one parse already has no `-{n}`; a parent-scope or neighbor-hash
disambiguator would mint different ids than `fromProseMarkdown`.
Cross-edit identity is the remap ledger (`remap_ledger_entries` +
`.mark-layer-nodes.json`), not a claim that the minted suffix is
stable.

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
layered on top to break ties is itself position-derived. Create now writes `mark_layer_node_id` as the sole required record
(client stamp id that agrees with quote, else unique match). Cross-edit
identity is the remap ledger (`align_mark_layer_nodes` + sidecar
rebind + persisted `remap_ledger`), not a change to `_content_id`
minting — the suffix still shifts in the twin emitter; the ledger
accounts that remap so a stored id keeps querySelector-hitting the
same sentence (or a named unpaired residual). Do not claim 6a closed:
the item-15 gate is green; dual-write/create/edit residuals stay
accepted, not a cutover.

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

# Twin mint (`fromProseMarkdown`) appends `-{n}` for the 2nd+ (prefix, text)
# in one parse. Changing that suffix is a Playmaker-side change. Identity
# across edits is the remap ledger, reason-tagged when the base hash is
# unchanged and only the occurrence index moved.
OCCURRENCE_SUFFIX_REASON = 'occurrence-suffix-shift'
ALIGN_REASON = 'align'


def content_id_base(node_id: str | None) -> str:
    """The `{prefix}-{hash}` (or `{prefix}-{hash}-frag-{hash}`) stem.

    A trailing `-{n}` occurrence suffix is stripped. Unique (kind, text)
    already uses this stem with no suffix — see
    `test_unique_kind_text_has_no_occurrence_suffix`.
    """
    if not node_id:
        return ''
    head, sep, tail = str(node_id).rpartition('-')
    if sep and tail.isdigit():
        return head
    return str(node_id)


def occurrence_suffix(node_id: str | None) -> int | None:
    """`n` when the id ends with a positional `-{n}` suffix, else None."""
    if not node_id:
        return None
    _head, sep, tail = str(node_id).rpartition('-')
    if sep and tail.isdigit():
        return int(tail)
    return None


def remap_reason(old_id: str | None, new_id: str | None) -> str:
    """Tag a remap: suffix shift on the same content-hash, or other align."""
    if (
        old_id and new_id and old_id != new_id
        and content_id_base(old_id) == content_id_base(new_id)
    ):
        return OCCURRENCE_SUFFIX_REASON
    return ALIGN_REASON


def remap_ledger_entries(applied: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """Normalize applied remaps into durable ledger rows.

    This is the permanent identity model for suffix drift: every real
    change is `{from, to, record_id, reason}`. Identity remaps are
    omitted. Twin minting is unchanged.
    """
    out: list[dict[str, str]] = []
    for item in applied or []:
        if not isinstance(item, dict):
            continue
        old = item.get('from')
        new = item.get('to')
        if not old or not new or old == new:
            continue
        out.append({
            'from': str(old),
            'to': str(new),
            'record_id': str(item.get('record_id') or ''),
            'reason': str(item.get('reason') or remap_reason(old, new)),
        })
    return out


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


def mark_layer_node_text(node: dict[str, Any] | None) -> str:
    """Public wrapper for a node's joined fragment text."""
    if not node:
        return ''
    return _node_text(node)


def _quote_agrees_with_node(node: dict[str, Any], quote: str | None) -> bool:
    """True when there is no quote to check, or it equals the node's text.

    Skip 2026-09-06 nit 1: a client stamp for sentence A plus a quote for
    sentence B must not silently persist A's id.
    """
    needle = (quote or '').strip()
    if not needle:
        return True
    return _node_text(node).strip() == needle


def find_mark_layer_node(
    nodes: list[dict[str, Any]] | None, node_id: str | None,
) -> dict[str, Any] | None:
    if not node_id or not nodes:
        return None
    return next((node for node in nodes if node.get('id') == node_id), None)


def block_for_mark_layer_node(
    blocks: list[dict[str, Any]] | None,
    nodes: list[dict[str, Any]] | None,
    node_id: str | None,
) -> dict[str, Any] | None:
    """The current parse block that carries `node_id`, via stamper order.

    Walks blocks the same way render stamps them, so a repeated sentence
    resolves to its occurrence, not the first text hit. None when the id
    is missing or no block consumed it.
    """
    if not node_id or not blocks:
        return None
    stamper = MarkLayerDomStamper(nodes)
    for block in blocks:
        para_id = stamper.bind_block(block.get('text') or '')
        ids = set(stamper.bound_node_ids())
        if para_id:
            ids.add(para_id)
        if node_id in ids:
            return block
    return None


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


def _node_fingerprint(node: dict[str, Any] | None) -> tuple[str, str] | None:
    if not node:
        return None
    return (str(node.get('kind') or ''), _node_text(node).strip())


def _group_fingerprint(
    group: tuple[dict[str, Any] | None, list[dict[str, Any]]],
) -> tuple[str, str] | None:
    para, sents = group
    if para is not None:
        return _node_fingerprint(para)
    if sents:
        return _node_fingerprint(sents[0])
    return None


def _alignment_neighbors(
    nodes: list[dict[str, Any]],
) -> dict[str, tuple[tuple[str, str] | None, tuple[str, str] | None,
                     tuple[str, str] | None, tuple[str, str] | None]]:
    """Per-id context: (prev group, next group, intra-left, intra-right).

    Group neighbors survive inserting an earlier duplicate of the same
    sentence: the untouched occurrence still sits next to the same
    sibling paragraph. Intra-group neighbors distinguish two Ready.
    sentences that live in different unique paragraphs.
    """
    groups = _paragraph_groups(nodes)
    neighbors: dict[str, tuple] = {}
    for gi, (para, sents) in enumerate(groups):
        prev_g = _group_fingerprint(groups[gi - 1]) if gi > 0 else None
        next_g = _group_fingerprint(groups[gi + 1]) if gi + 1 < len(groups) else None
        seq = ([para] if para is not None else []) + list(sents)
        for i, node in enumerate(seq):
            nid = node.get('id')
            if not nid:
                continue
            left = _node_fingerprint(seq[i - 1]) if i > 0 else None
            right = _node_fingerprint(seq[i + 1]) if i + 1 < len(seq) else None
            neighbors[nid] = (prev_g, next_g, left, right)
    return neighbors


def _unique_fingerprints(nodes: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """Fingerprints that appear on exactly one node — a distinguishing sibling."""
    counts: dict[tuple[str, str], int] = {}
    for node in nodes:
        fp = _node_fingerprint(node)
        if fp is None:
            continue
        counts[fp] = counts.get(fp, 0) + 1
    return {fp for fp, count in counts.items() if count == 1}


def _strong_neighbor_score(
    prev_nb: tuple, next_nb: tuple,
    unique_prev: set[tuple[str, str]],
    unique_next: set[tuple[str, str]],
) -> int:
    """Score only concrete neighbors that are unique on both sides.

    Shared start/end-of-doc Nones and another copy of the same text
    (Ready. next to Ready.) do not count — those are weak and used to
    let position-only pairing win when uniqueness failed.
    """
    score = 0
    for i in range(4):
        a, b = prev_nb[i], next_nb[i]
        if a is None or b is None or a != b:
            continue
        if a in unique_prev and b in unique_next:
            score += 2
    return score


def _ids_for_matched_node(
    nodes: list[dict[str, Any]], hit: dict[str, Any],
) -> list[str]:
    """Sentence id first, then its paragraph parent — same shape as match()."""
    ids = [hit['id']] if hit.get('id') else []
    if hit.get('kind') != 'sentence':
        return ids
    for para, sents in _paragraph_groups(nodes):
        if any(sent.get('id') == hit.get('id') for sent in sents):
            if para is not None and para.get('id') and para.get('id') not in ids:
                ids.append(para['id'])
            break
    return ids


def align_mark_layer_nodes(
    prev_nodes: list[dict[str, Any]] | None,
    next_nodes: list[dict[str, Any]] | None,
) -> dict[str, str]:
    """Map previous-parse node ids onto the current parse.

    Pass 1: a (kind, stripped text) fingerprint that is unique on both
    sides pairs (usually the same content-hash id).
    Pass 2: remaining duplicates pair only when a concrete neighbor
    fingerprint is unique on both sides and the pair is each other's
    unique best. That remaps an earlier-inserted twin that still sits
    next to a unique sibling. Identical one-sentence paragraphs with
    only other copies of themselves as neighbors do not pair — position
    is not a tie-break. Unpaired old ids are omitted (miss).

    Every pairing is returned, including identity. Does not change
    `_content_id` minting.
    """
    prev = [node for node in (prev_nodes or []) if node.get('id')]
    nxt = [node for node in (next_nodes or []) if node.get('id')]
    if not prev or not nxt:
        return {}

    prev_by_fp: dict[tuple[str, str], list[int]] = {}
    next_by_fp: dict[tuple[str, str], list[int]] = {}
    for index, node in enumerate(prev):
        prev_by_fp.setdefault(_node_fingerprint(node), []).append(index)
    for index, node in enumerate(nxt):
        next_by_fp.setdefault(_node_fingerprint(node), []).append(index)

    remap: dict[str, str] = {}
    used_next: set[int] = set()
    prev_nb = _alignment_neighbors(prev)
    next_nb = _alignment_neighbors(nxt)
    empty_nb = (None, None, None, None)
    unique_prev_fps = _unique_fingerprints(prev)
    unique_next_fps = _unique_fingerprints(nxt)

    for fp, prev_idxs in prev_by_fp.items():
        next_idxs = next_by_fp.get(fp) or []
        if len(prev_idxs) == 1 and len(next_idxs) == 1:
            remap[prev[prev_idxs[0]]['id']] = nxt[next_idxs[0]]['id']
            used_next.add(next_idxs[0])

    for fp, prev_idxs in prev_by_fp.items():
        remaining_prev = [i for i in prev_idxs if prev[i]['id'] not in remap]
        remaining_next = [i for i in (next_by_fp.get(fp) or []) if i not in used_next]
        if not remaining_prev or not remaining_next:
            continue

        def strong_score(pi: int, ni: int) -> int:
            return _strong_neighbor_score(
                prev_nb.get(prev[pi]['id'], empty_nb),
                next_nb.get(nxt[ni]['id'], empty_nb),
                unique_prev_fps,
                unique_next_fps,
            )

        # Mutual unique-best: pair only when one next is this prev's unique
        # best and this prev is that next's unique best. Ties and score-0
        # (weak / position-only) stay unpaired — mark may miss.
        prev_best: dict[int, tuple[int, list[int]]] = {}
        for pi in remaining_prev:
            by_score: dict[int, list[int]] = {}
            for ni in remaining_next:
                score = strong_score(pi, ni)
                if score <= 0:
                    continue
                by_score.setdefault(score, []).append(ni)
            if by_score:
                best = max(by_score)
                prev_best[pi] = (best, by_score[best])

        next_best: dict[int, tuple[int, list[int]]] = {}
        for ni in remaining_next:
            by_score: dict[int, list[int]] = {}
            for pi in remaining_prev:
                score = strong_score(pi, ni)
                if score <= 0:
                    continue
                by_score.setdefault(score, []).append(pi)
            if by_score:
                best = max(by_score)
                next_best[ni] = (best, by_score[best])

        for pi, (p_score, p_nis) in prev_best.items():
            if len(p_nis) != 1:
                continue
            ni = p_nis[0]
            n_info = next_best.get(ni)
            if not n_info or n_info[0] != p_score or n_info[1] != [pi]:
                continue
            remap[prev[pi]['id']] = nxt[ni]['id']
            used_next.add(ni)
    return remap


def rebind_mark_layer_node_ids(
    records: list[dict[str, Any]],
    remap: dict[str, str],
    only_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Apply an align() remap to stored mark_layer_node_id fields.

    Identity remaps are left untouched (no sidecar write). `only_ids`
    restricts to records whose current node id is in that set — used
    when only one block was restamped. Returns (records, applied) where
    applied lists {record_id, from, to, reason} for every real change.
    `reason` is `occurrence-suffix-shift` when only the twin `-{n}`
    moved, else `align`. Each hop is appended to
    `mark_layer_node_rebind_history` so multi-edit remaps stay
    accounted. This is the remap ledger, not a stable-id claim.
    """
    applied: list[dict[str, str]] = []
    if not remap:
        return records, applied
    for record in records:
        if not isinstance(record, dict):
            continue
        old = record.get('mark_layer_node_id')
        if not old or old not in remap:
            continue
        if only_ids is not None and old not in only_ids:
            continue
        new = remap[old]
        if not new or new == old:
            continue
        reason = remap_reason(old, new)
        record['mark_layer_node_id'] = new
        ids = [str(item) for item in (record.get('mark_layer_node_ids') or []) if item]
        record['mark_layer_node_ids'] = [remap.get(item, item) for item in ids] or [new]
        hop = {'from': old, 'to': new, 'reason': reason}
        record['mark_layer_node_rebound'] = hop
        history = list(record.get('mark_layer_node_rebind_history') or [])
        history.append(dict(hop))
        record['mark_layer_node_rebind_history'] = history
        applied.append({
            'record_id': str(record.get('id') or ''),
            'from': old,
            'to': new,
            'reason': reason,
        })
    return records, applied


def attach_mark_layer_node_ids(
    record: dict[str, Any],
    page_src: str | None,
    nodes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Write `mark_layer_node_id` / `mark_layer_node_ids` as the create record.

    Prefers a client-supplied id that still exists in the current parse
    (the DOM stamp at mark time) and whose text agrees with a supplied
    quote. A quote↔id mismatch falls through to unique quote/snapshot
    match instead of trusting the stamp. Adapter failure, empty nodes,
    or unmatched quote leave the record without a node id — page-level
    / ambiguous marks still persist. Does not raise. Does not write
    block_id/quote/snapshot; the server strips those on unique-match
    creates unless SOMA_REVIEW_MARK_LAYER_DUAL_WRITE is on.
    """
    try:
        if not isinstance(record, dict):
            return record
        if nodes is None:
            if not isinstance(page_src, str):
                return record
            nodes = to_mark_layer_nodes(page_src)
        if not nodes:
            return record
        supplied = record.get('mark_layer_node_id')
        if supplied:
            hit = find_mark_layer_node(nodes, supplied)
            if hit is not None and _quote_agrees_with_node(hit, record.get('quote')):
                ids = _ids_for_matched_node(nodes, hit)
                if ids:
                    record['mark_layer_node_id'] = ids[0]
                    record['mark_layer_node_ids'] = ids
                    return record
        matched = match_mark_layer_nodes(
            nodes,
            quote=record.get('quote'),
            snapshot=record.get('snapshot'),
        )
        ids = [node['id'] for node in matched if node.get('id')]
        if not ids:
            if supplied:
                record.pop('mark_layer_node_id', None)
                record.pop('mark_layer_node_ids', None)
            return record
        record['mark_layer_node_id'] = ids[0]
        record['mark_layer_node_ids'] = ids
    except Exception:  # noqa: BLE001 — attach must never fail a mark write
        return record
    return record


class MarkLayerDomStamper:
    """Assign adapter node ids to rendered blocks/sentences in document order.

    Live HTML is produced by `parse_markdown` + `sentence_ranges`; adapter
    nodes come from `to_mark_layer_nodes` (blank-line split +
    `segment_sentences`). The walker consumes unused groups/sentences
    forward so a later duplicate gets `pmsent-…-1`, not the first hit.

    `bind_block` prefers the next unused group when its text matches
    (positional), then scans forward for a text match so a list/table
    sitting between prose groups is skipped rather than stealing a stamp.
    `next_sentence` stays inside the bound group — it does not search the
    rest of the document. A miss returns None (no stamp) rather than
    lining up a later twin by text; that was the mid-doc-edit drift.
    Twin mint still uses `_content_id` occurrence suffixes; the remap
    ledger accounts stored-id shifts. Later blocks after a mid-doc edit
    are restamped by `_rerender_block`, not left stale.
    """

    def __init__(self, nodes: list[dict[str, Any]] | None = None):
        self._groups = _paragraph_groups(list(nodes or []))
        self._next_group = 0
        self._current_sentences: list[dict[str, Any]] = []
        self._next_sentence = 0
        self._used_sentence_ids: set[str] = set()

    def _group_matches(self, para: dict[str, Any] | None, needle: str, needle_norm: str) -> bool:
        if para is None:
            return False
        text = _node_text(para).strip()
        heading_stripped = _HEADING_RE.sub('', text).strip()
        return bool(
            text == needle
            or heading_stripped == needle
            or norm(text) == needle_norm
            or (heading_stripped and norm(heading_stripped) == needle_norm)
        )

    def bind_block(self, block_text: str) -> str | None:
        """Consume the next unused paragraph group matching `block_text`."""
        needle = (block_text or '').strip()
        if not needle:
            self._current_sentences = []
            self._next_sentence = 0
            return None
        needle_norm = norm(needle)
        # Prefer the next unused group when it already matches — do not
        # skip ahead to a later twin just because a search would find it.
        if self._next_group < len(self._groups):
            para, sents = self._groups[self._next_group]
            if self._group_matches(para, needle, needle_norm):
                self._next_group += 1
                self._current_sentences = sents
                self._next_sentence = 0
                return para.get('id') if para is not None else None
        for i in range(self._next_group, len(self._groups)):
            para, sents = self._groups[i]
            if not self._group_matches(para, needle, needle_norm):
                continue
            self._next_group = i + 1
            self._current_sentences = sents
            self._next_sentence = 0
            return para.get('id') if para is not None else None
        self._current_sentences = []
        self._next_sentence = 0
        return None

    def skip_block(self, block_text: str) -> None:
        """Consume a prior block so a later duplicate keeps its occurrence id."""
        if self.bind_block(block_text):
            self._next_sentence = len(self._current_sentences)
            for sent in self._current_sentences:
                sid = sent.get('id')
                if sid:
                    self._used_sentence_ids.add(sid)

    def bound_node_ids(self) -> set[str]:
        """Paragraph + sentence ids of the group `bind_block` just opened."""
        ids: set[str] = set()
        for sent in self._current_sentences:
            sid = sent.get('id')
            if sid:
                ids.add(sid)
        return ids

    def next_sentence(self, quote: str) -> str | None:
        """Consume the next unused sentence in the bound group matching `quote`.

        No document-wide text search: a miss is unstamped. Jump may fall
        back to text-occurrence; that path is counted, not the default.
        """
        needle = (quote or '').strip()
        if not needle:
            return None
        for i in range(self._next_sentence, len(self._current_sentences)):
            sent = self._current_sentences[i]
            sid = sent.get('id')
            if sid in self._used_sentence_ids:
                continue
            if _node_text(sent).strip() == needle:
                self._next_sentence = i + 1
                if sid:
                    self._used_sentence_ids.add(sid)
                return sid
        return None


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
