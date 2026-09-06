"""Live fromProseMarkdown port is the sole stamp path; twin is debug-only.

6a: emit_live_mark_layer_nodes uses from_prose_markdown (Playmaker
shared-model port). to_mark_layer_nodes is a debug alias and is not
called on the live render path when SOMA_REVIEW_MARK_LAYER_TWIN is off.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

V2_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, V2_DIR)

import server  # noqa: E402
from mark_layer_adapter import to_mark_layer_nodes  # noqa: E402
from mark_layer_engine import (  # noqa: E402
    emit_live_mark_layer_nodes, from_prose_markdown, from_prose_markdown_js,
    last_live_emitter_source, mark_layer_twin_enabled,
)

PINNED = 'Alpha is first. Beta is second.'


class FromProseMarkdownLiveTests(unittest.TestCase):
    def test_twin_flag_default_off(self):
        old = os.environ.pop('SOMA_REVIEW_MARK_LAYER_TWIN', None)
        try:
            self.assertFalse(mark_layer_twin_enabled())
        finally:
            if old is not None:
                os.environ['SOMA_REVIEW_MARK_LAYER_TWIN'] = old

    def test_emit_live_default_is_from_prose_markdown(self):
        old = os.environ.pop('SOMA_REVIEW_MARK_LAYER_TWIN', None)
        engine = os.environ.pop('SOMA_REVIEW_MARK_LAYER_ENGINE', None)
        try:
            live = emit_live_mark_layer_nodes(PINNED)
            port = from_prose_markdown(PINNED)
            self.assertEqual([n['id'] for n in port], [n['id'] for n in live])
            self.assertEqual('fromProseMarkdown', last_live_emitter_source())
        finally:
            if old is not None:
                os.environ['SOMA_REVIEW_MARK_LAYER_TWIN'] = old
            if engine is not None:
                os.environ['SOMA_REVIEW_MARK_LAYER_ENGINE'] = engine

    def test_twin_alias_matches_live_port(self):
        self.assertEqual(
            [n['id'] for n in from_prose_markdown(PINNED)],
            [n['id'] for n in to_mark_layer_nodes(PINNED)],
        )

    def test_pinned_alpha_beta_ids_are_stable(self):
        nodes = from_prose_markdown(PINNED)
        ids = [n['id'] for n in nodes]
        self.assertEqual(ids, [n['id'] for n in from_prose_markdown(PINNED)])
        kinds = [n['kind'] for n in nodes]
        self.assertEqual(kinds, ['paragraph', 'sentence', 'sentence'])
        self.assertTrue(all(nid.startswith(('pmpara-', 'pmsent-')) for nid in ids))
        sentences = [n['fragments'][0]['text'] for n in nodes if n['kind'] == 'sentence']
        self.assertEqual(sentences, ['Alpha is first.', ' Beta is second.'])

    def test_heading_has_no_sentence_node(self):
        nodes = from_prose_markdown('## A Heading. With A Period.')
        self.assertEqual([n['kind'] for n in nodes], ['paragraph'])

    @unittest.skipUnless(shutil.which('node'), 'node not available')
    def test_js_from_prose_markdown_matches_python_port(self):
        samples = [
            PINNED,
            'Ready.\n\nReady.',
            '# Review title\n\nAlpha is first. Beta is second.\n',
            'Dr. Smith said hi. He left.\n\nHe said "Stop." She left.\n',
            'This line wraps\nacross a soft break. The next sentence stays whole.\n',
        ]
        mismatches = []
        for text in samples:
            py_ids = [n['id'] for n in from_prose_markdown(text)]
            js_ids = [n['id'] for n in from_prose_markdown_js(text)]
            if py_ids != js_ids:
                mismatches.append({'input': text, 'python': py_ids, 'js': js_ids})
        self.assertEqual(mismatches, [], json.dumps(mismatches, indent=2))


class LiveRenderDoesNotCallTwinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, 'docs'))
        self.doc = os.path.join(self.root, 'docs', 'page.md')
        with open(self.doc, 'w', encoding='utf-8') as handle:
            handle.write('# Review title\n\nAlpha is first. Beta is second.\n')
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
        self.old_twin = os.environ.pop('SOMA_REVIEW_MARK_LAYER_TWIN', None)

    def tearDown(self):
        server.PROJECTS_ROOT = self.old_root
        server.WORKSPACES_CONFIG = self.old_config
        if self.old_twin is not None:
            os.environ['SOMA_REVIEW_MARK_LAYER_TWIN'] = self.old_twin
        self.tmp.cleanup()

    def test_render_stamps_without_calling_twin_name(self):
        def boom(*_args, **_kwargs):
            raise AssertionError('twin must not stamp live DOM')

        with patch('mark_layer_adapter.to_mark_layer_nodes', side_effect=boom):
            html = server.render_page('docs/page.md')
        self.assertIn('data-mark-layer-node-id=', html)
        self.assertIn('fromProseMarkdown', html)
        expected = next(
            n['id'] for n in from_prose_markdown(
                '# Review title\n\nAlpha is first. Beta is second.\n'
            )
            if n['kind'] == 'sentence'
            and n['fragments'][0]['text'].strip() == 'Alpha is first.'
        )
        self.assertIn(f'data-mark-layer-node-id="{expected}"', html)
        self.assertEqual('fromProseMarkdown', last_live_emitter_source())

    def test_twin_flag_routes_emit_live_through_twin_name(self):
        os.environ['SOMA_REVIEW_MARK_LAYER_TWIN'] = '1'
        called = {'n': 0}

        def wrapper(md):
            called['n'] += 1
            return from_prose_markdown(md)

        with patch('mark_layer_adapter.to_mark_layer_nodes', side_effect=wrapper):
            nodes = emit_live_mark_layer_nodes(PINNED)
        self.assertGreaterEqual(called['n'], 1)
        self.assertEqual('twin', last_live_emitter_source())
        self.assertEqual(
            [n['id'] for n in from_prose_markdown(PINNED)],
            [n['id'] for n in nodes],
        )


if __name__ == '__main__':
    unittest.main()
