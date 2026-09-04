"""Tests for the machine half of the ringer list (agreed model 12b, 2026-09-04).

Fork Q1 closed as Alternative B: a bracket of assent covers everything between
the reader's own marks, revisions included. So a revision can reach the trunk
with the reader's assent and without his attention. 12b requires every round to
close with a machine-generated list naming exactly those revisions back to him.

These tests pin the properties that make the list trustworthy:
  - a revision inside the bracket that the reader never touched IS listed;
  - a revision he marked, settled, or reverted is NOT (that is attention);
  - a revision past his last-read block is NOT (his bracket never reached it);
  - the list is server-rendered into the v3 page, so it cannot go missing when
    a client script fails;
  - with no reader marks and no signal there is no bracket, and the empty list
    says so rather than implying a clean round.
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


class RingerListTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, 'docs'))
        self.doc = os.path.join(self.root, 'docs', 'page.md')
        self.config = os.path.join(self.root, 'workspaces.json')
        with open(self.config, 'w', encoding='utf-8') as handle:
            json.dump({
                'estate': {
                    'label': 'Test', 'roots': [['docs', 'docs']],
                    'nav': [], 'home': 'docs/page.md', 'feedback_dir': 'feedback',
                    'nightly': False, 'tours': False,
                }
            }, handle)
        with open(self.doc, 'w', encoding='utf-8') as handle:
            handle.write('# Title\n\nAlpha one.\n\nBravo two.\n\n'
                         'Charlie three.\n\nDelta four.\n\n'
                         'Echo five. Foxtrot six.\n')
        self.old_root = server.PROJECTS_ROOT
        self.old_config = server.WORKSPACES_CONFIG
        server.PROJECTS_ROOT = self.root
        server.WORKSPACES_CONFIG = self.config
        server.render_page('docs/page.md', view='v3')
        self.blocks = server.current_page_blocks('docs/page.md')[1]
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

    # --- helpers ---------------------------------------------------------
    def _blk(self, i):
        return self.blocks[i]['id']

    def _post(self, payload, path='/api/comments'):
        data = json.dumps(payload).encode('utf-8')
        request = urllib.request.Request(
            f'http://127.0.0.1:{self.httpd.server_port}{path}',
            data=data, headers={'Content-Type': 'application/json'}, method='POST',
        )
        with urllib.request.urlopen(request) as response:
            return response.status, json.load(response)

    def _get(self, path):
        with urllib.request.urlopen(
                f'http://127.0.0.1:{self.httpd.server_port}{path}') as response:
            return response.status, json.load(response)

    def _revision(self, block_index, before, after, author='claude', **extra):
        payload = {'page': 'docs/page.md', 'type': 'edit',
                   'block_id': self._blk(block_index), 'author': author,
                   'quote': before, 'snapshot': before, 'proposed': after}
        payload.update(extra)
        return self._post(payload)

    def _mark(self, block_index, kind='agree', author='mike'):
        blocks = server.current_page_blocks('docs/page.md')[1]
        block = next(b for b in blocks if b['id'] == self._blk(block_index))
        return self._post({'page': 'docs/page.md', 'type': 'mark',
                           'mark_kind': kind, 'block_id': block['id'],
                           'quote': server.blockmap.norm(block['text']),
                           'author': author, 'text': 'ok'})

    def _mark_sentence(self, block_index, sentence, kind='agree', author='mike'):
        """A mark on ONE sentence of a block, the way the v3 surface posts it:
        the quote is the sentence, not the paragraph, and the server resolves
        it to a from/to span inside the block."""
        return self._post({'page': 'docs/page.md', 'type': 'mark',
                           'mark_kind': kind, 'block_id': self._blk(block_index),
                           'quote': sentence, 'author': author, 'text': 'ok'})

    # --- tests -----------------------------------------------------------
    def test_no_marks_and_no_signal_means_no_bracket(self):
        self._revision(1, 'Alpha one.', 'Alpha one, revised.')
        ringer = server.compute_ringer_list('docs/page.md')
        self.assertIsNone(ringer['bracket']['upper'])
        self.assertEqual([], ringer['ringers'])
        html = server.render_ringer_section(ringer)
        self.assertIn('No bracket yet', html)

    def test_revision_inside_bracket_and_unmarked_is_a_ringer(self):
        self._revision(1, 'Alpha one.', 'Alpha one, revised.')
        self._mark(3)  # reader marks a LATER block; block 1 falls inside
        ringer = server.compute_ringer_list('docs/page.md')
        self.assertEqual(1, ringer['swallowed'])
        self.assertEqual(self._blk(1), ringer['ringers'][0]['block_id'])
        self.assertEqual('swallowed', ringer['ringers'][0]['why'])

    def test_revision_on_a_block_the_reader_marked_is_not_a_ringer(self):
        self._revision(1, 'Alpha one.', 'Alpha one, revised.')
        self._mark(1)
        self._mark(3)
        ringer = server.compute_ringer_list('docs/page.md')
        self.assertEqual(0, ringer['swallowed'])

    def test_revision_past_the_last_read_block_is_not_a_ringer(self):
        self._revision(3, 'Charlie three.', 'Charlie three, revised.')
        self._mark(1)
        self._post({'page': 'docs/page.md', 'type': 'mark',
                    'mark_kind': 'reader-signal', 'signal': 'gave-up',
                    'author': 'mike', 'last_read_block': self._blk(2)})
        ringer = server.compute_ringer_list('docs/page.md')
        self.assertEqual(0, ringer['swallowed'],
                         'a revision below where he stopped was never inside his bracket')

    def test_reader_signal_extends_the_bracket(self):
        self._revision(2, 'Bravo two.', 'Bravo two, revised.')
        self._post({'page': 'docs/page.md', 'type': 'mark',
                    'mark_kind': 'reader-signal', 'signal': 'done',
                    'author': 'mike', 'last_read_block': self._blk(4)})
        ringer = server.compute_ringer_list('docs/page.md')
        self.assertEqual('reader-signal', ringer['bracket']['basis'])
        self.assertEqual(1, ringer['swallowed'])

    def test_settling_a_revision_as_the_reader_clears_it(self):
        _status, rev = self._revision(1, 'Alpha one.', 'Alpha one, revised.')
        self._mark(3)
        self.assertEqual(1, server.compute_ringer_list('docs/page.md')['swallowed'])
        self._post({'page': 'docs/page.md', 'id': rev['id'], 'action': 'settle',
                    'author': 'mike'}, path='/api/marks/merge')
        self.assertEqual(0, server.compute_ringer_list('docs/page.md')['swallowed'])

    def test_settling_as_the_writer_does_not_clear_it(self):
        _status, rev = self._revision(1, 'Alpha one.', 'Alpha one, revised.')
        self._mark(3)
        self._post({'page': 'docs/page.md', 'id': rev['id'], 'action': 'settle',
                    'author': 'claude'}, path='/api/marks/merge')
        self.assertEqual(1, server.compute_ringer_list('docs/page.md')['swallowed'],
                         'the writer resolving his own change is not the reader\'s attention')

    def test_writer_flagged_ringer_is_listed_without_a_bracket(self):
        self._revision(1, 'Alpha one.', 'Alpha one, revised.',
                       ringer=True, reason='contradicts his 09-03 ruling')
        ringer = server.compute_ringer_list('docs/page.md')
        self.assertEqual(1, ringer['flagged'])
        self.assertIn('contradicts his 09-03 ruling',
                      server.render_ringer_section(ringer))

    def test_reader_own_revision_is_never_a_ringer(self):
        self._revision(1, 'Alpha one.', 'Alpha one, revised.', author='mike')
        self._mark(3)
        self.assertEqual(0, server.compute_ringer_list('docs/page.md')['swallowed'])

    def test_section_is_server_rendered_into_the_v3_page(self):
        self._revision(1, 'Alpha one.', 'Alpha one, revised.')
        self._mark(3)
        html = server.render_page('docs/page.md', view='v3')
        self.assertIn('id="ringer-list"', html)
        self.assertIn('Ringer list (1)', html)
        self.assertIn('<del>', html.split('id="ringer-list"')[1])
        self.assertNotIn('id="ringer-list"', server.render_page('docs/page.md', view='classic'))

    def test_api_ringer_matches_the_rendered_section(self):
        self._revision(1, 'Alpha one.', 'Alpha one, revised.')
        self._mark(3)
        status, payload = self._get('/api/ringer?page=docs/page.md')
        self.assertEqual(200, status)
        self.assertEqual(1, payload['swallowed'])
        self.assertEqual(self._blk(1), payload['ringers'][0]['block_id'])
        self.assertEqual('docs/page.md', payload['page'])

    def test_revision_whose_block_vanished_is_still_listed(self):
        self._revision(1, 'Alpha one.', 'Alpha one, revised.')
        self._mark(3)
        # The block leaves the document entirely — the least reviewable state a
        # change can be in, and precisely the one a naive position filter drops.
        with open(self.doc, 'w', encoding='utf-8') as handle:
            handle.write('# Title\n\nBravo two.\n\nCharlie three.\n\nDelta four.\n')
        ringer = server.compute_ringer_list('docs/page.md')
        self.assertEqual(1, ringer['swallowed'])
        self.assertIsNone(ringer['ringers'][0]['position'])

    def test_revisions_below_the_bracket_edge_are_counted_not_hidden(self):
        self._revision(4, 'Delta four.', 'Delta four, revised.')
        self._mark(1)
        ringer = server.compute_ringer_list('docs/page.md')
        self.assertEqual(0, ringer['swallowed'])
        self.assertEqual(1, ringer['outside_bracket'])
        self.assertIn('below the bracket edge', server.render_ringer_section(ringer))

    def test_two_empties_are_worded_differently(self):
        """"Everything was handled" is a stronger claim than "there was nothing
        to handle" — conflating them is the over-claim this list exists to stop."""
        self._mark(1)
        self._mark(3)
        self.assertIn('nothing for the bracket to swallow',
                      server.render_ringer_section(server.compute_ringer_list('docs/page.md')))
        self._revision(2, 'Bravo two.', 'Bravo two, revised.')
        self._mark(2)
        self.assertIn('Nothing was swallowed this round',
                      server.render_ringer_section(server.compute_ringer_list('docs/page.md')))

    def test_a_mark_made_before_the_revision_does_not_suppress_it(self):
        """Skip's finding 3: `marked` had no time dimension, so one agree in
        round one suppressed every later revision to that block forever."""
        self._mark(1)
        self._revision(1, 'Alpha one.', 'Alpha one, revised.')
        self._mark(3)
        ringer = server.compute_ringer_list('docs/page.md')
        self.assertEqual(1, ringer['swallowed'],
                         'he marked that block before the change existed')

    def test_a_reply_is_not_attention_on_the_block(self):
        """Skip's finding 5: replies are re-bound to the thread root's block, so
        answering a decision card was registering as a mark on the sentence."""
        _s, root = self._post({'page': 'docs/page.md', 'type': 'mark',
                               'mark_kind': 'decision', 'block_id': self._blk(1),
                               'quote': 'Alpha one.', 'author': 'claude',
                               'text': 'Which reading?'})
        self._revision(1, 'Alpha one.', 'Alpha one, revised.')
        self._post({'page': 'docs/page.md', 'thread_id': root['id'],
                    'text': 'A', 'author': 'mike', 'status': 'seen'},
                   path='/api/comments/reply')
        self._mark(3)
        self.assertEqual(1, server.compute_ringer_list('docs/page.md')['swallowed'])

    def test_a_deleted_edit_row_whose_change_landed_is_listed_as_withdrawn(self):
        """Skip's finding 2: deleting the row does not revert the trunk, so a
        delete used to remove the change from every list while keeping it in
        the document."""
        _s, rev = self._revision(1, 'Alpha one.', 'Alpha one, revised.')
        server.update_comment('docs/page.md', rev['id'],
                              {'deleted': True, 'commit': 'deadbeef'}, 'estate')
        ringer = server.compute_ringer_list('docs/page.md')
        self.assertEqual(1, ringer['withdrawn'])
        self.assertIn('withdrawn, still in the trunk',
                      server.render_ringer_section(ringer))

    def test_an_unnamed_resolver_is_not_credited_as_the_reader(self):
        """Skip's finding 7: `resolved_by` defaulted to 'mike', so any settle
        with no author cleared the ringer it was added to preserve."""
        _s, rev = self._revision(1, 'Alpha one.', 'Alpha one, revised.')
        self._mark(3)
        self._post({'page': 'docs/page.md', 'id': rev['id'], 'action': 'settle'},
                   path='/api/marks/merge')
        self.assertEqual(1, server.compute_ringer_list('docs/page.md')['swallowed'])

    def test_api_ringer_requires_a_page(self):
        try:
            self._get('/api/ringer')
        except urllib.error.HTTPError as exc:
            self.assertEqual(400, exc.code)
        else:
            self.fail('expected 400 without a page')



    # --- sentence-level attention (2026-09-04) ---------------------------
    #
    # Mike marks a sentence, not a paragraph. Until this pass, any mark
    # anywhere in a block suppressed every earlier revision anywhere in that
    # block, so an `ack` on sentence one answered for a rewrite of sentence
    # four and the change left the list unnamed — an under-report, the one
    # direction this list must never fail in.

    def test_mark_on_another_sentence_does_not_suppress_the_revision(self):
        self._revision(5, 'Foxtrot six.', 'Foxtrot six, rewritten.')
        status, _ = self._mark_sentence(5, 'Echo five.')
        self.assertEqual(201, status)
        ringer = server.compute_ringer_list('docs/page.md')
        self.assertEqual(1, ringer['swallowed'],
                         'an ack on sentence one is not attention on a rewrite '
                         'of sentence two')
        self.assertEqual('swallowed', ringer['ringers'][0]['why'])

    def test_mark_on_the_revised_sentence_does_suppress_it(self):
        self._revision(5, 'Foxtrot six.', 'Foxtrot six, rewritten.')
        status, _ = self._mark_sentence(5, 'Foxtrot six, rewritten.')
        self.assertEqual(201, status)
        self.assertEqual(0, server.compute_ringer_list('docs/page.md')['swallowed'],
                         'a mark whose own text carries the revision IS attention on it')

    def test_whole_block_mark_still_suppresses_every_sentence(self):
        self._revision(5, 'Foxtrot six.', 'Foxtrot six, rewritten.')
        self._mark(5)
        self.assertEqual(0, server.compute_ringer_list('docs/page.md')['swallowed'],
                         'a mark on the whole paragraph covers all of it, as before')

    def test_two_sentence_marks_are_both_kept(self):
        """Collapsing a block's marks to the newest one threw away the span
        that mattered: the earlier mark is the one that covers the revision."""
        self._revision(5, 'Echo five.', 'Echo five, rewritten.')
        self._mark_sentence(5, 'Echo five, rewritten.')
        self._mark_sentence(5, 'Foxtrot six.')
        self.assertEqual(0, server.compute_ringer_list('docs/page.md')['swallowed'])

    def test_a_short_revision_matching_mid_sentence_is_not_suppressed(self):
        """Plain substring containment under-reports: a revision whose new text
        is short and common can appear inside an unrelated marked sentence. The
        match must land on whole sentences or it is not attention."""
        self.assertFalse(server._ringer_text_covers(
            'We are Done. And moving on.', 'Done.'))
        self.assertTrue(server._ringer_text_covers(
            'Echo five. Foxtrot six, rewritten.', 'Foxtrot six, rewritten.'))
        self.assertTrue(server._ringer_text_covers(
            'Foxtrot six, rewritten.', 'Foxtrot six, rewritten.'))

    def test_an_empty_span_mark_covers_nothing(self):
        """A caret click selects no text. The binder accepts from == to and
        stores an empty quote, so covering-on-empty would let a mark that read
        nothing suppress every revision in its block."""
        marks = [{'key': ('2026-09-04T10:00:00Z', 9), 'text': '', 'whole': False}]
        self.assertFalse(server._ringer_attention_covers(
            marks, {'proposed': 'Foxtrot six, rewritten.'},
            ('2026-09-04T09:00:00Z', 1)))

    def test_a_revision_with_no_text_is_never_assumed_seen(self):
        marks = [{'key': ('2026-09-04T10:00:00Z', 9), 'text': 'Echo five.',
                  'whole': False}]
        self.assertFalse(server._ringer_attention_covers(
            marks, {}, ('2026-09-04T09:00:00Z', 1)))

    def test_whole_block_branch_is_pinned_independently(self):
        """The whole-block escape must carry its own weight: a mark whose text
        does NOT contain the revision still covers when it spans the block."""
        marks = [{'key': ('2026-09-04T10:00:00Z', 9), 'text': 'nothing alike',
                  'whole': True}]
        self.assertTrue(server._ringer_attention_covers(
            marks, {'proposed': 'Foxtrot six, rewritten.'},
            ('2026-09-04T09:00:00Z', 1)))

if __name__ == '__main__':
    unittest.main()
