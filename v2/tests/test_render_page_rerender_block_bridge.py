"""render_page()'s and _rerender_block()'s HTTP-bridge wiring (item 6a,
2026-09-11 mission-1 sixth bridging slice) -- the last two call sites named
in soma-review/CLAUDE.md's scoping note ("render_page" runs on every
classic-view page load; "_rerender_block" is reached from every edit).
Pins the same three cases as the third/fourth/fifth slices' tests:
  (a) flag off -> twin nodes, unaffected
  (b) flag on + bridge succeeds -> bridge's nodes back the computation
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


class _BridgeTestBase(unittest.TestCase):
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


class RenderPageBridgeTests(_BridgeTestBase):
    def test_flag_off_renders_normally(self):
        os.environ.pop('SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE', None)
        html = server.render_page('docs/page.md', 'estate')
        self.assertIn('Alpha', html)

    def test_flag_on_bridge_success_calls_the_bridge(self):
        os.environ['SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE'] = '1'
        calls = []

        def spy_bridge(src, timeout=2.0):
            calls.append(src)
            return server.to_mark_layer_nodes(src)

        server.to_mark_layer_nodes_via_http_bridge = spy_bridge
        html = server.render_page('docs/page.md', 'estate')
        self.assertGreaterEqual(len(calls), 1)
        self.assertIn('Alpha', html)

    def test_flag_on_bridge_failure_falls_back_to_twin_no_exception(self):
        os.environ['SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE'] = '1'
        calls = []

        def failing_bridge(src, timeout=2.0):
            calls.append(src)
            raise ConnectionRefusedError('bridge down')

        server.to_mark_layer_nodes_via_http_bridge = failing_bridge
        html = server.render_page('docs/page.md', 'estate')
        self.assertGreaterEqual(len(calls), 1)
        self.assertIn('Alpha', html)


class RerenderBlockBridgeTests(_BridgeTestBase):
    def _target_block(self):
        _src, blocks, _map, _report = server.current_page_blocks('docs/page.md', 'estate')
        return blocks[-1]

    def test_flag_off_rerenders_normally(self):
        os.environ.pop('SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE', None)
        block = self._target_block()
        fs_path = server.resolve_page('docs/page.md', 'estate')
        new_src = PAGE.replace('Beta is second.', 'Beta is revised.')
        result = server._rerender_block(
            'docs/page.md', 'estate', fs_path, new_src, block['id'], block,
            prev_src=PAGE,
        )
        self.assertIsNotNone(result['html'])

    def test_flag_on_bridge_success_calls_the_bridge(self):
        os.environ['SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE'] = '1'
        calls = []

        def spy_bridge(src, timeout=2.0):
            calls.append(src)
            return server.to_mark_layer_nodes(src)

        server.to_mark_layer_nodes_via_http_bridge = spy_bridge
        block = self._target_block()
        fs_path = server.resolve_page('docs/page.md', 'estate')
        new_src = PAGE.replace('Beta is second.', 'Beta is revised.')
        result = server._rerender_block(
            'docs/page.md', 'estate', fs_path, new_src, block['id'], block,
            prev_src=PAGE,
        )
        # Both the next-src and prev-src parses must go through the bridge.
        self.assertEqual(len(calls), 2)
        self.assertIsNotNone(result['html'])

    def test_flag_on_bridge_failure_falls_back_to_twin_no_exception(self):
        os.environ['SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE'] = '1'
        calls = []

        def failing_bridge(src, timeout=2.0):
            calls.append(src)
            raise ConnectionRefusedError('bridge down')

        server.to_mark_layer_nodes_via_http_bridge = failing_bridge
        block = self._target_block()
        fs_path = server.resolve_page('docs/page.md', 'estate')
        new_src = PAGE.replace('Beta is second.', 'Beta is revised.')
        # Must not raise -- the outer try/except in _rerender_block still
        # produces usable html even if the bridge attempt fails.
        result = server._rerender_block(
            'docs/page.md', 'estate', fs_path, new_src, block['id'], block,
            prev_src=PAGE,
        )
        # Pinned-engine fix (2026-09-11): the first bridge failure aborts
        # the whole call and both sides fall back to the twin together --
        # it does NOT independently retry the bridge for the second source
        # (that per-source-independent retry was exactly the mixed-engine
        # risk this fix closed), so only one bridge call happens.
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(result['html'])


if __name__ == '__main__':
    unittest.main()
