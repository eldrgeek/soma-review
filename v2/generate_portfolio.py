#!/usr/bin/env python3
"""
generate_portfolio.py — composes _estate/PORTFOLIO.md, Mike's cancel/restart
triage board (stdlib only).

Run manually: /opt/homebrew/bin/python3 v2/generate_portfolio.py
Wired into: same nightly hygiene step as generate_board.py (see
scripts/nightly-estate-hygiene.sh) — cheap, safe when a source is missing.

Sources:
  1. PROJECT-REGISTRY.json — every project entry (name, district/home,
     lifecycle, git recency via `dirty`/`branch`; no true "last commit date"
     field exists in the registry, so recency is reported as
     git=yes/no + dirty + branch — best available signal without shelling
     out to `git log` per repo, which would make this script slow/heavy).
  2. _estate/PRODUCTIVITY-OPPORTUNITIES-2026-07-02.md — the tiered backlog
     tables (Tier 1/2/3 + Watch items) parsed as "ideas/open items" rows.
  3. _estate/audit-2026-07/raw-fleet-output-2026-07-01.json — best-effort:
     the directive expected a `backlog.items` array; that shape does not
     exist in the file as shipped (verified: top-level keys are
     summary/agentCount/logs/result/workflowProgress/totalTokens/
     totalToolCalls, and result has no `backlog` key either). Parsed
     defensively — if `backlog.items` (or `result.backlog.items`) appears in
     a future regeneration of this file, it's picked up; otherwise this
     source contributes 0 rows without erroring.
  4. SOMA/SOMA-STATE.md §5 ("What's broken or missing pieces") and §6
     ("What's designed but unbuilt") — §5 is `### heading` blocks, §6 is a
     markdown table; both parsed best-effort.

Grouping:
  - Active (skip verdicts) — lifecycle in {active, active(live), canonical,
    canonical(docs), infra, infra-stable}.
  - Parked+incubating+dormant (verdict needed) — lifecycle in {parked,
    incubating, archive-lean, archive-lean/incubating, fork-dup,
    fork-worktree, unmapped}. (No literal "dormant" tag exists in the
    registry today — parked/incubating/archive-lean-ish tags are the closest
    real signal; see registry_lifecycle_notes below.)
  - Ideas backlog (verdict needed) — productivity-opportunities rows +
    SOMA-STATE §5/§6 rows + any raw-fleet backlog.items rows.

Each verdict-needed row gets a `[[VERDICT:<row-id>]]` token in its rightmost
cell, rendered by mdblocks/server.py as four buttons (keep/restart/cancel/
later) that POST a `{type:"verdict", verdict:...}` comment to the existing
comment API — no new storage, just a comment with a verdict field.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

PROJECTS_ROOT = os.path.expanduser('~/Projects')
REGISTRY_JSON = os.path.join(PROJECTS_ROOT, 'PROJECT-REGISTRY.json')
PRODUCTIVITY_MD = os.path.join(PROJECTS_ROOT, '_estate', 'PRODUCTIVITY-OPPORTUNITIES-2026-07-02.md')
RAW_FLEET_JSON = os.path.join(PROJECTS_ROOT, '_estate', 'audit-2026-07', 'raw-fleet-output-2026-07-01.json')
SOMA_STATE_MD = os.path.join(PROJECTS_ROOT, 'SOMA', 'SOMA-STATE.md')
OUT_PATH = os.path.join(PROJECTS_ROOT, '_estate', 'PORTFOLIO.md')

ACTIVE_LIFECYCLES = {'active', 'active(live)', 'canonical', 'canonical(docs)', 'infra', 'infra-stable'}
PARKED_LIFECYCLES = {'parked', 'incubating', 'archive-lean', 'archive-lean/incubating',
                      'fork-dup', 'fork-worktree', 'unmapped'}
# archive/vendor: not "active" and not really "needs a Mike verdict" either
# (already decided/out of scope) — listed in a third small bucket, no verdict UI.
SKIP_LIFECYCLES = {'archive', 'vendor'}


def safe(fn, default, label):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        print(f"[generate_portfolio] WARN: {label} failed: {e}", file=sys.stderr)
        return default


# --- Source 1: PROJECT-REGISTRY.json -----------------------------------------

def load_registry_rows():
    with open(REGISTRY_JSON, 'r', encoding='utf-8') as f:
        data = json.load(f)
    rows = []
    for e in data.get('projects', []):
        rows.append({
            'name': e.get('name', '?'),
            'home': e.get('home', ''),
            'path': e.get('path', ''),
            'lifecycle': e.get('lifecycle', 'unmapped'),
            'git': e.get('git', False),
            'dirty': e.get('dirty'),
            'branch': e.get('branch'),
            'summary': (e.get('summary') or '').split('\n')[0][:160],
        })
    return rows


def git_recency_str(row):
    if not row['git']:
        return 'not a git repo'
    dirty = row.get('dirty')
    branch = row.get('branch') or '?'
    dirty_str = 'dirty' if dirty else ('clean' if dirty is False else 'unknown')
    return f'branch `{branch}`, {dirty_str}'


# --- Source 2: PRODUCTIVITY-OPPORTUNITIES-2026-07-02.md tables --------------

_TABLE_ROW_RE = re.compile(r'^\|(.+)\|\s*$')


def parse_productivity_backlog():
    """Parse the Tier 1 table (has a header row with '#', 'Item', 'Evidence',
    'Effort') plus the Tier 2/3 bullet-list sections and Watch items, as
    ideas/open-items rows. Best-effort line scan, not a full markdown parser
    (this file is small and its shape is known)."""
    if not os.path.isfile(PRODUCTIVITY_MD):
        return []
    with open(PRODUCTIVITY_MD, 'r', encoding='utf-8') as f:
        lines = f.read().split('\n')

    rows = []
    current_tier = None
    in_table = False
    header_seen = False
    bullet_sections = ('Tier 2', 'Tier 3', 'Watch items')

    def in_bullet_section():
        return bool(current_tier) and current_tier.startswith(bullet_sections)

    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith('## '):
            current_tier = stripped[3:].strip()
            in_table = False
            header_seen = False
            i += 1
            continue
        m = _TABLE_ROW_RE.match(stripped)
        if m:
            cells = [c.strip() for c in m.group(1).split('|')]
            if not header_seen:
                header_seen = True
                in_table = True
                i += 1
                continue
            if set(''.join(cells)) <= set('-: '):
                i += 1
                continue  # separator row
            if in_table and len(cells) >= 2:
                # Tier 1 shape: # | Item | Evidence | Effort
                item_text = cells[1] if len(cells) > 1 else cells[0]
                evidence = cells[2] if len(cells) > 2 else ''
                rows.append({
                    'source': f'productivity-opportunities ({current_tier or "?"})',
                    'text': item_text,
                    'evidence': evidence,
                })
            i += 1
            continue
        in_table = False
        if stripped.startswith('- ') and in_bullet_section():
            # Join wrapped continuation lines: a markdown paragraph-wrapped bullet
            # continues on the next line(s) with no leading '-' and non-blank text,
            # until a blank line, a new bullet, or a new heading ends it.
            bullet_lines = [stripped[2:].strip()]
            i += 1
            while i < n:
                cont = lines[i].strip()
                if not cont or cont.startswith('- ') or cont.startswith('#'):
                    break
                bullet_lines.append(cont)
                i += 1
            rows.append({
                'source': f'productivity-opportunities ({current_tier})',
                'text': ' '.join(bullet_lines),
                'evidence': '',
            })
            continue
        i += 1
    return rows


# --- Source 3: raw-fleet-output backlog.items (best-effort; likely absent) --

def parse_raw_fleet_backlog():
    if not os.path.isfile(RAW_FLEET_JSON):
        return []
    with open(RAW_FLEET_JSON, 'r', encoding='utf-8') as f:
        data = json.load(f)
    items = None
    if isinstance(data, dict):
        if isinstance(data.get('backlog'), dict):
            items = data['backlog'].get('items')
        if items is None and isinstance(data.get('result'), dict):
            result = data['result']
            if isinstance(result.get('backlog'), dict):
                items = result['backlog'].get('items')
    if not isinstance(items, list):
        return []
    rows = []
    for it in items:
        if isinstance(it, dict):
            text = it.get('title') or it.get('text') or it.get('name') or json.dumps(it)[:120]
        else:
            text = str(it)
        rows.append({'source': 'raw-fleet-output backlog.items', 'text': text, 'evidence': ''})
    return rows


# --- Source 4: SOMA-STATE.md §5/§6 -------------------------------------------

def parse_soma_state_backlog():
    if not os.path.isfile(SOMA_STATE_MD):
        return []
    with open(SOMA_STATE_MD, 'r', encoding='utf-8') as f:
        lines = f.read().split('\n')

    rows = []
    section = None  # '5' or '6' or None
    cur_heading = None

    for line in lines:
        stripped = line.strip()
        if stripped.startswith('## §5.'):
            section = '5'
            continue
        if stripped.startswith('## §6.'):
            section = '6'
            continue
        if stripped.startswith('## §') and not (stripped.startswith('## §5.') or stripped.startswith('## §6.')):
            section = None
            continue

        if section == '5' and stripped.startswith('### '):
            cur_heading = stripped[4:].strip()
            # Skip entries whose heading itself signals "already fixed/done" —
            # best-effort filter so the ideas backlog doesn't list closed items.
            if re.search(r'\bFIXED\b|\bDONE\b|\bRUNNING\b', cur_heading):
                cur_heading = None  # suppress body lines under this heading
                continue
            rows.append({'source': 'SOMA-STATE §5 (broken/missing)', 'text': cur_heading, 'evidence': ''})

        if section == '6':
            m = _TABLE_ROW_RE.match(stripped)
            if m:
                cells = [c.strip() for c in m.group(1).split('|')]
                if set(''.join(cells)) <= set('-: '):
                    continue
                if cells and cells[0].lower() in ('item',):
                    continue
                if len(cells) >= 3:
                    rows.append({
                        'source': 'SOMA-STATE §6 (unbuilt backlog)',
                        'text': cells[0],
                        'evidence': f'spec: {cells[1]}, priority: {cells[2]}',
                    })
    return rows


# --- Render -------------------------------------------------------------------

def _row_id(prefix, text):
    slug = re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')[:60]
    return f'{prefix}-{slug}' if slug else prefix


def render_portfolio(registry_rows, ideas_rows):
    active = [r for r in registry_rows if r['lifecycle'] in ACTIVE_LIFECYCLES]
    parked = [r for r in registry_rows if r['lifecycle'] in PARKED_LIFECYCLES]
    skipped = [r for r in registry_rows if r['lifecycle'] in SKIP_LIFECYCLES]
    other = [r for r in registry_rows
             if r['lifecycle'] not in ACTIVE_LIFECYCLES
             and r['lifecycle'] not in PARKED_LIFECYCLES
             and r['lifecycle'] not in SKIP_LIFECYCLES]
    # Anything not explicitly classified still needs a verdict — safer default
    # than silently dropping it.
    parked = parked + other

    lines = []
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    lines.append('# Portfolio')
    lines.append('')
    lines.append(f'_Generated {ts} by `v2/generate_portfolio.py`. Verdict buttons '
                  f'(keep / restart / cancel / later) post a structured comment to '
                  f'the row — no separate storage, just a `type:"verdict"` comment._')
    lines.append('')

    lines.append('## Active (no verdict needed)')
    lines.append('')
    lines.append('| Project | Home | Lifecycle | Git |')
    lines.append('|---|---|---|---|')
    for r in sorted(active, key=lambda r: r['name'].lower()):
        lines.append(f"| **{r['name']}** | `{r['home']}` | {r['lifecycle']} | {git_recency_str(r)} |")
    lines.append('')

    lines.append('## Parked / incubating / dormant — verdict needed')
    lines.append('')
    lines.append('| Project | Home | Lifecycle | Git | Evidence | Verdict |')
    lines.append('|---|---|---|---|---|---|')
    for r in sorted(parked, key=lambda r: r['name'].lower()):
        row_id = _row_id('project', r['name'])
        evidence = r['summary'] or '_(no summary in registry)_'
        lines.append(
            f"| **{r['name']}** | `{r['home']}` | {r['lifecycle']} | {git_recency_str(r)} "
            f"| {evidence} | [[VERDICT:{row_id}]] |"
        )
    lines.append('')

    if skipped:
        lines.append(f'_{len(skipped)} project(s) already archived/vendor (out of scope for '
                      f're-verdicting): ' + ', '.join(sorted(r["name"] for r in skipped)) + '_')
        lines.append('')

    lines.append('## Ideas backlog — verdict needed')
    lines.append('')
    lines.append('| Idea / open item | Source | Evidence | Verdict |')
    lines.append('|---|---|---|---|')
    seen_texts = set()
    for r in ideas_rows:
        text = r['text']
        if not text or text in seen_texts:
            continue
        seen_texts.add(text)
        row_id = _row_id('idea', text)
        evidence = r['evidence'] or '—'
        lines.append(f"| {text} | {r['source']} | {evidence} | [[VERDICT:{row_id}]] |")
    lines.append('')

    lines.append('## Streams digest')
    lines.append('')
    lines.append(f'- Registry projects: {len(registry_rows)} total — {len(active)} active, '
                  f'{len(parked)} parked/incubating/other, {len(skipped)} archived/vendor')
    lines.append(f'- Ideas backlog rows: {len(seen_texts)} (deduped from {len(ideas_rows)} raw rows)')
    lines.append('')

    return '\n'.join(lines)


def main():
    registry_rows = safe(load_registry_rows, [], 'PROJECT-REGISTRY.json load')
    ideas_rows = []
    ideas_rows += safe(parse_productivity_backlog, [], 'productivity-opportunities parse')
    ideas_rows += safe(parse_raw_fleet_backlog, [], 'raw-fleet-output backlog parse')
    ideas_rows += safe(parse_soma_state_backlog, [], 'SOMA-STATE §5/§6 parse')

    portfolio_md = render_portfolio(registry_rows, ideas_rows)

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        f.write(portfolio_md)

    print(f'Portfolio written to {OUT_PATH}')
    print(f'  registry rows: {len(registry_rows)}')
    print(f'  ideas rows (raw): {len(ideas_rows)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
