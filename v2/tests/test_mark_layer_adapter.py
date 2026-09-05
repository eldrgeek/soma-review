"""to_mark_layer_nodes — the Python-side emitter of the shared mark-layer
node/fragment model (SOMA agreed model item 6a; see
`v2/mark_layer_adapter.py` module docstring and
`playmaker/docs/MARK-LAYER-ENGINE.md` "Next" item 1).
"""
import os
import sys
import unittest

V2_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, V2_DIR)

from mark_layer_adapter import to_mark_layer_nodes  # noqa: E402
from mdblocks import norm  # noqa: E402


class MarkLayerAdapterTests(unittest.TestCase):
    def test_single_paragraph_emits_whole_and_sentence_nodes(self):
        text = 'Alpha is first. Beta is second. Gamma is third.'
        nodes = to_mark_layer_nodes(text)
        self.assertEqual(nodes[0]['kind'], 'paragraph')
        self.assertEqual(nodes[0]['fragments'][0]['text'], text)
        sentence_nodes = [n for n in nodes if n['kind'] == 'sentence']
        self.assertEqual(
            [n['fragments'][0]['text'] for n in sentence_nodes],
            ['Alpha is first.', ' Beta is second.', ' Gamma is third.'],
        )

    def test_sentence_offsets_are_code_point_offsets_into_norm(self):
        text = 'Alpha is first. Beta is second.'
        nodes = to_mark_layer_nodes(text)
        sentence_nodes = [n for n in nodes if n['kind'] == 'sentence']
        normalized = norm(text)
        for node in sentence_nodes:
            offset = node['attrs']['offset']
            frag_text = node['fragments'][0]['text']
            self.assertEqual(normalized[offset:offset + len(frag_text)], frag_text)

    def test_sentence_fragments_exactly_tile_the_normalized_paragraph(self):
        text = '  Alpha  is\nfirst.   Beta is second.  '
        nodes = to_mark_layer_nodes(text)
        sentence_nodes = [n for n in nodes if n['kind'] == 'sentence']
        joined = ''.join(n['fragments'][0]['text'] for n in sentence_nodes)
        self.assertEqual(joined, norm(text))

    def test_abbreviation_does_not_split(self):
        text = 'Dr. Smith arrived. He left.'
        nodes = to_mark_layer_nodes(text)
        sentence_nodes = [n for n in nodes if n['kind'] == 'sentence']
        self.assertEqual(
            [n['fragments'][0]['text'] for n in sentence_nodes],
            ['Dr. Smith arrived.', ' He left.'],
        )

    def test_heading_kept_whole_no_sentence_split(self):
        nodes = to_mark_layer_nodes('## A Heading. With A Period.')
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]['kind'], 'paragraph')
        self.assertEqual(nodes[0]['fragments'][0]['text'], '## A Heading. With A Period.')

    def test_blank_separators_preserved_as_blank_nodes(self):
        text = 'First paragraph.\n\n\nSecond paragraph.'
        nodes = to_mark_layer_nodes(text)
        kinds = [n['kind'] for n in nodes]
        self.assertIn('blank', kinds)
        blank = next(n for n in nodes if n['kind'] == 'blank')
        self.assertEqual(blank['fragments'][0]['text'], '\n\n\n')

    def test_empty_string_emits_no_nodes(self):
        self.assertEqual(to_mark_layer_nodes(''), [])

    def test_paragraph_and_sentence_children_use_different_coordinate_spaces(self):
        # Known, named gap (Skip's adversarial pass, 2026-09-05): a
        # paragraph node's own fragment text is the RAW block; its sentence
        # children tile `norm(block)`. For a soft-wrapped paragraph these
        # are different strings — pinned here so the divergence is exercised
        # knowingly rather than silently.
        text = 'Alpha is\nfirst. Beta is second.'
        nodes = to_mark_layer_nodes(text)
        paragraph = next(n for n in nodes if n['kind'] == 'paragraph')
        sentence_nodes = [n for n in nodes if n['kind'] == 'sentence']
        joined_sentences = ''.join(n['fragments'][0]['text'] for n in sentence_nodes)
        self.assertEqual(paragraph['fragments'][0]['text'], text)
        self.assertEqual(joined_sentences, norm(text))
        self.assertNotEqual(paragraph['fragments'][0]['text'], joined_sentences)

    def test_repeated_calls_on_identical_text_do_not_reuse_ids(self):
        # Known, named gap (Skip's adversarial pass, 2026-09-05): ids are
        # NOT stable across calls (module-global counter, parity with the JS
        # adapter's own `let counter = 0`). Any future live-route wiring
        # must replace this with a content-derived id before a client does
        # same-input-same-id diffing against it. Pinned here so a future
        # change to "make ids stable" is a deliberate, visible diff against
        # this test, not an accidental behavior change nobody notices.
        text = 'Alpha is first. Beta is second.'
        first_ids = [n['id'] for n in to_mark_layer_nodes(text)]
        second_ids = [n['id'] for n in to_mark_layer_nodes(text)]
        self.assertNotEqual(first_ids, second_ids)

    def test_node_ids_are_unique_across_a_call(self):
        text = 'One. Two.\n\nThree. Four.'
        nodes = to_mark_layer_nodes(text)
        ids = [n['id'] for n in nodes]
        self.assertEqual(len(ids), len(set(ids)))
        for node in nodes:
            frag_ids = [f['id'] for f in node['fragments']]
            self.assertEqual(len(frag_ids), len(set(frag_ids)))


if __name__ == '__main__':
    unittest.main()
