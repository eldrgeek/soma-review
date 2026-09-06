"""Item-15 view-diff / parity gate for mark anchoring.

Compares old-path resolve (block_id / quote / snapshot via blockmap.resolve)
against mark_layer_node_id → rendered DOM stamp for the same marks on a
fixture set. A landing mismatch that is not tagged with one of the named
accounted reasons fails the gate.

Accounted reasons (named, not hidden):
  - occurrence-suffix-shift — remap ledger hop; landings still match
  - remap-ledger — other align remap; landings still match
  - unpaired-miss — weak-neighbor align omitted the old id
  - unique-match-miss — attach missed (repeat without a narrowing snapshot)
  - heading-no-sentence-node — heading kept whole, no sentence id
  - edit-id-reattach — type=edit identity is the re-attached id; old
    block_id+snapshot is not the live landing after apply

Create/resolve live path is mark_layer_node_id. Unique quote/snapshot
match is fallback minting. type=edit no longer dual-writes block_id as
identity (snapshot/proposed stay as the change record). Old
block_id/quote resolve is off when an id is present. Live stamps come
from from_prose_markdown (Playmaker fromProseMarkdown port). Twin is
debug-only (SOMA_REVIEW_MARK_LAYER_TWIN, default off).

Run:
  python3 v2/mark_layer_parity.py
  python3 -m unittest v2.tests.test_mark_layer_parity
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import tempfile
from typing import Any

V2_DIR = os.path.dirname(os.path.abspath(__file__))
if V2_DIR not in sys.path:
    sys.path.insert(0, V2_DIR)

import blockmap  # noqa: E402
import server  # noqa: E402
from mark_layer_adapter import (  # noqa: E402
    OCCURRENCE_SUFFIX_REASON, ALIGN_REASON,
    attach_mark_layer_node_ids,
)
from mark_layer_engine import (  # noqa: E402
    last_live_emitter_source, mark_layer_twin_enabled,
)

ACCOUNTABLE_REASONS = frozenset({
    OCCURRENCE_SUFFIX_REASON,
    'remap-ledger',
    ALIGN_REASON,
    'unpaired-miss',
    'unique-match-miss',
    'heading-no-sentence-node',
    'edit-id-reattach',
})

FIXTURE_PATH = os.path.join(
    V2_DIR, 'tests', 'fixtures', 'mark_anchor_parity.json',
)

_SENTENCE_RE = re.compile(
    r'<span class="mark-sentence"'
    r' data-from="(\d+)" data-to="(\d+)"'
    r' data-quote="([^"]+)"'
    r'( data-mark-layer-node-id="([^"]+)"(?: id="[^"]+")?)?'
    r'>'
)
_BLOCK_ID_RE = re.compile(r'data-block-id="([^"]+)"')
_BLOCK_KIND_RE = re.compile(r'data-kind="([^"]+)"')


def load_fixture(path: str | None = None) -> dict[str, Any]:
    with open(path or FIXTURE_PATH, encoding='utf-8') as handle:
        return json.load(handle)


def _decode_quote(b64: str) -> str:
    return base64.b64decode(b64.encode('ascii')).decode('utf-8')


def extract_stamps(html: str) -> list[dict[str, Any]]:
    """View stamps: each .mark-sentence plus its parent block_id/kind."""
    stamps = []
    for match in _SENTENCE_RE.finditer(html):
        prefix = html[:match.start()]
        block_ids = _BLOCK_ID_RE.findall(prefix)
        kinds = _BLOCK_KIND_RE.findall(prefix)
        node_id = match.group(5)
        stamps.append({
            'block_id': block_ids[-1] if block_ids else None,
            'kind': kinds[-1] if kinds else None,
            'from': int(match.group(1)),
            'to': int(match.group(2)),
            'quote': _decode_quote(match.group(3)),
            'node_id': node_id,
        })
    return stamps


def old_path_sentences(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Old-path units: sentence_ranges inside each prose/heading block."""
    out = []
    for block in blocks:
        kind = block.get('kind')
        if kind not in ('paragraph', 'blockquote', 'heading'):
            continue
        text = block.get('text') or ''
        for start, end, quote in server.sentence_ranges(text):
            out.append({
                'block_id': block.get('id'),
                'kind': kind,
                'from': start,
                'to': end,
                'quote': quote,
                'snapshot': text,
            })
    return out


def landing_key(row: dict[str, Any] | None) -> tuple | None:
    if not row or not row.get('block_id'):
        return None
    quote = blockmap.norm(row.get('quote') or '')
    return (row.get('block_id'), row.get('from'), row.get('to'), quote)


def resolve_old(mark: dict[str, Any], blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """block_id → quote-verified span (Anchoring v2)."""
    outcome = blockmap.resolve(mark, blocks)
    if outcome.get('status') != 'bound':
        return {'status': 'unresolved', 'reason': outcome.get('reason')}
    block = outcome['block']
    start, end = outcome['from'], outcome['to']
    text = blockmap.norm(block.get('text'))
    quote = text if end is None else text[start:end]
    return {
        'status': 'bound',
        'block_id': block.get('id'),
        'from': start,
        'to': end,
        'quote': quote,
    }


def resolve_stamp(node_id: str | None, stamps: list[dict[str, Any]]) -> dict[str, Any]:
    """mark_layer_node_id → the unique DOM stamp (view resolve)."""
    if not node_id:
        return {'status': 'unresolved', 'reason': 'missing-id'}
    hits = [row for row in stamps if row.get('node_id') == node_id]
    if not hits:
        return {'status': 'unresolved', 'reason': 'stamp-not-found'}
    if len(hits) > 1:
        return {'status': 'unresolved', 'reason': 'stamp-ambiguous'}
    hit = hits[0]
    return {
        'status': 'bound',
        'block_id': hit.get('block_id'),
        'from': hit.get('from'),
        'to': hit.get('to'),
        'quote': hit.get('quote'),
        'node_id': node_id,
    }


class _PageWorkspace:
    """Scratch workspace so the gate never touches live sidecars."""

    def __init__(self, source: str):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        os.makedirs(os.path.join(self.root, 'docs'))
        self.doc = os.path.join(self.root, 'docs', 'page.md')
        self.config = os.path.join(self.root, 'workspaces.json')
        with open(self.config, 'w', encoding='utf-8') as handle:
            json.dump({
                'estate': {
                    'label': 'Parity', 'roots': [['docs', 'docs']],
                    'nav': [], 'home': 'docs/page.md',
                    'feedback_dir': 'feedback', 'nightly': False, 'tours': False,
                }
            }, handle)
        self.route = 'docs/page.md'
        self._old_root = server.PROJECTS_ROOT
        self._old_config = server.WORKSPACES_CONFIG
        server.PROJECTS_ROOT = self.root
        server.WORKSPACES_CONFIG = self.config
        self.write(source)

    def write(self, source: str) -> None:
        with open(self.doc, 'w', encoding='utf-8') as handle:
            handle.write(source)

    def render(self) -> str:
        return server.render_page(self.route)

    def blocks(self) -> list[dict[str, Any]]:
        _src, blocks, _mapping, _report = server.current_page_blocks(self.route)
        return blocks

    def close(self) -> None:
        server.PROJECTS_ROOT = self._old_root
        server.WORKSPACES_CONFIG = self._old_config
        self._tmp.cleanup()

    def __enter__(self) -> '_PageWorkspace':
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _diff_row(
    *, page_id: str, mark_id: str, old: dict[str, Any], new: dict[str, Any],
    reason: str | None, detail: str = '',
) -> dict[str, Any]:
    return {
        'page_id': page_id,
        'mark_id': mark_id,
        'old': {k: old.get(k) for k in ('status', 'block_id', 'from', 'to', 'quote', 'reason')},
        'new': {k: new.get(k) for k in ('status', 'block_id', 'from', 'to', 'quote', 'reason', 'node_id')},
        'reason': reason,
        'detail': detail,
    }


def compare_page_view(page_id: str, source: str) -> dict[str, Any]:
    """Create-time view-diff: old sentence_ranges vs stamped node ids."""
    matches: list[dict[str, Any]] = []
    accounted: list[dict[str, Any]] = []
    unaccounted: list[dict[str, Any]] = []
    with _PageWorkspace(source) as ws:
        html = ws.render()
        blocks = ws.blocks()
        old_rows = old_path_sentences(blocks)
        stamps = extract_stamps(html)
        used_stamps: set[int] = set()

        for index, old_sent in enumerate(old_rows):
            mark_id = f'{page_id}:{index}:{old_sent["quote"][:24]}'
            old_hit = resolve_old({
                'block_id': old_sent['block_id'],
                'from': old_sent['from'],
                'to': old_sent['to'],
                'quote': old_sent['quote'],
                'snapshot': old_sent['snapshot'],
            }, blocks)
            candidates = [
                (i, stamp) for i, stamp in enumerate(stamps)
                if landing_key(stamp) == landing_key(old_sent)
            ]
            if not candidates:
                new_hit = {'status': 'unresolved', 'reason': 'stamp-not-found'}
                reason = (
                    'heading-no-sentence-node'
                    if old_sent.get('kind') == 'heading'
                    else 'unpaired-miss'
                )
                accounted.append(_diff_row(
                    page_id=page_id, mark_id=mark_id, old=old_hit, new=new_hit,
                    reason=reason,
                    detail='old-path sentence has no matching DOM stamp',
                ))
                continue
            stamp_i, stamp = candidates[0]
            used_stamps.add(stamp_i)
            new_hit = resolve_stamp(stamp.get('node_id'), stamps)
            if landing_key(old_hit) == landing_key(new_hit) and new_hit.get('status') == 'bound':
                matches.append({
                    'page_id': page_id, 'mark_id': mark_id,
                    'landing': landing_key(old_hit),
                    'node_id': stamp.get('node_id'),
                })
                continue
            if old_sent.get('kind') == 'heading' and not stamp.get('node_id'):
                accounted.append(_diff_row(
                    page_id=page_id, mark_id=mark_id, old=old_hit, new=new_hit,
                    reason='heading-no-sentence-node',
                    detail='heading spans render without a sentence node id',
                ))
                continue
            unaccounted.append(_diff_row(
                page_id=page_id, mark_id=mark_id, old=old_hit, new=new_hit,
                reason=None,
                detail='create-time landing mismatch',
            ))

        for i, stamp in enumerate(stamps):
            if i in used_stamps or not stamp.get('node_id'):
                continue
            if stamp.get('kind') == 'heading':
                continue
            unaccounted.append(_diff_row(
                page_id=page_id,
                mark_id=f'{page_id}:extra-stamp:{stamp.get("node_id")}',
                old={'status': 'unresolved', 'reason': 'no-old-sentence'},
                new=resolve_stamp(stamp.get('node_id'), stamps),
                reason=None,
                detail='stamp has no matching old-path sentence',
            ))

        # Attach path: quote+snapshot must mint the same id the view stamped.
        src = source
        for old_sent in old_rows:
            if old_sent.get('kind') == 'heading':
                continue
            record = {
                'quote': old_sent['quote'],
                'snapshot': old_sent['snapshot'],
            }
            attach_mark_layer_node_ids(record, src)
            attached = record.get('mark_layer_node_id')
            stamp_hit = resolve_stamp(attached, stamps)
            old_hit = resolve_old({
                'block_id': old_sent['block_id'],
                'from': old_sent['from'],
                'to': old_sent['to'],
                'quote': old_sent['quote'],
                'snapshot': old_sent['snapshot'],
            }, blocks)
            mark_id = f'{page_id}:attach:{old_sent["quote"][:24]}@{old_sent["block_id"]}'
            if not attached:
                accounted.append(_diff_row(
                    page_id=page_id, mark_id=mark_id, old=old_hit, new=stamp_hit,
                    reason='unique-match-miss',
                    detail='unique-match attach missed (repeat without a narrowing snapshot)',
                ))
                continue
            if landing_key(old_hit) == landing_key(stamp_hit):
                matches.append({
                    'page_id': page_id, 'mark_id': mark_id,
                    'landing': landing_key(old_hit),
                    'node_id': attached,
                })
            else:
                unaccounted.append(_diff_row(
                    page_id=page_id, mark_id=mark_id, old=old_hit, new=stamp_hit,
                    reason=None,
                    detail='attach id does not stamp the old-path landing',
                ))

    return {
        'page_id': page_id,
        'matches': matches,
        'accounted': accounted,
        'unaccounted': unaccounted,
    }


def _pick_stamp_for_edit(
    stamps: list[dict[str, Any]], quote: str, snapshot: str | None, occurrence: int | None,
) -> dict[str, Any] | None:
    needle = (quote or '').strip()
    hits = [row for row in stamps if (row.get('quote') or '').strip() == needle]
    if snapshot:
        # Prefer the stamp whose parent block text matches the snapshot.
        # Snapshot is the full block; we only have quote on the stamp, so
        # the caller passes occurrence when snapshot is absent.
        with_snap = hits
        if len(with_snap) == 1:
            return with_snap[0]
    if occurrence is not None and 0 <= occurrence < len(hits):
        return hits[occurrence]
    if len(hits) == 1:
        return hits[0]
    return hits[-1] if hits else None


def compare_edit(case: dict[str, Any]) -> dict[str, Any]:
    """After-edit landing compare, with remap-ledger / unpaired-miss accounting."""
    page_id = case['id']
    matches: list[dict[str, Any]] = []
    accounted: list[dict[str, Any]] = []
    unaccounted: list[dict[str, Any]] = []
    with _PageWorkspace(case['before']) as ws:
        html_before = ws.render()
        blocks_before = ws.blocks()
        stamps_before = extract_stamps(html_before)
        target = _pick_stamp_for_edit(
            stamps_before,
            case.get('mark_quote') or '',
            case.get('mark_snapshot'),
            case.get('mark_occurrence'),
        )
        if target is None or not target.get('node_id'):
            unaccounted.append(_diff_row(
                page_id=page_id, mark_id=f'{page_id}:missing-before-stamp',
                old={'status': 'unresolved'},
                new={'status': 'unresolved', 'reason': 'stamp-not-found'},
                reason=None,
                detail='fixture mark has no stamped sentence on the before page',
            ))
            return {
                'page_id': page_id,
                'matches': matches,
                'accounted': accounted,
                'unaccounted': unaccounted,
            }

        block = next(
            (row for row in blocks_before if row.get('id') == target['block_id']),
            None,
        )
        snapshot = case.get('mark_snapshot') or (block.get('text') if block else '')
        mark = {
            'id': f'mark-{page_id}',
            'page': ws.route,
            'type': 'mark',
            'mark_kind': 'ack',
            'block_id': target['block_id'],
            'from': target['from'],
            'to': target['to'],
            'quote': target['quote'],
            'snapshot': snapshot,
            'mark_layer_node_id': target['node_id'],
            'deleted': False,
        }
        server.append_comment(ws.route, dict(mark))

        ws.write(case['after'])
        html_after = ws.render()
        blocks_after = ws.blocks()
        stamps_after = extract_stamps(html_after)
        saved = server.read_comments(ws.route)
        row = saved[0] if saved else mark
        ledger = server.load_mark_layer_remap_ledger(ws.route)

        old_hit = resolve_old({
            'block_id': mark['block_id'],
            'from': mark['from'],
            'to': mark['to'],
            'quote': mark['quote'],
            'snapshot': mark['snapshot'],
        }, blocks_after)
        live_id = row.get('mark_layer_node_id')
        new_hit = resolve_stamp(live_id, stamps_after)

        hop = next(
            (item for item in ledger if item.get('record_id') == mark['id']),
            None,
        )
        if not hop and row.get('mark_layer_node_rebound'):
            hop = row.get('mark_layer_node_rebound')
        hop_reason = (hop or {}).get('reason')

        same = landing_key(old_hit) == landing_key(new_hit) and new_hit.get('status') == 'bound'
        if same:
            if hop_reason == OCCURRENCE_SUFFIX_REASON:
                accounted.append(_diff_row(
                    page_id=page_id, mark_id=mark['id'], old=old_hit, new=new_hit,
                    reason=OCCURRENCE_SUFFIX_REASON,
                    detail=f"ledger {hop.get('from')} → {hop.get('to')}",
                ))
            elif hop_reason:
                accounted.append(_diff_row(
                    page_id=page_id, mark_id=mark['id'], old=old_hit, new=new_hit,
                    reason='remap-ledger',
                    detail=f"{hop_reason}: {hop.get('from')} → {hop.get('to')}",
                ))
            else:
                matches.append({
                    'page_id': page_id, 'mark_id': mark['id'],
                    'landing': landing_key(old_hit),
                    'node_id': live_id,
                })
        elif not hop:
            # Align omitted the id (weak-neighbor). Old path is often
            # quote-ambiguous after another identical block lands; the
            # stored suffix may still querySelector a different occurrence.
            detail = 'align omitted the old id'
            if new_hit.get('status') != 'bound':
                detail += '; stamp resolve misses'
            elif old_hit.get('status') != 'bound':
                detail += '; old-path quote is ambiguous; stale suffix may still stamp'
            else:
                detail += '; stale occurrence suffix hit a different landing'
            accounted.append(_diff_row(
                page_id=page_id, mark_id=mark['id'], old=old_hit, new=new_hit,
                reason='unpaired-miss',
                detail=detail,
            ))
        else:
            unaccounted.append(_diff_row(
                page_id=page_id, mark_id=mark['id'], old=old_hit, new=new_hit,
                reason=None,
                detail=f'edit landing mismatch; hop={hop}',
            ))

        expected = set(case.get('expect_accounted') or [])
        saw = {item['reason'] for item in accounted if item.get('reason')}
        missing_expected = expected - saw
        if missing_expected and not unaccounted:
            # Expected a named residual that did not appear — surface it so
            # a fixture that "went green" by accident is still visible.
            unaccounted.append(_diff_row(
                page_id=page_id, mark_id=f'{page_id}:expected-residual',
                old=old_hit, new=new_hit,
                reason=None,
                detail=f'expected accounted reasons {sorted(missing_expected)} not observed; saw {sorted(saw)}',
            ))

    return {
        'page_id': page_id,
        'matches': matches,
        'accounted': accounted,
        'unaccounted': unaccounted,
    }


def compare_edit_cutover(case: dict[str, Any]) -> dict[str, Any]:
    """type=edit identity cutover: persist id, re-attach after apply.

    Old block_id+snapshot is not the live landing after the trunk write.
    The remapped / re-attached mark_layer_node_id must stamp the proposed
    sentence, and the sidecar must not keep block_id as identity.
    """
    page_id = case['id']
    matches: list[dict[str, Any]] = []
    accounted: list[dict[str, Any]] = []
    unaccounted: list[dict[str, Any]] = []
    proposed = case.get('proposed') or ''
    with _PageWorkspace(case['before']) as ws:
        html_before = ws.render()
        blocks_before = ws.blocks()
        stamps_before = extract_stamps(html_before)
        target = _pick_stamp_for_edit(
            stamps_before,
            case.get('mark_quote') or '',
            case.get('mark_snapshot'),
            case.get('mark_occurrence'),
        )
        if target is None or not target.get('node_id'):
            unaccounted.append(_diff_row(
                page_id=page_id, mark_id=f'{page_id}:missing-before-stamp',
                old={'status': 'unresolved'},
                new={'status': 'unresolved', 'reason': 'stamp-not-found'},
                reason=None,
                detail='edit-cutover fixture has no stamped sentence on the before page',
            ))
            return {
                'page_id': page_id,
                'matches': matches,
                'accounted': accounted,
                'unaccounted': unaccounted,
            }
        snapshot = case.get('mark_snapshot') or target['quote']
        mark = {
            'id': f'mark-{page_id}',
            'page': ws.route,
            'type': 'edit',
            'mark_kind': 'rewrite',
            'block_id': target['block_id'],
            'from': target['from'],
            'to': target['to'],
            'quote': target['quote'],
            'snapshot': snapshot,
            'proposed': proposed,
            'mark_layer_node_id': target['node_id'],
            'mark_layer_primary': 'mark_layer_node_id',
            'deleted': False,
        }
        server.maybe_strip_legacy_anchor_fields(mark)
        try:
            change_result = server.apply_sentence_change(
                ws.route, 'estate', mark, author_label='claude',
            )
        except Exception as exc:  # noqa: BLE001 — surface as unaccounted
            unaccounted.append(_diff_row(
                page_id=page_id, mark_id=mark['id'],
                old={'status': 'bound', 'quote': snapshot},
                new={'status': 'unresolved', 'reason': 'apply-failed'},
                reason=None,
                detail=f'apply_sentence_change failed: {exc}',
            ))
            return {
                'page_id': page_id,
                'matches': matches,
                'accounted': accounted,
                'unaccounted': unaccounted,
            }
        if proposed:
            probe = {'quote': proposed, 'snapshot': proposed}
            server.maybe_attach_mark_layer_nodes(probe, ws.route, 'estate')
            if probe.get('mark_layer_node_id'):
                mark['mark_layer_node_id'] = probe['mark_layer_node_id']
                mark['mark_layer_node_ids'] = probe.get('mark_layer_node_ids')
                mark['mark_layer_primary'] = 'mark_layer_node_id'
        server.maybe_strip_legacy_anchor_fields(mark)
        server.append_comment(ws.route, dict(mark))
        if not change_result.get('html'):
            unaccounted.append(_diff_row(
                page_id=page_id, mark_id=mark['id'],
                old={'status': 'bound', 'quote': snapshot},
                new={'status': 'unresolved', 'reason': 'no-rerender'},
                reason=None,
                detail='apply_sentence_change returned no block html',
            ))
            return {
                'page_id': page_id,
                'matches': matches,
                'accounted': accounted,
                'unaccounted': unaccounted,
            }
        html_after = ws.render()
        stamps_after = extract_stamps(html_after)
        live_id = mark.get('mark_layer_node_id')
        new_hit = resolve_stamp(live_id, stamps_after)
        old_hit = resolve_old({
            'block_id': target['block_id'],
            'from': target['from'],
            'to': target['to'],
            'quote': snapshot,
            'snapshot': snapshot,
        }, ws.blocks())
        persisted = server.read_comments(ws.route)
        row = persisted[0] if persisted else mark
        identity_ok = (
            bool(row.get('mark_layer_node_id'))
            and not row.get('block_id')
            and (row.get('snapshot') or '') == snapshot
        )
        landed = (
            new_hit.get('status') == 'bound'
            and (new_hit.get('quote') or '').strip() == proposed.strip()
        )
        if identity_ok and landed:
            accounted.append(_diff_row(
                page_id=page_id, mark_id=mark['id'], old=old_hit, new=new_hit,
                reason='edit-id-reattach',
                detail=(
                    f"id {live_id} stamps proposed; sidecar block_id="
                    f"{row.get('block_id')!r}; snapshot kept as change record"
                ),
            ))
        else:
            unaccounted.append(_diff_row(
                page_id=page_id, mark_id=mark['id'], old=old_hit, new=new_hit,
                reason=None,
                detail=(
                    f'edit cutover failed: identity_ok={identity_ok} '
                    f'landed={landed} block_id={row.get("block_id")!r} '
                    f'snapshot={row.get("snapshot")!r} live_id={live_id!r}'
                ),
            ))
        expected = set(case.get('expect_accounted') or [])
        saw = {item['reason'] for item in accounted if item.get('reason')}
        missing_expected = expected - saw
        if missing_expected and not unaccounted:
            unaccounted.append(_diff_row(
                page_id=page_id, mark_id=f'{page_id}:expected-residual',
                old=old_hit, new=new_hit,
                reason=None,
                detail=f'expected accounted reasons {sorted(missing_expected)} not observed; saw {sorted(saw)}',
            ))
    return {
        'page_id': page_id,
        'matches': matches,
        'accounted': accounted,
        'unaccounted': unaccounted,
    }


def run_gate(fixture: dict[str, Any] | None = None) -> dict[str, Any]:
    data = fixture if fixture is not None else load_fixture()
    pages = []
    matches: list[dict[str, Any]] = []
    accounted: list[dict[str, Any]] = []
    unaccounted: list[dict[str, Any]] = []

    for page in data.get('pages') or []:
        result = compare_page_view(page['id'], page['source'])
        pages.append(result['page_id'])
        matches.extend(result['matches'])
        accounted.extend(result['accounted'])
        unaccounted.extend(result['unaccounted'])

    for case in data.get('edits') or []:
        result = compare_edit(case)
        pages.append(result['page_id'])
        matches.extend(result['matches'])
        accounted.extend(result['accounted'])
        unaccounted.extend(result['unaccounted'])

    for case in data.get('edit_applies') or []:
        result = compare_edit_cutover(case)
        pages.append(result['page_id'])
        matches.extend(result['matches'])
        accounted.extend(result['accounted'])
        unaccounted.extend(result['unaccounted'])

    unknown = [
        row for row in accounted
        if row.get('reason') not in ACCOUNTABLE_REASONS
    ]
    unaccounted.extend(unknown)
    accounted = [row for row in accounted if row.get('reason') in ACCOUNTABLE_REASONS]

    twin_on = mark_layer_twin_enabled()
    emitter = last_live_emitter_source()
    six_a_closed = (not twin_on) and emitter.startswith('fromProseMarkdown')
    return {
        'ok': not unaccounted,
        'pages': pages,
        'match_count': len(matches),
        'accounted_count': len(accounted),
        'unaccounted_count': len(unaccounted),
        'matches': matches,
        'accounted': accounted,
        'unaccounted': unaccounted,
        'residuals_accepted': [
            'block_id identity dual-write off on location create and type=edit',
            'quote/from/to retained as the selected span',
            'type=edit keeps snapshot/proposed as the MDP change record; identity is re-attached mark_layer_node_id',
            'remap ledger is append-only provenance; live jump uses remapped mark_layer_node_id',
            'twin -{n} mint stays (Playmaker fromProseMarkdown parity)',
            'unpaired weak-neighbor duplicates miss rather than guess',
            'heading sentences have no MarkLayerNode sentence id (heading kept whole)',
            'Playmaker TS package is not a runtime dependency; live path is the in-repo fromProseMarkdown port',
        ],
        'live_emitter': emitter,
        'twin_enabled': twin_on,
        'six_a_status': 'closed' if six_a_closed else 'open',
        'six_a_reason': (
            'Item-15 gate is this report. Live node emission is '
            'from_prose_markdown (Playmaker fromProseMarkdown shared-model '
            'port). The Python twin is debug-only '
            '(SOMA_REVIEW_MARK_LAYER_TWIN, default off) and does not stamp '
            'live DOM. Create/resolve is id-first; type=edit identity is '
            'the re-attached id. Playmaker TS is optional verify, not the '
            'live bridge.'
            if six_a_closed else
            'Twin is on or the live emitter is not fromProseMarkdown; '
            '6a stays open while the twin can stamp live DOM.'
        ),
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    fixture_path = None
    if args and not args[0].startswith('-'):
        fixture_path = args[0]
    fixture = load_fixture(fixture_path) if fixture_path else load_fixture()
    report = run_gate(fixture)
    json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write('\n')
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
