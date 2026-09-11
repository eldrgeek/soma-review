"""GET /api/mark-layer's HTTP-bridge instrumentation (item 6a, 2026-09-11
mission-1 second call site). Pins:
  (a) flag off -> engine python-twin, no bridge_error key, latency_ms present
  (b) flag on + bridge succeeds -> engine playmaker-http-bridge, same nodes
  (c) flag on + bridge fails -> falls back to python-twin, bridge_error set,
      and latency_ms times only the twin fallback call, not the failed
      bridge attempt plus the fallback (Skip's 2026-09-11 finding: a shared
      timer misreported bridge-attempt-time + twin-time as pure twin time).
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

V2_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, V2_DIR)

import server  # noqa: E402

PAGE = '# Title\n\nAlpha is first. Beta is second.\n'


class MarkLayerApiBridgeTests(unittest.TestCase):
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
        self.httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        server.PROJECTS_ROOT = self.old_root
        server.WORKSPACES_CONFIG = self.old_config
        server.to_mark_layer_nodes_via_http_bridge = self.old_bridge_fn
        if self.old_env is None:
            os.environ.pop('SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE', None)
        else:
            os.environ['SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE'] = self.old_env
        self.tmp.cleanup()

    def _get(self):
        url = f'http://127.0.0.1:{self.httpd.server_port}/api/mark-layer?page=docs/page.md'
        with urllib.request.urlopen(url) as response:
            return json.load(response)

    def test_flag_off_uses_twin_and_reports_no_bridge_error(self):
        os.environ.pop('SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE', None)
        body = self._get()
        self.assertEqual(body['engine'], 'python-twin')
        self.assertNotIn('bridge_error', body)
        self.assertIsInstance(body['latency_ms'], (int, float))
        self.assertGreaterEqual(body['latency_ms'], 0)

    def test_flag_on_bridge_success_uses_bridge_engine(self):
        os.environ['SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE'] = '1'
        twin_nodes = server.to_mark_layer_nodes(PAGE)
        server.to_mark_layer_nodes_via_http_bridge = lambda src, timeout=2.0: twin_nodes
        body = self._get()
        self.assertEqual(body['engine'], 'playmaker-http-bridge')
        self.assertNotIn('bridge_error', body)
        self.assertEqual(body['nodes'], twin_nodes)

    def test_flag_on_bridge_failure_falls_back_and_times_only_the_fallback(self):
        os.environ['SOMA_REVIEW_MARK_LAYER_HTTP_BRIDGE'] = '1'

        def slow_failing_bridge(src, timeout=2.0):
            time.sleep(0.2)  # simulate a slow, failed bridge attempt
            raise ConnectionRefusedError('bridge down')

        server.to_mark_layer_nodes_via_http_bridge = slow_failing_bridge
        body = self._get()
        self.assertEqual(body['engine'], 'python-twin')
        self.assertIn('bridge down', body['bridge_error'])
        self.assertEqual(body['nodes'], server.to_mark_layer_nodes(PAGE))
        # The regression this pins: latency_ms must not include the ~200ms
        # the failed bridge attempt burned before falling back to the twin.
        self.assertLess(body['latency_ms'], 100)


if __name__ == '__main__':
    unittest.main()
