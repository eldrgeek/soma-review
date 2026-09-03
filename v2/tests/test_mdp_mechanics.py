"""Tests for the MDP mechanics build (2026-09-03, "A mind's model of the room
is agreed by Mike+Claude 2026-09-03 evening"): merge-on-accept (item B),
resolve-advances-in-document-order ordering data (item C, JS behavior pinned
by scope tests only — no headless browser here), and the generated Terms
view with model-anchored back-links (item D).
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

V2_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, V2_DIR)

import blockmap  # noqa: E402
import mdblocks  # noqa: E402
import server  # noqa: E402


def _git(*args, cwd):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, check=True)


class ChangeSettleRevertTests(unittest.TestCase):
    """apply_sentence_change / apply_sentence_settle / apply_sentence_revert
    (MDP mechanics item B, corrected by Mike 2026-09-03 evening: an edit is
    never queued/pending — the trunk file is updated AT ONCE when the change
    is made, Settle just resolves the mark, Revert writes `before` back). A
    real temp git repo fixture so the commit half is genuinely exercised."""

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

    def tearDown(self):
        server.PROJECTS_ROOT = self.old_root
        server.WORKSPACES_CONFIG = self.old_config
        self.tmp.cleanup()

    def write_doc(self, text):
        with open(self.doc, 'w', encoding='utf-8') as handle:
            handle.write(text)

    def commit_doc(self, message='initial'):
        _git('add', 'page.md', cwd=self.docs_repo)
        _git('commit', '-q', '-m', message, cwd=self.docs_repo)

    def make_edit_mark(self, mark_id, quote, proposed, author='claude'):
        """Mirrors do_POST's real ctype=='edit' path: build the sidecar row,
        apply the change to the trunk file AT ONCE (apply_sentence_change),
        then persist the row (which now carries the resulting commit sha) —
        this is the create-time write, not a later accept step."""
        server.render_page('docs/page.md', view='v3')
        _src, blocks, _map, _report = server.current_page_blocks('docs/page.md', 'estate')
        block = next(b for b in blocks if quote in b['text'])
        binding = server.validated_binding('docs/page.md', 'estate', {
            'block_id': block['id'], 'quote': quote,
        })
        row = {
            'id': mark_id, 'page': 'docs/page.md', 'type': 'edit',
            'anchor': None, 'snapshot': quote, 'proposed': proposed,
            'author': author, 'text': '(sentence change)',
            'timestamp': '2026-09-03T00:00:00Z', 'status': 'queued',
            'thread_id': mark_id, 'deleted': False, **binding,
        }
        change_result = server.apply_sentence_change('docs/page.md', 'estate', row, author_label=author)
        row['commit'] = change_result.get('commit')
        server.append_comment('docs/page.md', row)
        return row, change_result

    def test_change_writes_file_and_commits_at_creation_time(self):
        self.write_doc('# Title\n\nOriginal typo sentence here.\n\nSecond paragraph.\n')
        self.commit_doc()
        before_head = _git('rev-parse', 'HEAD', cwd=self.docs_repo).stdout.strip()

        mark, change_result = self.make_edit_mark('e1', 'Original typo sentence here.', 'Fixed sentence here.')

        with open(self.doc, encoding='utf-8') as f:
            content = f.read()
        self.assertIn('Fixed sentence here.', content)
        self.assertNotIn('Original typo sentence here.', content)
        self.assertIn('Second paragraph.', content)  # untouched sibling block

        self.assertIsNotNone(change_result['commit'])
        after_head = _git('rev-parse', 'HEAD', cwd=self.docs_repo).stdout.strip()
        self.assertEqual(change_result['commit'], after_head)
        self.assertNotEqual(before_head, after_head)
        log = _git('log', '-1', '--pretty=%B', cwd=self.docs_repo).stdout
        self.assertIn('e1', log)
        self.assertIn('docs/page.md', log)
        self.assertIn('Seat: dee', log)
        self.assertIn('change', log)

        self.assertIsNotNone(change_result['html'])
        self.assertIn('Fixed sentence here.', change_result['html'])

        # The mark is still OPEN (not settled/reverted) — the trunk write
        # happened at creation, resolution is a separate later step.
        saved = server.read_comments('docs/page.md')[0]
        self.assertEqual('queued', saved['status'])
        self.assertNotIn('settled', saved)
        self.assertNotIn('reverted', saved)

    def test_change_refuses_on_before_text_mismatch(self):
        self.write_doc('# Title\n\nOriginal typo sentence here.\n')
        self.commit_doc()

        # Someone already changed this sentence before this edit was authored
        # against it — snapshot ("before") no longer matches what's on disk.
        stale_row = {
            'id': 'e2', 'page': 'docs/page.md', 'type': 'edit', 'anchor': None,
            'snapshot': 'A sentence that never existed here.', 'proposed': 'Fixed sentence here.',
            'author': 'claude', 'text': '(sentence change)', 'timestamp': '2026-09-03T00:00:00Z',
            'status': 'queued', 'thread_id': 'e2', 'deleted': False,
        }
        server.render_page('docs/page.md', view='v3')
        _src, blocks, _map, _report = server.current_page_blocks('docs/page.md', 'estate')
        block = next(b for b in blocks if 'Original typo sentence here.' in b['text'])
        stale_row.update(server.validated_binding('docs/page.md', 'estate', {
            'block_id': block['id'], 'quote': 'Original typo sentence here.',
        }))
        stale_row['snapshot'] = 'A sentence that never existed here.'  # override after binding

        with self.assertRaises(server.MergeConflict):
            server.apply_sentence_change('docs/page.md', 'estate', stale_row, author_label='claude')

        with open(self.doc, encoding='utf-8') as f:
            content = f.read()
        self.assertIn('Original typo sentence here.', content)
        self.assertNotIn('Fixed sentence here.', content)

    def test_settle_resolves_mark_with_no_file_change(self):
        self.write_doc('# Title\n\nOriginal typo sentence here.\n')
        self.commit_doc()
        mark, _ = self.make_edit_mark('e3', 'Original typo sentence here.', 'Fixed sentence here.')
        before = open(self.doc, encoding='utf-8').read()
        before_head = _git('rev-parse', 'HEAD', cwd=self.docs_repo).stdout.strip()

        result = server.apply_sentence_settle('docs/page.md', 'estate', mark)
        server.update_comment('docs/page.md', 'e3', {'status': 'done', 'settled': True})

        after = open(self.doc, encoding='utf-8').read()
        after_head = _git('rev-parse', 'HEAD', cwd=self.docs_repo).stdout.strip()
        self.assertEqual(before, after)          # trunk untouched by settle
        self.assertEqual(before_head, after_head)  # no new commit
        self.assertIsNone(result['commit'])
        saved = server.read_comments('docs/page.md')[0]
        self.assertEqual('done', saved['status'])
        self.assertTrue(saved['settled'])

    def test_revert_writes_before_text_back_and_commits(self):
        self.write_doc('# Title\n\nOriginal typo sentence here.\n')
        self.commit_doc()
        mark, _ = self.make_edit_mark('e4', 'Original typo sentence here.', 'Fixed sentence here.')
        with open(self.doc, encoding='utf-8') as f:
            self.assertIn('Fixed sentence here.', f.read())
        before_head = _git('rev-parse', 'HEAD', cwd=self.docs_repo).stdout.strip()

        result = server.apply_sentence_revert('docs/page.md', 'estate', mark)
        server.update_comment('docs/page.md', 'e4', {'status': 'done', 'reverted': True})

        with open(self.doc, encoding='utf-8') as f:
            content = f.read()
        self.assertIn('Original typo sentence here.', content)
        self.assertNotIn('Fixed sentence here.', content)
        self.assertIsNotNone(result['commit'])
        after_head = _git('rev-parse', 'HEAD', cwd=self.docs_repo).stdout.strip()
        self.assertNotEqual(before_head, after_head)
        log = _git('log', '-1', '--pretty=%B', cwd=self.docs_repo).stdout
        self.assertIn('revert', log)
        self.assertIn('e4', log)
        saved = server.read_comments('docs/page.md')[0]
        self.assertEqual('done', saved['status'])
        self.assertTrue(saved['reverted'])

    def test_revert_refuses_if_open_change_no_longer_on_disk(self):
        self.write_doc('# Title\n\nOriginal typo sentence here.\n')
        self.commit_doc()
        mark, _ = self.make_edit_mark('e5', 'Original typo sentence here.', 'Fixed sentence here.')

        # Someone changes the sentence again before Mike reverts the first change.
        self.write_doc('# Title\n\nSomeone changed it again.\n')
        self.commit_doc('second change')

        with self.assertRaises(server.MergeConflict):
            server.apply_sentence_revert('docs/page.md', 'estate', mark)
        with open(self.doc, encoding='utf-8') as f:
            self.assertIn('Someone changed it again.', f.read())

    def test_change_without_git_repo_still_writes_file(self):
        no_git_root = tempfile.mkdtemp()
        try:
            config = os.path.join(no_git_root, 'workspaces.json')
            docs_dir = os.path.join(no_git_root, 'docs')
            os.makedirs(docs_dir)
            with open(config, 'w', encoding='utf-8') as handle:
                json.dump({
                    'estate': {
                        'label': 'Test', 'roots': [['docs', 'docs']],
                        'nav': [], 'home': 'docs/page.md', 'feedback_dir': 'feedback',
                        'nightly': False, 'tours': False,
                    }
                }, handle)
            server.PROJECTS_ROOT = no_git_root
            server.WORKSPACES_CONFIG = config
            self.doc = os.path.join(docs_dir, 'page.md')
            self.write_doc('# Title\n\nOriginal typo sentence here.\n')
            _mark, change_result = self.make_edit_mark('e6', 'Original typo sentence here.', 'Fixed sentence here.')
            self.assertIsNone(change_result['commit'])
            with open(self.doc, encoding='utf-8') as f:
                self.assertIn('Fixed sentence here.', f.read())
        finally:
            import shutil
            shutil.rmtree(no_git_root, ignore_errors=True)


class SegmentSentencesTests(unittest.TestCase):
    """mdblocks.segment_sentences — item C's "unit of editing is the
    sentence, not the block/paragraph" primitive."""

    def test_splits_on_terminal_punctuation(self):
        spans = mdblocks.segment_sentences('First one. Second one? Third one!')
        texts = [t for _, _, t in spans]
        self.assertEqual(['First one.', ' Second one?', ' Third one!'], texts)

    def test_reconstructs_exactly(self):
        text = 'Alpha beta. Gamma delta? Epsilon zeta!'
        spans = mdblocks.segment_sentences(text)
        self.assertEqual(text, ''.join(t for _, _, t in spans))

    def test_does_not_split_on_abbreviations(self):
        text = 'See the spec, e.g. this one. It has two real sentences.'
        spans = mdblocks.segment_sentences(text)
        self.assertEqual(2, len(spans))
        self.assertTrue(spans[0][2].strip().endswith('this one.'))

    def test_single_sentence_block_yields_one_span(self):
        text = 'MDP is a means for multiple minds to align on a common underlying model.'
        spans = mdblocks.segment_sentences(text)
        self.assertEqual(1, len(spans))
        self.assertEqual(text, spans[0][2])


class BlockSourceSpanTests(unittest.TestCase):
    """mdblocks.block_source_span — the exact-location primitive merge-on-
    accept depends on, tested independent of any server/git plumbing."""

    def test_span_recovers_exact_block_text_single_line(self):
        src = '# Title\n\nFirst sentence here.\n\nSecond sentence here.\n'
        _title, blocks = mdblocks.parse_markdown(src)
        target = next(b for b in blocks if b['text'] == 'Second sentence here.')
        normalized, start, end, exact = mdblocks.block_source_span(src, target)
        self.assertEqual('Second sentence here.', exact)
        self.assertEqual(normalized[start:end], exact)

    def test_span_survives_leading_front_matter_and_marker(self):
        src = (
            '<!-- auto-lexicon -->\n---\nlevel: "object"\nview: "v3"\n---\n'
            '# Title\n\nOne.\n\nTwo.\n'
        )
        _title, blocks = mdblocks.parse_markdown(src)
        target = next(b for b in blocks if b['text'] == 'Two.')
        normalized, start, end, exact = mdblocks.block_source_span(src, target)
        self.assertEqual('Two.', exact)
        self.assertEqual(normalized[start:end], 'Two.')


class GeneratedTermsViewTests(unittest.TestCase):
    """item D: the ## Terms section renders as a generated view (v3 only),
    with use-counts and model-anchored (block_id, occurrence) back-links."""

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
        self.old_root = server.PROJECTS_ROOT
        self.old_config = server.WORKSPACES_CONFIG
        server.PROJECTS_ROOT = self.root
        server.WORKSPACES_CONFIG = self.config

    def tearDown(self):
        server.PROJECTS_ROOT = self.old_root
        server.WORKSPACES_CONFIG = self.old_config
        self.tmp.cleanup()

    def write_doc(self, text):
        with open(self.doc, 'w', encoding='utf-8') as handle:
            handle.write(text)

    def test_terms_section_generated_with_use_counts_in_v3(self):
        self.write_doc(
            '# Title\n\n'
            'A [claim graph](#terms) is the model. Another line about the '
            '[claim graph](#terms) too.\n\n'
            '## Terms\n\n'
            '- **claim graph** — the proposed underlying model.\n'
        )
        html = server.render_page('docs/page.md', view='v3')
        self.assertIn('v3-terms-generated', html)
        self.assertIn('v3-term-entry', html)
        self.assertIn('claim graph', html)
        # Used twice in the one prose block above (two occurrences in the
        # same block) — one term-entry, two back-links.
        self.assertIn('Used 2 times on this page', html)
        self.assertEqual(2, html.count('class="v3-term-use-link"'))

    def test_terms_section_unchanged_on_classic_view(self):
        self.write_doc(
            '# Title\n\nA claim graph is the model.\n\n'
            '## Terms\n\n- **claim graph** — the proposed underlying model.\n'
        )
        html = server.render_page('docs/page.md', view='classic')
        self.assertNotIn('class="v3-terms-generated"', html)

    def test_hand_written_terms_never_removed_from_file(self):
        original = (
            '# Title\n\nA claim graph is the model.\n\n'
            '## Terms\n\n- **claim graph** — the proposed underlying model.\n'
        )
        self.write_doc(original)
        server.render_page('docs/page.md', view='v3')
        with open(self.doc, encoding='utf-8') as f:
            self.assertEqual(original, f.read())

    def test_no_terms_heading_is_a_no_op(self):
        self.write_doc('# Title\n\nJust a plain page with no terms section.\n')
        html = server.render_page('docs/page.md', view='v3')
        self.assertNotIn('class="v3-terms-generated"', html)


if __name__ == '__main__':
    unittest.main()
