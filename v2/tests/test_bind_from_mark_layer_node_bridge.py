"""bind_from_mark_layer_node()'s HTTP-bridge wiring (item 6a, 2026-09-11
mission-1 fifth bridging slice) -- the last of the three hot-path call sites
named in soma-review/CLAUDE.md's scoping note (live create-binding path,
reached from every id-first mark create). Pins the same three cases as the
third/fourth slices' tests:
  (a) flag off -> twin nodes, unaffected
  (b) flag on + bridge succeeds -> bridge's nodes back the binding
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


class BindFromMarkLayerNodeBridgeTests(unittest.TestCase):
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
        # Resolve a real node id from the twin so the candidate is one the
        # binder can actually find (a miss returns None before the bridge
        # branch is even reached).
        _src, blocks, _mapping, _report = server.current_page_blocks('docs/page.md', 'estate')
        nodes = server.to_mark_layer_nodes(server._mark_layer_source(PAGE))
        self.node_id = nodes[0]['id']

    def tearDown(self):
        server.PROJECTS_ROOT = self.old_root
        server.WORKSPACES_CONFIG = self.old_config
        server.to_mark_layer_nodes_via_http_bridge = self.old_bridge_fn
        if self.old_env is None:
            os.environ.pop('SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE', None)
        else:
            os.environ['SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE'] = self.old_env
        self.tmp.cleanup()

    def test_flag_off_binds_normally(self):
        os.environ.pop('SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE', None)
        result = server.bind_from_mark_layer_node(
            'docs/page.md', 'estate', {'mark_layer_node_id': self.node_id})
        self.assertIsNotNone(result)
        self.assertEqual(result['mark_layer_node_id'], self.node_id)

    def test_flag_on_bridge_success_calls_the_bridge(self):
        os.environ['SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE'] = '1'
        calls = []

        def spy_bridge(src, timeout=2.0):
            calls.append(src)
            return server.to_mark_layer_nodes(src)

        server.to_mark_layer_nodes_via_http_bridge = spy_bridge
        result = server.bind_from_mark_layer_node(
            'docs/page.md', 'estate', {'mark_layer_node_id': self.node_id})
        # Proves the flag branch actually ran the bridge function, not just
        # that the twin's output happens to match (the same gap Skip caught
        # on the fourth slice's first test draft).
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(result)
        self.assertEqual(result['mark_layer_node_id'], self.node_id)

    def test_flag_on_bridge_failure_falls_back_to_twin_no_exception(self):
        os.environ['SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE'] = '1'
        calls = []

        def failing_bridge(src, timeout=2.0):
            calls.append(src)
            raise ConnectionRefusedError('bridge down')

        server.to_mark_layer_nodes_via_http_bridge = failing_bridge
        # Must not raise -- the fallback path inside bind_from_mark_layer_node's
        # try/except must still produce a usable binding.
        result = server.bind_from_mark_layer_node(
            'docs/page.md', 'estate', {'mark_layer_node_id': self.node_id})
        self.assertEqual(len(calls), 1)  # confirms the bridge was attempted
        self.assertIsNotNone(result)
        self.assertEqual(result['mark_layer_node_id'], self.node_id)


if __name__ == '__main__':
    unittest.main()
