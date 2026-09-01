import json
import os
import sys
import tempfile
import unittest

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


if __name__ == '__main__':
    unittest.main()
