#!/usr/bin/env node
/**
 * In-repo fromProseMarkdown — Playmaker mark-layer-engine shared model.
 *
 * Faithful port of playmaker/src/mark-layer-engine/adapters/proseMarkdown.ts
 * (blank-line blocks, headings kept whole, sibling sentences, sha1 ids).
 * Used for parity against the Python live port (`from_prose_markdown`).
 * Not the historical Python twin. Live default in soma-review is the
 * Python port (stdlib server); set SOMA_REVIEW_MARK_LAYER_ENGINE=js to
 * consume this CLI at stamp time.
 *
 * Stdin: markdown text. Stdout: {"nodes":[...]} JSON.
 */
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';

const HEADING_RE = /^#{1,6}\s+/;
const SENTENCE_CLOSERS = new Set(['"', '\u201d', '\u2019', ')', ']']);
const SENTENCE_ABBREVIATIONS = new Set([
  'e.g', 'i.e', 'etc', 'vs', 'mr', 'mrs', 'ms', 'dr', 'prof', 'jr', 'sr',
  'inc', 'ltd', 'co', 'no', 'st', 'approx', 'fig', 'vol', 'op', 'cf',
  'dept', 'univ', 'rev', 'sept', 'jan', 'feb', 'mar', 'apr', 'jun', 'jul',
  'aug', 'oct', 'nov', 'dec',
]);

function norm(text) {
  return (text || '').normalize('NFC').split(/\s+/).filter(Boolean).join(' ');
}

function isAlnum(ch) {
  return /\p{L}|\p{N}/u.test(ch);
}

function isDigit(ch) {
  return /\p{Nd}/u.test(ch);
}

function isAlpha(ch) {
  return /\p{L}/u.test(ch);
}

function isSentenceAbbreviation(word) {
  const lowered = word.toLowerCase();
  return SENTENCE_ABBREVIATIONS.has(lowered)
    || (lowered.length === 1 && isAlpha(lowered));
}

/** Code-point split matching mdblocks.segment_sentences. */
function splitSentences(text) {
  if (!text) return [];
  const chars = [...text];
  const n = chars.length;
  const starts = [0];
  let i = 0;
  while (i < n) {
    const ch = chars[i];
    if (ch === '.' || ch === '!' || ch === '?') {
      let j = i;
      while (j < n && (chars[j] === '.' || chars[j] === '!' || chars[j] === '?')) {
        j += 1;
      }
      let k = i;
      while (k > 0 && (isAlnum(chars[k - 1]) || chars[k - 1] === '.')) {
        k -= 1;
      }
      const word = chars.slice(k, i).join('').replace(/^\.+|\.+$/g, '');
      const isAbbrev = word ? isSentenceAbbreviation(word) : false;
      const isDecimal = (
        chars[i] === '.' && j === i + 1
        && k < i && isDigit(chars[i - 1])
        && j < n && isDigit(chars[j])
      );
      let m = j;
      while (m < n && SENTENCE_CLOSERS.has(chars[m])) {
        m += 1;
      }
      const boundaryOk = m >= n || chars[m] === ' ' || chars[m] === '\t' || chars[m] === '\n';
      if (boundaryOk && !isAbbrev && !isDecimal) {
        starts.push(m);
      }
      i = m;
      continue;
    }
    i += 1;
  }
  starts.push(n);
  const uniq = [...new Set(starts.filter((s) => s >= 0 && s <= n))].sort((a, b) => a - b);
  const spans = [];
  for (let idx = 0; idx < uniq.length - 1; idx += 1) {
    const a = uniq[idx];
    const b = uniq[idx + 1];
    if (a === b) continue;
    const slice = chars.slice(a, b).join('');
    spans.push([a, b, slice]);
  }
  return spans;
}

function contentId(seen, prefix, text) {
  const digest = createHash('sha1').update(`${prefix}:${text}`, 'utf8').digest('hex').slice(0, 10);
  const key = `${prefix}:${digest}`;
  const occurrence = seen[key] || 0;
  seen[key] = occurrence + 1;
  const base = `${prefix}-${digest}`;
  return occurrence === 0 ? base : `${base}-${occurrence}`;
}

function paragraphNode(seen, text) {
  const id = contentId(seen, 'pmpara', text);
  return {
    id,
    kind: 'paragraph',
    fragments: [{ id: contentId(seen, `${id}-frag`, text), text }],
  };
}

function blankNode(seen, text) {
  const id = contentId(seen, 'pmln', text);
  return {
    id,
    kind: 'blank',
    fragments: [{ id: contentId(seen, `${id}-frag`, text), text }],
  };
}

function sentenceNodes(seen, paragraphText) {
  const normalized = norm(paragraphText);
  const nodes = [];
  let offset = 0;
  for (const [_start, _end, text] of splitSentences(normalized)) {
    const id = contentId(seen, 'pmsent', text);
    nodes.push({
      id,
      kind: 'sentence',
      fragments: [{ id: contentId(seen, `${id}-frag`, text), text }],
      attrs: { offset },
    });
    offset += [...text].length;
  }
  return nodes;
}

export function fromProseMarkdown(md) {
  if (!md) return [];
  const nodes = [];
  const seen = Object.create(null);
  const blocks = md.split(/(\n{2,})/);
  for (const block of blocks) {
    if (!block) continue;
    if (/^\n{2,}$/.test(block)) {
      nodes.push(blankNode(seen, block));
      continue;
    }
    nodes.push(paragraphNode(seen, block));
    if (HEADING_RE.test(block)) continue;
    nodes.push(...sentenceNodes(seen, block));
  }
  return nodes;
}

function main() {
  const md = readFileSync(0, 'utf8');
  process.stdout.write(JSON.stringify({ nodes: fromProseMarkdown(md) }));
}

const isCli = process.argv[1] && process.argv[1].endsWith('fromProseMarkdown.mjs');
if (isCli) {
  main();
}
