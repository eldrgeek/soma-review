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


LEXICON_SRC = """# Lexicon

### bracketed assent · two marks imply agreement to everything between them

**What we mean.** Every line between two marks counts as at least acknowledged.

### trunk · the main branch of a git repository

**What we mean.** The line of development everything else merges into.
"""

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
        lexicon = mdblocks.build_lexicon_index(LEXICON_SRC)
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


class AdversarialPassFixes(unittest.TestCase):
    """Findings from the `skip` adversarial pass, 2026-09-04. Each test names the
    crack it closes and goes red against the first version of the change."""

    def test_a_hyphenated_compound_is_not_a_use_of_the_term(self):
        """`\\b` treats `-` as a boundary, so `round-trip identity test` linked the
        definition of a review round. Live on the trunk page when found."""
        src = ("# P\n\nThe round-trip identity test is a gate.\n\n"
               "## Terms\n\n- **round** — one pass of a document between writer and reader.\n")
        blocks, _ = render(src)
        self.assertEqual(slugs_linked(blocks), [])

    def test_a_free_standing_use_still_links(self):
        src = ("# P\n\nEach round ends with a list.\n\n"
               "## Terms\n\n- **round** — one pass of a document.\n")
        blocks, _ = render(src)
        self.assertIn('round', slugs_linked(blocks))

    def test_a_slash_separated_label_links_each_of_its_names(self):
        """`Watch-only / armed` produced one alternation branch matching the literal
        joined string, which appears nowhere — the terms with the most names got
        zero links."""
        src = ("# P\n\nThe seat is armed. Before that it was watch-only.\n\n"
               "## Terms\n\n- **Watch-only / armed** — whether the seat may act.\n")
        blocks, _ = render(src)
        # One TERM, so one link per block: `auto_seen` is keyed on the term, not
        # the alias. What matters is that a name other than the literal joined
        # string can match at all.
        self.assertEqual(slugs_linked(blocks), ['watch-only-armed'])
        two_blocks = ("# P\n\nThe seat is armed.\n\nBefore that it was watch-only.\n\n"
                      "## Terms\n\n- **Watch-only / armed** \u2014 whether the seat may act.\n")
        self.assertEqual(slugs_linked(render(two_blocks)[0]),
                         ['watch-only-armed', 'watch-only-armed'])

    def test_a_longer_lexicon_phrase_is_not_hijacked_by_a_shorter_local_label(self):
        """`_find_term_by_label`'s containment fallback gave the local `bracket`
        definition to the lexicon phrase `bracketed assent` — the phrase Mike
        actually ruled on getting the wrong definition."""
        lexicon = mdblocks.build_lexicon_index(LEXICON_SRC)
        src = ("<!-- auto-lexicon -->\n# P\n\nReading rules: bracketed assent.\n\n"
               "## Terms\n\n- **bracket** — the span between two explicit marks.\n")
        blocks, _ = render(src, lexicon=lexicon)
        self.assertEqual(slugs_linked(blocks), ['lex-bracketed-assent'])

    def test_an_all_digit_label_never_enters_the_alternation(self):
        """The alternation runs while code spans are still \\x00N\\x00 placeholders,
        and \\x00 is a non-word char, so a bare number would match a placeholder
        index and shred the output."""
        src = ("# P\n\nSee `x` and item 12 below.\n\n"
               "## Terms\n\n- **12** — a numbered item.\n")
        blocks, _ = render(src)
        for b in blocks:
            self.assertNotIn('\x00', b['html'])

    def test_the_server_terms_guard_actually_fires_on_a_capital_T_heading(self):
        """blockmap.norm() does not lowercase, so comparing it against the literal
        'terms' was True for every block on every page: the guard never fired."""
        import server, blockmap
        block = {'mark_layer_section_title': blockmap.norm('Terms')}
        self.assertFalse(server._auto_local_for(block, {'trunk': {}}))
        body = {'mark_layer_section_title': blockmap.norm('The model')}
        self.assertTrue(server._auto_local_for(body, {'trunk': {}}))
