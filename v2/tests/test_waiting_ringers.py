"""`/waiting` must count the ringer list, and must not compute it inline.

On 2026-09-04 the front door read sidecars only. `mdp-agreed-model.md` — the
trunk of the marked-document protocol, carrying nine changes Mike had never
been shown — rendered as `0 for you`, an empty inbox row on the one surface
built to tell him what was waiting. These tests go red against that behaviour.

The second half of the contract is cost. `render_sidebar` calls
`collect_waiting()` on every page in the app; `compute_ringer_list` over the
live estate workspace costs 9.9s for 17 documents. So `collect_waiting` reads a
cache and never computes, and a background pass fills it.

The third half is what happens when the cache is not current, which on this
estate is most of the time: `_estate/` has no repo of its own, so every
`_estate` document invalidates together each time the COO loop commits. A
`stale` entry therefore keeps its number and says it is recounting. Collapsing
`stale` into `unknown`, and `unknown` into zero, would have re-created the
original bug wearing a grey label.
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402

NOW = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
OLD_TS = '2026-01-01T00:00:00Z'
LIVE_ROUTE = 'soma/shared-cognition/mdp-agreed-model.md'


def row(**kw):
    base = {'workspace': 'estate', 'workspace_label': 'Estate', 'route': 'estate/X.md',
            'title': 'X', 'exists': True, 'on_mike': 0, 'on_dee': 0, 'total': 0,
            'last_ts': NOW, 'signal': '', 'first_open_block': None, 'kinds': [],
            'ringers': None, 'ringers_state': 'unknown', 'ringer_block': None,
            'reader_last_ts': NOW}
    base.update(kw)
    return base


class AskCount(unittest.TestCase):
    def test_ringers_alone_make_a_document_an_ask(self):
        """The regression itself: nine unseen changes, zero sidecar rows."""
        self.assertEqual(server._waiting_ask_count(row(ringers=9, ringers_state='fresh')), 9)

    def test_marks_and_ringers_add(self):
        self.assertEqual(
            server._waiting_ask_count(row(on_mike=15, ringers=1, ringers_state='fresh')), 16)

    def test_a_stale_cache_entry_keeps_its_number(self):
        """The coarse-invalidation trap. If `stale` counted as zero, a
        nine-change document would render as an empty inbox row again every
        time the COO loop committed — the original bug, wearing a label."""
        self.assertEqual(server._waiting_ask_count(row(ringers=9, ringers_state='stale')), 9)

    def test_never_counted_contributes_nothing_but_is_queued(self):
        r = row(ringers=None, ringers_state='unknown')
        self.assertEqual(server._waiting_ask_count(r), 0)
        self.assertEqual(server._ringer_warm_targets([r]), [('estate', 'estate/X.md')])

    def test_document_outside_a_live_round_contributes_marks_only(self):
        """WORKQUEUE.md carries 50 unattributed commits spanning two months of
        agent edits. That is churn, not a round."""
        r = row(on_mike=3, ringers=50, ringers_state='fresh',
                last_ts=OLD_TS, reader_last_ts=OLD_TS)
        self.assertEqual(server._waiting_ask_count(r), 3)

    def test_the_clock_is_the_readers_own_marks(self):
        """WORKQUEUE.md is written by agents hourly, so `last_ts` is always
        current while Mike's last mark there is from July. Judging the round by
        `last_ts` puts 50 commits of machine churn at the top of his inbox."""
        churned = row(ringers=50, ringers_state='fresh', last_ts=NOW, reader_last_ts=OLD_TS)
        self.assertEqual(server._waiting_ask_count(churned), 0)
        self.assertEqual(
            server._ringer_warm_targets([dict(churned, ringers_state='stale')]), [])

    def test_a_round_he_has_never_marked_is_not_filtered_out(self):
        """Falling back to `last_ts` matters: a brand-new round carries no mark
        of his yet, and must not be suppressed before he can make one."""
        self.assertEqual(
            server._waiting_ask_count(row(ringers=4, ringers_state='fresh', reader_last_ts='')), 4)


class WarmTargets(unittest.TestCase):
    def test_fresh_cache_is_not_recomputed(self):
        self.assertEqual(server._ringer_warm_targets([row(ringers=2, ringers_state='fresh')]), [])

    def test_document_outside_a_live_round_is_never_warmed(self):
        self.assertEqual(server._ringer_warm_targets(
            [row(ringers_state='unknown', last_ts=OLD_TS, reader_last_ts=OLD_TS)]), [])

    def test_a_cached_error_is_not_recomputed_forever(self):
        """Its fingerprint still matches, so a recompute can only raise again —
        ahead of legitimate documents in a single-threaded queue."""
        self.assertEqual(server._ringer_warm_targets([row(ringers_state='error')]), [])

    def test_a_stale_entry_is_warmed(self):
        self.assertEqual(server._ringer_warm_targets([row(ringers_state='stale', ringers=3)]),
                         [('estate', 'estate/X.md')])

    def test_unknown_and_current_is_warmed(self):
        self.assertEqual(server._ringer_warm_targets([row(ringers_state='unknown')]),
                         [('estate', 'estate/X.md')])


class NoDoubleCounting(unittest.TestCase):
    """A swallowed revision can also be an open row addressed to the reader —
    an edit by Dee, still queued, inside his bracket, that he never marked. It
    is one thing waiting on him, not two."""

    def test_overlap_is_subtracted(self):
        entry = {'total': 3, 'ids': ['a', 'b', 'c']}
        self.assertEqual(server._ringer_unseen_count(entry, {'b'}), 2)
        self.assertEqual(server._ringer_unseen_count(entry, {'a', 'b', 'c'}), 0)

    def test_no_overlap_is_untouched(self):
        self.assertEqual(server._ringer_unseen_count({'total': 3, 'ids': ['a']}, {'z'}), 3)

    def test_legacy_entry_without_ids_reports_its_total(self):
        self.assertEqual(server._ringer_unseen_count({'total': 4}, {'a'}), 4)

    def test_zero_stays_zero(self):
        self.assertEqual(server._ringer_unseen_count({'total': 0, 'ids': []}, {'a'}), 0)


class CacheContract(unittest.TestCase):
    def setUp(self):
        self._real = server.RINGER_COUNT_CACHE
        server.RINGER_COUNT_CACHE = tempfile.mktemp(suffix='.ringer-test.json')
        server._ringer_cache_mem = None

    def tearDown(self):
        for p in (server.RINGER_COUNT_CACHE,
                  f'{server.RINGER_COUNT_CACHE}.tmp.{os.getpid()}'):
            if os.path.exists(p):
                os.remove(p)
        server.RINGER_COUNT_CACHE = self._real
        server._ringer_cache_mem = None

    def test_missing_entry_is_unknown_not_zero(self):
        self.assertEqual(server.ringer_count_cached(LIVE_ROUTE), ('unknown', {}))

    def test_compute_then_read_is_fresh_and_cheap(self):
        server.ringer_count_compute(LIVE_ROUTE)
        server._ringer_cache_mem = None          # force a read from disk
        t = time.time()
        state, entry = server.ringer_count_cached(LIVE_ROUTE)
        elapsed = time.time() - t
        self.assertEqual(state, 'fresh')
        self.assertIsInstance(entry['total'], int)
        # compute_ringer_list on this page is ~0.6s; a cached read must be
        # nowhere near it, because collect_waiting does 17 per render.
        self.assertLess(elapsed, 0.05)

    def test_compute_settles_on_fresh_in_one_pass(self):
        """compute_ringer_list is itself a writer — it saves the block map and
        can remap sidecar offsets, both fingerprint inputs. Taking the
        fingerprint before the work stored one its own side effects had already
        invalidated: two computes per edit, and a document under active edit
        that never reached `fresh` at all."""
        server.ringer_count_compute(LIVE_ROUTE)
        self.assertEqual(server.ringer_count_cached(LIVE_ROUTE)[0], 'fresh')

    def test_compute_records_ids_for_the_overlap_subtraction(self):
        server.ringer_count_compute(LIVE_ROUTE)
        entry = server._ringer_cache_mem[f'{server.DEFAULT_WORKSPACE}/{LIVE_ROUTE}']
        self.assertEqual(len(entry['ids']), entry['total'])

    def test_changed_inputs_go_stale_and_keep_the_number(self):
        server.ringer_count_compute(LIVE_ROUTE)
        state, entry = server.ringer_count_cached(LIVE_ROUTE)
        self.assertEqual(state, 'fresh')
        total = entry['total']
        key = f'{server.DEFAULT_WORKSPACE}/{LIVE_ROUTE}'
        server._ringer_cache_put(key, dict(entry, fingerprint='not-the-fingerprint'))
        state, entry = server.ringer_count_cached(LIVE_ROUTE)
        self.assertEqual(state, 'stale')
        self.assertEqual(entry['total'], total)

    def test_fingerprint_moves_with_the_sidecar(self):
        before = server._ringer_fingerprint(LIVE_ROUTE, server.DEFAULT_WORKSPACE)
        path = server.sidecar_path(LIVE_ROUTE, server.DEFAULT_WORKSPACE)
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
        try:
            self.assertNotEqual(
                before, server._ringer_fingerprint(LIVE_ROUTE, server.DEFAULT_WORKSPACE))
        finally:
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))

    def test_fingerprint_moves_with_git_history(self):
        """A merge or a branch switch can leave the working tree byte-identical
        and still change what the trunk-gap half of the list reports. Driven for
        real, by moving the reflog the stamp is taken from."""
        fs_path = server.resolve_page(LIVE_ROUTE, server.DEFAULT_WORKSPACE)
        before = server._ringer_git_stamp(fs_path)
        self.assertNotEqual(before, '-')
        gitdir = server._ringer_git_dirs[os.path.dirname(fs_path)]
        log = os.path.join(gitdir, 'logs', 'HEAD')
        st = os.stat(log)
        os.utime(log, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
        try:
            self.assertNotEqual(before, server._ringer_git_stamp(fs_path))
        finally:
            os.utime(log, ns=(st.st_atime_ns, st.st_mtime_ns))

    def test_a_missing_repo_is_never_memoised(self):
        """Everything else here degrades toward a wasteful recount. A memoised
        `None` would degrade toward a confident wrong number: the stamp would
        stay '-' forever, so git could never invalidate the entry again."""
        d = tempfile.mkdtemp(dir='/tmp')
        try:
            self.assertEqual(server._ringer_git_stamp(os.path.join(d, 'x.md')), '-')
            self.assertNotIn(d, server._ringer_git_dirs)
        finally:
            os.rmdir(d)

    def test_compute_records_an_error_rather_than_a_zero(self):
        route = 'estate/NO-SUCH-DOCUMENT.md'
        server.ringer_count_compute(route)
        entry = server._ringer_cache_mem.get(f'{server.DEFAULT_WORKSPACE}/{route}')
        self.assertIsNotNone(entry)
        self.assertIn('error', entry)
        self.assertNotIn('total', entry)
        self.assertEqual(server.ringer_count_cached(route)[0], 'error')

    def test_version_bump_invalidates_old_entries(self):
        fp = server._ringer_fingerprint(LIVE_ROUTE, server.DEFAULT_WORKSPACE)
        self.assertTrue(fp.startswith(f'v{server.RINGER_CACHE_VERSION}|'))


class WarmPass(unittest.TestCase):
    def setUp(self):
        self._real = server.RINGER_COUNT_CACHE
        server.RINGER_COUNT_CACHE = tempfile.mktemp(suffix='.ringer-test.json')
        server._ringer_cache_mem = None

    def tearDown(self):
        for p in (server.RINGER_COUNT_CACHE,
                  f'{server.RINGER_COUNT_CACHE}.tmp.{os.getpid()}'):
            if os.path.exists(p):
                os.remove(p)
        server.RINGER_COUNT_CACHE = self._real
        server._ringer_cache_mem = None

    def _drain(self, limit=60):
        import threading
        deadline = time.time() + limit
        while time.time() < deadline:
            if not any(t.name == 'ringer-warm' for t in threading.enumerate()):
                return True
            time.sleep(0.1)
        return False

    def test_a_pass_makes_the_row_fresh(self):
        self.assertEqual(server.ringer_count_cached(LIVE_ROUTE)[0], 'unknown')
        self.assertTrue(server.ringer_warm_async([(server.DEFAULT_WORKSPACE, LIVE_ROUTE)]))
        self.assertTrue(self._drain())
        self.assertEqual(server.ringer_count_cached(LIVE_ROUTE)[0], 'fresh')

    def test_a_bad_route_does_not_stop_the_pass(self):
        server.ringer_warm_async([(server.DEFAULT_WORKSPACE, 'estate/NO-SUCH.md'),
                                  (server.DEFAULT_WORKSPACE, LIVE_ROUTE)])
        self.assertTrue(self._drain())
        self.assertEqual(server.ringer_count_cached(LIVE_ROUTE)[0], 'fresh')

    def test_empty_target_list_starts_no_thread(self):
        self.assertFalse(server.ringer_warm_async([]))

    def test_single_flight(self):
        """A second request while one is running is dropped, not queued: the
        pass competes with the reader's own page loads for the same disk."""
        server._ringer_warming = True
        try:
            self.assertFalse(server.ringer_warm_async([(server.DEFAULT_WORKSPACE, LIVE_ROUTE)]))
        finally:
            server._ringer_warming = False
        self.assertTrue(server.ringer_warm_async([(server.DEFAULT_WORKSPACE, LIVE_ROUTE)]))
        self.assertTrue(self._drain())


class LiveWaitingPage(unittest.TestCase):
    """These read the live estate workspace but must not touch the production
    cache, and must not leave a background pass running past the test."""

    def setUp(self):
        self._real = server.RINGER_COUNT_CACHE
        server.RINGER_COUNT_CACHE = tempfile.mktemp(suffix='.ringer-test.json')
        server._ringer_cache_mem = None

    def tearDown(self):
        import threading
        deadline = time.time() + 90
        while time.time() < deadline and any(t.name == 'ringer-warm'
                                             for t in threading.enumerate()):
            time.sleep(0.2)
        for p in (server.RINGER_COUNT_CACHE,
                  f'{server.RINGER_COUNT_CACHE}.tmp.{os.getpid()}'):
            if os.path.exists(p):
                os.remove(p)
        server.RINGER_COUNT_CACHE = self._real
        server._ringer_cache_mem = None

    def test_collect_waiting_never_computes(self):
        """Pinned by substitution, not by a stopwatch: the render path must not
        reach compute_ringer_list even once, with the cache completely empty."""
        calls = []
        real = server.compute_ringer_list
        server.compute_ringer_list = lambda *a, **k: calls.append(a) or real(*a, **k)
        try:
            rows = server.collect_waiting()
        finally:
            server.compute_ringer_list = real
        self.assertTrue(rows)
        self.assertEqual(calls, [])
        for r in rows:
            self.assertIn('ringers_state', r)
            self.assertIn('reader_last_ts', r)

    def test_page_renders_and_names_the_unseen(self):
        server.ringer_count_compute(LIVE_ROUTE)
        html = server.render_waiting()
        self.assertIn('unseen change', html)
        self.assertIn('ringer-list', html)

    def test_the_badge_and_the_headline_agree(self):
        """They used to come from two separate collect_waiting() calls with a
        warm thread running between them, so a count landing in that window made
        one HTML response disagree with itself."""
        import re
        server.ringer_count_compute(LIVE_ROUTE)
        html = server.render_waiting()
        badge = re.search(r'Waiting on you <span[^>]*>(\d+)</span>', html)
        headline = re.search(r'waiting-sub">(\d+) open item', html)
        self.assertIsNotNone(badge)
        self.assertIsNotNone(headline)
        self.assertEqual(badge.group(1), headline.group(1))


if __name__ == '__main__':
    unittest.main()
