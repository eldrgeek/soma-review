"""The trunk gap: changes that reached the document with no sidecar row at all
(agreed model 12b, second layer, 2026-09-04).

The machine half of the ringer list reads the sidecar, so it is complete only
for changes made through the review surface. A writer who edits the Markdown
file directly changes the reader's text and leaves no row, and every list on
the page comes back clean. That is the same silent swallow one layer down: a
list that is complete *if the writer behaved*.

The trunk is the second witness. Sidecar changes commit and record their sha,
so commits touching the document minus shas the sidecar claims is exactly the
set of changes made behind the surface's back. These tests pin that:
  - a direct file write inside the round IS named back to the reader;
  - a change made through the surface is NOT double-reported;
  - a commit under the reader's own git identity is rung anyway, with the caveat
    (this machine commits agent work under his name);
  - an uncommitted working-tree edit counts too;
  - when the check cannot run at all, the page says so instead of implying a
    clean round.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

V2_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, V2_DIR)

import server  # noqa: E402

DOC = '# Title\n\nAlpha one.\n\nBravo two.\n\nCharlie three.\n\nDelta four.\n'


class _Base(unittest.TestCase):
    git = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, 'docs'))
        self.doc = os.path.join(self.root, 'docs', 'page.md')
        self.config = os.path.join(self.root, 'workspaces.json')
        with open(self.config, 'w', encoding='utf-8') as handle:
            json.dump({'estate': {'label': 'Test', 'roots': [['docs', 'docs']], 'nav': [],
                                  'home': 'docs/page.md', 'feedback_dir': 'feedback',
                                  'nightly': False, 'tours': False}}, handle)
        with open(self.doc, 'w', encoding='utf-8') as handle:
            handle.write(DOC)
        if self.git:
            self._git('init', '-q')
            self._git('config', 'user.email', 'claude@mike-wolf.com')
            self._git('config', 'user.name', 'Dee')
            self._git('add', 'docs/page.md')
            self._git('commit', '-q', '-m', 'seed')
            # The seed is the document as it stood before anyone reviewed it.
            # Sidecar stamps are second-resolution and the window opens a
            # second early (over-report bias), so let the clock move on.
            time.sleep(2.2)
        self.old_root, self.old_config = server.PROJECTS_ROOT, server.WORKSPACES_CONFIG
        server.PROJECTS_ROOT, server.WORKSPACES_CONFIG = self.root, self.config
        server.render_page('docs/page.md', view='v3')
        self.blocks = server.current_page_blocks('docs/page.md')[1]
        self.httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        server.PROJECTS_ROOT, server.WORKSPACES_CONFIG = self.old_root, self.old_config
        self.tmp.cleanup()

    def _git(self, *args):
        return subprocess.run(['git', '-C', self.root] + list(args),
                              capture_output=True, text=True, check=False)

    def _blk(self, i):
        return self.blocks[i]['id']

    def _post(self, payload, path='/api/comments'):
        data = json.dumps(payload).encode('utf-8')
        request = urllib.request.Request(
            f'http://127.0.0.1:{self.httpd.server_port}{path}',
            data=data, headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(request) as response:
            return response.status, json.load(response)

    def _mark(self, block_index, kind='agree', author='mike'):
        blocks = server.current_page_blocks('docs/page.md')[1]
        block = next(b for b in blocks if b['id'] == self._blk(block_index))
        return self._post({'page': 'docs/page.md', 'type': 'mark', 'mark_kind': kind,
                           'block_id': block['id'], 'quote': server.blockmap.norm(block['text']),
                           'author': author, 'text': 'ok'})

    def _rewrite(self, old, new, commit_as=None, message='direct edit'):
        """Change the trunk the way an agent with an editor does: no sidecar row."""
        with open(self.doc, 'r', encoding='utf-8') as handle:
            text = handle.read()
        with open(self.doc, 'w', encoding='utf-8') as handle:
            handle.write(text.replace(old, new))
        if commit_as:
            name, email = commit_as
            self._git('-c', f'user.name={name}', '-c', f'user.email={email}',
                      'commit', '-q', '-a', '-m', message)


class TrunkGapTests(_Base):
    git = True

    def test_a_direct_file_write_is_named_back_to_the_reader(self):
        self._mark(1)  # opens the round
        self._rewrite('Bravo two.', 'Bravo two, quietly rewritten.',
                      commit_as=('Dee', 'claude@mike-wolf.com'), message='tighten Bravo')
        ringer = server.compute_ringer_list('docs/page.md')
        self.assertEqual(0, ringer['swallowed'],
                         'no sidecar row exists, so the sidecar half sees nothing')
        self.assertEqual(1, ringer['unattributed'])
        row = next(r for r in ringer['ringers'] if r['why'] == 'unattributed')
        self.assertIn('quietly rewritten', row['after'])
        html = server.render_ringer_section(ringer)
        self.assertIn('trunk change with no sidecar row', html)
        self.assertIn('with no row on this surface at all', html)

    def test_a_change_made_through_the_surface_is_not_double_reported(self):
        self._post({'page': 'docs/page.md', 'type': 'edit', 'block_id': self._blk(1),
                    'author': 'claude', 'quote': 'Alpha one.', 'snapshot': 'Alpha one.',
                    'proposed': 'Alpha one, revised.'})
        self._mark(3)
        ringer = server.compute_ringer_list('docs/page.md')
        self.assertEqual(1, ringer['swallowed'], 'the sidecar half owns this one')
        self.assertEqual(0, ringer['unattributed'],
                         'its commit traces to a row, so the trunk witness stays quiet')
        self.assertEqual(1, ringer['trunk_gap']['accounted'])

    def test_a_commit_under_the_readers_identity_is_still_rung_with_the_caveat(self):
        """This laptop's `user.name` is Mike Wolf, so agents commit under his
        name. Excluding "his own" commits would suppress almost exactly the set
        this list exists to name — 7 of 7 on the real agreed-model document."""
        self._mark(1)
        self._rewrite('Bravo two.', 'Bravo two, committed under his name.',
                      commit_as=('Mike Wolf', 'mw@mike-wolf.com'), message='looks like his')
        ringer = server.compute_ringer_list('docs/page.md')
        self.assertEqual(1, ringer['unattributed'])
        self.assertEqual(1, ringer['trunk_gap']['by_reader'])
        row = next(r for r in ringer['ringers'] if r['why'] == 'unattributed')
        self.assertIn('proves nothing here', row['reason'])

    def test_an_uncommitted_edit_counts_too(self):
        self._mark(1)
        self._rewrite('Charlie three.', 'Charlie three, uncommitted.')
        ringer = server.compute_ringer_list('docs/page.md')
        self.assertEqual(1, ringer['unattributed'])
        row = next(r for r in ringer['ringers'] if r['why'] == 'unattributed')
        self.assertIn('uncommitted', row['reason'])

    def test_a_change_from_before_the_round_is_not_listed(self):
        self._rewrite('Delta four.', 'Delta four, changed last week.',
                      commit_as=('Dee', 'claude@mike-wolf.com'), message='old work')
        time.sleep(2.2)  # sidecar stamps are second-resolution; the window opens a second early
        self._mark(1)    # the round opens AFTER that commit
        ringer = server.compute_ringer_list('docs/page.md')
        self.assertEqual(0, ringer['unattributed'],
                         'the window is this round, not the whole history of the file')

    def test_the_two_clean_trunks_are_worded_differently(self):
        """"Every commit traced to a row" is a stronger claim than "no commit
        touched the file" — the same distinction the two empty lists make."""
        self._mark(1)
        html = server.render_ringer_section(server.compute_ringer_list('docs/page.md'))
        self.assertIn('had nothing to check', html)
        self.assertNotIn("behind this surface's back", html)
        self._post({'page': 'docs/page.md', 'type': 'edit', 'block_id': self._blk(1),
                    'author': 'claude', 'quote': 'Alpha one.', 'snapshot': 'Alpha one.',
                    'proposed': 'Alpha one, revised.'})
        html = server.render_ringer_section(server.compute_ringer_list('docs/page.md'))
        self.assertIn("behind this surface's back", html)

    def test_a_direct_edit_swept_into_a_recorded_commit_is_not_laundered(self):
        """Skip's finding 1 and Codex's finding 1, independently: `git add` stages
        the WHOLE file, so a direct edit left dirty is swept into the next
        surface commit and its sha then vouches for text no row ever proposed."""
        self._mark(1)
        self._rewrite('Charlie three.', 'Charlie three, slipped in by an agent.')  # dirty, no row
        self._post({'page': 'docs/page.md', 'type': 'edit', 'block_id': self._blk(1),
                    'author': 'claude', 'quote': 'Alpha one.', 'snapshot': 'Alpha one.',
                    'proposed': 'Alpha one, revised.'})  # commits BOTH
        ringer = server.compute_ringer_list('docs/page.md')
        self.assertEqual(1, ringer['trunk_gap']['laundered'])
        self.assertEqual(1, ringer['unattributed'])
        row = next(r for r in ringer['ringers'] if r['why'] == 'unattributed')
        self.assertIn('slipped in by an agent', row['after'])
        self.assertIn('git add stages the whole file', row['reason'])

    def test_the_readers_own_revert_does_not_ring_itself(self):
        """Skip's finding 2: the revert commits, and the sha was never stored on
        the row — so the reader's most deliberate act of attention came back to
        him as an unrecorded change to his own document."""
        _s, rev = self._post({'page': 'docs/page.md', 'type': 'edit', 'block_id': self._blk(1),
                              'author': 'claude', 'quote': 'Alpha one.', 'snapshot': 'Alpha one.',
                              'proposed': 'Alpha one, revised.'})
        self._mark(3)
        self._post({'page': 'docs/page.md', 'id': rev['id'], 'action': 'revert',
                    'author': 'mike'}, path='/api/marks/merge')
        ringer = server.compute_ringer_list('docs/page.md')
        self.assertEqual(0, ringer['unattributed'],
                         'the revert is recorded on the row it reverted')

    def test_an_untracked_document_is_unverifiable_not_clean(self):
        """Codex's finding 2: `git status` reports `??` for the whole file, so a
        direct edit to it is invisible to both halves."""
        new_doc = os.path.join(self.root, 'docs', 'fresh.md')
        with open(new_doc, 'w', encoding='utf-8') as handle:
            handle.write('# Fresh\n\nOne.\n\nTwo.\n')
        server.render_page('docs/fresh.md', view='v3')
        blocks = server.current_page_blocks('docs/fresh.md')[1]
        self._post({'page': 'docs/fresh.md', 'type': 'mark', 'mark_kind': 'agree',
                    'block_id': blocks[1]['id'], 'quote': server.blockmap.norm(blocks[1]['text']),
                    'author': 'mike', 'text': 'ok'})
        ringer = server.compute_ringer_list('docs/fresh.md')
        self.assertEqual('untracked', ringer['trunk_gap']['status'])
        self.assertIn('could NOT be checked', server.render_ringer_section(ringer))


class TrunkGapUnavailableTests(_Base):
    git = False

    def test_an_unverifiable_trunk_says_so_instead_of_implying_a_clean_round(self):
        """The empty that matters most: the check could not run at all. Silence
        here would be the page claiming a clean round it never verified."""
        self._mark(1)
        ringer = server.compute_ringer_list('docs/page.md')
        self.assertEqual('untracked', ringer['trunk_gap']['status'])
        html = server.render_ringer_section(ringer)
        self.assertIn('could NOT be checked', html)
        self.assertIn('not inside a git repository', html)


if __name__ == '__main__':
    unittest.main()
