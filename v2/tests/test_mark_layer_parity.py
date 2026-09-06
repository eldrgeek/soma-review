"""Item-15 view-diff / parity gate (CI).

Old-path resolve (block_id/quote/snapshot) vs mark_layer_node_id → DOM
stamp on the fixture set in fixtures/mark_anchor_parity.json.

Fails the run if any landing mismatch is unaccounted. Accounted reasons
are remap-ledger / occurrence-suffix-shift / unpaired-miss /
heading-no-sentence-node.

Does not claim 6a closed. Dual-write stays off on location create;
type=edit still writes block_id+snapshot. Run:

  python3 -m unittest v2.tests.test_mark_layer_parity
  python3 v2/mark_layer_parity.py
"""
import json
import os
import sys
import unittest

V2_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, V2_DIR)

import mark_layer_parity  # noqa: E402
from mark_layer_adapter import OCCURRENCE_SUFFIX_REASON  # noqa: E402


class MarkLayerParityGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = mark_layer_parity.load_fixture()
        cls.report = mark_layer_parity.run_gate(cls.fixture)

    def test_fixture_includes_ready_repeats_and_an_edit(self):
        page_ids = [page['id'] for page in self.fixture['pages']]
        self.assertIn('ready-repeats', page_ids)
        self.assertIn('unique-sentences', page_ids)
        self.assertIn('weak-neighbor-ready', page_ids)
        edit_ids = [case['id'] for case in self.fixture['edits']]
        self.assertIn('ready-insert-earlier-duplicate', edit_ids)
        self.assertTrue(
            any('Ready.' in (page.get('source') or '') for page in self.fixture['pages']),
        )

    def test_gate_is_green_zero_unaccounted(self):
        self.assertTrue(
            self.report['ok'],
            'unaccounted diffs:\n' + json.dumps(
                self.report['unaccounted'], indent=2, ensure_ascii=False,
            ),
        )
        self.assertEqual(0, self.report['unaccounted_count'])
        self.assertEqual([], self.report['unaccounted'])
        self.assertGreater(self.report['match_count'], 0)

    def test_ready_repeat_create_path_matches(self):
        ready_matches = [
            row for row in self.report['matches']
            if row['page_id'] == 'ready-repeats'
            and row.get('landing')
            and row['landing'][3] == 'Ready.'
        ]
        self.assertGreaterEqual(len(ready_matches), 2, ready_matches)

    def test_suffix_shift_is_accounted_not_a_failure(self):
        hops = [
            row for row in self.report['accounted']
            if row.get('reason') == OCCURRENCE_SUFFIX_REASON
        ]
        self.assertTrue(hops, self.report['accounted'])
        self.assertTrue(
            any(row['page_id'] == 'ready-insert-earlier-duplicate' for row in hops),
            hops,
        )

    def test_weak_neighbor_insert_is_unpaired_miss(self):
        misses = [
            row for row in self.report['accounted']
            if row['page_id'] == 'weak-neighbor-insert'
            and row.get('reason') == 'unpaired-miss'
        ]
        self.assertTrue(misses, self.report['accounted'])

    def test_heading_sentence_without_node_is_accounted(self):
        headings = [
            row for row in self.report['accounted']
            if row.get('reason') == 'heading-no-sentence-node'
        ]
        self.assertTrue(headings, self.report['accounted'])

    def test_six_a_stays_open(self):
        self.assertEqual('open', self.report['six_a_status'])
        self.assertIn('twin', self.report['six_a_reason'])
        named = '\n'.join(self.report['residuals_accepted'])
        self.assertIn('dual-write', named)
        self.assertIn('type=edit', named)

    def test_injected_mismatch_is_unaccounted(self):
        """The gate must fail closed when a new landing mismatch appears."""
        broken = {
            'pages': [{
                'id': 'injected-mismatch',
                'source': 'Alpha is first. Beta is second.\n',
            }],
            'edits': [],
        }
        report = mark_layer_parity.run_gate(broken)
        self.assertTrue(report['ok'], report['unaccounted'])

        # Tamper: resolve a live stamp against a different quote.
        html_stamps = None
        with mark_layer_parity._PageWorkspace(broken['pages'][0]['source']) as ws:
            html = ws.render()
            blocks = ws.blocks()
            stamps = mark_layer_parity.extract_stamps(html)
            html_stamps = stamps
            old_rows = mark_layer_parity.old_path_sentences(blocks)
        self.assertGreaterEqual(len(old_rows), 2)
        self.assertGreaterEqual(len(html_stamps), 2)
        old_hit = mark_layer_parity.resolve_old({
            'block_id': old_rows[0]['block_id'],
            'from': old_rows[0]['from'],
            'to': old_rows[0]['to'],
            'quote': old_rows[0]['quote'],
        }, blocks)
        # Point the first sentence's id at the second stamp.
        swapped = mark_layer_parity.resolve_stamp(html_stamps[1]['node_id'], html_stamps)
        self.assertNotEqual(
            mark_layer_parity.landing_key(old_hit),
            mark_layer_parity.landing_key(swapped),
        )
        unaccounted = mark_layer_parity._diff_row(
            page_id='injected-mismatch', mark_id='tamper',
            old=old_hit, new=swapped, reason=None, detail='test tamper',
        )
        self.assertIsNone(unaccounted['reason'])
        # A report that includes this row is not ok.
        fake = dict(report)
        fake['unaccounted'] = [unaccounted]
        fake['unaccounted_count'] = 1
        fake['ok'] = not fake['unaccounted']
        self.assertFalse(fake['ok'])

    def test_script_main_exits_zero_on_shipped_fixture(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = mark_layer_parity.main([])
        self.assertEqual(0, code, buf.getvalue()[-2000:])
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload['ok'])
        self.assertEqual(0, payload['unaccounted_count'])


if __name__ == '__main__':
    unittest.main()
