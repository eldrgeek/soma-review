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

from mark_layer_adapter import (  # noqa: E402
    to_mark_layer_nodes, match_mark_layer_nodes, attach_mark_layer_node_ids,
    MarkLayerDomStamper, align_mark_layer_nodes, rebind_mark_layer_node_ids,
)
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


class AlignMarkLayerNodesTests(unittest.TestCase):
    """Edit-rebind: neighborhood align accounts occurrence-suffix remaps."""

    def test_unique_sentences_identity_map(self):
        text = 'Alpha is first. Beta is second.'
        nodes = to_mark_layer_nodes(text)
        remap = align_mark_layer_nodes(nodes, to_mark_layer_nodes(text))
        for node in nodes:
            self.assertEqual(node['id'], remap.get(node['id']), node['id'])

    def test_inserting_earlier_duplicate_remaps_later_occurrence(self):
        # Named minting gap (test_named_gap_...): suffixes shift. Align must
        # still pair each Ready. with the paragraph that still has its
        # unique sibling — not the newly inserted first occurrence.
        before = 'Ready. Unique first context.\n\nReady. Unique second context.'
        after = (
            'Ready. Brand new context.\n\n'
            'Ready. Unique first context.\n\n'
            'Ready. Unique second context.'
        )
        prev = to_mark_layer_nodes(before)
        nxt = to_mark_layer_nodes(after)
        prev_ready = [n['id'] for n in prev if n['kind'] == 'sentence'
                      and n['fragments'][0]['text'].strip() == 'Ready.']
        next_ready = [n['id'] for n in nxt if n['kind'] == 'sentence'
                      and n['fragments'][0]['text'].strip() == 'Ready.']
        self.assertEqual(2, len(prev_ready))
        self.assertEqual(3, len(next_ready))
        self.assertNotEqual(prev_ready[1], next_ready[2])

        remap = align_mark_layer_nodes(prev, nxt)
        self.assertEqual(next_ready[1], remap[prev_ready[0]])
        self.assertEqual(next_ready[2], remap[prev_ready[1]])
        first_ctx = next(
            n['id'] for n in prev if n['kind'] == 'sentence'
            and n['fragments'][0]['text'].strip() == 'Unique first context.'
        )
        first_ctx_after = next(
            n['id'] for n in nxt if n['kind'] == 'sentence'
            and n['fragments'][0]['text'].strip() == 'Unique first context.'
        )
        self.assertEqual(first_ctx_after, remap[first_ctx])

    def test_inserting_earlier_bare_duplicate_uses_group_neighbors(self):
        before = 'Ready.\n\nOther.\n\nReady.'
        after = 'Ready.\n\nReady.\n\nOther.\n\nReady.'
        prev = to_mark_layer_nodes(before)
        nxt = to_mark_layer_nodes(after)
        prev_ready = [n['id'] for n in prev if n['kind'] == 'paragraph'
                      and n['fragments'][0]['text'].strip() == 'Ready.']
        next_ready = [n['id'] for n in nxt if n['kind'] == 'paragraph'
                      and n['fragments'][0]['text'].strip() == 'Ready.']
        remap = align_mark_layer_nodes(prev, nxt)
        self.assertEqual(next_ready[1], remap[prev_ready[0]])
        self.assertEqual(next_ready[2], remap[prev_ready[1]])

    def test_rebind_updates_stored_ids_and_skips_identity(self):
        records = [
            {'id': 'm1', 'mark_layer_node_id': 'old-a',
             'mark_layer_node_ids': ['old-a', 'old-p']},
            {'id': 'm2', 'mark_layer_node_id': 'same'},
        ]
        updated, applied = rebind_mark_layer_node_ids(
            records, {'old-a': 'new-a', 'old-p': 'new-p', 'same': 'same'},
        )
        self.assertEqual('new-a', updated[0]['mark_layer_node_id'])
        self.assertEqual(['new-a', 'new-p'], updated[0]['mark_layer_node_ids'])
        self.assertEqual({'from': 'old-a', 'to': 'new-a'},
                         updated[0]['mark_layer_node_rebound'])
        self.assertEqual([{'record_id': 'm1', 'from': 'old-a', 'to': 'new-a'}], applied)
        self.assertEqual('same', updated[1]['mark_layer_node_id'])
        self.assertNotIn('mark_layer_node_rebound', updated[1])

    def test_rebind_only_ids_leaves_other_marks(self):
        records = [
            {'id': 'm1', 'mark_layer_node_id': 'old-a'},
            {'id': 'm2', 'mark_layer_node_id': 'old-b'},
        ]
        _updated, applied = rebind_mark_layer_node_ids(
            records, {'old-a': 'new-a', 'old-b': 'new-b'}, only_ids={'old-a'},
        )
        self.assertEqual([{'record_id': 'm1', 'from': 'old-a', 'to': 'new-a'}], applied)
        self.assertEqual('old-b', records[1]['mark_layer_node_id'])


class MatchMarkLayerNodesTests(unittest.TestCase):
    """6a beside: resolve a mark quote to the adapter's node id(s)."""

    def test_known_sentence_matches_adapter_sentence_id(self):
        text = 'Alpha is first. Beta is second. Gamma is third.'
        nodes = to_mark_layer_nodes(text)
        expected = next(
            n for n in nodes
            if n['kind'] == 'sentence' and n['fragments'][0]['text'].strip() == 'Beta is second.'
        )
        matched = match_mark_layer_nodes(nodes, quote='Beta is second.')
        self.assertTrue(matched)
        self.assertEqual(expected['id'], matched[0]['id'])
        self.assertEqual('sentence', matched[0]['kind'])

    def test_empty_nodes_or_quote_is_no_match(self):
        text = 'Alpha is first. Beta is second.'
        nodes = to_mark_layer_nodes(text)
        self.assertEqual([], match_mark_layer_nodes([], quote='Beta is second.'))
        self.assertEqual([], match_mark_layer_nodes(nodes, quote=''))
        self.assertEqual([], match_mark_layer_nodes(nodes, quote='no such sentence'))

    def test_attach_is_noop_when_there_are_no_nodes(self):
        record = {
            'type': 'mark', 'quote': 'Beta is second.',
            'snapshot': 'Beta is second.', 'anchor': None,
        }
        before = dict(record)
        attach_mark_layer_node_ids(record, '')
        self.assertEqual(before, record)
        self.assertNotIn('mark_layer_node_id', record)

    def test_attach_does_not_raise_on_bad_source(self):
        record = {'type': 'mark', 'quote': 'Beta is second.'}
        self.assertIs(record, attach_mark_layer_node_ids(record, None))
        self.assertNotIn('mark_layer_node_id', record)

    def test_repeated_sentence_snapshot_scopes_to_the_correct_occurrence(self):
        # Skip 2026-09-06 nit 1: duplicate sentences rely on snapshot
        # scoping. Two one-sentence-plus-context paragraphs share "Ready.";
        # the second block's snapshot must attach that occurrence's id, not
        # the first hit in document order.
        text = 'Ready. Unique first context.\n\nReady. Unique second context.'
        nodes = to_mark_layer_nodes(text)
        ready = [
            n for n in nodes
            if n['kind'] == 'sentence' and n['fragments'][0]['text'].strip() == 'Ready.'
        ]
        self.assertEqual(2, len(ready))
        self.assertNotEqual(ready[0]['id'], ready[1]['id'])
        second_para = [
            n for n in nodes
            if n['kind'] == 'paragraph'
            and n['fragments'][0]['text'].strip() == 'Ready. Unique second context.'
        ][0]
        matched = match_mark_layer_nodes(
            nodes, quote='Ready.', snapshot='Ready. Unique second context.',
        )
        self.assertTrue(matched)
        self.assertEqual(ready[1]['id'], matched[0]['id'])
        self.assertEqual(second_para['id'], matched[1]['id'])

        record = {
            'type': 'mark', 'quote': 'Ready.',
            'snapshot': 'Ready. Unique second context.',
        }
        attach_mark_layer_node_ids(record, text)
        self.assertEqual(ready[1]['id'], record['mark_layer_node_id'])
        self.assertEqual(ready[1]['id'], record['mark_layer_node_ids'][0])

    def test_attach_prefers_supplied_stamp_id_for_repeated_sentence(self):
        # Create rides the DOM stamp: an unscoped "Ready." would miss, but
        # the client-supplied occurrence id is the primary record.
        text = 'Ready. Unique first context.\n\nReady. Unique second context.'
        nodes = to_mark_layer_nodes(text)
        ready = [
            n for n in nodes
            if n['kind'] == 'sentence' and n['fragments'][0]['text'].strip() == 'Ready.'
        ]
        record = {
            'type': 'mark', 'quote': 'Ready.',
            'mark_layer_node_id': ready[1]['id'],
        }
        attach_mark_layer_node_ids(record, text)
        self.assertEqual(ready[1]['id'], record['mark_layer_node_id'])
        self.assertEqual(ready[1]['id'], record['mark_layer_node_ids'][0])

    def test_attach_drops_stale_supplied_id_when_quote_does_not_unique_match(self):
        text = 'Ready. Unique first context.\n\nReady. Unique second context.'
        record = {
            'type': 'mark', 'quote': 'Ready.',
            'mark_layer_node_id': 'pmsent-no-such',
        }
        attach_mark_layer_node_ids(record, text)
        self.assertNotIn('mark_layer_node_id', record)

    def test_repeated_sentence_misses_when_snapshot_does_not_narrow(self):
        # Skip 2026-09-06 nit 1: without a snapshot that uniquely names one
        # paragraph, do not first-hit attach. A wrong node id is silent and
        # permanent on the sidecar — prefer no id.
        text = 'Ready. Unique first context.\n\nReady. Unique second context.'
        nodes = to_mark_layer_nodes(text)
        ready_ids = {
            n['id'] for n in nodes
            if n['kind'] == 'sentence' and n['fragments'][0]['text'].strip() == 'Ready.'
        }
        self.assertEqual(2, len(ready_ids))

        self.assertEqual([], match_mark_layer_nodes(nodes, quote='Ready.'))
        self.assertEqual(
            [],
            match_mark_layer_nodes(nodes, quote='Ready.', snapshot='no such paragraph'),
        )

        unscoped = {'type': 'mark', 'quote': 'Ready.', 'snapshot': ''}
        attach_mark_layer_node_ids(unscoped, text)
        self.assertNotIn('mark_layer_node_id', unscoped)
        self.assertNotIn('mark_layer_node_ids', unscoped)

        stale = {
            'type': 'mark', 'quote': 'Ready.',
            'snapshot': 'no such paragraph',
        }
        attach_mark_layer_node_ids(stale, text)
        self.assertNotIn('mark_layer_node_id', stale)
        self.assertNotIn('mark_layer_node_ids', stale)

    def test_ambiguous_containment_is_a_miss_not_first_hit(self):
        # The fall-through `needle in text or text in needle` used to take
        # the first hit. "is" sits in both sentences; attaching Alpha would
        # be silent and wrong.
        text = 'Alpha is first. Beta is second.'
        nodes = to_mark_layer_nodes(text)
        self.assertEqual([], match_mark_layer_nodes(nodes, quote='is'))

    def test_unique_containment_still_attaches(self):
        text = 'Alpha is first. Beta is second.'
        nodes = to_mark_layer_nodes(text)
        expected = next(
            n for n in nodes
            if n['kind'] == 'sentence'
            and n['fragments'][0]['text'].strip() == 'Beta is second.'
        )
        matched = match_mark_layer_nodes(nodes, quote='Beta is')
        self.assertTrue(matched)
        self.assertEqual(expected['id'], matched[0]['id'])


class MarkLayerDomStamperTests(unittest.TestCase):
    """Render-time walker: repeated sentences get their occurrence id, not first-hit."""

    def test_repeated_sentence_consumes_occurrence_ids_in_order(self):
        text = 'Ready. Unique first context.\n\nReady. Unique second context.'
        nodes = to_mark_layer_nodes(text)
        ready = [
            n for n in nodes
            if n['kind'] == 'sentence' and n['fragments'][0]['text'].strip() == 'Ready.'
        ]
        self.assertEqual(2, len(ready))
        self.assertNotEqual(ready[0]['id'], ready[1]['id'])

        stamper = MarkLayerDomStamper(nodes)
        first_para = stamper.bind_block('Ready. Unique first context.')
        first_ready = stamper.next_sentence('Ready.')
        first_ctx = stamper.next_sentence('Unique first context.')
        second_para = stamper.bind_block('Ready. Unique second context.')
        second_ready = stamper.next_sentence('Ready.')
        second_ctx = stamper.next_sentence('Unique second context.')

        self.assertEqual(ready[0]['id'], first_ready)
        self.assertEqual(ready[1]['id'], second_ready)
        self.assertNotEqual(first_ready, second_ready)
        self.assertTrue(first_para and first_para.startswith('pmpara-'))
        self.assertTrue(second_para and second_para.startswith('pmpara-'))
        self.assertNotEqual(first_para, second_para)
        self.assertTrue(first_ctx and first_ctx.startswith('pmsent-'))
        self.assertTrue(second_ctx and second_ctx.startswith('pmsent-'))

    def test_skip_block_preserves_later_duplicate_occurrence(self):
        text = 'Ready. Unique first context.\n\nReady. Unique second context.'
        nodes = to_mark_layer_nodes(text)
        ready = [
            n for n in nodes
            if n['kind'] == 'sentence' and n['fragments'][0]['text'].strip() == 'Ready.'
        ]
        stamper = MarkLayerDomStamper(nodes)
        stamper.skip_block('Ready. Unique first context.')
        stamper.bind_block('Ready. Unique second context.')
        self.assertEqual(ready[1]['id'], stamper.next_sentence('Ready.'))

    def test_heading_block_text_matches_hashed_source(self):
        nodes = to_mark_layer_nodes('# Review title\n\nAlpha is first.')
        stamper = MarkLayerDomStamper(nodes)
        heading_id = stamper.bind_block('Review title')
        self.assertTrue(heading_id and heading_id.startswith('pmpara-'))
        # Adapter does not sentence-split headings; sentence stamp is a miss.
        self.assertIsNone(stamper.next_sentence('Review title'))
        para_id = stamper.bind_block('Alpha is first.')
        self.assertTrue(para_id and para_id.startswith('pmpara-'))
        sent_id = stamper.next_sentence('Alpha is first.')
        expected = next(
            n for n in nodes
            if n['kind'] == 'sentence'
            and n['fragments'][0]['text'].strip() == 'Alpha is first.'
        )
        self.assertEqual(expected['id'], sent_id)

    def test_empty_stamper_is_noop(self):
        stamper = MarkLayerDomStamper([])
        self.assertIsNone(stamper.bind_block('Ready.'))
        self.assertIsNone(stamper.next_sentence('Ready.'))

    def test_next_sentence_does_not_steal_from_another_group(self):
        # Mid-doc-edit drift: a document-wide text search used to stamp a
        # later twin's id onto this block. A miss in the bound group is
        # unstamped — jump may fall back to text, counted, not default.
        text = 'Ready. Unique first context.\n\nReady. Unique second context.'
        nodes = to_mark_layer_nodes(text)
        ready = [
            n for n in nodes
            if n['kind'] == 'sentence' and n['fragments'][0]['text'].strip() == 'Ready.'
        ]
        stamper = MarkLayerDomStamper(nodes)
        stamper.bind_block('Ready. Unique first context.')
        stamper.next_sentence('Ready.')
        self.assertIsNone(stamper.next_sentence('Ready.'))
        stamper.bind_block('Ready. Unique second context.')
        self.assertEqual(ready[1]['id'], stamper.next_sentence('Ready.'))


if __name__ == '__main__':
    unittest.main()
