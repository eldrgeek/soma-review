"""6a create rides MarkLayerNode id; dual-write of block_id/quote is off.

Parity / success gate:
  (a) a mark on a known sentence gets a stable node id matching the adapter
  (b) unique-match creates persist the id, not block_id/quote/snapshot
  (c) pages without nodes still create marks (graceful no-op on attach)
  (d) a client-supplied stamp id is the primary record, even for repeats
  (e) SOMA_REVIEW_MARK_LAYER_DUAL_WRITE=1 still writes the old fields
  (f) create with only mark_layer_node_id is enough

Does not claim 6a closed — twin `-{n}` mint and the item-15 gate
remain. Remap ledger is the identity model for suffix drift.
block_id identity dual-write is off on location create.
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

# Two paragraphs share the sentence "Ready."; snapshot must pick the
# occurrence. Skip 2026-09-06 nit 1.
REPEAT_PAGE = (
    '# Review title\n\n'
    'Ready. Unique first context.\n\n'
    'Ready. Unique second context.\n'
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
        with open(self.doc, encoding='utf-8') as handle:
            src = handle.read()
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
        self.assertEqual(2, row['schema'])
        self.assertFalse(row['unresolved'])
        self.assertIsInstance(row['id'], str)
        self.assertIsInstance(row['heading_path'], list)
        self.assertIsInstance(row['source_sha'], str)
        self.assertEqual('Tighten this.', row['text'])
        self.assertEqual('Beta is precise.', row['proposed'])
        self.assertEqual('Less vague', row['reason'])
        self.assertEqual(2.0, row['strength'])
        # Sole write path: node id is required; legacy anchors are cleared.
        self.assertIn('mark_layer_node_id', row)
        self.assertEqual('mark_layer_node_id', row.get('mark_layer_primary'))
        self.assertIsNone(row['block_id'])
        self.assertEqual('Beta is second.', row['quote'])
        self.assertEqual('', row['snapshot'])
        self.assertEqual(16, row['from'])
        self.assertEqual(31, row['to'])
        saved = [c for c in server.read_comments('docs/page.md') if c['id'] == row['id']]
        self.assertEqual(1, len(saved))
        self.assertIsNone(saved[0].get('block_id'))
        self.assertEqual(row['mark_layer_node_id'], saved[0]['mark_layer_node_id'])

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

    def test_repeated_sentence_snapshot_scopes_to_the_correct_occurrence(self):
        # Skip 2026-09-06 nit 1: snapshot of the second block attaches that
        # occurrence's node id, not the first "Ready." in document order.
        self.write_doc(REPEAT_PAGE)
        server.render_page('docs/page.md')
        mapping = blockmap.load_map(server.block_map_path('docs/page.md'))
        targets = [
            row for row in mapping['blocks']
            if (row.get('text') or '').startswith('Ready.')
        ]
        self.assertEqual(2, len(targets))
        second = targets[1]
        with open(self.doc, encoding='utf-8') as handle:
            src = handle.read()
        expected = [
            n for n in to_mark_layer_nodes(src)
            if n['kind'] == 'sentence'
            and n['fragments'][0]['text'].strip() == 'Ready.'
        ]
        self.assertEqual(2, len(expected))

        status, row = self._post({
            'page': 'docs/page.md', 'type': 'mark', 'mark_kind': 'ack',
            'block_id': second['id'], 'from': 0, 'to': 6,
            'quote': 'Ready.',
            'snapshot': 'Ready. Unique second context.',
        })
        self.assertEqual(201, status)
        self.assertEqual(expected[1]['id'], row['mark_layer_node_id'])
        self.assertIn(expected[1]['id'], row['mark_layer_node_ids'])
        saved = [c for c in server.read_comments('docs/page.md') if c['id'] == row['id']]
        self.assertEqual(1, len(saved))
        self.assertEqual(expected[1]['id'], saved[0]['mark_layer_node_id'])
        # Dual-write off: block_id is not the persisted identity; quote is the span.
        self.assertIsNone(row['block_id'])
        self.assertEqual('Ready.', row['quote'])
        self.assertEqual('', row['snapshot'])

    def test_repeated_sentence_misses_when_snapshot_does_not_narrow(self):
        # Skip 2026-09-06 nit 1: no unique snapshot → no node id. First-hit
        # attach would be silent and permanent in the sidecar.
        self.write_doc(REPEAT_PAGE)
        server.render_page('docs/page.md')
        mapping = blockmap.load_map(server.block_map_path('docs/page.md'))
        first = next(
            row for row in mapping['blocks']
            if (row.get('text') or '').startswith('Ready.')
        )
        payload = {
            'page': 'docs/page.md', 'type': 'mark', 'mark_kind': 'ack',
            'block_id': first['id'], 'from': 0, 'to': 6,
            'quote': 'Ready.',
        }
        status, row = self._post(payload)
        self.assertEqual(201, status)
        self.assertEqual('mark', row['type'])
        self.assertEqual('Ready.', row['quote'])
        self.assertEqual(first['id'], row['block_id'])
        self.assertNotIn('mark_layer_node_id', row)
        self.assertNotIn('mark_layer_node_ids', row)
        saved = [c for c in server.read_comments('docs/page.md') if c['id'] == row['id']]
        self.assertEqual(1, len(saved))
        self.assertNotIn('mark_layer_node_id', saved[0])

        status2, row2 = self._post({
            **payload, 'snapshot': 'no such paragraph',
        })
        self.assertEqual(201, status2)
        self.assertNotIn('mark_layer_node_id', row2)
        self.assertNotIn('mark_layer_node_ids', row2)

    def test_supplied_stamp_id_is_the_primary_create_record(self):
        # Unscoped repeated quote used to miss attach; the client stamp wins.
        self.write_doc(REPEAT_PAGE)
        server.render_page('docs/page.md')
        mapping = blockmap.load_map(server.block_map_path('docs/page.md'))
        second = [row for row in mapping['blocks']
                  if (row.get('text') or '').startswith('Ready.')][1]
        with open(self.doc, encoding='utf-8') as handle:
            src = handle.read()
        expected = [
            n for n in to_mark_layer_nodes(src)
            if n['kind'] == 'sentence'
            and n['fragments'][0]['text'].strip() == 'Ready.'
        ]
        status, row = self._post({
            'page': 'docs/page.md', 'type': 'mark', 'mark_kind': 'ack',
            'block_id': second['id'], 'from': 0, 'to': 6,
            'quote': 'Ready.',
            'mark_layer_node_id': expected[1]['id'],
        })
        self.assertEqual(201, status)
        self.assertEqual(expected[1]['id'], row['mark_layer_node_id'])
        self.assertEqual('mark_layer_node_id', row.get('mark_layer_primary'))
        saved = [c for c in server.read_comments('docs/page.md') if c['id'] == row['id']]
        self.assertEqual(expected[1]['id'], saved[0]['mark_layer_node_id'])
        self.assertIsNone(row.get('block_id'))

    def test_create_with_only_node_id_does_not_require_dual_fields(self):
        self.write_doc(SENTENCE_PAGE)
        server.render_page('docs/page.md')
        with open(self.doc, encoding='utf-8') as handle:
            src = handle.read()
        expected = next(
            n for n in to_mark_layer_nodes(src)
            if n['kind'] == 'sentence'
            and n['fragments'][0]['text'].strip() == 'Beta is second.'
        )
        status, row = self._post({
            'page': 'docs/page.md', 'type': 'mark', 'mark_kind': 'ack',
            'mark_layer_node_id': expected['id'],
        })
        self.assertEqual(201, status)
        self.assertEqual(expected['id'], row['mark_layer_node_id'])
        self.assertEqual('mark_layer_node_id', row.get('mark_layer_primary'))
        self.assertIsNone(row.get('block_id'))
        self.assertIsNone(row.get('quote'))
        saved = [c for c in server.read_comments('docs/page.md') if c['id'] == row['id']]
        self.assertEqual(expected['id'], saved[0]['mark_layer_node_id'])
        self.assertIsNone(saved[0].get('block_id'))

    def test_quote_id_mismatch_rejects_stamp_and_unique_matches(self):
        self.write_doc(SENTENCE_PAGE)
        server.render_page('docs/page.md')
        with open(self.doc, encoding='utf-8') as handle:
            src = handle.read()
        nodes = to_mark_layer_nodes(src)
        alpha = next(
            n for n in nodes
            if n['kind'] == 'sentence'
            and n['fragments'][0]['text'].strip() == 'Alpha is first.'
        )
        beta = next(
            n for n in nodes
            if n['kind'] == 'sentence'
            and n['fragments'][0]['text'].strip() == 'Beta is second.'
        )
        mapping = blockmap.load_map(server.block_map_path('docs/page.md'))
        target = next(row for row in mapping['blocks'] if 'Beta is second.' in row.get('text', ''))
        status, row = self._post({
            'page': 'docs/page.md', 'type': 'mark', 'mark_kind': 'ack',
            'block_id': target['id'], 'from': 16, 'to': 31,
            'quote': 'Beta is second.',
            'snapshot': 'Alpha is first. Beta is second. Gamma is third.',
            'mark_layer_node_id': alpha['id'],
        })
        self.assertEqual(201, status)
        self.assertEqual(beta['id'], row['mark_layer_node_id'])
        self.assertNotEqual(alpha['id'], row['mark_layer_node_id'])

    def test_dual_write_flag_still_persists_legacy_fields(self):
        self.write_doc(SENTENCE_PAGE)
        server.render_page('docs/page.md')
        mapping = blockmap.load_map(server.block_map_path('docs/page.md'))
        target = next(row for row in mapping['blocks'] if 'Beta is second.' in row.get('text', ''))
        old = os.environ.get('SOMA_REVIEW_MARK_LAYER_DUAL_WRITE')
        os.environ['SOMA_REVIEW_MARK_LAYER_DUAL_WRITE'] = '1'
        try:
            status, row = self._post({
                'page': 'docs/page.md', 'type': 'mark', 'mark_kind': 'ack',
                'block_id': target['id'], 'from': 16, 'to': 31,
                'quote': 'Beta is second.',
                'snapshot': 'Alpha is first. Beta is second. Gamma is third.',
            })
        finally:
            if old is None:
                os.environ.pop('SOMA_REVIEW_MARK_LAYER_DUAL_WRITE', None)
            else:
                os.environ['SOMA_REVIEW_MARK_LAYER_DUAL_WRITE'] = old
        self.assertEqual(201, status)
        self.assertIn('mark_layer_node_id', row)
        self.assertEqual(target['id'], row['block_id'])
        self.assertEqual('Beta is second.', row['quote'])
        self.assertEqual('Alpha is first. Beta is second. Gamma is third.', row['snapshot'])


if __name__ == '__main__':
    unittest.main()
