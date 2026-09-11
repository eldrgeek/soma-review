"""`_mark_layer_nodes_pinned_engine()` -- closes the mixed-engine residual
Skip named on `_rerender_block`'s sixth bridging slice (2026-09-11, item
6a): `next_nodes` and `prev_nodes` used to try the HTTP bridge
independently, so a bridge that answered one call and failed the other
could hand `align_mark_layer_nodes` one engine's nodes for one side and the
twin's nodes for the other within a single rerender. The fix computes every
source in one call from a single engine choice: the bridge for all of them,
or -- the instant any bridge call fails -- the twin for all of them,
including sources the bridge had already answered.

Pins three cases:
  (a) flag off -> every source via the twin, bridge never touched
  (b) flag on + bridge succeeds for every source -> every source via the
      bridge
  (c) flag on + bridge fails partway through -> EVERY source re-derived via
      the twin, not a mix of the two already-bridged sources plus a
      twin-derived failure -- this is the regression case; it fails red
      against the pre-fix per-source try/except shape.
"""
import os
import sys
import unittest

V2_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, V2_DIR)

import server  # noqa: E402

SRC_A = 'Alpha is first.\n\nBeta is second.\n'
SRC_B = 'Gamma is third.\n\nDelta is fourth.\n'


class PinnedEngineTests(unittest.TestCase):
    def setUp(self):
        self.old_env = os.environ.get('SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE')
        self.old_bridge_fn = server.to_mark_layer_nodes_via_http_bridge

    def tearDown(self):
        server.to_mark_layer_nodes_via_http_bridge = self.old_bridge_fn
        if self.old_env is None:
            os.environ.pop('SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE', None)
        else:
            os.environ['SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE'] = self.old_env

    def test_flag_off_uses_twin_for_every_source(self):
        os.environ.pop('SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE', None)
        calls = []
        server.to_mark_layer_nodes_via_http_bridge = (
            lambda src, timeout=2.0: calls.append(src) or []
        )
        a, b = server._mark_layer_nodes_pinned_engine(
            [SRC_A, SRC_B], 'docs/page.md', 'test')
        self.assertEqual(calls, [])
        self.assertEqual(a, server.to_mark_layer_nodes(SRC_A))
        self.assertEqual(b, server.to_mark_layer_nodes(SRC_B))

    def test_flag_on_bridge_success_uses_bridge_for_every_source(self):
        os.environ['SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE'] = '1'
        calls = []

        def spy_bridge(src, timeout=2.0):
            calls.append(src)
            return server.to_mark_layer_nodes(src)

        server.to_mark_layer_nodes_via_http_bridge = spy_bridge
        a, b = server._mark_layer_nodes_pinned_engine(
            [SRC_A, SRC_B], 'docs/page.md', 'test')
        self.assertEqual(calls, [SRC_A, SRC_B])
        self.assertEqual(a, server.to_mark_layer_nodes(SRC_A))
        self.assertEqual(b, server.to_mark_layer_nodes(SRC_B))

    def test_flag_on_bridge_fails_on_second_source_falls_back_for_both(self):
        # The regression case: the first source's bridge call succeeds, the
        # second fails. The pre-fix code would have returned bridge-engine
        # nodes for the first source and twin-engine nodes for the second --
        # a mixed-engine pair handed straight to align_mark_layer_nodes.
        os.environ['SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE'] = '1'
        calls = []

        def spy_bridge(src, timeout=2.0):
            calls.append(src)
            if src == SRC_B:
                raise ConnectionError('bridge down for second source')
            return server.to_mark_layer_nodes(src)

        server.to_mark_layer_nodes_via_http_bridge = spy_bridge
        a, b = server._mark_layer_nodes_pinned_engine(
            [SRC_A, SRC_B], 'docs/page.md', 'test')
        # Both sides land on the twin -- no mixed-engine pair, even though
        # the bridge briefly succeeded for SRC_A before failing on SRC_B.
        self.assertEqual(a, server.to_mark_layer_nodes(SRC_A))
        self.assertEqual(b, server.to_mark_layer_nodes(SRC_B))

    def test_single_source_flag_on_bridge_success(self):
        # `_rerender_block`'s prev_src=None path calls this with one source
        # -- a distinct tuple-unpack shape from the two-source case above.
        os.environ['SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE'] = '1'
        server.to_mark_layer_nodes_via_http_bridge = (
            lambda src, timeout=2.0: server.to_mark_layer_nodes(src))
        (a,) = server._mark_layer_nodes_pinned_engine(
            [SRC_A], 'docs/page.md', 'test')
        self.assertEqual(a, server.to_mark_layer_nodes(SRC_A))

    def test_single_source_flag_on_bridge_failure_falls_back(self):
        os.environ['SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE'] = '1'

        def failing_bridge(src, timeout=2.0):
            raise ConnectionError('bridge down')

        server.to_mark_layer_nodes_via_http_bridge = failing_bridge
        (a,) = server._mark_layer_nodes_pinned_engine(
            [SRC_A], 'docs/page.md', 'test')
        self.assertEqual(a, server.to_mark_layer_nodes(SRC_A))


if __name__ == '__main__':
    unittest.main()
