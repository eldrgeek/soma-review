"""Cross-repo parity test for MDP item 6a (SOMA agreed model): Playmaker and
soma-review are meant to "share one engine" for sentence-level splitting.
Playmaker's side is `splitSentences`/`norm`
(src/mark-layer-engine/adapters/proseMarkdown.ts). soma-review actually has
TWO independent split loops that are each documented ports of the other and
of Playmaker's: `mdblocks.segment_sentences` (used only by
`apply_sentence_change`'s `_locate_change_span` to find an edit's before-text
on disk) and `server.sentence_ranges` (the function that decides which spans
actually render as clickable `.mark-sentence` units — the one real readers
interact with). These two already diverged once in production (the "ref. op.
cit." bug, `server.py:3392-3394`) before their abbreviation check was
unified; comparing Playmaker only against `segment_sentences` would leave
`sentence_ranges` — the one that matters to a live reader — unverified, so
both are checked here (Skip's adversarial pass, 2026-09-05, caught the first
draft doing only the former).

Skips (does not fail) if `node` is unavailable or too old for
`--experimental-strip-types`, mirroring this test suite's existing pattern of
skipping browser-dependent tests rather than failing the whole run on missing
tooling (see test_v3_sentence_marks.py's Playwright skip).
"""
import json
import os
import shutil
import subprocess
import sys
import unittest

V2_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, V2_DIR)

import mdblocks  # noqa: E402
import server  # noqa: E402

CORPUS_PATH = os.path.join(V2_DIR, 'tests', 'fixtures', 'sentence_parity_corpus.json')
PLAYMAKER_ROOT = os.path.expanduser('~/Projects/playmaker')
PARITY_CLI = os.path.join(PLAYMAKER_ROOT, 'scripts', 'sentence-parity-cli.mjs')


def _node_supports_strip_types():
    node = shutil.which('node')
    if not node:
        return False
    try:
        out = subprocess.run([node, '--version'], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    version = out.stdout.strip().lstrip('v')
    try:
        major = int(version.split('.')[0])
    except (ValueError, IndexError):
        return False
    return major >= 22


def _run_js_parity(corpus):
    node = shutil.which('node')
    result = subprocess.run(
        [node, '--experimental-strip-types', PARITY_CLI, CORPUS_PATH],
        capture_output=True, text=True, timeout=30, cwd=PLAYMAKER_ROOT,
    )
    if result.returncode != 0:
        raise RuntimeError(f'sentence-parity-cli.mjs failed: {result.stderr}')
    return json.loads(result.stdout)


@unittest.skipUnless(os.path.isfile(CORPUS_PATH), 'shared corpus fixture missing')
@unittest.skipUnless(os.path.isfile(PARITY_CLI), 'playmaker parity CLI missing (playmaker repo not checked out?)')
@unittest.skipUnless(_node_supports_strip_types(), 'node >= 22 with --experimental-strip-types not available')
class EngineParityTests(unittest.TestCase):
    """Runs the shared corpus through both engines and asserts they produce
    the same sentence boundaries on the same (NFC + whitespace-collapsed)
    normalized text — the coordinate space both sides agree is authoritative
    (`blockmap.norm()` / proseMarkdown.ts's `norm()`)."""

    @classmethod
    def setUpClass(cls):
        with open(CORPUS_PATH, 'r', encoding='utf-8') as f:
            cls.corpus = json.load(f)
        cls.js_results = _run_js_parity(cls.corpus)

    def test_corpus_and_js_results_line_up(self):
        self.assertEqual(len(self.corpus), len(self.js_results))

    def test_every_case_agrees_on_sentence_boundaries(self):
        mismatches = []
        for text, js in zip(self.corpus, self.js_results):
            normalized = mdblocks.norm(text) if hasattr(mdblocks, 'norm') else _local_norm(text)
            py_spans = mdblocks.segment_sentences(normalized)
            py_sentences = [s for (_start, _end, s) in py_spans]
            if py_sentences != js['sentences']:
                mismatches.append({
                    'input': text,
                    'normalized': normalized,
                    'python': py_sentences,
                    'js': js['sentences'],
                })
        self.assertEqual(
            mismatches, [],
            f'{len(mismatches)} of {len(self.corpus)} corpus case(s) split differently '
            f'between mdblocks.segment_sentences and splitSentences: {json.dumps(mismatches, indent=2, ensure_ascii=False)}'
        )

    def test_sentence_ranges_the_actually_rendered_engine_agrees_too(self):
        """`server.sentence_ranges` (not `segment_sentences`) is what decides
        which spans render as clickable `.mark-sentence` units in production
        — the function a real reader's marks actually bind to. Comparing
        against `segment_sentences` alone would miss a divergence here even
        though both Python functions currently share one abbreviation check,
        because their loop bodies are still separately maintained (the two
        already disagreed once, on 'op'/'cf'/month abbreviations, before
        2026-09-05's unification — see server.py's own docstring on
        `sentence_ranges`). `sentence_ranges` returns stripped quotes
        (`normalized[start:end].strip()`), so both sides are compared
        stripped here — that's a presentation difference in `segment_sentences`/
        `splitSentences` (whitespace trails onto the following span, by
        design, so the spans tile the text with no gaps), not a boundary
        disagreement."""
        mismatches = []
        for text, js in zip(self.corpus, self.js_results):
            py_ranges = server.sentence_ranges(text)
            py_sentences = [q.strip() for (_start, _end, q) in py_ranges]
            js_sentences = [s.strip() for s in js['sentences']]
            if py_sentences != js_sentences:
                mismatches.append({
                    'input': text,
                    'sentence_ranges': py_sentences,
                    'js_splitSentences': js_sentences,
                })
        self.assertEqual(
            mismatches, [],
            f'{len(mismatches)} of {len(self.corpus)} corpus case(s): server.sentence_ranges '
            f'(the production rendering engine) disagrees with splitSentences: '
            f'{json.dumps(mismatches, indent=2, ensure_ascii=False)}'
        )

    def test_normalization_agrees(self):
        """Both sides must land on the identical normalized string before
        splitting even starts, or a sentence-level agreement above would be
        coincidental rather than load-bearing."""
        mismatches = []
        for text, js in zip(self.corpus, self.js_results):
            normalized = mdblocks.norm(text) if hasattr(mdblocks, 'norm') else _local_norm(text)
            if normalized != js['normalized']:
                mismatches.append({'input': text, 'python': normalized, 'js': js['normalized']})
        self.assertEqual(mismatches, [], f'normalization mismatch: {mismatches}')


def _local_norm(text):
    import unicodedata
    return ' '.join(unicodedata.normalize('NFC', text or '').split())


if __name__ == '__main__':
    unittest.main()
