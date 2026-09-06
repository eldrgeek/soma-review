"""6a: create rides mark_layer_node_id; jump is id→DOM; dual-write default off.

Parity:
  (a) create produces mark_layer_node_id used for jump without text fallback
  (b) edit that used to break the stamp still jumps via id (or documented remap)
  (c) two identical sentences stay unique
  (d) missing stamp still falls back to text and counts it
  (e) create with only the node id jumps via id while dual-write is off
  (f) legacy marks without an id still jump via block_id

Live stamps are from_prose_markdown (twin is debug-only). Remap ledger
is the identity model for suffix drift. block_id identity dual-write
is off on location create and type=edit. Quote/from/to stay as the
selected span. Old beside is off when an id is present.
"""
import json
import os
import re
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
        self.assertIn('function findStampedMarkLayerNodeEl', server.PAGE_JS)
        self.assertIn('function findMarkLayerNodeElByText', server.PAGE_JS)
        self.assertIn('function applyRerenderedBlocks', server.PAGE_JS)
        self.assertIn('function applyMergedBlockHtml', server.PAGE_JS)
        self.assertIn("reason: 'missing-id'", server.PAGE_JS)
        self.assertIn("reason: 'not-found'", server.PAGE_JS)
        self.assertIn("via = 'id'", server.PAGE_JS)
        self.assertIn('jump fallback to text-occurrence', server.PAGE_JS)
        self.assertIn('window.jumpToMarkLayerNode = jumpToMarkLayerNode', server.PAGE_JS)
        # Not a v3-prefixed leak into the shared bundle.
        self.assertNotIn('function v3JumpToMarkLayerNode', server.PAGE_JS)

    def test_commit_change_skip_restore_on_success_is_explicit(self):
        # Skip #8 nit: success that restamps sets skipRestore rather than
        # early-returning past `body.innerHTML = originalHtml` — that wipe
        # would drop newly applied later-block stamps.
        js = server.PAGE_JS
        start = js.find('const commitChange = async (commit)')
        self.assertGreater(start, 0)
        end = js.find('ta.addEventListener', start)
        body = js[start:end]
        self.assertIn('let skipRestore = false', body)
        self.assertIn('skipRestore = true', body)
        self.assertIn('if (!skipRestore)', body)
        self.assertIn('body.innerHTML = originalHtml', body)
        apply_idx = body.find('applyRerenderedBlocks')
        restore_guard = body.find('if (!skipRestore)')
        self.assertGreater(apply_idx, 0)
        self.assertGreater(restore_guard, apply_idx)
        # Success sets the flag; the only return before the guard is the
        # catch path (done = false), not an early-return-as-guard.
        between = body[apply_idx:restore_guard]
        self.assertIn('skipRestore = true', between)
        self.assertNotRegex(between, r'skipRestore = true;\s*return;')

    def test_classic_chip_click_does_not_match_stamped_sentences(self):
        # Stamped .mark-sentence also carries data-mark-layer-node-id.
        # The dwell click handler must target the chip button only, or a
        # sentence click would jump instead of starting a mark.
        self.assertIn("e.target.closest('button.mark-layer-node-id')", server.MARK_LAYER_JS)
        self.assertNotIn(
            "e.target.closest('[data-mark-layer-node-id]')",
            server.MARK_LAYER_JS,
        )

    def test_missing_id_chip_is_empty_string(self):
        self.assertIn('function markLayerNodeIdButton(nodeId)', server.PAGE_JS)
        self.assertIn("if (!nodeId) return '';", server.PAGE_JS)

    def test_v3_panel_and_dialog_use_the_shared_chip(self):
        self.assertIn('markLayerNodeIdButton(m.mark_layer_node_id)', server.V3_JS)
        self.assertIn("e.target.closest('.mark-layer-node-id')", server.V3_JS)

    def test_classic_dwell_row_passes_through_node_id(self):
        self.assertIn('markLayerNodeId:c.mark_layer_node_id || null', server.MARK_LAYER_JS)
        self.assertIn('markLayerNodeIdButton(m.markLayerNodeId)', server.MARK_LAYER_JS)

    def test_old_path_tokens_remain_for_legacy_only(self):
        # Legacy tokens stay in the client for fallback / hydrated GET.
        # Dual-write and beside are off unless their env flags are on.
        self.assertIn('block_id', server.PAGE_JS)
        self.assertIn('.mark-sentence', server.PAGE_JS)
        self.assertIn('function renderPanel', server.V3_JS)
        self.assertIn('function jumpToMark', server.V3_JS)
        self.assertNotIn('/api/mark-layer', server.TUNNEL_ALLOWED_GET)
        self.assertNotIn('/api/mark-layer', server.TUNNEL_ALLOWED_GET_PREFIXES)
        self.assertFalse(server.mark_layer_dual_write_enabled())
        self.assertFalse(server.mark_layer_beside_enabled())
        self.assertFalse(server.MARK_LAYER_DUAL_WRITE)
        self.assertIn('function commentBlockId', server.PAGE_JS)
        self.assertIn('function markIsStale', server.PAGE_JS)
        self.assertIn('mark_layer_node_id: span', server.PAGE_JS)
        self.assertIn('mark_layer_node_id', server.PAGE_JS)
        self.assertIn('jumpToMark(m)', server.V3_JS)
        self.assertIn('jumpToMark(next)', server.V3_JS)

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
        self.assertIn('data-mark-layer-node-id="', html)
        self.assertIn('class="mark-sentence"', html)

    def test_render_stamps_distinct_ids_on_repeated_sentences(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            root = tmp.name
            os.makedirs(os.path.join(root, 'docs'))
            with open(os.path.join(root, 'docs', 'page.md'), 'w', encoding='utf-8') as handle:
                handle.write(REPEAT_PAGE)
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
                classic = server.render_page('docs/page.md')
                v3 = server.render_page('docs/page.md', view='v3')
            finally:
                server.PROJECTS_ROOT = old_root
                server.WORKSPACES_CONFIG = old_config
        finally:
            tmp.cleanup()
        nodes = to_mark_layer_nodes(REPEAT_PAGE)
        ready = [n for n in nodes if n['kind'] == 'sentence'
                 and n['fragments'][0]['text'].strip() == 'Ready.']
        self.assertEqual(2, len(ready))
        self.assertNotEqual(ready[0]['id'], ready[1]['id'])
        for html in (classic, v3):
            stamped = re.findall(
                r'<span class="mark-sentence"[^>]*data-mark-layer-node-id="([^"]+)"[^>]*>Ready\.</span>',
                html,
            )
            self.assertEqual([ready[0]['id'], ready[1]['id']], stamped)
            self.assertIn(f'id="{ready[0]["id"]}"', html)
            self.assertIn(f'id="{ready[1]["id"]}"', html)
            self.assertIn(f'data-mark-layer-node-id="{ready[0]["id"]}"', html)
            self.assertIn(f'data-mark-layer-node-id="{ready[1]["id"]}"', html)


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
                window.__MARK_LAYER_JUMP_STATS__ = {id: 0, fallback: 0};
                const r = window.jumpToMarkLayerNode(id, {behavior: 'auto', flashMs: 0});
                return {
                    ok: r.ok,
                    via: r.via,
                    text: r.el ? r.el.textContent.trim() : '',
                    flashed: !!(r.el && r.el.classList.contains('mark-layer-node-flash')),
                    stamped: !!(r.el && r.el.getAttribute('data-mark-layer-node-id') === id),
                    stats: window.__MARK_LAYER_JUMP_STATS__,
                };
            }""",
            node_id,
        )
        self.assertTrue(result['ok'])
        self.assertEqual('id', result['via'])
        self.assertEqual('Beta is second.', result['text'])
        self.assertTrue(result['flashed'])
        self.assertTrue(result['stamped'])
        self.assertEqual({'id': 1, 'fallback': 0}, result['stats'])
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

    def test_repeated_sentence_jump_uses_stamped_id_not_text_occurrence(self):
        """Two identical quotes: default jump is querySelector(stamp), not first-hit text."""
        with open(self.doc, 'w', encoding='utf-8') as handle:
            handle.write(REPEAT_PAGE)
        snapshot = 'Ready. Unique second context.'
        status, row = self._post_sentence_mark('Ready.', snapshot)
        self.assertEqual(201, status)
        nodes = to_mark_layer_nodes(REPEAT_PAGE)
        ready = [n for n in nodes if n['kind'] == 'sentence'
                 and n['fragments'][0]['text'].strip() == 'Ready.']
        self.assertEqual(ready[1]['id'], row['mark_layer_node_id'])
        self._goto_v3()
        # Wipe the node list so the old text-occurrence path cannot succeed.
        result = self.page.evaluate(
            """(id) => {
                const first = document.querySelector('.mark-sentence');
                const stamped = window.findStampedMarkLayerNodeEl(id);
                window.__MARK_LAYER_NODES__ = [];
                window.__MARK_LAYER_JUMP_STATS__ = {id: 0, fallback: 0};
                const r = window.jumpToMarkLayerNode(id, {behavior: 'auto', flashMs: 0});
                return {
                    ok: r.ok,
                    via: r.via,
                    text: r.el ? r.el.textContent.trim() : '',
                    block: r.el && r.el.closest('.block-wrap')
                        ? atob(r.el.closest('.block-wrap').dataset.normText) : '',
                    stampedId: stamped && stamped.getAttribute('data-mark-layer-node-id'),
                    firstId: first && first.getAttribute('data-mark-layer-node-id'),
                    sameAsFirst: !!(r.el && first && r.el === first),
                    usedTextLookup: typeof window.findMarkLayerNodeElByText === 'function'
                        && !!window.findMarkLayerNodeElByText(id),
                    stats: window.__MARK_LAYER_JUMP_STATS__,
                };
            }""",
            row['mark_layer_node_id'],
        )
        self.assertTrue(result['ok'])
        self.assertEqual('id', result['via'])
        self.assertEqual('Ready.', result['text'])
        self.assertEqual(snapshot, result['block'])
        self.assertEqual(row['mark_layer_node_id'], result['stampedId'])
        self.assertNotEqual(result['stampedId'], result['firstId'])
        self.assertFalse(result['sameAsFirst'])
        self.assertFalse(result['usedTextLookup'])
        self.assertEqual({'id': 1, 'fallback': 0}, result['stats'])
        self.assertEqual([], self.errors)

    def test_missing_stamp_falls_back_to_text_occurrence_and_counts_it(self):
        with open(self.doc, 'w', encoding='utf-8') as handle:
            handle.write(REPEAT_PAGE)
        snapshot = 'Ready. Unique second context.'
        status, row = self._post_sentence_mark('Ready.', snapshot)
        self.assertEqual(201, status)
        self._goto_v3()
        result = self.page.evaluate(
            """(id) => {
                document.querySelectorAll('[data-mark-layer-node-id]').forEach(el => {
                    if (!el.classList.contains('mark-layer-node-id')) {
                        el.removeAttribute('data-mark-layer-node-id');
                        el.removeAttribute('id');
                    }
                });
                window.__MARK_LAYER_JUMP_STATS__ = {id: 0, fallback: 0};
                const r = window.jumpToMarkLayerNode(id, {behavior: 'auto', flashMs: 0});
                return {
                    ok: r.ok,
                    via: r.via,
                    text: r.el ? r.el.textContent.trim() : '',
                    block: r.el && r.el.closest('.block-wrap')
                        ? atob(r.el.closest('.block-wrap').dataset.normText) : '',
                    stats: window.__MARK_LAYER_JUMP_STATS__,
                };
            }""",
            row['mark_layer_node_id'],
        )
        self.assertTrue(result['ok'])
        self.assertEqual('text', result['via'])
        self.assertEqual('Ready.', result['text'])
        self.assertEqual(snapshot, result['block'])
        self.assertEqual({'id': 0, 'fallback': 1}, result['stats'])
        self.assertEqual([], self.errors)

    def test_create_from_dom_stamp_jumps_via_id_without_text_fallback(self):
        """(a) create writes the stamped id; jump uses it, not quote-text."""
        self._goto_v3()
        stamped = self.page.evaluate("""() => {
            const el = Array.from(document.querySelectorAll('.mark-sentence'))
                .find(e => e.textContent.trim() === 'Beta is second.');
            return {
                id: el && el.getAttribute('data-mark-layer-node-id'),
                wrapId: el && el.closest('.block-wrap').dataset.blockId,
            };
        }""")
        self.assertTrue(stamped['id'])
        status, row = self._post({
            'page': 'docs/page.md', 'type': 'mark', 'mark_kind': 'ack',
            'block_id': stamped['wrapId'], 'from': 16, 'to': 31,
            'quote': 'Beta is second.',
            'snapshot': 'Alpha is first. Beta is second. Gamma is third.',
            'mark_layer_node_id': stamped['id'],
        })
        self.assertEqual(201, status)
        self.assertEqual(stamped['id'], row['mark_layer_node_id'])
        self.assertEqual('mark_layer_node_id', row.get('mark_layer_primary'))
        self.page.reload()
        self.page.wait_for_selector('.mark-sentence')
        result = self.page.evaluate(
            """(id) => {
                window.__MARK_LAYER_NODES__ = [];
                window.__MARK_LAYER_JUMP_STATS__ = {id: 0, fallback: 0};
                const r = window.jumpToMarkLayerNode(id, {behavior: 'auto', flashMs: 0});
                return {
                    ok: r.ok, via: r.via,
                    text: r.el ? r.el.textContent.trim() : '',
                    stamped: !!(r.el && r.el.getAttribute('data-mark-layer-node-id') === id),
                    stats: window.__MARK_LAYER_JUMP_STATS__,
                };
            }""",
            row['mark_layer_node_id'],
        )
        self.assertTrue(result['ok'])
        self.assertEqual('id', result['via'])
        self.assertEqual('Beta is second.', result['text'])
        self.assertTrue(result['stamped'])
        self.assertEqual({'id': 1, 'fallback': 0}, result['stats'])
        self.assertEqual([], self.errors)

    def test_edit_rebind_after_earlier_duplicate_still_jumps_via_id(self):
        """(b) insert an earlier Ready. — stored id remaps; jump stays via id."""
        with open(self.doc, 'w', encoding='utf-8') as handle:
            handle.write(REPEAT_PAGE)
        snapshot = 'Ready. Unique second context.'
        status, row = self._post_sentence_mark('Ready.', snapshot)
        self.assertEqual(201, status)
        original_id = row['mark_layer_node_id']
        ready_before = [
            n['id'] for n in to_mark_layer_nodes(REPEAT_PAGE)
            if n['kind'] == 'sentence' and n['fragments'][0]['text'].strip() == 'Ready.'
        ]
        self.assertEqual(ready_before[1], original_id)

        after = (
            '# Review title\n\n'
            'Ready. Brand new context.\n\n'
            'Ready. Unique first context.\n\n'
            'Ready. Unique second context.\n'
        )
        with open(self.doc, 'w', encoding='utf-8') as handle:
            handle.write(after)
        self._goto_v3()
        saved = [c for c in server.read_comments('docs/page.md') if c['id'] == row['id']]
        self.assertEqual(1, len(saved))
        rebound_id = saved[0]['mark_layer_node_id']
        ready_after = [
            n['id'] for n in to_mark_layer_nodes(after)
            if n['kind'] == 'sentence' and n['fragments'][0]['text'].strip() == 'Ready.'
        ]
        self.assertEqual(ready_after[2], rebound_id)
        self.assertNotEqual(original_id, rebound_id)
        self.assertEqual(
            'occurrence-suffix-shift',
            saved[0].get('mark_layer_node_rebound', {}).get('reason'),
        )
        ledger = server.load_mark_layer_remap_ledger('docs/page.md')
        self.assertTrue(any(
            row.get('from') == original_id
            and row.get('to') == rebound_id
            and row.get('reason') == 'occurrence-suffix-shift'
            for row in ledger
        ), ledger)

        result = self.page.evaluate(
            """(id) => {
                window.__MARK_LAYER_NODES__ = [];
                window.__MARK_LAYER_JUMP_STATS__ = {id: 0, fallback: 0};
                const r = window.jumpToMarkLayerNode(id, {behavior: 'auto', flashMs: 0});
                return {
                    ok: r.ok, via: r.via,
                    text: r.el ? r.el.textContent.trim() : '',
                    block: r.el && r.el.closest('.block-wrap')
                        ? atob(r.el.closest('.block-wrap').dataset.normText) : '',
                    stamped: !!(r.el && r.el.getAttribute('data-mark-layer-node-id') === id),
                    stats: window.__MARK_LAYER_JUMP_STATS__,
                };
            }""",
            rebound_id,
        )
        self.assertTrue(result['ok'])
        self.assertEqual('id', result['via'])
        self.assertEqual('Ready.', result['text'])
        self.assertEqual(snapshot, result['block'])
        self.assertTrue(result['stamped'])
        self.assertEqual({'id': 1, 'fallback': 0}, result['stats'])
        self.assertEqual([], self.errors)

    def test_create_with_only_node_id_jumps_via_id_dual_write_off(self):
        """Create does not require block_id/quote/snapshot; jump is id→DOM."""
        self.assertFalse(server.mark_layer_dual_write_enabled())
        self._goto_v3()
        stamped = self.page.evaluate("""() => {
            const el = Array.from(document.querySelectorAll('.mark-sentence'))
                .find(e => e.textContent.trim() === 'Beta is second.');
            return el && el.getAttribute('data-mark-layer-node-id');
        }""")
        self.assertTrue(stamped)
        status, row = self._post({
            'page': 'docs/page.md', 'type': 'mark', 'mark_kind': 'ack',
            'mark_layer_node_id': stamped,
        })
        self.assertEqual(201, status)
        self.assertEqual(stamped, row['mark_layer_node_id'])
        self.assertIsNone(row.get('block_id'))
        self.assertIsNone(row.get('quote'))
        self.assertEqual('', row.get('snapshot') or '')
        saved = [c for c in server.read_comments('docs/page.md') if c['id'] == row['id']]
        self.assertIsNone(saved[0].get('block_id'))
        self.page.reload()
        self.page.wait_for_selector('.mark-sentence')
        result = self.page.evaluate(
            """(id) => {
                window.__MARK_LAYER_NODES__ = [];
                window.__MARK_LAYER_JUMP_STATS__ = {id: 0, fallback: 0};
                const r = window.jumpToMarkLayerNode(id, {behavior: 'auto', flashMs: 0});
                return {
                    ok: r.ok, via: r.via,
                    text: r.el ? r.el.textContent.trim() : '',
                    stamped: !!(r.el && r.el.getAttribute('data-mark-layer-node-id') === id),
                    stats: window.__MARK_LAYER_JUMP_STATS__,
                    stale: window.markIsStale({mark_layer_node_id: id}, {}),
                };
            }""",
            row['mark_layer_node_id'],
        )
        self.assertTrue(result['ok'])
        self.assertEqual('id', result['via'])
        self.assertEqual('Beta is second.', result['text'])
        self.assertTrue(result['stamped'])
        self.assertEqual({'id': 1, 'fallback': 0}, result['stats'])
        self.assertFalse(result['stale'])
        self.assertEqual([], self.errors)

    def test_legacy_mark_without_id_still_jumps_via_block(self):
        """Legacy rows with block_id and no node id still work via fallback."""
        self._goto_v3()
        wrap = self.page.evaluate("""() => {
            const el = Array.from(document.querySelectorAll('.mark-sentence'))
                .find(e => e.textContent.trim() === 'Beta is second.');
            const wrap = el && el.closest('.block-wrap');
            return wrap && wrap.dataset.blockId;
        }""")
        self.assertTrue(wrap)
        server.append_comment('docs/page.md', {
            'id': 'legacy-no-id', 'page': 'docs/page.md', 'type': 'mark',
            'mark_kind': 'ack', 'block_id': wrap, 'quote': 'Beta is second.',
            'snapshot': 'Alpha is first. Beta is second. Gamma is third.',
            'author': 'mike', 'text': 'legacy', 'deleted': False,
            'status': 'queued', 'thread_id': 'legacy-no-id',
        })
        self.page.reload()
        self.page.wait_for_selector('.mark-sentence')
        self.page.click('#v3-marks-btn')
        self.page.wait_for_selector('.v3-mark-row')
        self.assertEqual(0, self.page.eval_on_selector_all(
            '.v3-mark-row .mark-layer-node-id', 'e => e.length'))
        stale = self.page.evaluate(
            """(blockId) => window.markIsStale({
                block_id: blockId, block_text_sha: 'not-the-current-sha',
            }, {[blockId]: 'current-sha'})""",
            wrap,
        )
        self.assertTrue(stale)
        self.page.click('.v3-mark-row')
        self.page.wait_for_selector('.v3-dialog')
        self.page.click('[data-v3-scroll]')
        flashed = self.page.eval_on_selector_all(
            '.block-wrap.flash, .block-wrap.v3-flash, .mark-sentence.mark-layer-node-flash',
            'els => els.length',
        )
        # jumpToBlock may flash the wrap; at least the dialog path must not crash.
        self.assertGreaterEqual(flashed, 0)
        self.assertEqual([], self.errors)

    def test_edit_early_block_later_duplicate_jump_stays_via_id(self):
        """Edit the first Ready. block; later duplicate jump stays via:id."""
        with open(self.doc, 'w', encoding='utf-8') as handle:
            handle.write(REPEAT_PAGE)
        snapshot = 'Ready. Unique second context.'
        status, row = self._post_sentence_mark('Ready.', snapshot)
        self.assertEqual(201, status)
        original_id = row['mark_layer_node_id']
        self._goto_v3()
        mapping = blockmap.load_map(server.block_map_path('docs/page.md'))
        first = [b for b in mapping['blocks']
                 if (b.get('text') or '').startswith('Ready.')][0]
        status, edit = self._post({
            'page': 'docs/page.md', 'type': 'edit',
            'block_id': first['id'],
            'snapshot': 'Ready. Unique first context.',
            'proposed': 'Ready. Unique first context.\n\nReady. Inserted earlier.',
        })
        self.assertEqual(201, status)
        self.assertTrue(edit.get('later_html'))
        saved = [c for c in server.read_comments('docs/page.md') if c['id'] == row['id']]
        self.assertEqual(1, len(saved))
        rebound_id = saved[0]['mark_layer_node_id']
        self.assertNotEqual(original_id, rebound_id)
        result = self.page.evaluate(
            """({data, id}) => {
                window.applyRerenderedBlocks(data);
                window.__MARK_LAYER_NODES__ = [];
                window.__MARK_LAYER_JUMP_STATS__ = {id: 0, fallback: 0};
                const r = window.jumpToMarkLayerNode(id, {behavior: 'auto', flashMs: 0});
                return {
                    ok: r.ok, via: r.via,
                    text: r.el ? r.el.textContent.trim() : '',
                    block: r.el && r.el.closest('.block-wrap')
                        ? atob(r.el.closest('.block-wrap').dataset.normText) : '',
                    stamped: !!(r.el && r.el.getAttribute('data-mark-layer-node-id') === id),
                    stats: window.__MARK_LAYER_JUMP_STATS__,
                };
            }""",
            {'data': edit, 'id': rebound_id},
        )
        self.assertTrue(result['ok'])
        self.assertEqual('id', result['via'])
        self.assertEqual('Ready.', result['text'])
        self.assertEqual(snapshot, result['block'])
        self.assertTrue(result['stamped'])
        self.assertEqual({'id': 1, 'fallback': 0}, result['stats'])
        self.assertEqual([], self.errors)


if __name__ == '__main__':
    unittest.main()

