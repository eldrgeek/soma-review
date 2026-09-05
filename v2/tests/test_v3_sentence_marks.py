"""The v3 view can mark one sentence (2026-09-04).

Every v3 affordance was block-scoped — the comment box, click-to-edit, the
verdict buttons — so `blockPayload()` hardcoded `from: 0, to: null` and every
row v3 wrote claimed the whole paragraph. `compute_ringer_list` reads a
whole-block mark as attention on every sentence in the block, so an ack on a
paragraph suppressed every revision inside it. That is the ringer list's
cardinal failure direction (under-report), and it was live on the only view
Mike opens.

These cases run a real browser, because the thing under test is a DOM selection
turning into code-point offsets — there is no honest way to fake that. They are
skipped when Playwright or its Chromium build is absent, so the stdlib-only
suite still runs everywhere.
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

import server  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - environment-dependent
    sync_playwright = None


@unittest.skipIf(sync_playwright is None, 'playwright not installed')
class V3SentenceMarkTests(unittest.TestCase):
    PARAGRAPH = ('Alpha is the first sentence. Beta is the second sentence. '
                 'Gamma is the third sentence.')

    @classmethod
    def setUpClass(cls):
        # `.start()` hangs in this environment; `__enter__` is the same call
        # through the context-manager path and does not.
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
        with open(os.path.join(self.root, 'docs', 'page.md'), 'w', encoding='utf-8') as handle:
            handle.write(f'# Demo\n\n{self.PARAGRAPH}\n')
        self.config = os.path.join(self.root, 'workspaces.json')
        with open(self.config, 'w', encoding='utf-8') as handle:
            json.dump({'estate': {'label': 'Test', 'roots': [['docs', 'docs']],
                                  'nav': [], 'home': 'docs/page.md',
                                  'feedback_dir': 'feedback',
                                  'nightly': False, 'tours': False}}, handle)
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
        self.page.goto(f'{self.base}/page/docs/page.md?view=v3')
        self.page.wait_for_selector('.mark-sentence')

    def tearDown(self):
        self.page.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        server.PROJECTS_ROOT = self.old_root
        server.WORKSPACES_CONFIG = self.old_config
        self.tmp.cleanup()

    # --- helpers ---------------------------------------------------------
    def _select_sentence(self, index):
        # The `# Demo` heading is now sentence-addressable too (headings gained
        # .mark-sentence spans alongside paragraph/blockquote), so it occupies
        # global index 0. Every caller here means "sentence `index` of
        # PARAGRAPH", so offset past the heading's one span.
        self.page.eval_on_selector_all('.mark-sentence', """(els, i) => {
            const r = document.createRange();
            r.selectNodeContents(els[i]);
            const s = window.getSelection();
            s.removeAllRanges();
            s.addRange(r);
            document.dispatchEvent(new Event('selectionchange'));
        }""", index + 1)

    def _payload(self):
        return json.loads(self.page.evaluate(
            "JSON.stringify(blockPayload(document.querySelectorAll('.block-wrap')[1]))"))

    def _rows(self):
        with urllib.request.urlopen(f'{self.base}/api/comments?page=docs/page.md') as response:
            return [r for r in json.load(response) if not r.get('deleted')]

    # --- tests -----------------------------------------------------------
    def test_v3_renders_sentence_spans(self):
        """The spans are server-rendered in every view; only the classic dwell
        layer's behaviour is view-gated. This is what makes the fix possible.
        4, not 3: the `# Demo` heading is now sentence-addressable too."""
        self.assertEqual(4, self.page.eval_on_selector_all('.mark-sentence', 'e => e.length'))

    def test_no_selection_still_posts_the_whole_block(self):
        payload = self._payload()
        self.assertEqual(0, payload['from'])
        self.assertIsNone(payload['to'])
        self.assertEqual(self.PARAGRAPH, payload['quote'])

    def test_a_selected_sentence_becomes_a_real_span(self):
        self._select_sentence(1)
        payload = self._payload()
        self.assertEqual('Beta is the second sentence.', payload['quote'])
        self.assertEqual(payload['quote'],
                         self.PARAGRAPH[payload['from']:payload['to']],
                         'offsets must index the block normalized text the quote came from')

    def test_selecting_every_sentence_is_the_whole_block(self):
        """Selecting all of it is not a narrowing, and a payload that claims a
        span there would understate what he read."""
        self.page.eval_on_selector('.block-wrap:nth-of-type(2) .block-body', """el => {
            const r = document.createRange();
            r.selectNodeContents(el);
            const s = window.getSelection();
            s.removeAllRanges();
            s.addRange(r);
            document.dispatchEvent(new Event('selectionchange'));
        }""")
        self.assertIsNone(self._payload()['to'])

    def test_mark_bar_writes_a_sentence_bound_row(self):
        self._select_sentence(1)
        self.page.wait_for_selector('.v3-markbar')
        self.page.click('.v3-markbar [data-v3-mark="ack"]')
        self.page.wait_for_timeout(800)
        marks = [r for r in self._rows() if r.get('type') == 'mark']
        self.assertEqual(1, len(marks))
        self.assertEqual('ack', marks[0]['mark_kind'])
        self.assertEqual('Beta is the second sentence.', marks[0]['quote'])
        self.assertEqual(29, marks[0]['from'])
        self.assertEqual(57, marks[0]['to'])
        self.assertFalse(marks[0].get('unresolved'),
                         'the server must bind the span, not retain it unresolved')
        self.assertEqual([], self.errors)

    def test_an_astral_character_earlier_in_the_block_does_not_shift_the_quote(self):
        """`from`/`to` are Python code-point offsets; JS slice() counts UTF-16
        code units. One emoji ahead of the sentence shifts a sliced quote by one
        and the server refuses the mark 409. 434 such characters live in 215
        estate documents, so this is the corpus, not an edge case."""
        para = 'Alpha \U0001f512 first sentence here. Beta is the second sentence. Gamma third.'
        with open(os.path.join(self.root, 'docs', 'page.md'), 'w', encoding='utf-8') as handle:
            handle.write(f'# Demo\n\n{para}\n')
        self.page.goto(f'{self.base}/page/docs/page.md?view=v3')
        self.page.wait_for_selector('.mark-sentence')
        self._select_sentence(1)
        payload = self._payload()
        self.assertEqual(para[payload['from']:payload['to']], payload['quote'],
                         'the quote must be the code-point slice, not the UTF-16 one')
        self.page.wait_for_selector('.v3-markbar')
        self.page.click('.v3-markbar [data-v3-mark="ack"]')
        self.page.wait_for_timeout(800)
        marks = [r for r in self._rows() if r.get('type') == 'mark']
        self.assertEqual(1, len(marks), 'the mark must be accepted, not refused 409')
        self.assertFalse(marks[0].get('unresolved'))

    def test_a_lost_selection_is_refused_not_recorded_whole_block(self):
        """A touch device dismisses the selection before the press lands. Posting
        anyway would write a whole-block mark — which suppresses every revision
        in the paragraph — under a toast claiming one sentence."""
        self._select_sentence(1)
        self.page.wait_for_selector('.v3-markbar')
        # Hold the button, THEN drop the selection: losing it removes the bar
        # from the document (the safe branch), so the press has to be delivered
        # to the retained node to exercise the branch where it does not.
        self.page.evaluate("""() => {
            const btn = document.querySelector('.v3-markbar [data-v3-mark="ack"]');
            window.__retainedBtn = btn;
            window.getSelection().removeAllRanges();
        }""")
        self.assertEqual(0, self.page.eval_on_selector_all('.v3-markbar', 'e => e.length'),
                         'losing the selection takes the bar down')
        self.page.evaluate("window.__retainedBtn.dispatchEvent("
                           "new MouseEvent('mousedown', {bubbles: true, button: 0}))")
        self.page.wait_for_timeout(600)
        self.assertEqual([], [r for r in self._rows() if r.get('type') == 'mark'],
                         'no selection means no mark, not a whole-block mark')
        # The absence of a row is not proof the guard ran: before the guard, the
        # same press threw inside the handler (markBar was already null) and
        # wrote nothing by accident. The refusal message is what distinguishes
        # a deliberate refusal from a crash that happened to be harmless.
        self.assertIn('Selection lost',
                      self.page.eval_on_selector('#toast', 'el => el.textContent'),
                      'the reader must be told why nothing was marked')

    def test_no_mark_bar_without_a_selection(self):
        self.assertEqual(0, self.page.eval_on_selector_all('.v3-markbar', 'e => e.length'))

    def test_mark_bar_reaches_an_unchanged_sentence_in_a_revised_block(self):
        """The named gap: v3PaintInlineDiffs used to replace the whole block's
        innerHTML with a flat word diff carrying no .mark-sentence spans, so a
        block with any open edit/replace mark could never take a sentence mark
        again — exactly the blocks the ringer list exists for. An unchanged
        sentence in a revised block must still be selectable and bindable."""
        proposed = 'Alpha is the first sentence. Beta has been rewritten. Gamma is the third sentence.'
        block_id = self.page.eval_on_selector_all(
            '.block-wrap', 'els => els[1].dataset.blockId')
        req = urllib.request.Request(
            f'{self.base}/api/comments', method='POST',
            headers={'Content-Type': 'application/json'},
            data=json.dumps({'page': 'docs/page.md', 'type': 'edit', 'block_id': block_id,
                              'anchor': None, 'quote': self.PARAGRAPH, 'from': 0, 'to': None,
                              'snapshot': self.PARAGRAPH, 'proposed': proposed}).encode())
        urllib.request.urlopen(req)
        self.page.reload()
        self.page.wait_for_selector('.v3-inline-diff')
        # Gamma (sentence 3) never changed, so it must still carry a real
        # .mark-sentence span with its original binding intact.
        self.assertEqual(
            3, self.page.eval_on_selector_all('.v3-inline-diff .mark-sentence', 'e => e.length'),
            'the unrevised sentences must keep their spans, only the changed one is a diff')
        # The changed sentence (Beta) must actually show as a diff — not just
        # keep a span. `body`'s spans are the AFTER state (apply_sentence_change
        # already wrote `proposed` into the trunk before this renders), so the
        # function must be fed `mark.snapshot` (before) to diff against, not
        # `mark.proposed` — passing `proposed` diffs the live document against
        # itself and every sentence lands in the `eq` bucket, painting no
        # diff at all (the bug Skip's adversarial pass caught, 2026-09-05).
        middle_html = self.page.eval_on_selector_all(
            '.v3-inline-diff .mark-sentence', 'els => els[1].innerHTML')
        self.assertIn('<del>', middle_html, 'the revised sentence must show what changed')
        self.assertIn('<ins>', middle_html, 'the revised sentence must show what changed')
        # v3RenderDiffHtml diffs at word granularity, so the old/new words
        # appear individually rather than as contiguous old/new sentences.
        self.assertIn('second', middle_html)
        self.assertIn('rewritten', middle_html)
        self.page.eval_on_selector_all('.v3-inline-diff .mark-sentence', """els => {
            const r = document.createRange();
            r.selectNodeContents(els[2]);
            const s = window.getSelection();
            s.removeAllRanges();
            s.addRange(r);
            document.dispatchEvent(new Event('selectionchange'));
        }""")
        self.page.wait_for_selector('.v3-markbar')
        self.page.click('.v3-markbar [data-v3-mark="ack"]')
        self.page.wait_for_timeout(800)
        marks = [r for r in self._rows() if r.get('type') == 'mark']
        self.assertEqual(1, len(marks), 'the sentence mark must be accepted on a revised block')
        self.assertEqual('Gamma is the third sentence.', marks[0]['quote'])
        self.assertEqual([], self.errors)


@unittest.skipIf(sync_playwright is None, 'playwright not installed')
class V3ListItemMarkTests(unittest.TestCase):
    """List blocks were whole-block-only in v3: mark_layer_inner already emits
    per-item ranges (mark_layer_list_units / data-list-units) and the classic
    dwell layer already turns those into addressable .mark-block-unit <li>s
    (hydrateRichUnits), but v3 never called an equivalent, so a list item had
    no binding of any kind — not even the block-wide fallback other rich
    blocks get. hydrateListUnits() (shared PAGE_JS scope) closes that gap."""

    ITEMS = ['Alpha item text.', 'Beta item text.', 'Gamma item text.']

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
        list_md = '\n'.join(f'- {item}' for item in self.ITEMS)
        with open(os.path.join(self.root, 'docs', 'page.md'), 'w', encoding='utf-8') as handle:
            handle.write(f'# Demo\n\n{list_md}\n')
        self.config = os.path.join(self.root, 'workspaces.json')
        with open(self.config, 'w', encoding='utf-8') as handle:
            json.dump({'estate': {'label': 'Test', 'roots': [['docs', 'docs']],
                                  'nav': [], 'home': 'docs/page.md',
                                  'feedback_dir': 'feedback',
                                  'nightly': False, 'tours': False}}, handle)
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
        self.page.goto(f'{self.base}/page/docs/page.md?view=v3')
        self.page.wait_for_selector('.mark-block-unit')

    def tearDown(self):
        self.page.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        server.PROJECTS_ROOT = self.old_root
        server.WORKSPACES_CONFIG = self.old_config
        self.tmp.cleanup()

    def _select_item(self, index):
        self.page.eval_on_selector_all('.mark-block-unit', """(els, i) => {
            const r = document.createRange();
            r.selectNodeContents(els[i]);
            const s = window.getSelection();
            s.removeAllRanges();
            s.addRange(r);
            document.dispatchEvent(new Event('selectionchange'));
        }""", index)

    def _rows(self):
        with urllib.request.urlopen(f'{self.base}/api/comments?page=docs/page.md') as response:
            return [r for r in json.load(response) if not r.get('deleted')]

    def test_each_list_item_is_hydrated_with_a_real_binding(self):
        count = self.page.eval_on_selector_all('.mark-block-unit', 'e => e.length')
        self.assertEqual(3, count)
        froms = self.page.eval_on_selector_all(
            '.mark-block-unit', 'els => els.map(e => e.dataset.from)')
        self.assertTrue(all(f not in (None, '') for f in froms))

    def test_selecting_one_item_raises_the_mark_bar(self):
        self._select_item(1)
        self.page.wait_for_selector('.v3-markbar')

    def test_mark_bar_writes_an_item_bound_row_not_the_whole_list(self):
        self._select_item(1)
        self.page.wait_for_selector('.v3-markbar')
        self.page.click('.v3-markbar [data-v3-mark="ack"]')
        self.page.wait_for_timeout(800)
        marks = [r for r in self._rows() if r.get('type') == 'mark']
        self.assertEqual(1, len(marks))
        self.assertEqual('ack', marks[0]['mark_kind'])
        self.assertEqual('Beta item text.', marks[0]['quote'])
        self.assertIsNotNone(marks[0]['to'],
                             'a bound item mark must not fall back to whole-block (to: null)')
        self.assertFalse(marks[0].get('unresolved'),
                         'the server must bind the span, not retain it unresolved')
        self.assertEqual([], self.errors)

    def test_selecting_the_whole_list_is_still_the_whole_block(self):
        self.page.eval_on_selector('.block-wrap:nth-of-type(2) .block-body', """el => {
            const r = document.createRange();
            r.selectNodeContents(el);
            const s = window.getSelection();
            s.removeAllRanges();
            s.addRange(r);
            document.dispatchEvent(new Event('selectionchange'));
        }""")
        payload = json.loads(self.page.evaluate(
            "JSON.stringify(blockPayload(document.querySelectorAll('.block-wrap')[1]))"))
        self.assertIsNone(payload['to'])

    def test_an_open_edit_on_the_list_does_not_destroy_item_bindings(self):
        """Skip's finding (2026-09-05): v3PaintInlineDiffs's flat word-diff
        fallback replaces body.innerHTML with plain <span> text and no
        <ul>/<li> markup, which used to be harmless for a list block (it had
        no bindings to lose) but became a real regression the moment this fix
        gave list items bindings to lose. An open edit/replace mark on a list
        block must leave the hydrated .mark-block-unit <li>s (and the list
        markup itself) intact."""
        proposed = '\n'.join(f'- {item}' for item in
                             ['Alpha item text.', 'Beta item text CHANGED.', 'Gamma item text.'])
        block_id = self.page.eval_on_selector_all(
            '.block-wrap', 'els => els[1].dataset.blockId')
        list_md = '\n'.join(f'- {item}' for item in self.ITEMS)
        req = urllib.request.Request(
            f'{self.base}/api/comments', method='POST',
            headers={'Content-Type': 'application/json'},
            data=json.dumps({'page': 'docs/page.md', 'type': 'edit', 'block_id': block_id,
                              'anchor': None, 'quote': list_md, 'from': 0, 'to': None,
                              'snapshot': list_md, 'proposed': proposed}).encode())
        urllib.request.urlopen(req)
        self.page.reload()
        self.page.wait_for_selector('.mark-block-unit')
        units = self.page.eval_on_selector_all(
            '.mark-block-unit', 'els => els.map(e => e.tagName)')
        self.assertEqual(['LI', 'LI', 'LI'], units,
                         'the list must still render as real <li> elements, not flat diff text')
        self._select_item(2)
        self.page.wait_for_selector('.v3-markbar')
        self.page.click('.v3-markbar [data-v3-mark="ack"]')
        self.page.wait_for_timeout(800)
        marks = [r for r in self._rows() if r.get('type') == 'mark']
        self.assertEqual(1, len(marks), 'an item mark must still be postable on a block with an open edit')
        self.assertEqual('Gamma item text.', marks[0]['quote'])
        self.assertEqual([], self.errors)


if __name__ == '__main__':
    unittest.main()
