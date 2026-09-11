"""`_mark_layer_nodes_pinned_by_source()` (2026-09-11 mission-1) -- closes the
third bridging slice's named cross-request residual: create and resolve are
two separate HTTP requests against `load_page_mark_layer_nodes`, so without
pinning, a bridge that flaps between the two calls could mint a twin-engine
id on one and a bridge-engine id on the other for the SAME page content.

Pins:
  (a) two calls for identical source text return the byte-identical nodes,
      even when the bridge is only up for the first call (the exact race).
  (b) the second call does not re-invoke the bridge/twin at all (proves the
      cache short-circuits, not just that the two engines happen to agree).
  (c) different source text is never served from another text's cache entry.
"""
import os
import sys
import unittest

V2_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, V2_DIR)

import server  # noqa: E402


class MarkLayerNodesPinnedBySourceTests(unittest.TestCase):
    def setUp(self):
        server._MARK_LAYER_NODES_CACHE.clear()
        self.old_env = os.environ.get('SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE')
        self.old_bridge_fn = server.to_mark_layer_nodes_via_http_bridge
        os.environ['SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE'] = '1'

    def tearDown(self):
        server._MARK_LAYER_NODES_CACHE.clear()
        server.to_mark_layer_nodes_via_http_bridge = self.old_bridge_fn
        if self.old_env is None:
            os.environ.pop('SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE', None)
        else:
            os.environ['SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE'] = self.old_env

    def test_bridge_flap_between_two_calls_does_not_mix_engines(self):
        src = 'Alpha is first. Beta is second.\n'
        calls = {'n': 0}
        bridge_nodes = [{'id': 'bridge-node', 'kind': 'paragraph', 'fragments': []}]

        def flapping_bridge(text, timeout=2.0):
            calls['n'] += 1
            if calls['n'] == 1:
                return bridge_nodes
            raise ConnectionRefusedError('bridge down for the second call')

        server.to_mark_layer_nodes_via_http_bridge = flapping_bridge

        first = server._mark_layer_nodes_pinned_by_source(src, 'docs/page.md')
        second = server._mark_layer_nodes_pinned_by_source(src, 'docs/page.md')

        self.assertEqual(first, bridge_nodes)
        self.assertEqual(second, bridge_nodes)
        self.assertEqual(first, second)
        # The second call must be served from the pinned cache, not a live
        # bridge/twin call -- proves this isn't just engine agreement.
        self.assertEqual(calls['n'], 1)

    def test_bind_from_mark_layer_node_shares_the_pin_with_load_page(self):
        """Skip finding #1 (2026-09-11): the first draft of this fix only
        routed `load_page_mark_layer_nodes` through the pinned cache, leaving
        `bind_from_mark_layer_node` (the create-binding path the original
        residual explicitly named) making its own independent bridge/twin
        choice. A create call and a later resolve call for the SAME page
        content must land on the identical cache entry, or the "cross-request
        residual is closed" claim is false for the exact pairing that named
        it.
        """
        import json
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        os.makedirs(os.path.join(root, 'docs'))
        page_src = '# Title\n\nAlpha is first. Beta is second.\n'
        with open(os.path.join(root, 'docs', 'page.md'), 'w', encoding='utf-8') as handle:
            handle.write(page_src)
        config = os.path.join(root, 'workspaces.json')
        with open(config, 'w', encoding='utf-8') as handle:
            json.dump({
                'estate': {
                    'label': 'Test', 'roots': [['docs', 'docs']],
                    'nav': [], 'home': 'docs/page.md', 'feedback_dir': 'feedback',
                    'nightly': False, 'tours': False,
                }
            }, handle)
        old_root, old_config = server.PROJECTS_ROOT, server.WORKSPACES_CONFIG
        server.PROJECTS_ROOT, server.WORKSPACES_CONFIG = root, config
        self.addCleanup(lambda: setattr(server, 'PROJECTS_ROOT', old_root))
        self.addCleanup(lambda: setattr(server, 'WORKSPACES_CONFIG', old_config))

        bridge_nodes = [{'id': 'bridge-node', 'kind': 'paragraph', 'fragments': []}]
        calls = {'n': 0}

        def bridge_once_then_down(text, timeout=2.0):
            calls['n'] += 1
            if calls['n'] == 1:
                return bridge_nodes
            raise ConnectionRefusedError('bridge down for the second (resolve) call')

        server.to_mark_layer_nodes_via_http_bridge = bridge_once_then_down

        # Create: bind_from_mark_layer_node, bridge up.
        create_result = server.bind_from_mark_layer_node(
            'docs/page.md', 'estate', {'mark_layer_node_id': 'bridge-node'})
        self.assertIsNotNone(create_result)
        self.assertEqual(create_result['mark_layer_node_id'], 'bridge-node')

        # Resolve: load_page_mark_layer_nodes, bridge now down -- must still
        # see the SAME (bridge-engine) nodes via the shared pinned cache,
        # not fall through to the twin for this content.
        _blocks, resolve_nodes = server.load_page_mark_layer_nodes('docs/page.md', 'estate')
        self.assertEqual(resolve_nodes, bridge_nodes)
        self.assertEqual(calls['n'], 1)  # bridge invoked once total, by create only

    def test_different_source_text_is_not_served_from_another_entry(self):
        server.to_mark_layer_nodes_via_http_bridge = lambda text, timeout=2.0: (
            (_ for _ in ()).throw(ConnectionRefusedError('bridge down'))
        )
        nodes_a = server._mark_layer_nodes_pinned_by_source('Alpha is first.\n', 'docs/a.md')
        nodes_b = server._mark_layer_nodes_pinned_by_source('Gamma is third.\n', 'docs/b.md')
        self.assertEqual(nodes_a, server.to_mark_layer_nodes('Alpha is first.\n'))
        self.assertEqual(nodes_b, server.to_mark_layer_nodes('Gamma is third.\n'))
        self.assertNotEqual(nodes_a, nodes_b)


if __name__ == '__main__':
    unittest.main()
