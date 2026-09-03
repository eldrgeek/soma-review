import os
import sys
import unittest

V2_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, V2_DIR)

import mdblocks  # noqa: E402


SAMPLE_LEXICON = """# The SOMA Lexicon

## I. Philosophy & Doctrine

### SOMA \xb7 Society of Minds Aligned

**What we mean.** The name of the whole enterprise. Second sentence here.

*See also:* nothing.

### Pulse / Pulse Core \xb7 the operator surface, and the memory ledger behind it

**Plain reading.** An aside that is not the definition.

**What we mean.** Two related things worth keeping distinct. More words follow.
"""


class LexiconLookupTests(unittest.TestCase):
    """One test for lexicon lookup (build_lexicon_index + resolution)."""

    def setUp(self):
        self.lexicon = mdblocks.build_lexicon_index(SAMPLE_LEXICON)

    def test_build_index_and_alias_resolution(self):
        # Primary entry present, keyed by the primary alias's slug.
        self.assertIn('soma', self.lexicon['by_slug'])
        soma = self.lexicon['by_slug']['soma']
        self.assertEqual(soma['term'], 'SOMA')
        self.assertEqual(soma['gloss'], 'Society of Minds Aligned')
        self.assertEqual(
            soma['first_sentence'], 'The name of the whole enterprise.'
        )
        # Multi-alias heading ("Pulse / Pulse Core") resolves both names to
        # the same slug, and "Plain reading." is skipped in favor of
        # "What we mean." for the definition text.
        self.assertEqual(self.lexicon['by_alias'].get('pulse'), 'pulse')
        self.assertEqual(self.lexicon['by_alias'].get('pulse core'), 'pulse')
        pulse = self.lexicon['by_slug']['pulse']
        self.assertNotIn('aside', pulse['first_sentence'])
        self.assertEqual(pulse['first_sentence'], 'Two related things worth keeping distinct.')

    def test_terms_link_and_lexicon_scheme_resolve(self):
        doc = (
            "# Page\n\n"
            "See [SOMA](#terms) and [the shared surface](lexicon:Pulse Core) "
            "and [by anchor](#lex-soma).\n"
        )
        _title, blocks = mdblocks.parse_markdown(doc, lexicon=self.lexicon)
        html = blocks[1]['html']
        self.assertIn('data-term-slug="lex-soma"', html)
        self.assertIn('data-term-slug="lex-pulse"', html)
        self.assertEqual(html.count('class="term-link"'), 3)

    def test_page_local_terms_win_over_lexicon(self):
        doc = (
            "# Page\n\n"
            "See [SOMA](#terms).\n\n"
            "## Terms\n\n"
            "- **SOMA** — this page's own override definition.\n"
        )
        _title, blocks = mdblocks.parse_markdown(doc, lexicon=self.lexicon)
        html = blocks[1]['html']
        self.assertIn('data-term-slug="soma"', html)
        self.assertNotIn('data-term-slug="lex-soma"', html)
        self.assertIn('own override definition.', html)


class AutoLexiconOptInTests(unittest.TestCase):
    """One test for the opt-in default: off unless the page asks for it, and
    the marker itself never renders as visible text once it does."""

    def setUp(self):
        self.lexicon = mdblocks.build_lexicon_index(SAMPLE_LEXICON)

    def test_default_off_byte_identical_to_no_lexicon(self):
        doc = "# Page\n\nSOMA appears here in plain prose, unlinked.\n"
        _title, blocks_with_lexicon = mdblocks.parse_markdown(doc, lexicon=self.lexicon)
        _title2, blocks_without_lexicon = mdblocks.parse_markdown(doc, lexicon=None)
        html_with = '\n'.join(b['html'] for b in blocks_with_lexicon)
        html_without = '\n'.join(b['html'] for b in blocks_without_lexicon)
        self.assertNotIn('term-link', html_with)
        self.assertEqual(html_with, html_without)

    def test_marker_opts_in_and_is_stripped_from_output(self):
        doc = (
            "<!-- auto-lexicon -->\n"
            "# Page\n\n"
            "SOMA appears here and SOMA again.\n"
        )
        title, blocks = mdblocks.parse_markdown(doc, lexicon=self.lexicon)
        self.assertEqual(title, 'Page')  # marker didn't shift heading detection
        full_html = '\n'.join(b['html'] for b in blocks)
        self.assertNotIn('auto-lexicon', full_html)  # marker never rendered
        self.assertEqual(full_html.count('class="term-link"'), 1)  # first occurrence only

    def test_front_matter_flag_also_opts_in(self):
        doc = "---\nauto-lexicon: true\n---\n# Page\n\nSOMA appears here.\n"
        title, blocks = mdblocks.parse_markdown(doc, lexicon=self.lexicon)
        self.assertEqual(title, 'Page')
        full_html = '\n'.join(b['html'] for b in blocks)
        self.assertIn('class="term-link"', full_html)
        self.assertNotIn('---', full_html)

    def test_headings_and_terms_section_never_auto_linked(self):
        doc = (
            "<!-- auto-lexicon -->\n"
            "# SOMA overview\n\n"
            "SOMA appears in prose.\n\n"
            "## Terms\n\n"
            "- **note** — SOMA should not auto-link inside this definition.\n"
        )
        _title, blocks = mdblocks.parse_markdown(doc, lexicon=self.lexicon)
        heading_block = blocks[0]
        self.assertEqual(heading_block['kind'], 'heading')
        self.assertNotIn('term-link', heading_block['html'])
        terms_list_block = blocks[-1]
        self.assertEqual(terms_list_block['kind'], 'list')
        self.assertNotIn('lex-soma', terms_list_block['html'])


if __name__ == '__main__':
    unittest.main()
