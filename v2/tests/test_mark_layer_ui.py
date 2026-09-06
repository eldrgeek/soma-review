"""6a beside: UI can display/jump via mark_layer_node_id; old path stays default.

Parity for the first visible UI slice:
  (a) jumpToMarkLayerNode no-ops on a missing/unknown id (no throw)
  (b) a stored id finds the matching sentence and flashes it
  (c) v3 marks panel shows a clickable chip only when the id is present

Does not cut over create/render to nodes. Does not claim 6a closed.
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

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - environment-dependent
    sync_playwright = None


SENTENCE_PAGE = (
    '# Review title\n\n'
    'Alpha is first. Beta is second. Gamma is third.\n'
)

REPEAT_PAGE = (
    '# Review title\n\n'
    'Ready. Unique first context.\n\n'
    'Ready. Unique second context.\n'
)


class MarkLayerUiSourceTests(unittest.TestCase):
    """Stdlib: helper + chip markup exist; missing-id path is a no-op; no cutover."""

    def test_shared_helper_lives_in_page_js_not_v3_only(self):
        self.assertIn('function jumpToMarkLayerNode', server.PAGE_JS)
        self.assertIn('function findMarkLayerNodeEl', server.PAGE_JS)
        self.assertIn("reason: 'missing-id'", server.PAGE_JS)
        self.assertIn("reason: 'not-found'", server.PAGE_JS)
        self.assertIn('window.jumpToMarkLayerNode = jumpToMarkLayerNode', server.PAGE_JS)
        # Not a v3-prefixed leak into the shared bundle.
        self.assertNotIn('function v3JumpToMarkLayerNode', server.PAGE_JS)

    def test_missing_id_chip_is_empty_string(self):
        self.assertIn('function markLayerNodeIdButton(nodeId)', server.PAGE_JS)
        self.assertIn("if (!nodeId) return '';", server.PAGE_JS)

    def test_v3_panel_and_dialog_use_the_shared_chip(self):
        self.assertIn('markLayerNodeIdButton(m.mark_layer_node_id)', server.V3_JS)
        self.assertIn("e.target.closest('.mark-layer-node-id')", server.V3_JS)

    def test_classic_dwell_row_passes_through_node_id(self):
        self.assertIn('markLayerNodeId:c.mark_layer_node_id || null', server.MARK_LAYER_JS)
        self.assertIn('markLayerNodeIdButton(m.markLayerNodeId)', server.MARK_LAYER_JS)

    def test_old_path_tokens_still_default(self):
        # Create/render still rides block_id / .mark-sentence / v3 panel.
        self.assertIn('block_id', server.PAGE_JS)
        self.assertIn('.mark-sentence', server.PAGE_JS)
        self.assertIn('function renderPanel', server.V3_JS)
        self.assertNotIn('/api/mark-layer', server.TUNNEL_ALLOWED_GET)
        self.assertNotIn('/api/mark-layer', server.TUNNEL_ALLOWED_GET_PREFIXES)

    def test_classic_render_does_not_ship_v3_panel(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            root = tmp.name
            os.makedirs(os.path.join(root, 'docs'))
            with open(os.path.join(root, 'docs', 'page.md'), 'w', encoding='utf-8') as handle:
                handle.write(SENTENCE_PAGE)
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
            server.PROJECTS_ROOT = root
            server.WORKSPACES_CONFIG = config
            try:
                html = server.render_page('docs/page.md')
            finally:
                server.PROJECTS_ROOT = old_root
                server.WORKSPACES_CONFIG = old_config
        finally:
            tmp.cleanup()
        self.assertIn('function jumpToMarkLayerNode', html)
        self.assertNotIn('v3-panel', html)
        self.assertIn('class="mark-layer"', html)


@unittest.skipIf(sync_playwright is None, 'playwright not installed')
class MarkLayerUiBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._cm = sync_playwright()
        cls._pw = cls._cm.__enter__()
        try:
            cls.browser = cls._pw.chromium.launch()
        except Exception as exc:  # pragma: no cover - no browser build present
            cls._cm.__exit__(None, None, None)
            raise unittest.SkipTest(f'no chromium build: {exc}')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls._cm.__exit__(None, None, None)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, 'docs'))
        self.doc = os.path.join(self.root, 'docs', 'page.md')
        with open(self.doc, 'w', encoding='utf-8') as handle:
            handle.write(SENTENCE_PAGE)
        self.config = os.path.join(self.root, 'workspaces.json')
        with open(self.config, 'w', encoding='utf-8') as handle:
            json.dump({
                'estate': {
                    'label': 'Test', 'roots': [['docs', 'docs']],
                    'nav': [], 'home': 'docs/page.md', 'feedback_dir': 'feedback',
                    'nightly': False, 'tours': False,
                }
            }, handle)
        self.old_root, self.old_config = server.PROJECTS_ROOT, server.WORKSPACES_CONFIG
        server.PROJECTS_ROOT = self.root
        server.WORKSPACES_CONFIG = self.config
        self.httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.httpd.server_port}'
        self.page = self.browser.new_page()
        self.errors = []
        self.page.on('pageerror', lambda e: self.errors.append(str(e)))

    def tearDown(self):
        self.page.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        server.PROJECTS_ROOT = self.old_root
        server.WORKSPACES_CONFIG = self.old_config
        self.tmp.cleanup()

    def _goto_v3(self):
        self.page.goto(f'{self.base}/page/docs/page.md?view=v3')
        self.page.wait_for_selector('.mark-sentence')

    def _post(self, payload):
        data = json.dumps(payload).encode('utf-8')
        request = urllib.request.Request(
            f'{self.base}/api/comments',
            data=data, headers={'Content-Type': 'application/json'}, method='POST',
        )
        with urllib.request.urlopen(request) as response:
            return response.status, json.load(response)

    def _post_sentence_mark(self, quote, snapshot, mark_kind='ack'):
        server.render_page('docs/page.md', view='v3')
        mapping = blockmap.load_map(server.block_map_path('docs/page.md'))
        target = next(
            (row for row in mapping['blocks'] if row.get('text') == snapshot),
            None,
        ) or next(row for row in mapping['blocks'] if quote in row.get('text', ''))
        text = target['text']
        start = text.find(quote)
        return self._post({
            'page': 'docs/page.md', 'type': 'mark', 'mark_kind': mark_kind,
            'block_id': target['id'], 'from': start, 'to': start + len(quote),
            'quote': quote, 'snapshot': snapshot,
        })

    def test_helper_missing_id_is_a_noop(self):
        self._goto_v3()
        result = self.page.evaluate("""() => {
            const out = [];
            out.push(window.jumpToMarkLayerNode(''));
            out.push(window.jumpToMarkLayerNode(null));
            out.push(window.jumpToMarkLayerNode(undefined));
            return out.map(r => ({ok: r.ok, reason: r.reason}));
        }""")
        self.assertEqual(
            [{'ok': False, 'reason': 'missing-id'}] * 3,
            result,
        )
        self.assertEqual(0, self.page.eval_on_selector_all(
            '.mark-layer-node-flash', 'e => e.length'))
        self.assertEqual([], self.errors)

    def test_helper_unknown_id_is_not_found(self):
        self._goto_v3()
        result = self.page.evaluate(
            "() => { const r = window.jumpToMarkLayerNode('pmsent-no-such'); return {ok: r.ok, reason: r.reason}; }")
        self.assertEqual({'ok': False, 'reason': 'not-found'}, result)
        self.assertEqual([], self.errors)

    def test_helper_jumps_and_flashes_the_matching_sentence(self):
        self._goto_v3()
        node_id = self.page.evaluate("""() => {
            const nodes = window.__MARK_LAYER_NODES__ || [];
            const hit = nodes.find(n => n.kind === 'sentence'
                && (n.fragments||[]).some(f => (f.text||'').includes('Beta is second')));
            return hit && hit.id;
        }""")
        self.assertTrue(node_id)
        result = self.page.evaluate(
            """(id) => {
                const r = window.jumpToMarkLayerNode(id, {behavior: 'auto', flashMs: 0});
                return {
                    ok: r.ok,
                    text: r.el ? r.el.textContent.trim() : '',
                    flashed: !!(r.el && r.el.classList.contains('mark-layer-node-flash')),
                };
            }""",
            node_id,
        )
        self.assertTrue(result['ok'])
        self.assertEqual('Beta is second.', result['text'])
        self.assertTrue(result['flashed'])
        self.assertEqual([], self.errors)

    def test_v3_panel_shows_chip_and_click_flashes_sentence(self):
        status, row = self._post_sentence_mark(
            'Beta is second.',
            'Alpha is first. Beta is second. Gamma is third.',
        )
        self.assertEqual(201, status)
        self.assertIn('mark_layer_node_id', row)
        self._goto_v3()
        self.page.click('#v3-marks-btn')
        self.page.wait_for_selector('.v3-mark-row .mark-layer-node-id')
        chip_text = self.page.eval_on_selector(
            '.v3-mark-row .mark-layer-node-id', 'el => el.textContent')
        self.assertEqual(row['mark_layer_node_id'], chip_text)
        self.page.click('.v3-mark-row .mark-layer-node-id')
        flashed = self.page.eval_on_selector_all(
            '.mark-sentence.mark-layer-node-flash',
            'els => els.map(e => e.textContent.trim())',
        )
        self.assertEqual(['Beta is second.'], flashed)
        self.assertEqual([], self.errors)

    def test_missing_id_mark_has_no_chip_and_does_not_crash(self):
        status, row = self._post({
            'page': 'docs/page.md', 'type': 'mark', 'mark_kind': 'ack',
            'text': 'Page-level ack with no node id',
        })
        self.assertEqual(201, status)
        self.assertNotIn('mark_layer_node_id', row)
        self._goto_v3()
        self.page.click('#v3-marks-btn')
        self.page.wait_for_selector('.v3-mark-row')
        self.assertEqual(0, self.page.eval_on_selector_all(
            '.v3-mark-row .mark-layer-node-id', 'e => e.length'))
        self.page.click('.v3-mark-row')
        self.page.wait_for_selector('.v3-dialog')
        self.assertEqual(0, self.page.eval_on_selector_all(
            '.v3-dialog .mark-layer-node-id', 'e => e.length'))
        self.assertEqual([], self.errors)

    def test_repeated_sentence_jumps_to_the_attached_occurrence(self):
        with open(self.doc, 'w', encoding='utf-8') as handle:
            handle.write(REPEAT_PAGE)
        snapshot = 'Ready. Unique second context.'
        status, row = self._post_sentence_mark('Ready.', snapshot)
        self.assertEqual(201, status)
        nodes = to_mark_layer_nodes(REPEAT_PAGE)
        ready = [n for n in nodes if n['kind'] == 'sentence'
                 and n['fragments'][0]['text'].strip() == 'Ready.']
        self.assertEqual(2, len(ready))
        self.assertEqual(ready[1]['id'], row['mark_layer_node_id'])
        self._goto_v3()
        result = self.page.evaluate(
            """(id) => {
                const el = window.findMarkLayerNodeEl(id);
                if (!el) return null;
                const wrap = el.closest('.block-wrap');
                return {text: el.textContent.trim(), block: wrap && atob(wrap.dataset.normText)};
            }""",
            row['mark_layer_node_id'],
        )
        self.assertEqual('Ready.', result['text'])
        self.assertEqual(snapshot, result['block'])
        self.assertEqual([], self.errors)


if __name__ == '__main__':
    unittest.main()
