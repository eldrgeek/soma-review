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
# Estate workspace sidecars (tours are estate-only; see tour_page_assets guard).
FEEDBACK_DIR = os.path.expanduser('~/Projects/_estate/review-feedback')
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
_DEMO_RE = re.compile(r'\*\*Demo:?\*\*:?\s*<?(https?://\S+?)>?(?:\s|$)', re.IGNORECASE)
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

    # Demo link (optional `## Demo` convention, completions/README.md): a
    # `**Demo:** <product-url>?sg_tour=<id>` line. The URL deep-links the REAL
    # product page; the product site's guide config starts that walkthrough
    # from the sg_tour param. The block itself carries the autolinked URL, so
    # highlighting it gives Mike a real click target (new tab).
    demo_anchor = None
    demo_url = None
    for b in blocks:
        m = _DEMO_RE.search(b['text'])
        if m:
            demo_anchor = b['anchor']
            demo_url = m.group(1).rstrip('.,)')
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
    if demo_anchor and demo_url:
        # Live-demo step: the completion declared a demo — deep-link the real
        # product page with the sg_tour param and the walkthrough continues
        # THERE (registered in that site's guide config).
        steps.append({
            'id': 's3-demo',
            'target': '[data-anchor="%s"]' % demo_anchor,
            'page': page,
            'label': 'Live demo',
            'narration': 'Now the demo — the highlighted link opens the actual product page '
                         'in a new tab, and the walkthrough picks up right there on the real thing.',
            'instruction': 'Click the Demo link (opens in a new tab; the tour starts there '
                           'automatically): %s' % demo_url,
        })
    elif live_anchor and live_url:
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

    # Verdict step — every completion tour ends here. HELPER_JS renders the
    # ✅ Approve / ✏️ Recommend-changes buttons into the walkthrough panel when
    # the tour is on a step whose id is 's9-verdict' (see sg-verdict-bar).
    steps.append({
        'id': 's9-verdict',
        'target': '[data-anchor="%s"]' % blocks[-1]['anchor'],
        'page': page,
        'label': 'Your verdict',
        'narration': 'Your call. Approve it and it comes off the review list, or recommend '
                     'changes and I’ll file it straight into the development loop.',
        'instruction': 'Pick one below: ✅ Approve, or ✏️ Recommend changes.',
    })

    return {
        'id': 'tour-' + _slug(re.sub(r'\.md$', '', name)),
        'label': title,
        'keywords': [],
        'steps': steps,
        '_route': route,  # stripped before serialization; used for the index map
    }


def _sidecar_path(route):
    """Estate-workspace sidecar for a route (matches server.py sidecar_path
    for the estate workspace: route with '/' -> '_', + '.jsonl')."""
    return os.path.join(FEEDBACK_DIR, route.replace('/', '_') + '.jsonl')


def review_state():
    """{route: {'verdict': 'approve'|'recommend-changes', 'timestamp': ts}}
    for every completion page that has received a verdict. Derived per request
    from the sidecar JSONL — the .md files are never the state store. The
    LATEST non-deleted verdict row wins; soft-deleting a verdict row puts the
    item back in the unreviewed inbox (verified behavior, relied on by
    COMPLETED.md's Reviewed section)."""
    state = {}
    for name in list_completion_files():
        route = COMPLETIONS_ROUTE_PREFIX + name
        path = _sidecar_path(route)
        if not os.path.isfile(path):
            continue
        latest = None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if (row.get('type') == 'verdict' and not row.get('deleted')
                            and row.get('verdict') in ('approve', 'recommend-changes')):
                        if latest is None or row.get('timestamp', '') >= latest.get('timestamp', ''):
                            latest = row
        except OSError:
            continue
        if latest:
            state[route] = {'verdict': latest['verdict'],
                            'timestamp': latest.get('timestamp', '')}
    return state


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
  var REVIEWED = window.__SOMA_REVIEW_STATE__ || {};
  // Reverse map: tour-id -> '/page/estate/completions/...' page path.
  var REV = {};
  Object.keys(IDX).forEach(function (k) { REV[IDX[k]] = k; });

  function apiRoute(pagePath) {  // '/page/estate/...' -> 'estate/...'
    return pagePath.replace(/^\/page\//, '');
  }

  /* ── Verdict capture: ✅ Approve / ✏️ Recommend changes on the tour's final
     step. The engine has no custom-button step API, so we watch the wt panel:
     after every engine render, if the current step is s9-verdict, mount the
     bar into the walkthrough panel. Reuses the existing /api/comments verdict
     machinery (type:"verdict") — no new storage. ── */
  var pendingRecommend = null;

  function currentTour(g) { return g && g.wt ? g.wt.id : null; }

  function postVerdict(route, tourLabel, verdict, text) {
    return fetch('/api/comments', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        page: route, anchor: null, snapshot: tourLabel,
        type: 'verdict', verdict: verdict,
        row_id: 'completion:' + route,
        text: text || ('Verdict: ' + verdict)
      })
    }).then(function (r) {
      if (!r.ok) throw new Error('verdict post failed: ' + r.status);
      return r.json();
    });
  }

  function mountVerdictBar(g) {
    var panel = document.querySelector('#soma-guide .sg-wt-ui');
    if (!panel || panel.querySelector('.sg-verdict-bar')) return;
    var tourId = currentTour(g);
    var pagePath = REV[tourId];
    if (!pagePath) return;
    var route = apiRoute(pagePath);
    var label = '';
    try { label = (g.cfg.walkthroughs.filter(function (w) { return w.id === tourId; })[0] || {}).label || ''; } catch (e) {}

    var bar = document.createElement('div');
    bar.className = 'sg-verdict-bar';
    var ok = document.createElement('button');
    ok.className = 'sg-verdict-approve';
    ok.textContent = '✅ Approve';
    var rec = document.createElement('button');
    rec.className = 'sg-verdict-recommend';
    rec.textContent = '✏️ Recommend changes';
    var note = document.createElement('div');
    note.className = 'sg-verdict-note';
    bar.appendChild(ok); bar.appendChild(rec); bar.appendChild(note);

    ok.addEventListener('click', function () {
      ok.disabled = true; rec.disabled = true;
      postVerdict(route, label, 'approve', 'Approved via Quinn tour').then(function () {
        note.textContent = '✅ Approved — off the review list.';
      }).catch(function (e) {
        ok.disabled = false; rec.disabled = false;
        note.textContent = 'Could not record the verdict (' + e.message + ') — try again.';
      });
    });

    rec.addEventListener('click', function () {
      pendingRecommend = { route: route, label: label };
      note.textContent = 'Type what needs to change in the Page-discussion box below, ' +
                         'then Save — I’ll file it as a Development Request.';
      var box = document.querySelector('#page-comment-box textarea');
      if (box) { box.scrollIntoView({ behavior: 'smooth', block: 'center' }); box.focus(); }
    });

    var instr = panel.querySelector('.sg-wt-instruction');
    if (instr && instr.parentNode) instr.parentNode.insertBefore(bar, instr.nextSibling);
    else panel.appendChild(bar);
  }

  function unmountVerdictBar() {
    var bar = document.querySelector('#soma-guide .sg-verdict-bar');
    if (bar) bar.remove();
  }

  function patchEngine(g) {
    if (g.__sgVerdictPatched) return;
    g.__sgVerdictPatched = true;
    var origRender = g._renderWtStep.bind(g);
    g._renderWtStep = function () {
      origRender();
      var step = null;
      try { step = this._wtCurrentStep(); } catch (e) {}
      if (step && step.id === 's9-verdict') {
        // Park auto-play on the verdict step: the tour must not auto-finish
        // out from under the buttons. Mike ends it with "Finish ✓".
        this._autoStopped = true;
        mountVerdictBar(this);
      } else {
        unmountVerdictBar();
      }
    };
  }

  // Comment machinery hook: PAGE_JS dispatches 'soma-comment-saved' after any
  // successful comment POST. If a recommend-changes flow is pending, that
  // comment's text becomes the Development Request narrative.
  document.addEventListener('soma-comment-saved', function (ev) {
    if (!pendingRecommend) return;
    var c = ev.detail || {};
    if (c.type !== 'comment' || !c.text) return;
    var p = pendingRecommend; pendingRecommend = null;
    postVerdict(p.route, p.label, 'recommend-changes', c.text).then(function (row) {
      var note = document.querySelector('#soma-guide .sg-verdict-note');
      var dr = row && row._dr;
      if (note) {
        note.textContent = dr && dr.card
          ? '✏️ Filed — Development Request routed to a board card (' + dr.card + ').'
          : '✏️ Changes requested — recorded' + (dr && dr.error ? ' (DR routing hit a snag: ' + dr.error + ')' : '') + '.';
      }
    }).catch(function () { pendingRecommend = p; /* let Mike retry by saving again */ });
  });

  function whenGuideReady(cb, tries) {
    tries = tries || 0;
    if (window.somaGuide && typeof window.somaGuide.startWalkthrough === 'function') { cb(); return; }
    if (tries < 80) setTimeout(function () { whenGuideReady(cb, tries + 1); }, 250);
  }

  // Verdict-bar engine patch: as soon as the engine exists, wrap its step
  // renderer so the final tour step mounts the Approve/Recommend buttons.
  whenGuideReady(function () { patchEngine(window.somaGuide); });

  // Deep link: ?tour=<id> starts that tour once the engine is up.
  var tourParam = null;
  try { tourParam = new URLSearchParams(location.search).get('tour'); } catch (e) {}
  if (tourParam) {
    // Kill the first-visit auto-offer race: 500ms after init the engine opens
    // the idle offer panel for un-introduced users (_openIdle), which would
    // stomp the walkthrough mode the deep link just started. A deep link IS
    // the introduction — consume the introduce-once gate up front. (This
    // classic script runs before the engine's module script by load order.)
    try { localStorage.setItem('soma-guide:soma-review-quinn:introduced', '1'); } catch (e) {}
    whenGuideReady(function () { window.somaGuide.startWalkthrough(tourParam); });
  }

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

    /* ── Review inbox: COMPLETED.md is the UNREVIEWED list. Any completion
       that has a verdict (approve OR recommend-changes) moves out of the
       main list into a collapsed "Reviewed" section at the bottom, showing
       the verdict it got. State comes from __SOMA_REVIEW_STATE__ (derived
       server-side from the sidecar JSONL at render time — the .md file is
       never edited as state). Runs AFTER pill injection so the ▶ Tour pill
       travels with the item: reviewed items stay tourable. ── */
    if (!/COMPLETED\.md$/.test(location.pathname)) return;
    var reviewedRoutes = Object.keys(REVIEWED);
    if (!reviewedRoutes.length) return;

    var main = document.querySelector('.main');
    var moved = [];
    document.querySelectorAll('.main .block-body li').forEach(function (li) {
      var a = li.querySelector('a[href]');
      if (!a) return;
      var href = a.getAttribute('href') || '';
      for (var i = 0; i < reviewedRoutes.length; i++) {
        if (href.indexOf(reviewedRoutes[i]) !== -1) {
          moved.push({ li: li, verdict: REVIEWED[reviewedRoutes[i]].verdict });
          return;
        }
      }
    });
    if (!moved.length || !main) return;

    var details = document.createElement('details');
    details.className = 'sg-reviewed';
    var summary = document.createElement('summary');
    summary.textContent = 'Reviewed (' + moved.length + ') — verdict cast, off the inbox';
    details.appendChild(summary);
    var ul = document.createElement('ul');
    details.appendChild(ul);
    moved.forEach(function (m) {
      var badge = document.createElement('span');
      badge.className = 'sg-reviewed-badge sg-reviewed-' + m.verdict;
      badge.textContent = m.verdict === 'approve' ? '✅ approved' : '✏️ changes requested';
      m.li.insertBefore(badge, m.li.firstChild);
      ul.appendChild(m.li);   // moves the node (tour pill + links travel with it)
    });
    var discussion = document.querySelector('.page-discussion');
    if (discussion) main.insertBefore(details, discussion);
    else main.appendChild(details);
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
/* Verdict bar on the tour's final step. */
.sg-verdict-bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 10px; }
.sg-verdict-bar button { border-radius: 8px; padding: 6px 14px; font-size: 13px; font-weight: 600;
                         cursor: pointer; border: 1px solid #2b3140; background: #1c2634; color: #d8dee9; }
.sg-verdict-approve:hover { border-color: #7ee08a; color: #9cf0ac; }
.sg-verdict-recommend:hover { border-color: #e6c26a; color: #f0c674; }
.sg-verdict-bar button:disabled { opacity: 0.5; cursor: default; }
.sg-verdict-note { flex-basis: 100%; font-size: 12px; color: #9aa3b5; }
/* Reviewed section on COMPLETED.md (the review inbox). */
.sg-reviewed { margin-top: 40px; border-top: 1px solid #2b3140; padding-top: 14px; }
.sg-reviewed summary { cursor: pointer; color: #9aa3b5; font-size: 14px; font-weight: 600; }
.sg-reviewed ul { margin-top: 10px; }
.sg-reviewed li { margin: 6px 0; }
.sg-reviewed-badge { display: inline-block; margin-right: 8px; padding: 1px 8px; border-radius: 10px;
                     font-size: 11px; font-weight: 600; }
.sg-reviewed-approve { background: #1f4a2a; color: #9cf0ac; }
.sg-reviewed-recommend-changes { background: #4a3f1f; color: #e6c26a; }
/* Verdict-badge colors for the new completion verdicts in comment threads. */
.badge-verdict-approve { background: #1f4a2a; color: #9cf0ac; }
.badge-verdict-recommend-changes { background: #4a3f1f; color: #e6c26a; }
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
    # Review-inbox state: which completions already carry a verdict (keyed by
    # '/page/<route>' to match rendered hrefs, same convention as index_map).
    reviewed = {}
    try:
        for route, st in review_state().items():
            reviewed['/page/' + route] = st
    except Exception:  # noqa: BLE001 — bad sidecar must not break tour assets
        reviewed = {}

    body = (
        '<script>window.SomaGuideConfig = %s;\n'
        'window.__SOMA_TOUR_INDEX__ = %s;\n'
        'window.__SOMA_REVIEW_STATE__ = %s;</script>\n'
        '<script type="module" src="%s"></script>\n'
        '<script>%s</script>'
        % (_json_for_script(config),
           _json_for_script(index_map),
           _json_for_script(reviewed),
           GUIDE_JS_URL,
           HELPER_JS)
    )
    return head, body
