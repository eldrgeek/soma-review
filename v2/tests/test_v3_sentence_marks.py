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
        self.page.eval_on_selector_all('.mark-sentence', """(els, i) => {
            const r = document.createRange();
            r.selectNodeContents(els[i]);
            const s = window.getSelection();
            s.removeAllRanges();
            s.addRange(r);
            document.dispatchEvent(new Event('selectionchange'));
        }""", index)

    def _payload(self):
        return json.loads(self.page.evaluate(
            "JSON.stringify(blockPayload(document.querySelectorAll('.block-wrap')[1]))"))

    def _rows(self):
        with urllib.request.urlopen(f'{self.base}/api/comments?page=docs/page.md') as response:
            return [r for r in json.load(response) if not r.get('deleted')]

    # --- tests -----------------------------------------------------------
    def test_v3_renders_sentence_spans(self):
        """The spans are server-rendered in every view; only the classic dwell
        layer's behaviour is view-gated. This is what makes the fix possible."""
        self.assertEqual(3, self.page.eval_on_selector_all('.mark-sentence', 'e => e.length'))

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


if __name__ == '__main__':
    unittest.main()
