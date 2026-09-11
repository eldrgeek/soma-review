"""load_page_mark_layer_nodes()'s HTTP-bridge wiring (item 6a, 2026-09-11
mission-1 third bridging slice) -- the first REAL (non-debug) call site,
backing resolve_mark_block()'s two callers, i.e. actual mark create/resolve
traffic. Pins:
  (a) flag off -> twin nodes, unaffected
  (b) flag on + bridge succeeds -> bridge's nodes are returned
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


class LoadPageMarkLayerNodesBridgeTests(unittest.TestCase):
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

    def test_flag_off_uses_twin(self):
        os.environ.pop('SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE', None)
        _blocks, nodes = server.load_page_mark_layer_nodes('docs/page.md', 'estate')
        self.assertEqual(nodes, server.to_mark_layer_nodes(PAGE))

    def test_flag_on_bridge_success_uses_bridge_nodes(self):
        os.environ['SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE'] = '1'
        sentinel_nodes = [{'id': 'sentinel', 'kind': 'paragraph', 'fragments': []}]
        server.to_mark_layer_nodes_via_http_bridge = lambda src, timeout=2.0: sentinel_nodes
        _blocks, nodes = server.load_page_mark_layer_nodes('docs/page.md', 'estate')
        self.assertEqual(nodes, sentinel_nodes)

    def test_flag_on_bridge_failure_falls_back_to_twin_no_exception(self):
        os.environ['SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE'] = '1'

        def failing_bridge(src, timeout=2.0):
            raise ConnectionRefusedError('bridge down')

        server.to_mark_layer_nodes_via_http_bridge = failing_bridge
        _blocks, nodes = server.load_page_mark_layer_nodes('docs/page.md', 'estate')
        self.assertEqual(nodes, server.to_mark_layer_nodes(PAGE))


if __name__ == '__main__':
    unittest.main()
