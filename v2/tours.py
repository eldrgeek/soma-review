#!/usr/bin/env python3
"""
tours.py — Quinn-guided tours of completed jobs on the soma-review surface.

Mounts the soma-guide widget engine (the same CDN-hosted engine Legends uses
for Bill/Quinn — https://soma-guide.netlify.app) on completion pages and the
COMPLETED.md index, with walkthroughs generated dynamically from
~/Projects/_estate/completions/*.md.

Design notes:
- Tours are built PER REQUEST from whatever completion pages exist on disk —
  inherently idempotent; a concurrent worker can keep adding completion pages
  and they show up on the next page load, no regeneration step.
- Block anchors are computed with the SAME mdblocks.parse_markdown the page
  renderer uses, so `[data-anchor="..."]` selectors always match the rendered
  DOM exactly (if the doc changes, both change together).
- Persona is Quinn — the estate reuses Legends' reviewer persona (Mike knows
  her). Text mode only: no voiceAgentId, so the engine hides all voice UI and
  auto-advances tours on a text-length fallback timer. Audio narration via the
  gen-tour-audio.mjs path is a v2 candidate.
- Completion page contract (produced by the completions worker):
  first `#` heading = title, `##` sections, `**Live:** <url>` lines.
  Everything here degrades gracefully when a section is missing.

Feature flag: workspaces.json -> <ws>.tours == true, AND the route must be a
tour-bearing page (the completions index or a completion page). Nothing is
injected anywhere else, so existing pages/commenting are untouched.
"""
import json
import os
import re

from mdblocks import parse_markdown

COMPLETIONS_DIR = os.path.expanduser('~/Projects/_estate/completions')
# Route prefix (estate workspace) for completion pages.
COMPLETIONS_ROUTE_PREFIX = 'estate/completions/'
# Routes that get the index treatment (auto-offer + per-item Tour affordances).
# WORKQUEUE.md added 2026-07-03: its Done-today lines end in
# `→ [receipts](completions/<slug>.md)` links, which the HELPER_JS pill
# injector matches against the same route→tour-id index — so the queue page
# gets the exact ▶ Tour affordance COMPLETED.md has, no extra mechanism.
INDEX_ROUTES = ('estate/COMPLETED.md', 'estate/completions/COMPLETED.md',
                'estate/WORKQUEUE.md')

GUIDE_CSS_URL = 'https://soma-guide.netlify.app/soma-guide.css'
GUIDE_JS_URL = 'https://soma-guide.netlify.app/soma-guide.js'

_LIVE_RE = re.compile(r'\*\*Live:?\*\*:?\s*<?(https?://\S+?)>?(?:\s|$)', re.IGNORECASE)
_RECEIPTS_HEADING_RE = re.compile(r'receipt|evidence|commit|verif|proof', re.IGNORECASE)


def is_tour_route(route_path):
    route_path = (route_path or '').strip('/')
    return route_path in INDEX_ROUTES or (
        route_path.startswith(COMPLETIONS_ROUTE_PREFIX) and route_path.endswith('.md'))


def _slug(name):
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')


def list_completion_files():
    """Sorted (newest name first — files are date-prefixed) list of completion
    page filenames, excluding the index itself."""
    if not os.path.isdir(COMPLETIONS_DIR):
        return []
    out = []
    for name in os.listdir(COMPLETIONS_DIR):
        if not name.endswith('.md'):
            continue
        if name in ('COMPLETED.md', 'README.md'):
            continue
        if os.path.isfile(os.path.join(COMPLETIONS_DIR, name)):
            out.append(name)
    return sorted(out, reverse=True)


def _first_sentences(text, limit=280):
    """Plain-English trim: strip markdown emphasis/links/code, cut at a sentence
    boundary near `limit`."""
    t = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)      # [label](url) -> label
    t = re.sub(r'[*_`#>]+', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    if len(t) <= limit:
        return t
    cut = t[:limit]
    dot = cut.rfind('. ')
    return (cut[:dot + 1] if dot > 40 else cut.rstrip() + '…')


def build_tour_for_file(name):
    """Parse one completion page -> a soma-guide walkthrough dict, or None if
    the file is unreadable/empty. Steps:
      1. what-was-done (title block, plain-English summary)
      2. walk the receipts (receipts/evidence/commits section highlighted)
      3. see it live (block containing the **Live:** link; opens in new tab)
    """
    fs_path = os.path.join(COMPLETIONS_DIR, name)
    try:
        with open(fs_path, 'r', encoding='utf-8') as f:
            src = f.read()
    except OSError:
        return None
    title, blocks = parse_markdown(src)
    if not blocks:
        return None
    title = title or name

    route = COMPLETIONS_ROUTE_PREFIX + name
    page = '/page/' + route  # default-workspace URL form (estate is unprefixed)

    # -- locate the pieces ---------------------------------------------------
    first_anchor = blocks[0]['anchor']

    # Step-1 narration: the opening what-was-done paragraph. Completion pages
    # (per _estate/completions/README.md) put a `**Date:** … · **Project:** …`
    # meta line right after the title — skip that, take the next prose block.
    summary = ''
    for b in blocks:
        txt = b['text'].strip()
        if b['kind'] == 'heading' or not txt:
            continue
        if txt.startswith('**Date:**'):
            continue
        summary = _first_sentences(txt)
        break

    # Receipts section: first ## heading matching the receipts vocabulary.
    receipts_anchor = None
    receipts_label = None
    for b in blocks:
        if b['kind'] == 'heading' and _RECEIPTS_HEADING_RE.search(b['text']):
            receipts_anchor = b['anchor']
            receipts_label = re.sub(r'^#+\s*', '', b['text']).strip()
            break
    if receipts_anchor is None:
        # fall back: second heading on the page, if any
        headings = [b for b in blocks if b['kind'] == 'heading']
        if len(headings) > 1:
            receipts_anchor = headings[1]['anchor']
            receipts_label = re.sub(r'^#+\s*', '', headings[1]['text']).strip()

    # Live link: first block whose raw text carries a **Live:** URL.
    live_anchor = None
    live_url = None
    for b in blocks:
        m = _LIVE_RE.search(b['text'])
        if m:
            live_anchor = b['anchor']
            live_url = m.group(1).rstrip('.,)')
            break

    # -- assemble steps --------------------------------------------------------
    steps = [{
        'id': 's1-what',
        'target': '[data-anchor="%s"]' % first_anchor,
        'page': page,
        'label': 'What was done',
        'narration': 'Here’s what got done: %s' % (summary or title),
        'instruction': 'This page is the completion record — everything below is the detail.',
    }]
    if receipts_anchor:
        lbl = receipts_label or 'Receipts'
        if re.search(r'receipt', lbl, re.IGNORECASE):
            rec_narration = ('Now the receipts — commits, evidence, and verification are in the '
                             'highlighted section. Every claim above should trace to one of these.')
        else:
            rec_narration = ('Now the detail — the “%s” section is highlighted. '
                             'Every claim above should trace to something here.' % lbl)
        steps.append({
            'id': 's2-receipts',
            'target': '[data-anchor="%s"]' % receipts_anchor,
            'page': page,
            'label': lbl,
            'narration': rec_narration,
            'instruction': 'Skim the highlighted block. Comment on anything that doesn’t hold up.',
        })
    if live_anchor and live_url:
        steps.append({
            'id': 's3-live',
            'target': '[data-anchor="%s"]' % live_anchor,
            'page': page,
            'label': 'See it live',
            'narration': 'And it’s live — the highlighted link opens the real thing in a new tab. '
                         'Go see it for yourself.',
            'instruction': 'Click the Live link (opens in a new tab): %s' % live_url,
        })
    else:
        steps.append({
            'id': 's3-wrap',
            'target': '[data-anchor="%s"]' % blocks[-1]['anchor'],
            'page': page,
            'label': 'Wrap-up',
            'narration': 'No live URL on this one — it’s local or infrastructure work. '
                         'That’s the whole record; comment anywhere if something needs a second pass.',
            'instruction': 'Use the + affordance on any block to leave a comment.',
        })

    return {
        'id': 'tour-' + _slug(re.sub(r'\.md$', '', name)),
        'label': title,
        'keywords': [],
        'steps': steps,
        '_route': route,  # stripped before serialization; used for the index map
    }


def build_walkthroughs():
    tours = []
    for name in list_completion_files():
        t = build_tour_for_file(name)
        if t:
            tours.append(t)
    return tours


def _json_for_script(obj):
    """JSON safe for inline <script> embedding (no </script> breakout)."""
    return json.dumps(obj, ensure_ascii=False).replace('</', '<\\/')


HELPER_JS = r"""
(function () {
  'use strict';
  var IDX = window.__SOMA_TOUR_INDEX__ || {};

  function whenGuideReady(cb, tries) {
    tries = tries || 0;
    if (window.somaGuide && typeof window.somaGuide.startWalkthrough === 'function') { cb(); return; }
    if (tries < 80) setTimeout(function () { whenGuideReady(cb, tries + 1); }, 250);
  }

  // Deep link: ?tour=<id> starts that tour once the engine is up.
  var tourParam = null;
  try { tourParam = new URLSearchParams(location.search).get('tour'); } catch (e) {}
  if (tourParam) whenGuideReady(function () { window.somaGuide.startWalkthrough(tourParam); });

  // Per-item "Tour" affordance: next to any in-app link that points at a
  // completion page, add a small pill that starts Quinn's tour of it in place
  // (no reload — the engine navigates itself if the tour lives on another page).
  document.addEventListener('DOMContentLoaded', function () {
    var routes = Object.keys(IDX);
    if (!routes.length) return;
    document.querySelectorAll('.main .block-body a[href]').forEach(function (a) {
      var href = a.getAttribute('href') || '';
      for (var i = 0; i < routes.length; i++) {
        if (href.indexOf(routes[i]) !== -1) {
          if (a.nextElementSibling && a.nextElementSibling.classList &&
              a.nextElementSibling.classList.contains('sg-tour-link')) return;
          var id = IDX[routes[i]];
          var pill = document.createElement('a');
          pill.className = 'sg-tour-link';
          pill.textContent = '▶ Tour';
          pill.href = '?tour=' + encodeURIComponent(id);
          pill.title = 'Quinn walks you through this completed job';
          pill.addEventListener('click', function (e) {
            if (window.somaGuide && typeof window.somaGuide.startWalkthrough === 'function') {
              e.preventDefault();
              window.somaGuide.startWalkthrough(id);
            } // else: fall through to the ?tour= href (engine starts it on load)
          });
          a.insertAdjacentElement('afterend', pill);
          return;
        }
      }
    });
  });
})();
"""

TOUR_CSS = """
.sg-tour-link { display: inline-block; margin-left: 8px; padding: 1px 9px; border-radius: 11px;
                font-size: 11px; font-weight: 600; text-decoration: none; background: #22344a;
                color: #9cc7ff; border: 1px solid #2a5adf; vertical-align: middle; }
.sg-tour-link:hover { background: #2a4160; }
/* Keep the engine's block highlight readable on the dark review theme. */
.block-wrap.sg-highlight { background: #1c2a3f; box-shadow: 0 0 0 2px #4f8cff; }
/* Quinn has no ElevenLabs agent on this surface — hide voice-mode affordances
   (the engine renders them unconditionally; clicking would dead-end). */
#soma-guide .sg-btn-voice, #soma-guide .sg-io-voice { display: none !important; }
"""


def _build_knowledge(walkthroughs):
    """Small knowledge pack for Quinn's text chat: one line per completed job."""
    lines = ['COMPLETED JOBS (newest first):']
    for t in walkthroughs:
        live = ''
        for s in t['steps']:
            m = re.search(r'(https?://\S+)', s.get('instruction', ''))
            if s['id'] == 's3-live' and m:
                live = ' Live: ' + m.group(1)
        what = t['steps'][0]['narration'].replace('Here’s what got done: ', '')
        lines.append('- %s — %s%s' % (t['label'], what, live))
    return '\n'.join(lines)


def tour_page_assets(route_path, url_prefix=''):
    """(head_html, body_html) to inject into a rendered review page, or None if
    this route doesn't carry tours. url_prefix is the workspace prefix ('' for
    estate). Tours are estate-only today; non-empty prefixes get nothing."""
    if url_prefix:
        return None
    if not is_tour_route(route_path):
        return None
    walkthroughs = build_walkthroughs()
    if not walkthroughs:
        return None

    index_map = {}
    for t in walkthroughs:
        index_map['/page/' + t.pop('_route')] = t['id']

    n = len(walkthroughs)
    offer = ('Want the tour of what got done? %d completed job%s below — pick one and '
             'I’ll walk you through it.' % (n, '' if n == 1 else 's'))
    config = {
        'persona': {
            'name': 'Quinn',
            'id': 'soma-review-quinn',
            'avatar': '\U0001F50E',
            'greeting': 'I’m Quinn — I review completed work. ' + offer,
            'shortGreeting': offer,
            'walkthroughDone': 'That’s the tour. Comment on any block if something '
                               'needs another pass — it lands straight in Dee’s queue.',
        },
        # No voiceAgentId / ttsProxyUrl: text-mode tours only (voice UI hidden
        # via TOUR_CSS). Text chat answers from the completions knowledge pack
        # via the same public VPS inference endpoint Legends' Bill uses.
        'inferenceUrl': 'https://vpsmikewolf.duckdns.org/infer/ask',
        'knowledge': (
            'SCOPE: You are Quinn, the reviewer on Mike Wolf’s local soma-review '
            'surface. You walk Mike through completed jobs — what was done, the '
            'receipts, and where it’s live. Answer only from the completed-jobs '
            'list below; for anything else, suggest opening the completion page '
            'or leaving a comment for Dee.\n\n' + _build_knowledge(walkthroughs)
        ),
        'cleanOnClose': True,
        'walkthroughs': walkthroughs,
    }

    head = ('<link rel="stylesheet" href="%s">\n<style>%s</style>' % (GUIDE_CSS_URL, TOUR_CSS))
    body = (
        '<script>window.SomaGuideConfig = %s;\n'
        'window.__SOMA_TOUR_INDEX__ = %s;</script>\n'
        '<script type="module" src="%s"></script>\n'
        '<script>%s</script>'
        % (_json_for_script(config),
           _json_for_script(index_map),
           GUIDE_JS_URL,
           HELPER_JS)
    )
    return head, body
