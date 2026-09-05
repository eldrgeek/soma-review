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

    def test_repeated_calls_on_identical_text_reuse_ids(self):
        # Fixed 2026-09-05 (mission-1): ids are now content-derived
        # (`_content_id`, sha1 of `(prefix, text)`), a deliberate, visible
        # change from the prior module-global-counter behavior this test
        # used to pin (see git history for the old
        # `test_repeated_calls_on_identical_text_do_not_reuse_ids`). Required
        # before any client can do same-input-same-id diffing against this
        # adapter's output. The JS adapter (`fromProseMarkdown`) was NOT
        # given the matching fix in this pass — see the module docstring.
        text = 'Alpha is first. Beta is second.'
        first_ids = [n['id'] for n in to_mark_layer_nodes(text)]
        second_ids = [n['id'] for n in to_mark_layer_nodes(text)]
        self.assertEqual(first_ids, second_ids)

    def test_node_ids_are_unique_across_a_call(self):
        text = 'One. Two.\n\nThree. Four.'
        nodes = to_mark_layer_nodes(text)
        ids = [n['id'] for n in nodes]
        self.assertEqual(len(ids), len(set(ids)))
        for node in nodes:
            frag_ids = [f['id'] for f in node['fragments']]
            self.assertEqual(len(frag_ids), len(set(frag_ids)))

    def test_duplicate_text_within_one_call_gets_distinct_ids(self):
        # A real case, not a contrived one: two paragraphs with literally
        # identical text (e.g. a repeated stage direction or refrain) must
        # not collide on the same content-derived id within one call, even
        # though the whole point of the id scheme is that identical text
        # hashes to the same base id. Two separate one-sentence paragraphs,
        # not one two-sentence paragraph, so the sentence text is identical
        # too (a non-first sentence in a paragraph carries a leading space
        # from how `splitSentences`/`segment_sentences` attach the
        # boundary's trailing whitespace, so 'Ready. Ready.' as ONE
        # paragraph would NOT produce identical sentence text).
        text = 'Ready.\n\nReady.'
        nodes = to_mark_layer_nodes(text)
        paragraph_ids = [n['id'] for n in nodes if n['kind'] == 'paragraph']
        sentence_ids = [n['id'] for n in nodes if n['kind'] == 'sentence']
        self.assertEqual(len(paragraph_ids), len(set(paragraph_ids)))
        self.assertEqual(len(sentence_ids), len(set(sentence_ids)))
        # And the base (pre-disambiguation) hash is shared, proving these
        # are the SAME content colliding, not accidentally-different text.
        # Id shape is `{prefix}-{hash}` or `{prefix}-{hash}-{occurrence}`;
        # the prefix and hash never themselves contain a `-`.
        base_ids = {'-'.join(sid.split('-')[:2]) for sid in paragraph_ids}
        self.assertEqual(len(base_ids), 1)

    def test_duplicate_text_ids_are_stable_and_ordered_across_calls(self):
        text = 'Ready.\n\nReady.'
        first = [n['id'] for n in to_mark_layer_nodes(text) if n['kind'] == 'paragraph']
        second = [n['id'] for n in to_mark_layer_nodes(text) if n['kind'] == 'paragraph']
        self.assertEqual(first, second)

    def test_named_gap_inserting_an_earlier_duplicate_reassigns_later_ids(self):
        # Named, not fixed (Skip's adversarial pass, 2026-09-05): the
        # occurrence-index disambiguation is positional AMONG a text's own
        # duplicates, so inserting a new occurrence earlier in the document
        # reassigns the suffix of every later occurrence of that same text —
        # even though neither the later node's own text nor its position
        # relative to the surrounding document changed. This is a real,
        # named limitation (see the module docstring), pinned here so a
        # future attempt to fix it has a red test to turn green, and so a
        # regression that makes it WORSE (e.g. reordering unrelated ids too)
        # is caught.
        before = 'Ready.\n\nOther.\n\nReady.'
        after = 'Ready.\n\nReady.\n\nOther.\n\nReady.'
        before_ids = [n['id'] for n in to_mark_layer_nodes(before) if n['kind'] == 'paragraph']
        after_ids = [n['id'] for n in to_mark_layer_nodes(after) if n['kind'] == 'paragraph']
        # `before`: [Ready(occ0), Other, Ready(occ1)] — the trailing "Ready."
        # is the SAME unchanged text, same position relative to "Other.", as
        # the trailing "Ready." in `after`: [Ready(occ0), Ready(occ1), Other,
        # Ready(occ2)]. Yet the id it gets SHIFTS (occ1 -> occ2), because a
        # new earlier occurrence of "Ready." was inserted before it. This
        # pins the current (limited) behavior; if a future fix makes ids
        # stable under this kind of edit too, this assertion should flip to
        # assertEqual and the module docstring's gap note should be removed.
        self.assertNotEqual(before_ids[2], after_ids[3])



if __name__ == '__main__':
    unittest.main()
