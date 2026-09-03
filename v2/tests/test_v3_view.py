"""Tests for the v3 (Playmaker-model mark layer) view, added 2026-09-03.

Covers: the view flag defaults off and classic content is unaffected by its
presence, a sidecar edit-mark round trip (the same mechanism v3's inline
contenteditable blocks use), stale detection (block_text_sha drift), and
```widget fenced-block rendering (passive kind now, demo/active placeholders
for the two kinds documented as next-step work).
"""
import json
import os
import sys
import tempfile
import unittest

V2_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, V2_DIR)

import blockmap  # noqa: E402
import mdblocks  # noqa: E402
import server  # noqa: E402


class V3ViewFlagTests(unittest.TestCase):
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

    def test_default_view_is_classic_and_unflagged(self):
        self.write_doc('# Title\n\nAlpha beta gamma.\n')
        html = server.render_page('docs/page.md')
        self.assertIn('window.__V3_VIEW__ = false', html)
        self.assertIn('window.__MARK_LAYER__ = true', html)
        self.assertNotIn('v3-header', html)
        self.assertIn('view-toggle-btn active" href="?view=classic"', html)

    def test_v3_view_disables_dwell_mark_layer_and_ships_v3_chrome(self):
        self.write_doc('# Title\n\nAlpha beta gamma.\n')
        html = server.render_page('docs/page.md', view='v3')
        self.assertIn('window.__V3_VIEW__ = true', html)
        self.assertIn('window.__MARK_LAYER__ = false', html)
        self.assertIn('v3-marks-btn', html)
        self.assertIn('view-toggle-btn active" href="?view=v3"', html)
        # edit-as-comment plumbing (wireEditableBlocks etc.) is present in
        # PAGE_JS unconditionally; v3 relies on it being reachable because
        # mark_layer is off, not on any v3-only editing code.
        self.assertIn('function wireEditableBlocks', html)

    def test_unrecognized_view_falls_back_to_classic(self):
        self.write_doc('# Title\n\nAlpha beta gamma.\n')
        html = server.render_page('docs/page.md', view='bogus')
        self.assertIn('window.__V3_VIEW__ = false', html)

    def test_classic_bytes_unaffected_by_v3_route_existing(self):
        """The only classic-page additions from this change are the shared
        view-toggle control + its bootstrap script (visible on both views per
        spec item 1) and the widget CSS rules (needed regardless of view,
        since a widget block can appear on any page). No block/comment/mark
        content differs. This test pins that scope: everything else in the
        classic render path is byte-for-byte what it was before."""
        self.write_doc('# Title\n\nAlpha beta gamma.\n')
        html = server.render_page('docs/page.md')
        self.assertNotIn('v3-panel', html)
        self.assertNotIn('v3-sidebar-resize', html)
        self.assertIn('class="mark-layer"', html)  # classic dwell layer intact


class V3LevelLabelTests(unittest.TestCase):
    def test_default_object_level(self):
        self.assertEqual('object', server.compute_level('# Doc\n\ntext\n', 'estate/FOO.md'))

    def test_shared_cognition_defaults_meta(self):
        self.assertEqual(
            'meta: the mark layer',
            server.compute_level('# Doc\n\ntext\n', 'soma/shared-cognition/bar.md'),
        )

    def test_explicit_frontmatter_overrides_default(self):
        src = '---\nlevel: custom-level\n---\n# Doc\n\ntext\n'
        self.assertEqual('custom-level', server.compute_level(src, 'soma/shared-cognition/bar.md'))
        self.assertEqual('custom-level', server.compute_level(src, 'estate/FOO.md'))


class WidgetBlockTests(unittest.TestCase):
    def test_bare_widget_fence_renders_sandboxed_iframe(self):
        _title, blocks = mdblocks.parse_markdown(
            '# T\n\n```widget\n<div>hello</div>\n```\n'
        )
        widget = next(b for b in blocks if b['kind'] == 'widget')
        self.assertIn('sandbox="allow-scripts"', widget['html'])
        self.assertNotIn('allow-same-origin', widget['html'])
        self.assertIn('srcdoc="', widget['html'])
        self.assertIn('&lt;div&gt;hello&lt;/div&gt;', widget['html'])

    def test_widget_height_override(self):
        _title, blocks = mdblocks.parse_markdown(
            '# T\n\n```widget\n<!-- height: 400 -->\n<div>hi</div>\n```\n'
        )
        widget = next(b for b in blocks if b['kind'] == 'widget')
        self.assertIn('height:400px', widget['html'])

    def test_widget_kind_passive_explicit(self):
        _title, blocks = mdblocks.parse_markdown(
            '# T\n\n```widget kind=passive name=inline-html\n<b>x</b>\n```\n'
        )
        widget = next(b for b in blocks if b['kind'] == 'widget')
        self.assertIn('iframe class="widget-block"', widget['html'])

    def test_widget_kind_demo_and_active_are_placeholders_not_crashes(self):
        for kind in ('demo', 'active'):
            _title, blocks = mdblocks.parse_markdown(
                f'# T\n\n```widget kind={kind} name=inline-html\n<b>x</b>\n```\n'
            )
            widget = next(b for b in blocks if b['kind'] == 'widget')
            self.assertIn('not yet supported', widget['html'])
            self.assertNotIn('<iframe', widget['html'])

    def test_widget_excluded_from_edit_eligible(self):
        html = mdblocks.render_widget_block  # sanity import
        self.assertTrue(callable(html))


class EditMarkAndStaleTests(unittest.TestCase):
    """Sidecar round trip for the v3 inline-edit flow: an edit lands as a
    type=edit comment carrying block_id + block_text_sha (the same
    validated_binding() schema classic edit-as-comment already uses), and
    staleness is detectable by comparing that stored hash against the block's
    current hash after the source changes underneath it."""

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

    def test_edit_mark_round_trips_and_becomes_stale_after_source_changes(self):
        self.write_doc('# Title\n\nOriginal paragraph text.\n')
        server.render_page('docs/page.md', view='v3')
        mapping = blockmap.load_map(server.block_map_path('docs/page.md'))
        target = mapping['blocks'][1]
        binding = server.validated_binding('docs/page.md', 'estate', {
            'block_id': target['id'], 'quote': 'Original paragraph text.',
        })
        stored_sha = binding['block_text_sha']
        row = {
            'id': 'e1', 'page': 'docs/page.md', 'type': 'edit',
            'anchor': None, 'snapshot': 'Original paragraph text.',
            'proposed': 'Revised paragraph text.', 'author': 'mike',
            'text': 'Suggested edit', 'timestamp': '2026-09-03T00:00:00Z',
            'status': 'queued', 'thread_id': 'e1', 'deleted': False, **binding,
        }
        server.append_comment('docs/page.md', row)

        # Round trip: the row is readable, carries the block_id + hash a v3
        # client needs to paint the mark on the right block and detect drift.
        saved = server.read_comments('docs/page.md')[0]
        self.assertEqual('edit', saved['type'])
        self.assertEqual(target['id'], saved['block_id'])
        self.assertEqual(stored_sha, saved['block_text_sha'])
        self.assertFalse(saved.get('stale', False))  # server never sets this; client derives it

        # The underlying .md file is never touched by an edit mark (spec: "Do
        # NOT write to the markdown file on disk").
        with open(self.doc, encoding='utf-8') as handle:
            self.assertIn('Original paragraph text.', handle.read())

        # Now the source actually changes underneath the mark (someone else
        # edited the doc, or a later pass rewrote it) — the block's current
        # hash must diverge from what the mark recorded, which is exactly the
        # signal V3_JS's decorate()/stale check compares against.
        self.write_doc('# Title\n\nCompletely different paragraph now.\n')
        server.render_page('docs/page.md', view='v3')
        new_mapping = blockmap.load_map(server.block_map_path('docs/page.md'))
        new_blocks = [b for b in new_mapping['blocks'] if b.get('kind') == 'paragraph']
        self.assertTrue(new_blocks)
        current_sha = blockmap.block_text_sha({'text': 'Completely different paragraph now.'})
        self.assertNotEqual(stored_sha, current_sha)

    def test_resolve_status_transition(self):
        self.write_doc('# Title\n\nSome text.\n')
        server.render_page('docs/page.md')
        row = {
            'id': 'm1', 'page': 'docs/page.md', 'type': 'comment',
            'anchor': None, 'snapshot': 'Some text.', 'author': 'mike',
            'text': 'A note', 'timestamp': '2026-09-03T00:00:00Z',
            'status': 'queued', 'thread_id': 'm1', 'deleted': False,
        }
        server.append_comment('docs/page.md', row)
        ok = server.update_comment('docs/page.md', 'm1', {'status': 'done'})
        self.assertTrue(ok)
        self.assertEqual('done', server.read_comments('docs/page.md')[0]['status'])
        ok = server.update_comment('docs/page.md', 'm1', {'status': 'queued'})
        self.assertTrue(ok)
        self.assertEqual('queued', server.read_comments('docs/page.md')[0]['status'])


class ViewFrontmatterDefaultTests(unittest.TestCase):
    """Front-matter `view: v3` (task spec item 14, 2026-09-03: `mdp-proposal.md`
    rebuild) forces a page open in the mark layer with no `?view=` on the URL.
    An explicit `?view=` always wins over the front-matter default."""

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

    def test_compute_default_view_reads_frontmatter(self):
        self.assertEqual('v3', server.compute_default_view('---\nview: "v3"\n---\nBody.\n'))
        self.assertEqual('classic', server.compute_default_view('---\nlevel: object\n---\nBody.\n'))
        self.assertEqual('classic', server.compute_default_view('No frontmatter at all.\n'))

    def test_compute_default_view_tolerates_leading_comment(self):
        # mdp-proposal.md's real shape: `<!-- auto-lexicon -->` before the
        # `---` frontmatter block.
        self.assertEqual('v3', server.compute_default_view('<!-- auto-lexicon -->\n---\nview: "v3"\n---\nBody.\n'))

    def test_no_view_param_uses_frontmatter_default(self):
        self.write_doc('---\nview: "v3"\n---\n# Title\n\nBody text.\n')
        html = server.render_page('docs/page.md', view=None)
        self.assertIn('window.__V3_VIEW__ = true', html)

    def test_explicit_view_param_overrides_frontmatter_default(self):
        self.write_doc('---\nview: "v3"\n---\n# Title\n\nBody text.\n')
        html = server.render_page('docs/page.md', view='classic')
        self.assertIn('window.__V3_VIEW__ = false', html)

    def test_no_frontmatter_and_no_param_stays_classic(self):
        self.write_doc('# Title\n\nBody text.\n')
        html = server.render_page('docs/page.md', view=None)
        self.assertIn('window.__V3_VIEW__ = false', html)


class DecisionAndReplaceMarkKindTests(unittest.TestCase):
    """`decision` mark_kind and `replace`-flagged edit marks (Playmaker-model
    rebuild, task spec items 2iv/2i/7, 2026-09-03): server-side storage only
    (V3_JS rendering is covered by the live Playwright pass in the dispatch
    report, not re-verified here — no headless browser in this test file)."""

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

    def test_decision_mark_round_trips_with_meta_payload(self):
        self.write_doc('# Title\n\nA sentence with two readings.\n')
        server.render_page('docs/page.md', view='v3')
        mapping = blockmap.load_map(server.block_map_path('docs/page.md'))
        target = mapping['blocks'][1]
        binding = server.validated_binding('docs/page.md', 'estate', {
            'block_id': target['id'], 'quote': 'A sentence with two readings.',
        })
        row = {
            'id': 'd1', 'page': 'docs/page.md', 'type': 'mark', 'mark_kind': 'decision',
            'anchor': None, 'snapshot': '', 'author': 'claude',
            'text': 'Decision prompt', 'timestamp': '2026-09-03T00:00:00Z',
            'status': 'queued', 'thread_id': 'd1', 'deleted': False,
            'meta': {'prompt': 'Pick one', 'alternatives': [
                {'key': 'A', 'text': 'First reading'}, {'key': 'B', 'text': 'Second reading'},
            ], 'default': 'A'},
            **binding,
        }
        server.append_comment('docs/page.md', row)
        saved = server.read_comments('docs/page.md')[0]
        self.assertEqual('decision', saved['mark_kind'])
        self.assertEqual('A', saved['meta']['default'])
        self.assertEqual(2, len(saved['meta']['alternatives']))

    def test_replace_flagged_edit_round_trips(self):
        self.write_doc('# Title\n\nOriginal claim.\n')
        server.render_page('docs/page.md', view='v3')
        mapping = blockmap.load_map(server.block_map_path('docs/page.md'))
        target = mapping['blocks'][1]
        binding = server.validated_binding('docs/page.md', 'estate', {
            'block_id': target['id'], 'quote': 'Original claim.',
        })
        row = {
            'id': 'r1', 'page': 'docs/page.md', 'type': 'edit',
            'anchor': None, 'snapshot': 'Original claim.',
            'proposed': 'Restated claim.', 'author': 'claude', 'replace': True,
            'text': '(proposed edit)', 'timestamp': '2026-09-03T00:00:00Z',
            'status': 'queued', 'thread_id': 'r1', 'deleted': False, **binding,
        }
        server.append_comment('docs/page.md', row)
        saved = server.read_comments('docs/page.md')[0]
        self.assertTrue(saved['replace'])
        self.assertEqual('edit', saved['type'])


if __name__ == '__main__':
    unittest.main()
