"""Tests for the v3 reader-signal features added 2026-09-03 (Mike's spec):

- header "I've given up" / end-of-doc "I'm done" buttons persist a
  `type: mark, mark_kind: reader-signal` row to the same sidecar the other
  marks use (round trip via a real HTTP POST, the same pattern
  test_server_anchoring.py uses for `test_post_mark_persists_review_metadata`).
- GET /api/read-state surfaces the latest such mark for a harvesting session.
- the v3 "terms" marks-panel filter defaults off (visual gating is CSS-only
  and covered in the server module directly, since it has no DOM to assert
  against without a browser).
"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

V2_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, V2_DIR)

import server  # noqa: E402


class ReaderSignalHTTPTests(unittest.TestCase):
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
        with open(self.doc, 'w', encoding='utf-8') as handle:
            handle.write('# Title\n\nFirst paragraph.\n\nSecond paragraph.\n')
        self.old_root = server.PROJECTS_ROOT
        self.old_config = server.WORKSPACES_CONFIG
        server.PROJECTS_ROOT = self.root
        server.WORKSPACES_CONFIG = self.config
        server.render_page('docs/page.md', view='v3')
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

    def _post(self, payload):
        data = json.dumps(payload).encode('utf-8')
        request = urllib.request.Request(
            f'http://127.0.0.1:{self.httpd.server_port}/api/comments',
            data=data, headers={'Content-Type': 'application/json'}, method='POST',
        )
        with urllib.request.urlopen(request) as response:
            return response.status, json.load(response)

    def _get(self, path):
        with urllib.request.urlopen(f'http://127.0.0.1:{self.httpd.server_port}{path}') as response:
            return response.status, json.load(response)

    def test_gave_up_signal_round_trips_to_sidecar(self):
        status, row = self._post({
            'page': 'docs/page.md', 'type': 'mark', 'mark_kind': 'reader-signal',
            'signal': 'gave-up', 'last_read_block': 'blk-2',
        })
        self.assertEqual(201, status)
        self.assertEqual('mark', row['type'])
        self.assertEqual('reader-signal', row['mark_kind'])
        self.assertEqual('reader-signal', row['kind'])
        self.assertEqual('gave-up', row['signal'])
        self.assertEqual('blk-2', row['last_read_block'])

        saved = [c for c in server.read_comments('docs/page.md') if c['id'] == row['id']]
        self.assertEqual(1, len(saved))
        self.assertEqual('gave-up', saved[0]['signal'])

    def test_done_signal_carries_read_states_map(self):
        read_states = {'blk-1': 'read', 'blk-2': 'skipped', 'blk-3': 'unreached'}
        status, row = self._post({
            'page': 'docs/page.md', 'type': 'mark', 'mark_kind': 'reader-signal',
            'signal': 'done', 'last_read_block': 'blk-2', 'read_states': read_states,
        })
        self.assertEqual(201, status)
        self.assertEqual('done', row['signal'])
        self.assertEqual(read_states, row['read_states'])

    def test_invalid_signal_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post({
                'page': 'docs/page.md', 'type': 'mark', 'mark_kind': 'reader-signal',
                'signal': 'bogus',
            })
        self.assertEqual(400, ctx.exception.code)

    def test_second_signal_of_same_kind_is_independent_row(self):
        # The client disables its own button after one click (server has no
        # opinion on duplicates) — confirm two posts just produce two rows,
        # and read-state reports whichever is newest.
        self._post({'page': 'docs/page.md', 'type': 'mark', 'mark_kind': 'reader-signal',
                     'signal': 'gave-up', 'last_read_block': 'blk-1'})
        self._post({'page': 'docs/page.md', 'type': 'mark', 'mark_kind': 'reader-signal',
                     'signal': 'done', 'last_read_block': 'blk-2', 'read_states': {'blk-1': 'read'}})
        rows = [c for c in server.read_comments('docs/page.md') if c.get('mark_kind') == 'reader-signal']
        self.assertEqual(2, len(rows))

    def test_read_state_endpoint_returns_latest_signal(self):
        self._post({'page': 'docs/page.md', 'type': 'mark', 'mark_kind': 'reader-signal',
                     'signal': 'gave-up', 'last_read_block': 'blk-1'})
        self._post({'page': 'docs/page.md', 'type': 'mark', 'mark_kind': 'reader-signal',
                     'signal': 'done', 'last_read_block': 'blk-2',
                     'read_states': {'blk-1': 'read', 'blk-2': 'read'}})
        status, body = self._get('/api/read-state?page=docs%2Fpage.md')
        self.assertEqual(200, status)
        self.assertTrue(body['found'])
        self.assertEqual('done', body['signal'])
        self.assertEqual('blk-2', body['last_read_block'])
        self.assertEqual({'blk-1': 'read', 'blk-2': 'read'}, body['read_states'])

    def test_read_state_endpoint_no_signals_yet(self):
        status, body = self._get('/api/read-state?page=docs%2Fpage.md')
        self.assertEqual(200, status)
        self.assertFalse(body['found'])

    def test_reader_signal_excluded_from_ordinary_marks_shape(self):
        # A reader-signal mark must round-trip through the same /api/comments
        # list an ordinary mark does (V3_JS's client-side filter, not the
        # server, keeps it out of the marks panel) — assert the shape is
        # there for the client to filter on.
        self._post({'page': 'docs/page.md', 'type': 'mark', 'mark_kind': 'reader-signal',
                     'signal': 'gave-up'})
        status, body = self._get('/api/comments?page=docs%2Fpage.md')
        self.assertEqual(200, status)
        self.assertEqual(1, len(body))
        self.assertEqual('reader-signal', body[0]['mark_kind'])


class V3TermsFilterDefaultOffTests(unittest.TestCase):
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

    def test_v3_ships_terms_toggle_default_off_and_gives_up_done_buttons(self):
        self.write_doc('# Title\n\nAlpha beta gamma.\n')
        html = server.render_page('docs/page.md', view='v3')
        self.assertIn('let termsOn = false;', html)
        self.assertIn('v3-terms-chip', html)
        # The default-off CSS gate: v3 pages neutralize term-link styling
        # unless body carries v3-terms-on, so "off by default" is enforced
        # even before any JS runs (progressive: works pre-hydration too).
        self.assertIn('body.v3-view a.term-link{color:inherit', html)
        self.assertIn('body.v3-view.v3-terms-on a.term-link{color:#a3c9ff', html)
        self.assertIn("I've given up", html)
        self.assertIn("I'm done", html)
        self.assertIn('v3-giveup-btn', html)
        self.assertIn('v3-done-btn', html)

    def test_classic_gets_none_of_the_reader_signal_chrome(self):
        self.write_doc('# Title\n\nAlpha beta gamma.\n')
        html = server.render_page('docs/page.md')
        self.assertNotIn('v3-giveup-btn', html)
        self.assertNotIn('v3-done-btn', html)
        self.assertNotIn('v3-terms-chip', html)
        # Classic's shared PAGE_CSS term-link rule is untouched/unshadowed.
        self.assertNotIn('body.v3-view a.term-link', html)


if __name__ == '__main__':
    unittest.main()
