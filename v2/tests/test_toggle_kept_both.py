"""Server-side enforcement that a `kept-both` toggle mark is terminal per widget.

Closes the residual named in `SOMA/shared-cognition/marked-document-widgets.md`
Terms/gotchas section (and `_estate/design-alt-widget-demo.md`'s own doc text,
Skip 2026-09-11): before this, `kept-both` was "client-derived, not
server-enforced" — nothing stopped a stale tab or a direct API call from
silently reopening a widget past its documented-permanent state. The fix
rejects (409) any `mark_kind: toggle` POST whose `meta.widget` already has a
non-deleted `kept-both` mark, unless the caller explicitly passes `reopen: true`.
"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

V2_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, V2_DIR)

import server  # noqa: E402

PAGE = '# Widget demo\n\nSome content.\n'


class ToggleKeptBothTests(unittest.TestCase):
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
        self.httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        server.PROJECTS_ROOT = self.old_root
        server.WORKSPACES_CONFIG = self.old_config
        self.tmp.cleanup()

    def _post(self, payload):
        data = json.dumps(payload).encode('utf-8')
        request = urllib.request.Request(
            f'http://127.0.0.1:{self.httpd.server_port}/api/comments',
            data=data, headers={'Content-Type': 'application/json'}, method='POST',
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def _toggle(self, widget, state, **extra):
        payload = {
            'page': 'docs/page.md', 'type': 'mark', 'mark_kind': 'toggle',
            'text': state, 'meta': {'widget': widget, 'state': state},
        }
        payload.update(extra)
        return self._post(payload)

    def test_kept_both_rejects_a_later_toggle_on_the_same_widget(self):
        status, row = self._toggle('w1', 'chosen-A')
        self.assertEqual(201, status)
        status, row = self._toggle('w1', 'kept-both')
        self.assertEqual(201, status)

        status, body = self._toggle('w1', 'chosen-B')
        self.assertEqual(409, status)
        self.assertTrue(body.get('kept_both'))

        # kept-both itself is idempotently re-postable (still refused, same reason).
        status, body = self._toggle('w1', 'kept-both')
        self.assertEqual(409, status)

    def test_reopen_true_overrides_the_terminal_state(self):
        self._toggle('w1', 'chosen-A')
        self._toggle('w1', 'kept-both')

        status, row = self._toggle('w1', 'chosen-B', reopen=True)
        self.assertEqual(201, status)
        self.assertEqual('toggle', row['mark_kind'])
        self.assertTrue(row.get('toggle_reopened'))

    def test_reopen_makes_the_widget_normally_toggleable_again_afterward(self):
        # A reopen is a one-time override, not a permanent bypass requirement:
        # the row it writes becomes the new latest state, so a later NORMAL
        # (non-reopen) toggle must not be blocked by the old kept-both row.
        self._toggle('w1', 'kept-both')
        status, _ = self._toggle('w1', 'chosen-A', reopen=True)
        self.assertEqual(201, status)

        status, row = self._toggle('w1', 'chosen-B')
        self.assertEqual(201, status)
        self.assertFalse(row.get('toggle_reopened'))

        # And re-entering kept-both blocks again without reopen.
        self._toggle('w1', 'kept-both')
        status, _ = self._toggle('w1', 'chosen-A')
        self.assertEqual(409, status)

    def test_reopen_via_meta_reopen_also_overrides(self):
        self._toggle('w1', 'kept-both')
        status, row = self._toggle('w1', 'chosen-A', meta={'widget': 'w1', 'state': 'chosen-A', 'reopen': True})
        self.assertEqual(201, status)

    def test_check_and_write_are_atomic_under_concurrency(self):
        # Regression for the TOCTOU gap Skip's second pass found: a check
        # performed in a separate lock acquisition from the append left a
        # window where a concurrent kept-both write could land between this
        # request's check and its own append, silently reopening the widget
        # with no error and no `reopen` flag. Fire many concurrent toggles at
        # one widget, half of them a `kept-both` write; the invariant that
        # must hold regardless of interleaving is: once ANY kept-both row is
        # the true latest row on disk, no non-reopen write can have landed
        # after it.
        self._toggle('w1', 'previewing')
        results = []
        lock = threading.Lock()

        def fire(i):
            state = 'kept-both' if i % 2 == 0 else 'chosen-A'
            status, body = self._toggle('w1', state)
            with lock:
                results.append((status, state, body))

        threads = [threading.Thread(target=fire, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        final = [c for c in server.read_comments('docs/page.md')
                 if c.get('type') == 'mark' and c.get('mark_kind') == 'toggle'
                 and not c.get('deleted')
                 and isinstance(c.get('meta'), dict) and c['meta'].get('widget') == 'w1']
        # Every accepted (201) write must be reflected on disk; every rejected
        # (409) write must not be. If the check and the write ever ran under
        # separate locks, a race could accept a write whose precondition was
        # stale by the time it landed, or the accepted set could not match
        # what's actually on disk — either way this count would disagree.
        accepted = sum(1 for status, _, _ in results if status == 201)
        # +1 for the seed 'previewing' write in setUp of this test.
        self.assertEqual(accepted + 1, len(final))
        # Once kept-both is the true latest row, nothing non-reopen after it
        # should have been accepted with a later append order.
        kept_both_indices = [i for i, c in enumerate(final) if c['meta']['state'] == 'kept-both']
        if kept_both_indices and kept_both_indices[-1] != len(final) - 1:
            # A kept-both row exists but isn't the last row on disk: only
            # acceptable if every later row is itself kept-both (idempotent
            # re-entry) — never a plain chosen-A/B slipping in behind it.
            for row in final[kept_both_indices[-1] + 1:]:
                self.assertEqual('kept-both', row['meta']['state'])

    def test_a_deleted_kept_both_mark_no_longer_blocks(self):
        status, row = self._toggle('w1', 'kept-both')
        self.assertEqual(201, row and status)
        delete_req = urllib.request.Request(
            f'http://127.0.0.1:{self.httpd.server_port}/api/comments/delete',
            data=json.dumps({'page': 'docs/page.md', 'id': row['id']}).encode('utf-8'),
            headers={'Content-Type': 'application/json'}, method='POST',
        )
        with urllib.request.urlopen(delete_req) as response:
            self.assertEqual(200, response.status)

        status, row2 = self._toggle('w1', 'chosen-A')
        self.assertEqual(201, status)

    def test_kept_both_on_one_widget_does_not_block_a_different_widget(self):
        self._toggle('w1', 'kept-both')
        status, row = self._toggle('w2', 'chosen-A')
        self.assertEqual(201, status)

    def test_toggle_with_no_meta_widget_is_not_gated(self):
        # meta.widget is how the check finds prior state; no widget id means
        # nothing to check against, so this must not raise or 500.
        status, row = self._post({
            'page': 'docs/page.md', 'type': 'mark', 'mark_kind': 'toggle',
            'text': 'chosen-A',
        })
        self.assertEqual(201, status)


if __name__ == '__main__':
    unittest.main()
