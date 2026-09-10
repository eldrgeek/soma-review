"""The trunk-write endpoints are read-modify-write cycles over a real file,
served by a ThreadingHTTPServer. Before `_trunk_lock` (2026-09-10) two clicks
arriving close together on the same page could interleave: both requests read
the same source, each spliced only its own edit into that snapshot, and the
second write erased the first — silently, because both marks resolved and both
git commits exist. The hash guard in `_locate_change_span` does not catch it,
since both requests pass that guard before either writes.

This was flagged as real-but-unfixed by the adversarial pass on the 2026-09-06
Fold-button change ("no file lock in apply_sentence_fold/apply_sentence_change,
ThreadingHTTPServer") and is shared by change/settle/revert/fold alike.

The interleaving is forced by instrumentation, not by a timeout, because a
timing-based version of this test fails in the wrong direction. A first draft
parked both threads on a `Barrier(2, timeout=1.5)` and asserted the barrier
BROKE — but that assertion also passes when the second thread simply hasn't
been scheduled within 1.5s on a loaded machine, and it passes with the lock
deleted. The pass condition here is instead "the second thread reached the lock
and was held there", which is the thing the lock actually buys:

  * `_trunk_lock` is wrapped to record every arrival, so the test can tell
    "blocked on the lock" from "never got there" — and so removing the
    `with _trunk_lock(...)` wrapper turns this test red rather than green.
  * `_locate_change_span` is wrapped to record what source each request read.
    Serialized, the second request must read text the first one already wrote.
    That comparison is the direct evidence of no lost update.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

V2_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, V2_DIR)

import server  # noqa: E402


def _git(*args, cwd):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, check=True)


class TrunkWriteConcurrencyTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.docs_repo = os.path.join(self.root, 'docs')
        os.makedirs(self.docs_repo)
        _git('init', '-q', cwd=self.docs_repo)
        _git('config', 'user.email', 'test@example.com', cwd=self.docs_repo)
        _git('config', 'user.name', 'Test', cwd=self.docs_repo)
        self.doc = os.path.join(self.docs_repo, 'page.md')
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
        # Locks are cached per realpath in a module global that outlives the
        # temp dir; drop this fixture's keys so runs don't accumulate them.
        self.known_lock_keys = set(server._trunk_locks)

    def tearDown(self):
        server.PROJECTS_ROOT = self.old_root
        server.WORKSPACES_CONFIG = self.old_config
        for key in list(server._trunk_locks):
            if key not in self.known_lock_keys:
                server._trunk_locks.pop(key, None)
        self.tmp.cleanup()

    def write_doc(self, text):
        with open(self.doc, 'w', encoding='utf-8') as handle:
            handle.write(text)
        _git('add', 'page.md', cwd=self.docs_repo)
        _git('commit', '-q', '-m', 'initial', cwd=self.docs_repo)

    def edit_mark(self, mark_id, quote, proposed):
        """A create-time edit row, bound the way do_POST binds one."""
        server.render_page('docs/page.md', view='v3')
        _src, blocks, _map, _report = server.current_page_blocks('docs/page.md', 'estate')
        block = next(b for b in blocks if quote in b['text'])
        binding = server.validated_binding('docs/page.md', 'estate', {
            'block_id': block['id'], 'quote': quote,
        })
        return {
            'id': mark_id, 'page': 'docs/page.md', 'type': 'edit',
            'anchor': None, 'snapshot': quote, 'proposed': proposed,
            'author': 'claude', 'text': '(sentence change)',
            'timestamp': '2026-09-10T00:00:00Z', 'status': 'queued',
            'thread_id': mark_id, 'deleted': False, **binding,
        }

    def test_concurrent_changes_on_one_page_do_not_lose_each_other(self):
        self.write_doc('# Title\n\nAlpha original sentence.\n\nBeta original sentence.\n')
        mark_a = self.edit_mark('c-a', 'Alpha original sentence.', 'Alpha edited sentence.')
        mark_b = self.edit_mark('c-b', 'Beta original sentence.', 'Beta edited sentence.')

        state = threading.Lock()
        arrivals = []
        reads = []
        both_arrived = threading.Event()

        original_trunk_lock = server._trunk_lock
        original_locate = server._locate_change_span

        def recording_trunk_lock(fs_path):
            # Called BEFORE the `with` acquires, so this records "reached the
            # lock", including the caller that is about to block on it.
            lock = original_trunk_lock(fs_path)
            with state:
                arrivals.append(threading.current_thread().name)
                if len(arrivals) == 2:
                    both_arrived.set()
            return lock

        def recording_locate(*args, **kwargs):
            result = original_locate(*args, **kwargs)
            with state:
                reads.append(result[1])
            # The first request through parks here, inside the critical section,
            # until the second has reached the lock and blocked on it. The second
            # finds the event already set and sails through.
            both_arrived.wait(timeout=15)
            return result

        errors = []

        def run(mark):
            try:
                server.apply_sentence_change('docs/page.md', 'estate', mark, author_label='claude')
            except Exception as exc:  # noqa: BLE001 — surfaced by the assertion below
                errors.append(exc)

        server._trunk_lock = recording_trunk_lock
        server._locate_change_span = recording_locate
        try:
            threads = [threading.Thread(target=run, args=(m,), name=f'w{i}')
                       for i, m in enumerate((mark_a, mark_b))]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=45)
            still_running = [t.name for t in threads if t.is_alive()]
        finally:
            server._trunk_lock = original_trunk_lock
            server._locate_change_span = original_locate

        self.assertEqual(still_running, [], 'a trunk write never returned — deadlock')
        self.assertEqual(errors, [], f'a concurrent trunk write raised: {errors}')
        self.assertTrue(
            both_arrived.is_set(),
            'both requests never reached _trunk_lock — apply_sentence_change is '
            'not taking a trunk lock at all, so nothing is serializing it')
        self.assertEqual(len(reads), 2, 'expected exactly one file read per request')
        self.assertNotEqual(
            reads[0], reads[1],
            'both requests read identical source text, so the second one read '
            'before the first had written — the read-modify-write cycles '
            'interleaved and one edit is about to be lost')
        with open(self.doc, encoding='utf-8') as f:
            content = f.read()
        self.assertIn('Alpha edited sentence.', content)
        self.assertIn('Beta edited sentence.', content)
        self.assertNotIn('Alpha original sentence.', content)
        self.assertNotIn('Beta original sentence.', content)

    def _fire_two_marks_merge_requests(self, action):
        """Shared instrumentation for the double-click race tests below.
        Sets up one open edit mark on a fresh doc, then fires two concurrent
        HTTP `/api/marks/merge` requests with the given `action`
        ('settle' or 'revert'), forcing real interleaving the same way
        `test_concurrent_changes_on_one_page_do_not_lose_each_other` forces
        it for `apply_sentence_change`: wrap `_trunk_lock` to record when
        each thread reaches it, and block the winner (inside the lock, at
        its first call to `read_comments`) until the loser has also reached
        and blocked on the lock — proving the loser's own status re-check
        happens strictly after the winner's guard->apply->update_comment,
        not concurrently with it. Returns (results, both_arrived,
        still_running) for the caller to assert on."""
        self.write_doc('# Title\n\nAlpha original sentence.\n')
        mark = self.edit_mark('c-a', 'Alpha original sentence.', 'Alpha edited sentence.')
        server.apply_sentence_change('docs/page.md', 'estate', mark, author_label='claude')
        server.append_comment('docs/page.md', mark, 'estate')

        arrivals = []
        both_arrived = threading.Event()
        state = threading.Lock()
        original_trunk_lock = server._trunk_lock

        def recording_trunk_lock(fs_path):
            # Called before the `with` blocks on acquiring it, so this
            # records "reached the lock" for both the winner and the loser.
            lock = original_trunk_lock(fs_path)
            with state:
                arrivals.append(threading.current_thread().name)
                if len(arrivals) == 2:
                    both_arrived.set()
            return lock

        original_read_comments = server.read_comments
        first_read_done = threading.Event()

        def blocking_read_comments(*args, **kwargs):
            result = original_read_comments(*args, **kwargs)
            # `read_comments` is the first call inside the locked block. The
            # winner of the lock race parks here — still holding the trunk
            # lock — until the loser has also reached (and blocked on) the
            # lock, so the loser's own `read_comments` cannot run until
            # AFTER this request's apply + update_comment have completed.
            if not first_read_done.is_set():
                first_read_done.set()
                both_arrived.wait(timeout=15)
            return result

        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        results = {}

        def post(name):
            payload = json.dumps({
                'page': 'docs/page.md', 'id': 'c-a', 'action': action, 'author': 'mike',
            }).encode('utf-8')
            request = urllib.request.Request(
                f'http://127.0.0.1:{httpd.server_port}/api/marks/merge',
                data=payload, headers={'Content-Type': 'application/json'}, method='POST',
            )
            try:
                with urllib.request.urlopen(request) as response:
                    results[name] = (response.status, json.load(response))
            except urllib.error.HTTPError as exc:
                results[name] = (exc.code, json.load(exc))

        server._trunk_lock = recording_trunk_lock
        server.read_comments = blocking_read_comments
        try:
            threads = [threading.Thread(target=post, args=(n,), name=n)
                       for n in ('r0', 'r1')]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=45)
            still_running = [t.name for t in threads if t.is_alive()]
        finally:
            server._trunk_lock = original_trunk_lock
            server.read_comments = original_read_comments
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

        return results, both_arrived, still_running

    def test_double_click_revert_does_not_race_the_status_guard(self):
        """Regression for the race `/api/marks/merge` introduced when
        `_trunk_lock` first shipped (2026-09-10, commit 41684e2): the
        `status == 'done'` guard, the apply, and the `update_comment` that
        sets status were three separate steps outside any lock, so two
        Revert clicks close together both read `status != 'done'`, both
        proceeded, and the second one's `_locate_change_span` hash guard
        then found the FIRST request's already-reverted text instead of the
        mark's `proposed` text it expected — a confusing drift/"refusing
        change" 409 on a benign double-click, on Mike's most deliberate act.
        Fixed by holding the trunk lock across guard -> apply ->
        update_comment, so the loser re-reads a status that is already
        'done' and gets the plain, correct "mark already resolved" 409
        instead of a drift error, and the trunk ends up reverted exactly
        once, not corrupted."""
        results, both_arrived, still_running = self._fire_two_marks_merge_requests('revert')

        self.assertEqual(still_running, [], 'a revert request never returned — deadlock')
        self.assertTrue(
            both_arrived.is_set(),
            'both requests never reached _trunk_lock — /api/marks/merge is not '
            'holding the trunk lock across guard->apply->update_comment')
        statuses = sorted(status for status, _ in results.values())
        self.assertEqual(
            [200, 409], statuses,
            f'expected exactly one clean revert (200) and one clean refusal (409), got: {results}')
        ok_body = next(body for status, body in results.values() if status == 200)
        err_body = next(body for status, body in results.values() if status == 409)
        self.assertTrue(ok_body.get('ok'))
        self.assertTrue(ok_body.get('reverted'))
        self.assertEqual(
            'mark already resolved', err_body.get('error'),
            'the loser must see the plain status-already-done refusal, not a '
            'drift/"refusing change" MergeConflict raised by racing the winner\'s '
            'own write — that confusing 409 is the exact regression this pins')

        with open(self.doc, encoding='utf-8') as f:
            content = f.read()
        self.assertIn('Alpha original sentence.', content)
        self.assertNotIn('Alpha edited sentence.', content)

    def test_double_click_settle_does_not_race_the_status_guard(self):
        """Same race, Settle side (Skip's adversarial pass on the Revert fix,
        2026-09-10: the fix is inside the same `with _trunk_lock` block for
        both branches by construction, but nothing had exercised the Settle
        branch's own apply path — `_apply_sentence_settle_unlocked` takes a
        different route than Revert's: no file write, no
        `_locate_change_span` hash guard, just a re-render of already-correct
        trunk text — so a bug specific to that path would not be caught by
        a Revert-only test). Two Settle clicks close together: the loser
        must see the same clean "mark already resolved" 409, never a crash
        or a duplicate resolution."""
        results, both_arrived, still_running = self._fire_two_marks_merge_requests('settle')

        self.assertEqual(still_running, [], 'a settle request never returned — deadlock')
        self.assertTrue(
            both_arrived.is_set(),
            'both requests never reached _trunk_lock — /api/marks/merge is not '
            'holding the trunk lock across guard->apply->update_comment')
        statuses = sorted(status for status, _ in results.values())
        self.assertEqual(
            [200, 409], statuses,
            f'expected exactly one clean settle (200) and one clean refusal (409), got: {results}')
        ok_body = next(body for status, body in results.values() if status == 200)
        err_body = next(body for status, body in results.values() if status == 409)
        self.assertTrue(ok_body.get('ok'))
        self.assertTrue(ok_body.get('settled'))
        self.assertEqual('mark already resolved', err_body.get('error'))

        # Settle never writes the file (the trunk already holds the right
        # text) — the edited sentence must survive untouched either way.
        with open(self.doc, encoding='utf-8') as f:
            content = f.read()
        self.assertIn('Alpha edited sentence.', content)
        self.assertNotIn('Alpha original sentence.', content)

    def test_double_click_fold_does_not_drift_conflict_the_loser(self):
        """Regression for the Fold-side twin of the Revert/Settle double-click
        race: `/api/fold` has no mark/status row to guard (it operates
        straight on `sentence_text`), so before this fix two Fold clicks on
        the same sentence produced one clean 200 and one confusing drift
        `"...refusing change"` 409 — the winner's write replaced the sentence
        with `[term](#terms)` before the loser's own `_locate_change_span`
        ran, and the loser had no way to tell "someone already did this" from
        "the block genuinely changed underneath me." Fires two real
        concurrent HTTP `/api/fold` requests (both clicks named the exact
        same sentence/term — the literal double-click shape) and asserts
        both come back 200 `folded: true`, the trunk holds exactly one Terms
        bullet, and exactly one of the two responses carries a real commit
        (the other is the idempotent already-done report, no new commit)."""
        self.write_doc(
            '# Title\n\nAn agreed extension may be folded out of the sentence.\n\n'
            '## Terms\n'
        )
        server.render_page('docs/page.md', workspace='estate', view='v3')
        _src, blocks, _map, _report = server.current_page_blocks('docs/page.md', 'estate')
        block = next(b for b in blocks
                     if 'An agreed extension' in (b.get('text') or ''))

        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        results = {}

        def post(name):
            payload = json.dumps({
                'page': 'docs/page.md', 'block_id': block['id'],
                'sentence': 'An agreed extension may be folded out of the sentence.',
                'term': 'fold',
            }).encode('utf-8')
            request = urllib.request.Request(
                f'http://127.0.0.1:{httpd.server_port}/api/fold',
                data=payload, headers={'Content-Type': 'application/json'}, method='POST',
            )
            try:
                with urllib.request.urlopen(request) as response:
                    results[name] = (response.status, json.load(response))
            except urllib.error.HTTPError as exc:
                results[name] = (exc.code, json.load(exc))

        try:
            threads = [threading.Thread(target=post, args=(n,), name=n)
                       for n in ('r0', 'r1')]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=45)
            still_running = [t.name for t in threads if t.is_alive()]
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

        self.assertEqual(still_running, [], 'a fold request never returned — deadlock')
        statuses = sorted(status for status, _ in results.values())
        self.assertEqual(
            [200, 200], statuses,
            f'a double-click Fold must never surface the drift-conflict 409 to '
            f'the loser, got: {results}')
        for _name, (_status, body) in results.items():
            self.assertTrue(body.get('ok'))
            self.assertTrue(body.get('folded'))
        commits = [body.get('commit') for _status, body in results.values()]
        self.assertEqual(
            sorted(c is None for c in commits), [False, True],
            f'expected exactly one real commit (the winner) and one None '
            f'(the idempotent already-folded report), got commits: {commits}')

        with open(self.doc, encoding='utf-8') as f:
            content = f.read()
        self.assertEqual(
            content.count('[fold](#terms)'), 1,
            'the sentence must be folded exactly once, not duplicated or corrupted')
        self.assertEqual(
            content.count('**fold**'), 1,
            'exactly one Terms bullet must exist for the folded term')
        self.assertNotIn(
            'An agreed extension may be folded out of the sentence.\n\n## Terms',
            content, 'the original sentence must no longer sit in the body')

    def test_already_folded_check_is_not_fooled_by_a_substring_elsewhere(self):
        """Skip's adversarial pass on the Fold double-click fix (2026-09-10):
        the first draft of `_already_folded_result` tested "is the sentence
        still unfolded" with a bare `sentence in raw_text[...]` substring
        check, which reads a positive on ANY sentence that merely CONTAINS
        the folded text — a real shape in this tool, built to consolidate
        repeated/near-duplicate language. That false positive would silently
        re-surface the original confusing drift 409 for a page containing
        exactly this pattern. Fixed to compare whole blocks/sentences via
        `segment_sentences`, the same unit `_locate_change_span` itself uses.
        This pins the fix directly, independent of any HTTP/threading
        timing."""
        self.write_doc(
            '# Title\n\n[fold](#terms)\n\n'
            'The full context: An agreed extension may be folded out of the '
            'sentence, in a broader sense.\n\n'
            '## Terms\n\n'
            '- **fold** — An agreed extension may be folded out of the sentence.\n'
        )
        result = server._already_folded_result(
            'docs/page.md', 'estate',
            'stale-block-id',
            'An agreed extension may be folded out of the sentence.',
            'fold',
        )
        self.assertIsNotNone(
            result,
            'a longer sentence merely containing the folded text must not be '
            'read as "still unfolded" — the fold already happened and the '
            'loser must get the idempotent success report, not a drift 409')
        self.assertTrue(result.get('ok', True))  # success-shaped: no 'ok' key set false
        self.assertIsNone(result.get('commit'))

    def test_already_folded_check_refuses_to_guess_when_the_term_is_ambiguous(self):
        """Skip's other finding on the same pass: the first draft picked
        `next(block containing link_text)` with no ambiguity check, so once
        `term` names more than one already-folded sentence on the page (this
        file's own dedupe-bullet logic, right above this function, exists
        specifically to let one term serve several sentences), a double-click
        on ANY of them would resolve to whichever matching block happened to
        come first — silently reporting success for the wrong block, which a
        client then repaints as if it were the block the user actually
        clicked. Fixed to require exactly one candidate block; two or more
        must fall through to the original (safe, if unhelpful) drift error
        rather than guess."""
        self.write_doc(
            '# Title\n\n[dup](#terms)\n\n[dup](#terms)\n\n'
            '## Terms\n\n'
            '- **dup** — Second dup sentence.\n'
        )
        result = server._already_folded_result(
            'docs/page.md', 'estate',
            'stale-block-id',
            'Second dup sentence.',
            'dup',
        )
        self.assertIsNone(
            result,
            'two blocks both carry the "dup" link, so which one this click '
            'was actually about is genuinely ambiguous from content alone — '
            'must refuse rather than silently pick one')

    def test_lock_is_per_page_so_different_pages_still_write_in_parallel(self):
        one = os.path.join(self.docs_repo, 'page.md')
        two = os.path.join(self.docs_repo, 'other.md')
        self.assertIs(server._trunk_lock(one), server._trunk_lock(one))
        self.assertIsNot(server._trunk_lock(one), server._trunk_lock(two))

    def test_board_regenerate_takes_the_same_lock_as_a_sentence_edit(self):
        """`run_board_regenerate` shells two generators that each end in a
        whole-file `open(OUT_PATH, 'w')` on BOARD.md / PORTFOLIO.md — both
        ordinary markable pages, BOARD.md being the estate workspace's home.
        Found by an adversarial pass as the one in-process writer that bypassed
        the trunk lock entirely; assert it now resolves to the SAME lock object
        an edit on that page would take."""
        board = os.path.join(self.docs_repo, 'page.md')
        server._REGENERATE_TARGET_ROUTES['board'] = ('docs/page.md', 'estate')
        try:
            self.write_doc('# Title\n\nA sentence.\n')
            self.assertIs(server._regenerate_target_lock('board'), server._trunk_lock(board))
        finally:
            server._REGENERATE_TARGET_ROUTES['board'] = ('estate/BOARD.md', 'estate')
        # An unresolvable target degrades to None rather than refusing to run.
        self.assertIsNone(server._regenerate_target_lock('no-such-generator'))
