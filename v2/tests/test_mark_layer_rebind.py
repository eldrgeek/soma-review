"""6a remap-ledger: stored mark_layer_node_id survives a mid-doc edit.

After an earlier-paragraph insert that would shift occurrence suffixes,
render_page remaps the stored id onto the new parse so jump can still
querySelector the stamp. The remap is persisted on the page ledger
(`.mark-layer-nodes.json`) and on the row. `_rerender_block` restamps
subsequent blocks on the same edit so later marks do not keep stale
live stamps.
Does not claim 6a closed — twin still stamps live DOM. block_id
identity dual-write is off on location create and type=edit.
"""
import json
import os
import re
import sys
import tempfile
import unittest

V2_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, V2_DIR)

import blockmap  # noqa: E402
import server  # noqa: E402
from mark_layer_adapter import (  # noqa: E402
    to_mark_layer_nodes, occurrence_suffix, OCCURRENCE_SUFFIX_REASON,
)


BEFORE = (
    '# Review title\n\n'
    'Ready. Unique first context.\n\n'
    'Ready. Unique second context.\n'
)
AFTER = (
    '# Review title\n\n'
    'Ready. Brand new context.\n\n'
    'Ready. Unique first context.\n\n'
    'Ready. Unique second context.\n'
)


class MarkLayerEditRebindTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, 'docs'))
        self.doc = os.path.join(self.root, 'docs', 'page.md')
        self.config = os.path.join(self.root, 'workspaces.json')
        with open(self.config, 'w', encoding='utf-8') as handle:
            json.dump({
                'estate': {
                    'label': 'Test', 'roots': [['docs', 'docs']],
                    'nav': [], 'home': 'docs/page.md', 'feedback_dir': 'feedback',
                    'nightly': False, 'tours': False,
                }
            }, handle)
        self.old_root = server.PROJECTS_ROOT
        self.old_config = server.WORKSPACES_CONFIG
        server.PROJECTS_ROOT = self.root
        server.WORKSPACES_CONFIG = self.config

    def tearDown(self):
        server.PROJECTS_ROOT = self.old_root
        server.WORKSPACES_CONFIG = self.old_config
        self.tmp.cleanup()

    def write_doc(self, text):
        with open(self.doc, 'w', encoding='utf-8') as handle:
            handle.write(text)

    def _ready_ids(self, text):
        return [
            n['id'] for n in to_mark_layer_nodes(text)
            if n['kind'] == 'sentence'
            and n['fragments'][0]['text'].strip() == 'Ready.'
        ]

    def _create_mark_on_second_ready(self):
        self.write_doc(BEFORE)
        server.render_page('docs/page.md')
        mapping = blockmap.load_map(server.block_map_path('docs/page.md'))
        second = [row for row in mapping['blocks']
                  if (row.get('text') or '').startswith('Ready.')][1]
        ready = self._ready_ids(BEFORE)
        comment = {
            'id': 'mark-second-ready',
            'page': 'docs/page.md',
            'type': 'mark',
            'mark_kind': 'ack',
            'block_id': second['id'],
            'from': 0,
            'to': 6,
            'quote': 'Ready.',
            'snapshot': 'Ready. Unique second context.',
            'mark_layer_node_id': ready[1],
            'mark_layer_node_ids': [ready[1]],
            'deleted': False,
        }
        server.append_comment('docs/page.md', comment)
        return ready

    def test_mid_doc_insert_rebinds_stored_id_to_the_same_sentence(self):
        ready_before = self._create_mark_on_second_ready()
        self.write_doc(AFTER)
        html = server.render_page('docs/page.md')
        ready_after = self._ready_ids(AFTER)
        self.assertNotEqual(ready_before[1], ready_after[2])

        saved = server.read_comments('docs/page.md')
        self.assertEqual(1, len(saved))
        self.assertEqual(ready_after[2], saved[0]['mark_layer_node_id'])
        hop = {
            'from': ready_before[1], 'to': ready_after[2],
            'reason': OCCURRENCE_SUFFIX_REASON,
        }
        self.assertEqual(hop, saved[0].get('mark_layer_node_rebound'))
        self.assertEqual([hop], saved[0].get('mark_layer_node_rebind_history'))
        ledger = server.load_mark_layer_remap_ledger('docs/page.md')
        self.assertIn(hop | {'record_id': 'mark-second-ready'}, [
            {k: row.get(k) for k in ('from', 'to', 'reason', 'record_id')}
            for row in ledger
        ])

        stamped = re.findall(
            r'<span class="mark-sentence"[^>]*data-mark-layer-node-id="([^"]+)"[^>]*>Ready\.</span>',
            html,
        )
        self.assertEqual(ready_after, stamped)
        self.assertIn(f'data-mark-layer-node-id="{ready_after[2]}"', html)
        # The stored (rebound) id is on the third Ready., not the inserted first.
        self.assertEqual(ready_after[2], stamped[2])

    def test_mid_doc_unique_edit_keeps_the_same_id(self):
        # Editing an earlier unique paragraph must not remap a later unique
        # sentence — content-hash ids stay put; rebind is identity.
        self.write_doc(
            '# Review title\n\nAlpha is first.\n\nBeta is second.\n'
        )
        server.render_page('docs/page.md')
        mapping = blockmap.load_map(server.block_map_path('docs/page.md'))
        beta_block = next(
            row for row in mapping['blocks'] if 'Beta is second.' in row.get('text', '')
        )
        beta_id = next(
            n['id'] for n in to_mark_layer_nodes(
                '# Review title\n\nAlpha is first.\n\nBeta is second.\n'
            )
            if n['kind'] == 'sentence'
            and n['fragments'][0]['text'].strip() == 'Beta is second.'
        )
        server.append_comment('docs/page.md', {
            'id': 'mark-beta', 'page': 'docs/page.md', 'type': 'mark',
            'mark_kind': 'ack', 'block_id': beta_block['id'],
            'quote': 'Beta is second.', 'mark_layer_node_id': beta_id,
            'deleted': False,
        })
        self.write_doc(
            '# Review title\n\nAlpha is first. Inserted earlier.\n\nBeta is second.\n'
        )
        html = server.render_page('docs/page.md')
        saved = server.read_comments('docs/page.md')
        self.assertEqual(beta_id, saved[0]['mark_layer_node_id'])
        self.assertNotIn('mark_layer_node_rebound', saved[0])
        self.assertIn(f'data-mark-layer-node-id="{beta_id}"', html)
        self.assertEqual([], server.load_mark_layer_remap_ledger('docs/page.md'))

    def test_early_block_edit_restamps_later_duplicate(self):
        # Residual close: edit the first Ready. paragraph so a new Ready.
        # occurrence is inserted there. The later mark's suffix shifts;
        # _rerender_block must rebind the sidecar AND restamp the later
        # block in later_html — not wait for a full-page render.
        ready_before = self._create_mark_on_second_ready()
        mapping = blockmap.load_map(server.block_map_path('docs/page.md'))
        first = [row for row in mapping['blocks']
                 if (row.get('text') or '').startswith('Ready.')][0]
        mark = {
            'id': 'edit-early', 'page': 'docs/page.md', 'type': 'edit',
            'snapshot': 'Ready. Unique first context.',
            # A new Ready. *paragraph* (not a same-block second sentence —
            # those get a leading space and a different hash) so the later
            # occurrence suffix actually shifts.
            'proposed': 'Ready. Unique first context.\n\nReady. Inserted earlier.',
            'block_id': first['id'], 'deleted': False,
        }
        result = server.apply_sentence_change(
            'docs/page.md', 'estate', mark, author_label='claude',
        )
        later = [item for item in (result.get('later_html') or [])
                 if 'Unique second context' in (item.get('html') or '')]
        self.assertTrue(later, 'subsequent Ready. block must be restamped')
        ready_after = self._ready_ids(
            '# Review title\n\n'
            'Ready. Unique first context.\n\n'
            'Ready. Inserted earlier.\n\n'
            'Ready. Unique second context.\n'
        )
        self.assertEqual(3, len(ready_after))
        self.assertNotEqual(ready_before[1], ready_after[2])
        saved = [c for c in server.read_comments('docs/page.md')
                 if c['id'] == 'mark-second-ready']
        self.assertEqual(1, len(saved))
        self.assertEqual(ready_after[2], saved[0]['mark_layer_node_id'])
        self.assertEqual(
            OCCURRENCE_SUFFIX_REASON,
            saved[0].get('mark_layer_node_rebound', {}).get('reason'),
        )
        ledger = server.load_mark_layer_remap_ledger('docs/page.md')
        self.assertTrue(
            any(
                row.get('from') == ready_before[1]
                and row.get('to') == ready_after[2]
                and row.get('reason') == OCCURRENCE_SUFFIX_REASON
                and row.get('record_id') == 'mark-second-ready'
                for row in ledger
            ),
            ledger,
        )
        self.assertIn(
            f'data-mark-layer-node-id="{ready_after[2]}"',
            later[0]['html'],
        )
        self.assertNotIn(
            f'data-mark-layer-node-id="{ready_before[1]}"',
            later[0]['html'],
        )

    def test_unique_sentence_gains_accounted_suffix_when_earlier_duplicate_lands(self):
        # Unique (kind, text) minted with no suffix. Inserting an earlier
        # twin (same sentence, unique sibling paragraph) forces the
        # Playmaker `-{n}` mint onto the later sentence; the stored id
        # remaps and the ledger accounts the suffix shift.
        before = (
            '# Review title\n\n'
            'Alpha is first. Unique context.\n\n'
            'Beta is second.\n'
        )
        after = (
            '# Review title\n\n'
            'Alpha is first. Brand new context.\n\n'
            'Alpha is first. Unique context.\n\n'
            'Beta is second.\n'
        )
        self.write_doc(before)
        server.render_page('docs/page.md')
        mapping = blockmap.load_map(server.block_map_path('docs/page.md'))
        alpha_block = next(
            row for row in mapping['blocks']
            if 'Unique context.' in (row.get('text') or '')
        )
        alpha_before = next(
            n['id'] for n in to_mark_layer_nodes(before)
            if n['kind'] == 'sentence'
            and n['fragments'][0]['text'].strip() == 'Alpha is first.'
        )
        self.assertIsNone(occurrence_suffix(alpha_before))
        server.append_comment('docs/page.md', {
            'id': 'mark-alpha', 'page': 'docs/page.md', 'type': 'mark',
            'mark_kind': 'ack', 'block_id': alpha_block['id'],
            'quote': 'Alpha is first.',
            'snapshot': 'Alpha is first. Unique context.',
            'mark_layer_node_id': alpha_before, 'deleted': False,
        })
        self.write_doc(after)
        html = server.render_page('docs/page.md')
        alpha_after = [
            n['id'] for n in to_mark_layer_nodes(after)
            if n['kind'] == 'sentence'
            and n['fragments'][0]['text'].strip() == 'Alpha is first.'
        ]
        self.assertEqual(2, len(alpha_after))
        self.assertEqual(alpha_before, alpha_after[0])  # first occ keeps stem
        self.assertNotEqual(alpha_before, alpha_after[1])
        saved = server.read_comments('docs/page.md')
        self.assertEqual(alpha_after[1], saved[0]['mark_layer_node_id'])
        self.assertEqual(
            OCCURRENCE_SUFFIX_REASON,
            saved[0]['mark_layer_node_rebound']['reason'],
        )
        ledger = server.load_mark_layer_remap_ledger('docs/page.md')
        self.assertTrue(any(
            row.get('from') == alpha_before
            and row.get('to') == alpha_after[1]
            and row.get('reason') == OCCURRENCE_SUFFIX_REASON
            for row in ledger
        ), ledger)
        self.assertIn(f'data-mark-layer-node-id="{alpha_after[1]}"', html)
        self.assertIn(f'id="{alpha_after[1]}"', html)


if __name__ == '__main__':
    unittest.main()
