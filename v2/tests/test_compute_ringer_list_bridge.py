"""compute_ringer_list()'s HTTP-bridge wiring (item 6a, 2026-09-11 mission-1
fourth bridging slice) -- the interactive page-render hot path named in
soma-review/CLAUDE.md's scoping note (compute_ringer_list runs on every
classic-view page load via render_page). Pins the same three cases as the
third slice's test:
  (a) flag off -> twin nodes, unaffected
  (b) flag on + bridge succeeds -> bridge's nodes back the ringer computation
  (c) flag on + bridge fails -> falls back to twin nodes, no exception
"""
import json
import os
import sys
import tempfile
import unittest

V2_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, V2_DIR)

import server  # noqa: E402

PAGE = '# Title\n\nAlpha is first. Beta is second.\n'


class ComputeRingerListBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, 'docs'))
        self.doc = os.path.join(self.root, 'docs', 'page.md')
        with open(self.doc, 'w', encoding='utf-8') as handle:
            handle.write(PAGE)
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
        self.old_env = os.environ.get('SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE')
        self.old_bridge_fn = server.to_mark_layer_nodes_via_http_bridge

    def tearDown(self):
        server.PROJECTS_ROOT = self.old_root
        server.WORKSPACES_CONFIG = self.old_config
        server.to_mark_layer_nodes_via_http_bridge = self.old_bridge_fn
        if self.old_env is None:
            os.environ.pop('SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE', None)
        else:
            os.environ['SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE'] = self.old_env
        self.tmp.cleanup()

    def test_flag_off_computes_normally(self):
        os.environ.pop('SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE', None)
        result = server.compute_ringer_list('docs/page.md', 'estate')
        self.assertIn('ringers', result)

    def test_flag_on_bridge_success_calls_the_bridge(self):
        os.environ['SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE'] = '1'
        calls = []

        def spy_bridge(src, timeout=2.0):
            calls.append(src)
            return server.to_mark_layer_nodes(src)

        server.to_mark_layer_nodes_via_http_bridge = spy_bridge
        result = server.compute_ringer_list('docs/page.md', 'estate')
        # Proves the flag branch actually ran the bridge function, not just
        # that the twin's output happens to match (Skip's finding on the
        # first draft of this test: asserting only `'ringers' in result` is
        # true whether or not the bridge was ever called).
        self.assertEqual(len(calls), 1)
        self.assertIn('ringers', result)

    def test_flag_on_bridge_failure_falls_back_to_twin_no_exception(self):
        os.environ['SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE'] = '1'
        calls = []

        def failing_bridge(src, timeout=2.0):
            calls.append(src)
            raise ConnectionRefusedError('bridge down')

        server.to_mark_layer_nodes_via_http_bridge = failing_bridge
        # Must not raise -- the fallback path inside compute_ringer_list's
        # try/except must still produce a usable ringer result.
        result = server.compute_ringer_list('docs/page.md', 'estate')
        self.assertEqual(len(calls), 1)  # confirms the bridge was attempted
        self.assertIn('ringers', result)


if __name__ == '__main__':
    unittest.main()
