"""6a edit-rebind: stored mark_layer_node_id survives a mid-doc edit.

After an earlier-paragraph insert that would shift occurrence suffixes,
render_page remaps the stored id onto the new parse so jump can still
querySelector the stamp. Does not claim 6a closed — the suffix mint,
weak-neighbor pairing, and later-block stamps until full-page rebind
remain.
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
from mark_layer_adapter import to_mark_layer_nodes  # noqa: E402


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
        self.assertEqual(
            {'from': ready_before[1], 'to': ready_after[2]},
            saved[0].get('mark_layer_node_rebound'),
        )

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


if __name__ == '__main__':
    unittest.main()
