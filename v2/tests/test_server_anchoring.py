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


class ServerAnchoringTests(unittest.TestCase):
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

    def test_render_mints_ids_and_detaches_deleted_quote(self):
        self.write_doc('# Title\n\nAlpha beta gamma.\n')
        html = server.render_page('docs/page.md')
        self.assertIn('data-block-id="blk_', html)
        mapping = blockmap.load_map(server.block_map_path('docs/page.md'))
        target = mapping['blocks'][1]
        binding = server.validated_binding('docs/page.md', 'estate', {
            'block_id': target['id'], 'from': 6, 'to': 10, 'quote': 'beta',
        })
        row = {
            'id': 'm1', 'page': 'docs/page.md', 'type': 'comment',
            'anchor': None, 'snapshot': 'Alpha beta gamma.', 'author': 'mike',
            'text': 'Keep this word', 'timestamp': '2026-09-01T00:00:00Z',
            'status': 'queued', 'thread_id': 'm1', 'deleted': False, **binding,
        }
        server.append_comment('docs/page.md', row)
        self.write_doc('# Title\n\nAlpha gamma.\n')
        server.render_page('docs/page.md')
        migrated = server.read_comments('docs/page.md')[0]
        self.assertTrue(migrated['unresolved'])
        self.assertEqual('beta', migrated['quote'])

    def test_post_binding_repairs_stale_id_by_exact_quote(self):
        self.write_doc('# Title\n\nA unique exact quote long enough to identify this block.\n')
        server.render_page('docs/page.md')
        fields = server.validated_binding('docs/page.md', 'estate', {
            'block_id': 'blk_stale', 'from': 900, 'to': 901,
            'quote': 'unique exact quote', 'heading_path': ['Title'],
        })
        self.assertTrue(fields['reanchored'])
        self.assertEqual('unique exact quote', fields['quote'])
        with self.assertRaises(server.BindingConflict):
            server.validated_binding('docs/page.md', 'estate', {
                'block_id': 'blk_stale', 'from': 0, 'to': 4,
                'quote': 'does not exist', 'heading_path': ['Title'],
            })

    def test_torn_append_does_not_consume_following_row(self):
        self.write_doc('# Title\n\nText.\n')
        path = server.sidecar_path('docs/page.md')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as handle:
            handle.write(b'{"id":"first","page":"docs/page.md"}\n{"id":"torn"')
        server.append_comment('docs/page.md', {'id': 'second', 'page': 'docs/page.md'})
        rows = server.read_comments('docs/page.md')
        self.assertEqual(['first', 'second'], [row['id'] for row in rows])

    def test_mark_layer_renders_sentence_ranges_in_block_coordinates(self):
        self.write_doc(
            '# Review title\n\n'
            'Dr. Wolf kept **the first claim** together. Second claim uses `code`.\n'
        )
        html = server.render_page('docs/page.md')
        self.assertIn('class="mark-layer"', html)
        self.assertIn('class="mark-sentence" data-from="0"', html)
        self.assertIn('data-quote="', html)
        self.assertIn('window.__MARK_LAYER__ = true', html)
        self.assertIn('Ask for a synthesis pass', html)

        ranges = server.sentence_ranges(
            'Dr. Wolf kept **the first claim** together. Second claim uses `code`.'
        )
        self.assertEqual(2, len(ranges))
        self.assertTrue(ranges[0][2].startswith('Dr. Wolf'))
        self.assertEqual('Second claim uses `code`.', ranges[1][2])

    def test_mark_layer_offsets_are_unicode_codepoints(self):
        text = 'A 😀 claim survives. Another sentence.'
        ranges = server.sentence_ranges(text)
        self.assertEqual('A 😀 claim survives.', text[ranges[0][0]:ranges[0][1]])
        self.assertEqual('Another sentence.', text[ranges[1][0]:ranges[1][1]])

    def test_underscore_emphasis_does_not_break_filename_underscores(self):
        rendered = server.render_inline('_Generated by v2/generate_board.py._')
        self.assertEqual('<em>Generated by v2/generate_board.py.</em>', rendered)

    def test_mark_layer_list_items_keep_exact_block_ranges(self):
        text = '- First **claim**.\n  - Nested second claim.\n- Final claim.'
        ranges = server.list_item_ranges(text)
        normalized = blockmap.norm(text)
        self.assertEqual(3, len(ranges))
        self.assertEqual(
            ['First **claim**.', 'Nested second claim.', 'Final claim.'],
            [normalized[start:end] for start, end, _quote in ranges],
        )
        self.write_doc(f'# Review title\n\n{text}\n')
        html = server.render_page('docs/page.md')
        self.assertIn('data-list-units="', html)

    def test_list_item_ranges_disambiguates_duplicate_item_text(self):
        # Line-1 next item (20260905T045607Z-mission-1): does the monotonic
        # `cursor` in list_item_ranges() resolve a later mark to the wrong
        # item when two items share identical text? It does not: cursor only
        # ever advances, so each duplicate is matched at the first occurrence
        # AT OR AFTER the previous item's end, never re-matching an earlier,
        # already-claimed position.
        text = '- Buy milk\n- Buy milk\n- Something else\n- Buy milk'
        normalized = blockmap.norm(text)
        ranges = server.list_item_ranges(text)
        self.assertEqual(4, len(ranges))
        quotes = [normalized[start:end] for start, end, _quote in ranges]
        self.assertEqual(['Buy milk', 'Buy milk', 'Something else', 'Buy milk'], quotes)
        starts = [start for start, _end, _quote in ranges]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(len(starts), len(set(starts)), 'each duplicate must get its own offset')

    def test_stale_offsets_on_ambiguous_duplicate_quote_refuse_not_misbind(self):
        # Complement to the ranges test above: once a mark's own (from, to)
        # offsets no longer match the live text (block edited elsewhere,
        # `_resolve_on_block` falls through to `_all_occurrences`), an
        # ambiguous quote — shared by two list items — must refuse (409 /
        # BindingConflict) rather than silently rebind to whichever
        # occurrence happens to be found. Safety-by-refusal, not by luck.
        self.write_doc('# Title\n\n- Buy milk\n- Buy milk\n- Buy eggs\n')
        server.render_page('docs/page.md')
        mapping = blockmap.load_map(server.block_map_path('docs/page.md'))
        list_block = next(row for row in mapping['blocks'] if 'Buy milk' in row.get('text', ''))
        with self.assertRaises(server.BindingConflict):
            server.validated_binding('docs/page.md', 'estate', {
                'block_id': list_block['id'],
                'from': 9999, 'to': 9999 + len('Buy milk'),  # stale, forces quote fallback
                'quote': 'Buy milk',
            })

    def test_list_item_ranges_folds_soft_wrapped_continuation_lines(self):
        # Line-1 next item (20260905T052632Z-mission-1): a soft-wrapped list
        # item's continuation line (no marker of its own) was invisible to
        # list_item_ranges() — only the item's first physical splitlines()
        # line was ranged, so the bound quote under-selected the item's full
        # rendered text. Fixed by folding continuation lines into the same
        # item before matching, indent-independent: the first version of
        # this fix required the continuation to be MORE indented than the
        # marker, but an adversarial pass + a grep of `_estate/*.md` found
        # the estate's real wraps are flush-left (zero extra indent), which
        # that version silently missed — the exact case this test now pins.
        text = (
            '- First item wraps\n'
            'with no continuation indent at all.\n'
            '- Second item, single line.\n'
            '1. Ordered item also wraps\n'
            'flush-left too.\n'
        )
        normalized = blockmap.norm(text)
        ranges = server.list_item_ranges(text)
        quotes = [normalized[start:end] for start, end, _quote in ranges]
        self.assertEqual(
            [
                'First item wraps with no continuation indent at all.',
                'Second item, single line.',
                'Ordered item also wraps flush-left too.',
            ],
            quotes,
        )
        starts = [start for start, _end, _quote in ranges]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(len(starts), len(set(starts)))

    def test_list_item_ranges_nested_item_is_not_swallowed_as_continuation(self):
        # A nested list item (its own marker, deeper indent) must still be
        # its own range, not folded into the parent as a continuation line —
        # the marker check has to run before any indent reasoning.
        text = '- First **claim**.\n  - Nested second claim.\n- Final claim.'
        ranges = server.list_item_ranges(text)
        normalized = blockmap.norm(text)
        self.assertEqual(
            ['First **claim**.', 'Nested second claim.', 'Final claim.'],
            [normalized[start:end] for start, end, _quote in ranges],
        )

    def test_list_item_ranges_marker_like_continuation_becomes_its_own_item(self):
        # Named limitation (Skip's adversarial pass, 2026-09-05): a
        # continuation line that itself looks like a marker is read as a new
        # item, not folded — e.g. an author soft-wraps "see item / 2. below
        # for detail." as one sentence. Not a misbinding (both resulting
        # ranges still resolve to real, correct text); pinning the actual
        # behavior so a future change to this can see what it's changing.
        text = '1. See the note\n2. below for detail.\n'
        normalized = blockmap.norm(text)
        ranges = server.list_item_ranges(text)
        self.assertEqual(
            ['See the note', 'below for detail.'],
            [normalized[start:end] for start, end, _quote in ranges],
        )

    def test_post_mark_persists_review_metadata(self):
        self.write_doc('# Review title\n\nA focused sentence for review.\n')
        server.render_page('docs/page.md')
        mapping = blockmap.load_map(server.block_map_path('docs/page.md'))
        target = mapping['blocks'][1]
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            payload = json.dumps({
                'page': 'docs/page.md', 'type': 'mark', 'mark_kind': 'rewrite',
                'block_id': target['id'], 'from': 2, 'to': 9,
                'quote': 'focused', 'snapshot': 'focused',
                'text': 'Tighten this.', 'proposed': 'precise',
                'reason': 'Less vague', 'strength': 2,
                'sent_because': 'test',
            }).encode('utf-8')
            request = urllib.request.Request(
                f'http://127.0.0.1:{httpd.server_port}/api/comments',
                data=payload, headers={'Content-Type': 'application/json'},
                method='POST',
            )
            with urllib.request.urlopen(request) as response:
                row = json.load(response)
                status = response.status
            self.assertEqual(201, status)
            self.assertEqual('mark', row['type'])
            self.assertEqual('rewrite', row['mark_kind'])
            self.assertEqual(2.0, row['strength'])
            self.assertEqual('precise', row['proposed'])
            self.assertEqual('Less vague', row['reason'])
            self.assertEqual('test', row['sent_because'])
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
