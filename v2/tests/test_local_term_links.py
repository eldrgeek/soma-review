"""Page-local Terms entries link at their first use (Mike's ruling, 2026-09-03:
"define every term the page uses" — Terms section, linked at first use).

Before 2026-09-04 the auto-link alternation was built from LEXICON aliases only,
so a page-local term was linked only where it happened to collide with a lexicon
alias. `mdp-agreed-model.md` defined 21 terms and linked 1. These tests go red
against that behaviour.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mdblocks  # noqa: E402


def render(src, lexicon=None):
    terms_out = {}
    _title, blocks = mdblocks.parse_markdown(src, terms_out=terms_out, lexicon=lexicon)
    return blocks, terms_out


def slugs_linked(blocks, in_terms_section=False):
    """Every data-term-slug on the page, split by whether the block sits inside
    the `## Terms` section."""
    out = []
    seen_terms_heading = False
    for b in blocks:
        if b['kind'] == 'heading' and b['text'].strip().lower() == 'terms':
            seen_terms_heading = True
            continue
        if seen_terms_heading != in_terms_section:
            continue
        out += re.findall(r'data-term-slug="([^"]+)"', b['html'])
    return out


PAGE = """# A page

The trunk is the file. A bracket is a span between two marks.

## Terms

- **trunk** — the current agreed text, held as the `.md` file in git.
- **bracket** — the span between two explicit marks by the same reader.
"""


class LocalTermLinking(unittest.TestCase):
    def test_local_terms_link_in_the_body_without_any_lexicon(self):
        """No lexicon, no `<!-- auto-lexicon -->` marker: a page that wrote its own
        Terms section has opted in by writing it."""
        blocks, terms = render(PAGE)
        self.assertEqual(set(terms), {'trunk', 'bracket'})
        self.assertEqual(sorted(set(slugs_linked(blocks))), ['bracket', 'trunk'])

    def test_the_terms_section_never_links_itself(self):
        """A definition of `trunk` must not link the word `trunk` to itself."""
        blocks, _ = render(PAGE)
        self.assertEqual(slugs_linked(blocks, in_terms_section=True), [])

    def test_single_word_local_terms_match_case_insensitively(self):
        """A page-local coinage is this page's own word, so `Trunk` at the start of
        a sentence links. (The case-SENSITIVE rule protects everyday words from the
        estate lexicon, which this page did not write.)"""
        src = PAGE.replace('The trunk is the file.', 'Trunk is the file.')
        blocks, _ = render(src)
        self.assertIn('trunk', slugs_linked(blocks))

    def test_a_page_with_no_terms_section_is_unchanged(self):
        blocks, terms = render("# A page\n\nThe trunk is the file.\n")
        self.assertEqual(terms, {})
        self.assertEqual(slugs_linked(blocks), [])

    def test_page_local_definition_wins_over_the_lexicon(self):
        lexicon = mdblocks.build_lexicon_index(
            "# Lexicon\n\n## trunk\n\nThe main branch of a git repository.\n"
        )
        blocks, _ = render('<!-- auto-lexicon -->\n' + PAGE, lexicon=lexicon)
        linked = slugs_linked(blocks)
        self.assertIn('trunk', linked)
        self.assertNotIn('lex-trunk', linked)

    def test_headings_are_still_never_auto_linked(self):
        src = "# Trunk rules\n\n## The trunk\n\nBody.\n\n## Terms\n\n- **trunk** — the file.\n"
        _title, blocks = mdblocks.parse_markdown(src)
        for b in blocks:
            if b['kind'] == 'heading':
                self.assertNotIn('data-term-slug', b['html'], b['text'])


if __name__ == '__main__':
    unittest.main()


class MarkLayerPath(unittest.TestCase):
    """The v3 mark layer re-renders prose sentence by sentence, discarding the
    paragraph HTML parse_markdown produced. Until 2026-09-04 it did not thread
    the page-local-terms flag, so EVERY paragraph on a v3 page silently lost its
    page-local term links while list blocks (which keep their parsed html
    verbatim) kept theirs.
    """

    def setUp(self):
        import server
        self.server = server

    def _blocks(self, src):
        terms_out = {}
        _t, blocks = mdblocks.parse_markdown(src, terms_out=terms_out)
        for b in blocks:
            b.setdefault('mark_layer_section_title', '')
        return blocks, terms_out

    def test_paragraph_sentences_keep_page_local_term_links(self):
        blocks, terms_out = self._blocks(PAGE)
        para = next(b for b in blocks if b['kind'] == 'paragraph')
        html, _units = self.server.mark_layer_inner(
            para, terms=terms_out,
            auto_local_terms=self.server._auto_local_for(para, terms_out),
        )
        self.assertIn('class="mark-sentence"', html)
        self.assertIn('data-term-slug="trunk"', html)
        self.assertIn('data-term-slug="bracket"', html)

    def test_terms_section_prose_does_not_link_itself(self):
        blocks, terms_out = self._blocks(PAGE)
        para = next(b for b in blocks if b['kind'] == 'paragraph')
        para['mark_layer_section_title'] = 'terms'
        self.assertFalse(self.server._auto_local_for(para, terms_out))

    def test_first_use_is_scoped_to_the_block_not_the_sentence(self):
        """`auto_seen` is created once per block and threaded into every
        sentence, so a term used in two sentences of one paragraph links once."""
        src = PAGE.replace(
            'The trunk is the file. A bracket is a span between two marks.',
            'The trunk is the file. The trunk is still the file.',
        )
        blocks, terms_out = self._blocks(src)
        para = next(b for b in blocks if b['kind'] == 'paragraph')
        html, _ = self.server.mark_layer_inner(
            para, terms=terms_out,
            auto_local_terms=self.server._auto_local_for(para, terms_out),
        )
        self.assertEqual(html.count('data-term-slug="trunk"'), 1)

    def test_a_page_with_no_terms_never_arms_the_flag(self):
        blocks, terms_out = self._blocks("# P\n\nThe trunk is the file.\n")
        para = next(b for b in blocks if b['kind'] == 'paragraph')
        self.assertFalse(self.server._auto_local_for(para, terms_out))
