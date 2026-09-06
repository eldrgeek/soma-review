"""6a beside: new marks store MarkLayerNode ids; old wire path stays default.

Parity / success gate for the first write-side slice:
  (a) a mark on a known sentence gets a stable node id matching the adapter
  (b) old wire fields are still present and unchanged in shape
  (c) pages without nodes still create marks (graceful no-op on attach)

Does not rewire the live comment/mark client. Does not delete the twin
emitter. 6a stays open until live UI rides nodes.
"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request

V2_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, V2_DIR)

import blockmap  # noqa: E402
import server  # noqa: E402
from mark_layer_adapter import to_mark_layer_nodes  # noqa: E402


# Fields the live mark path already persisted before this beside-step.
# New optional node-id keys may appear alongside these; none of these
# may disappear or change type.
OLD_MARK_WIRE_FIELDS = (
    'id', 'page', 'type', 'anchor', 'snapshot', 'author', 'text',
    'timestamp', 'status', 'thread_id', 'deleted',
    'schema', 'block_id', 'from', 'to', 'quote', 'origin_quote',
    'block_text_sha', 'heading_path', 'source_sha', 'unresolved',
    'quote_verified_at',
    'mark_kind', 'strength',
)

SENTENCE_PAGE = (
    '# Review title\n\n'
    'Alpha is first. Beta is second. Gamma is third.\n'
)


class MarkLayerAttachHTTPTests(unittest.TestCase):
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
        self.httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        server.PROJECTS_ROOT = self.old_root
        server.WORKSPACES_CONFIG = self.old_config
        self.tmp.cleanup()

    def write_doc(self, text):
        with open(self.doc, 'w', encoding='utf-8') as handle:
            handle.write(text)

    def _post(self, payload):
        data = json.dumps(payload).encode('utf-8')
        request = urllib.request.Request(
            f'http://127.0.0.1:{self.httpd.server_port}/api/comments',
            data=data, headers={'Content-Type': 'application/json'}, method='POST',
        )
        with urllib.request.urlopen(request) as response:
            return response.status, json.load(response)

    def test_mark_on_known_sentence_gets_stable_adapter_node_id(self):
        self.write_doc(SENTENCE_PAGE)
        server.render_page('docs/page.md')
        mapping = blockmap.load_map(server.block_map_path('docs/page.md'))
        target = next(row for row in mapping['blocks'] if 'Beta is second.' in row.get('text', ''))
        src = open(self.doc, encoding='utf-8').read()
        expected = next(
            n for n in to_mark_layer_nodes(src)
            if n['kind'] == 'sentence'
            and n['fragments'][0]['text'].strip() == 'Beta is second.'
        )

        status, row = self._post({
            'page': 'docs/page.md', 'type': 'mark', 'mark_kind': 'ack',
            'block_id': target['id'], 'from': 16, 'to': 31,
            'quote': 'Beta is second.',
            'snapshot': 'Alpha is first. Beta is second. Gamma is third.',
        })
        self.assertEqual(201, status)
        self.assertEqual(expected['id'], row['mark_layer_node_id'])
        self.assertIn(expected['id'], row['mark_layer_node_ids'])

        # Stable across a second write and a second adapter call on the same text.
        status2, row2 = self._post({
            'page': 'docs/page.md', 'type': 'mark', 'mark_kind': 'agree',
            'block_id': target['id'], 'from': 16, 'to': 31,
            'quote': 'Beta is second.',
            'snapshot': 'Alpha is first. Beta is second. Gamma is third.',
        })
        self.assertEqual(201, status2)
        again = next(
            n for n in to_mark_layer_nodes(src)
            if n['kind'] == 'sentence'
            and n['fragments'][0]['text'].strip() == 'Beta is second.'
        )
        self.assertEqual(expected['id'], again['id'])
        self.assertEqual(expected['id'], row2['mark_layer_node_id'])

        saved = [c for c in server.read_comments('docs/page.md') if c['id'] == row['id']]
        self.assertEqual(1, len(saved))
        self.assertEqual(expected['id'], saved[0]['mark_layer_node_id'])

    def test_old_wire_fields_still_present_and_unchanged_in_shape(self):
        self.write_doc(SENTENCE_PAGE)
        server.render_page('docs/page.md')
        mapping = blockmap.load_map(server.block_map_path('docs/page.md'))
        target = next(row for row in mapping['blocks'] if 'Beta is second.' in row.get('text', ''))
        status, row = self._post({
            'page': 'docs/page.md', 'type': 'mark', 'mark_kind': 'rewrite',
            'block_id': target['id'], 'from': 16, 'to': 31,
            'quote': 'Beta is second.',
            'snapshot': 'Alpha is first. Beta is second. Gamma is third.',
            'text': 'Tighten this.', 'proposed': 'Beta is precise.',
            'reason': 'Less vague', 'strength': 2,
        })
        self.assertEqual(201, status)
        for field in OLD_MARK_WIRE_FIELDS:
            self.assertIn(field, row, f'old wire field {field!r} missing')
        self.assertEqual('mark', row['type'])
        self.assertEqual('rewrite', row['mark_kind'])
        self.assertEqual('docs/page.md', row['page'])
        self.assertEqual('Beta is second.', row['quote'])
        self.assertEqual('Alpha is first. Beta is second. Gamma is third.', row['snapshot'])
        self.assertEqual(target['id'], row['block_id'])
        self.assertEqual(16, row['from'])
        self.assertEqual(31, row['to'])
        self.assertEqual(2, row['schema'])
        self.assertFalse(row['unresolved'])
        self.assertIsInstance(row['id'], str)
        self.assertIsInstance(row['heading_path'], list)
        self.assertIsInstance(row['source_sha'], str)
        self.assertEqual('Tighten this.', row['text'])
        self.assertEqual('Beta is precise.', row['proposed'])
        self.assertEqual('Less vague', row['reason'])
        self.assertEqual(2.0, row['strength'])
        # Additive only — node ids sit beside the old fields, they do not
        # replace them.
        self.assertIn('mark_layer_node_id', row)
        self.assertNotEqual(row['mark_layer_node_id'], row['block_id'])

    def test_page_without_nodes_still_creates_mark(self):
        # Empty source → to_mark_layer_nodes([]) = []. A location-less mark
        # (same envelope as a page-level / reader-signal write) must still
        # persist; attach is a no-op.
        self.write_doc('')
        status, row = self._post({
            'page': 'docs/page.md', 'type': 'mark', 'mark_kind': 'ack',
            'text': 'Page-level ack',
        })
        self.assertEqual(201, status)
        self.assertEqual('mark', row['type'])
        self.assertEqual('ack', row['mark_kind'])
        self.assertEqual('docs/page.md', row['page'])
        self.assertEqual('Page-level ack', row['text'])
        self.assertEqual(2, row['schema'])
        self.assertIsNone(row['block_id'])
        self.assertIsNone(row['quote'])
        self.assertNotIn('mark_layer_node_id', row)
        self.assertNotIn('mark_layer_node_ids', row)
        saved = server.read_comments('docs/page.md')
        self.assertEqual(1, len(saved))
        self.assertEqual(row['id'], saved[0]['id'])
        self.assertNotIn('mark_layer_node_id', saved[0])


if __name__ == '__main__':
    unittest.main()
