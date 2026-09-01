#!/usr/bin/env python3
"""
generate_board.py — composes _estate/BOARD.md from live streams (stdlib only).

Run manually: /opt/homebrew/bin/python3 v2/generate_board.py
Wired into: scripts/nightly-estate-hygiene.sh (nightly) and POST /api/board/regenerate
(soma-review's "Regenerate board" button, on-demand).

Streams (each wrapped so a missing/broken source degrades to a skipped section,
never a crash):
  1. ESTATE.md changelog entries dated in the last 48h.
  2. New files in ~/Projects/SOMA/audits/ (mtime < 48h).
  3. Memory index (~/.claude/projects/-Users-mikewolf-Projects/memory/MEMORY.md)
     diffed against a cached copy in _estate/board-state/.
  4. Open review comments (status queued/seen, not done/deleted) across every
     workspace's _estate/review-feedback/**/*.jsonl.
  5. Status lines: _estate/hygiene/LATEST-STATUS.txt + second-brain freshness
     summary (best-effort, skipped if the script errors or is slow).
  6. Board inbox: ~/Projects/SOMA/board/inbox/*.{md,json} — cards from other
     surfaces/conversations. Processed cards move to inbox/processed/.
  7. Today's completions: _estate/completions/YYYY-MM-DD-*.md pages dated
     today — rendered as the "Done today" strip at the top of the board so
     completion state is visible at the front door (Mike missed COMPLETED.md
     entirely before this existed).

Sections rendered, in order: Done-today strip, Needs Mike, Shipped/changed
(48h), Fleet status, Streams digest.

Idempotent: re-running regenerates BOARD.md from scratch each time (except the
board-state cache, which advances forward, and inbox cards, which move to
processed/ once read — re-running won't re-list an already-processed card).
"""
import glob
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import blockmap
from mdblocks import parse_markdown

PROJECTS_ROOT = os.path.expanduser('~/Projects')
ESTATE_MD = os.path.join(PROJECTS_ROOT, 'ESTATE.md')
AUDITS_DIR = os.path.join(PROJECTS_ROOT, 'SOMA', 'audits')
MEMORY_MD = os.path.expanduser(
    '~/.claude/projects/-Users-mikewolf-Projects/memory/MEMORY.md'
)
BOARD_STATE_DIR = os.path.join(PROJECTS_ROOT, '_estate', 'board-state')
MEMORY_CACHE = os.path.join(BOARD_STATE_DIR, 'MEMORY.md.cache')
REVIEW_FEEDBACK_ROOT = os.path.join(PROJECTS_ROOT, '_estate', 'review-feedback')
HYGIENE_STATUS = os.path.join(PROJECTS_ROOT, '_estate', 'hygiene', 'LATEST-STATUS.txt')
FRESHNESS_SCRIPT = os.path.join(PROJECTS_ROOT, 'second-brain', 'scripts', 'freshness_report.py')
BOARD_INBOX = os.path.join(PROJECTS_ROOT, 'SOMA', 'board', 'inbox')
BOARD_INBOX_PROCESSED = os.path.join(BOARD_INBOX, 'processed')
COMPLETIONS_DIR = os.path.join(PROJECTS_ROOT, '_estate', 'completions')
OUT_PATH = os.path.join(PROJECTS_ROOT, '_estate', 'BOARD.md')
WORKSPACES_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'workspaces.json')

WINDOW_HOURS = 48


def now_utc():
    return datetime.now(timezone.utc)


def safe(fn, default, label):
    """Run fn(); on any exception, print a note to stderr and return default so
    one broken source never kills the whole board."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        print(f"[generate_board] WARN: {label} failed: {e}", file=sys.stderr)
        return default


# --- Stream 1: ESTATE.md changelog, last 48h ---------------------------------

_CHANGELOG_HEADER_RE = re.compile(r'^### (\d{4}-\d{2}-\d{2})')


def parse_estate_changelog():
    """Return list of (date_str, header_text, body_lines[]) for changelog entries
    dated within the last WINDOW_HOURS."""
    if not os.path.isfile(ESTATE_MD):
        return []
    with open(ESTATE_MD, 'r', encoding='utf-8') as f:
        lines = f.read().split('\n')

    cutoff = now_utc() - timedelta(hours=WINDOW_HOURS)
    entries = []
    in_changelog = False
    cur_date = None
    cur_header = None
    cur_body = []

    def flush():
        if cur_header is not None:
            entries.append((cur_date, cur_header, cur_body[:]))

    for line in lines:
        if line.strip() == '## Changelog':
            in_changelog = True
            continue
        if not in_changelog:
            continue
        m = _CHANGELOG_HEADER_RE.match(line)
        if m:
            flush()
            cur_date = m.group(1)
            cur_header = line[4:].strip()  # strip '### '
            cur_body = []
        elif cur_header is not None:
            cur_body.append(line)
    flush()

    recent = []
    for date_str, header, body in entries:
        try:
            d = datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        # A dated entry could be from any time that day; be generous (include
        # the whole day plus WINDOW_HOURS) rather than clipping at midnight.
        if d >= cutoff - timedelta(hours=24):
            recent.append((date_str, header, body))
    return recent


# --- Stream 2: new audit files, mtime < 48h ----------------------------------

def scan_recent_audits():
    if not os.path.isdir(AUDITS_DIR):
        return []
    cutoff_ts = time.time() - WINDOW_HOURS * 3600
    out = []
    for name in sorted(os.listdir(AUDITS_DIR)):
        path = os.path.join(AUDITS_DIR, name)
        if not os.path.isfile(path):
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime < cutoff_ts:
            continue
        first_heading = name
        if name.endswith('.md'):
            try:
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith('#'):
                            first_heading = line.lstrip('#').strip()
                            break
            except OSError:
                pass
        out.append((name, first_heading, mtime))
    out.sort(key=lambda t: t[2], reverse=True)
    return out


# --- Stream 3: memory index diff ---------------------------------------------

def diff_memory_index():
    """Compare current MEMORY.md against the cached copy in board-state/.
    Returns list of added/changed lines (simple line-set diff, no need for a
    real differ at this file size). Updates the cache after reading."""
    if not os.path.isfile(MEMORY_MD):
        return []
    with open(MEMORY_MD, 'r', encoding='utf-8') as f:
        current_lines = f.read().split('\n')

    cached_lines = []
    if os.path.isfile(MEMORY_CACHE):
        with open(MEMORY_CACHE, 'r', encoding='utf-8') as f:
            cached_lines = f.read().split('\n')

    cached_set = set(cached_lines)
    changed = [
        line for line in current_lines
        if line.strip().startswith('- [') and line not in cached_set
    ]

    os.makedirs(BOARD_STATE_DIR, exist_ok=True)
    with open(MEMORY_MD, 'r', encoding='utf-8') as f:
        content = f.read()
    with open(MEMORY_CACHE, 'w', encoding='utf-8') as f:
        f.write(content)

    return changed


# --- Stream 4: open review comments ------------------------------------------

def _route_source(page, workspace):
    if not page or '/' not in page:
        return None
    try:
        with open(WORKSPACES_CONFIG, 'r', encoding='utf-8') as handle:
            config = json.load(handle)[workspace]
    except (OSError, ValueError, KeyError):
        return None
    prefix, rest = page.split('/', 1)
    for route_prefix, relative_root in config.get('roots', []):
        if route_prefix == prefix:
            candidate = os.path.normpath(os.path.join(PROJECTS_ROOT, relative_root, rest))
            root = os.path.normpath(os.path.join(PROJECTS_ROOT, relative_root))
            if candidate.startswith(root + os.sep) and os.path.isfile(candidate):
                return candidate
    return None


def _is_unresolved(row, page, workspace, sidecar, cache):
    if not any(row.get(key) is not None for key in ('block_id', 'anchor', 'quote')):
        return False
    key = (page, workspace)
    if key not in cache:
        source = _route_source(page, workspace)
        if not source:
            cache[key] = None
        else:
            with open(source, 'rb') as handle:
                src_bytes = handle.read()
            _title, blocks = parse_markdown(src_bytes.decode('utf-8'))
            map_path = re.sub(r'\.jsonl$', '.blocks.json', sidecar)
            try:
                mapping = blockmap.load_map(map_path) or {}
            except (OSError, ValueError):
                mapping = {}
            records = mapping.get('blocks') or []
            if (mapping.get('source_sha256') == blockmap.source_sha256(src_bytes)
                    and len(records) == len(blocks)):
                for block, record in zip(blocks, records):
                    if blockmap.fingerprint(block) == blockmap.fingerprint(record):
                        block['id'] = record['id']
            old_by_id = {
                record.get('id'): record
                for record in records + list(mapping.get('retired') or [])
                if record.get('id')
            }
            cache[key] = (blocks, old_by_id)
    state = cache[key]
    if state is None:
        return True
    outcome = blockmap.resolve(row, state[0], state[1])
    return outcome['status'] != 'bound'


def scan_open_comments():
    """Walk every workspace's feedback dir for *.jsonl sidecars, count comments
    with status in (queued, seen) and not deleted. Returns list of
    (page_route, workspace_guess, open_count, unresolved_count) plus both totals."""
    if not os.path.isdir(REVIEW_FEEDBACK_ROOT):
        return [], 0, 0
    results = []
    total = 0
    unresolved_total = 0
    page_cache = {}
    for path in sorted(glob.glob(os.path.join(REVIEW_FEEDBACK_ROOT, '**', '*.jsonl'), recursive=True)):
        open_count = 0
        unresolved_count = 0
        page = None
        rows = []
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                page = row.get('page', page)
                rows.append(row)
        rel = os.path.relpath(path, REVIEW_FEEDBACK_ROOT)
        parts = rel.split(os.sep)
        workspace = parts[0] if len(parts) > 1 else 'estate'
        for row in rows:
            if row.get('deleted') or row.get('status') not in ('queued', 'seen'):
                continue
            if _is_unresolved(row, page or row.get('page'), workspace, path, page_cache):
                unresolved_count += 1
            else:
                open_count += 1
        if open_count or unresolved_count:
            # workspace = subdir if nested, else 'estate' (root sidecars are the
            # estate workspace's back-compat no-subdir location).
            results.append((page or rel, workspace, open_count, unresolved_count))
            total += open_count
            unresolved_total += unresolved_count
    results.sort(key=lambda t: (t[2] + t[3], t[3]), reverse=True)
    return results, total, unresolved_total


# --- Stream 5: status lines ----------------------------------------------------

def read_hygiene_status():
    if not os.path.isfile(HYGIENE_STATUS):
        return None
    with open(HYGIENE_STATUS, 'r', encoding='utf-8') as f:
        return f.read().strip()


def read_freshness_summary():
    if not os.path.isfile(FRESHNESS_SCRIPT):
        return None
    try:
        res = subprocess.run(
            ['/opt/homebrew/bin/python3', FRESHNESS_SCRIPT, '--summary'],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if res.returncode != 0:
        return None
    return res.stdout.strip() or None


# --- Stream 6: board inbox ----------------------------------------------------

def _parse_frontmatter_needs_mike(text):
    """Cheap frontmatter sniff: '---\\nneeds-mike: true\\n---' at file start, or
    a bare 'needs-mike: true' anywhere in the first 20 lines (json cards won't
    have frontmatter; handled by the caller via json key lookup instead)."""
    for line in text.split('\n')[:20]:
        if re.match(r'^\s*needs-mike\s*:\s*true\s*$', line, re.IGNORECASE):
            return True
    return False


def _read_card(path, name):
    """Return (title, needs_mike_bool) for a single inbox card file."""
    needs_mike = False
    title = name
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        if name.endswith('.json'):
            try:
                obj = json.loads(content)
                needs_mike = bool(obj.get('needs-mike') or obj.get('needs_mike'))
                title = obj.get('title') or name
            except json.JSONDecodeError:
                pass
        else:
            needs_mike = _parse_frontmatter_needs_mike(content)
            for line in content.split('\n'):
                line = line.strip()
                if line.startswith('#'):
                    title = line.lstrip('#').strip()
                    break
    except OSError:
        pass
    return title, needs_mike


def scan_board_inbox():
    """Return list of (filename, title, needs_mike_bool, fs_path) for every
    unprocessed card in SOMA/board/inbox/, and move them to processed/ after
    reading. New cards land in "Shipped/changed" (and "Needs Mike" if flagged)
    on the run that processes them.

    needs-mike cards get one extra safety net: rather than surfacing in "Needs
    Mike" on exactly one run (real risk of Mike missing the board that day),
    any needs-mike card still inside WINDOW_HOURS of its processed-mtime is
    re-surfaced in Needs Mike on every run within that window (see
    recent_needs_mike_from_processed()). Cards outside the window age out —
    same 48h horizon as the rest of the board, not indefinite."""
    os.makedirs(BOARD_INBOX, exist_ok=True)
    os.makedirs(BOARD_INBOX_PROCESSED, exist_ok=True)
    cards = []
    for name in sorted(os.listdir(BOARD_INBOX)):
        path = os.path.join(BOARD_INBOX, name)
        if not os.path.isfile(path):
            continue
        if not (name.endswith('.md') or name.endswith('.json')):
            continue
        title, needs_mike = _read_card(path, name)
        cards.append((name, title, needs_mike, path))

    # Move to processed/ so a re-run doesn't re-list it under "Shipped/changed"
    # every time — needs-mike visibility past this point is handled by
    # recent_needs_mike_from_processed() scanning processed/ by mtime instead.
    for name, _title, _nm, path in cards:
        dest = os.path.join(BOARD_INBOX_PROCESSED, name)
        try:
            os.replace(path, dest)
        except OSError as e:
            print(f"[generate_board] WARN: could not move inbox card {name}: {e}", file=sys.stderr)
    return cards


def recent_needs_mike_from_processed():
    """Re-surface needs-mike cards from inbox/processed/ whose mtime is within
    WINDOW_HOURS, so a needs-mike card stays visible in "Needs Mike" across
    every board regeneration in its window, not just the one run that first
    processed it. Returns list of (name, title, mtime)."""
    if not os.path.isdir(BOARD_INBOX_PROCESSED):
        return []
    cutoff_ts = time.time() - WINDOW_HOURS * 3600
    out = []
    for name in sorted(os.listdir(BOARD_INBOX_PROCESSED)):
        path = os.path.join(BOARD_INBOX_PROCESSED, name)
        if not os.path.isfile(path):
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime < cutoff_ts:
            continue
        title, needs_mike = _read_card(path, name)
        if needs_mike:
            out.append((name, title, mtime))
    return out


# --- Stream 7: today's completions ---------------------------------------------

def scan_todays_completions():
    """Completion pages in _estate/completions/ whose filename is dated today
    (local time — completion pages are written same-day by convention).
    Returns list of (filename, title, mtime), newest-touched first. Title is
    the page's first '#' heading, falling back to the filename."""
    if not os.path.isdir(COMPLETIONS_DIR):
        return []
    today = datetime.now().strftime('%Y-%m-%d')
    out = []
    for name in sorted(os.listdir(COMPLETIONS_DIR)):
        if not name.endswith('.md') or not name.startswith(today + '-'):
            continue
        path = os.path.join(COMPLETIONS_DIR, name)
        if not os.path.isfile(path):
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        title = name
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('#'):
                        title = line.lstrip('#').strip()
                        break
        except OSError:
            pass
        out.append((name, title, mtime))
    out.sort(key=lambda t: t[2], reverse=True)
    return out


# --- Compose ------------------------------------------------------------------

def _completion_link_label(title, max_len=70):
    """Shorten a completion-page title for the inline strip: prefer the part
    before ' — ' when the full title is long, then hard-cap at max_len.
    Square brackets are stripped so the label can't break the markdown link."""
    label = title.replace('[', '(').replace(']', ')')
    if len(label) > max_len and ' — ' in label:
        label = label.split(' — ', 1)[0]
    if len(label) > max_len:
        label = label[:max_len - 1].rstrip() + '…'
    return label


def _reviewed_completion_names():
    """Completion filenames that already carry a review verdict (approve or
    recommend-changes), derived from the sidecar JSONL via tours.review_state()
    — same source of truth the review surface uses for COMPLETED.md's Reviewed
    section. Best-effort: any failure means 'nothing reviewed'."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import tours  # noqa: PLC0415 — sibling module, imported lazily on purpose
        return {os.path.basename(route): st['verdict']
                for route, st in tours.review_state().items()}
    except Exception:
        return {}


def render_done_today_strip(todays_completions):
    """One-line strip: count of UNREVIEWED completion pages dated today (the
    board is Mike's inbox — items with a verdict cast drop off), the 3 most
    recent unreviewed titles as in-app links, and a link to COMPLETED.md."""
    reviewed = _reviewed_completion_names()
    unreviewed = [t for t in todays_completions if t[0] not in reviewed]
    n_rev = len(todays_completions) - len(unreviewed)
    rev_note = f' · {n_rev} reviewed' if n_rev else ''
    n = len(unreviewed)
    if n == 0:
        return (f'**✓ Done today: 0 to review**{rev_note} · '
                '[full index →](COMPLETED.md)')
    parts = []
    for name, title, _mtime in unreviewed[:3]:
        parts.append(f'[{_completion_link_label(title)}](completions/{name})')
    strip = f"**✓ Done today: {n} to review** — " + ' · '.join(parts)
    strip += f' · […all {n}{rev_note} →](COMPLETED.md)'
    return strip


def render_board(changelog_entries, audit_files, memory_changes,
                  open_comments, open_comments_total, unresolved_comments_total, hygiene_status,
                  freshness_summary, inbox_cards, recent_needs_mike_cards,
                  todays_completions):
    lines = []
    ts = now_utc().strftime('%Y-%m-%d %H:%M UTC')
    lines.append('# Board')
    lines.append('')
    lines.append(f'_Generated {ts} by `v2/generate_board.py` — never hand-curated, '
                  f'always regenerate (nightly + on-demand via the Regenerate button)._')
    lines.append('')
    # Done-today strip — completion state at the front door, derived from
    # _estate/completions/ at generation time (self-maintaining).
    lines.append(render_done_today_strip(todays_completions))
    lines.append('')
    # Standing links (not stream-derived — always present on the board).
    lines.append('**Work queue:** [WORKQUEUE.md](WORKQUEUE.md) — calibration review open: '
                  'every item carries a Conf score and a Mike column; comment on any item to recalibrate.')
    lines.append('')
    lines.append('**Completed:** [COMPLETED.md](COMPLETED.md) — every finished job, newest first, '
                  'each linking to a what-was-done page with receipts (commits, evidence, live URLs).')
    lines.append('')

    # --- Needs Mike ---
    lines.append('## Needs Mike')
    lines.append('')
    needs_mike_items = []

    for page, workspace, count, unresolved_count in open_comments:
        pieces = []
        if count:
            pieces.append(f"{count} open comment{'s' if count != 1 else ''}")
        if unresolved_count:
            pieces.append(f"{unresolved_count} unresolved mark{'s' if unresolved_count != 1 else ''}")
        needs_mike_items.append(
            f"- **{' · '.join(pieces)}** on `{page}` "
            f"(workspace: {workspace}) — [open]({_page_link(page, workspace)})"
        )

    # This-run cards + still-recent processed cards, deduped by filename (a
    # card processed this run appears in both lists — inbox_cards from the
    # move-just-happened, recent_needs_mike_cards from the processed/ mtime
    # scan — so only add it once).
    seen_card_names = set()
    for name, title, needs_mike, _path in inbox_cards:
        if needs_mike and name not in seen_card_names:
            needs_mike_items.append(f"- **Inbox card:** {title} (`SOMA/board/inbox/processed/{name}`)")
            seen_card_names.add(name)
    for name, title, _mtime in recent_needs_mike_cards:
        if name not in seen_card_names:
            needs_mike_items.append(f"- **Inbox card:** {title} (`SOMA/board/inbox/processed/{name}`)")
            seen_card_names.add(name)

    for date_str, header, body in changelog_entries:
        combined = header + ' ' + ' '.join(body)
        if re.search(r'\bMike\b|\bdecision\b', combined, re.IGNORECASE):
            needs_mike_items.append(f"- {header}")

    if needs_mike_items:
        lines.extend(needs_mike_items)
    else:
        lines.append('_Nothing flagged — no open comments, no needs-mike inbox cards, '
                      'no decision-flagged changelog lines in the last 48h._')
    lines.append('')

    # --- Shipped/changed (48h) ---
    lines.append('## Shipped/changed (48h)')
    lines.append('')
    if changelog_entries:
        for date_str, header, body in changelog_entries:
            lines.append(f'### {date_str} — {header.split("—", 1)[-1].strip() if "—" in header else header}')
            for b in body:
                if b.strip():
                    lines.append(b)
            lines.append('')
    else:
        lines.append('_No ESTATE.md changelog entries in the last 48h._')
        lines.append('')

    if memory_changes:
        lines.append('**Memory index — added/changed:**')
        lines.extend(memory_changes)
        lines.append('')

    if inbox_cards:
        lines.append('**Board inbox (processed this run):**')
        for name, title, needs_mike, _path in inbox_cards:
            flag = ' _(needs Mike)_' if needs_mike else ''
            lines.append(f'- {title} (`{name}`){flag}')
        lines.append('')

    # --- Fleet status ---
    lines.append('## Fleet status')
    lines.append('')
    if hygiene_status:
        lines.append(f'- **Estate hygiene:** {hygiene_status}')
    else:
        lines.append('- **Estate hygiene:** _no `LATEST-STATUS.txt` found — hygiene job may not have run yet._')
    if freshness_summary:
        lines.append(f'- **Second-brain freshness:** {freshness_summary}')
    lines.append('')

    # --- Streams digest ---
    lines.append('## Streams digest')
    lines.append('')
    lines.append(f'- ESTATE.md changelog entries (48h): {len(changelog_entries)}')
    lines.append(f'- New audit files in `SOMA/audits/` (48h): {len(audit_files)}')
    if audit_files:
        for name, heading, mtime in audit_files[:15]:
            when = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime('%m-%d %H:%M')
            lines.append(f'  - `{name}` — {heading} ({when} UTC)')
        if len(audit_files) > 15:
            lines.append(f'  - _...and {len(audit_files) - 15} more._')
    lines.append(f'- Memory index lines added/changed: {len(memory_changes)}')
    lines.append(f'- Open review comments (queued/seen): {open_comments_total} across {len(open_comments)} page(s)')
    lines.append(f'- Unresolved review marks: {unresolved_comments_total}')
    lines.append(f'- Board inbox cards processed this run: {len(inbox_cards)}')
    lines.append('')

    return '\n'.join(lines)


def _page_link(page, workspace):
    """Best-effort in-app link to a page given its route and workspace guess."""
    if workspace == 'estate':
        return f'/page/{page}'
    return f'/w/{workspace}/page/{page}'


def main():
    changelog_entries = safe(parse_estate_changelog, [], 'ESTATE.md changelog parse')
    audit_files = safe(scan_recent_audits, [], 'audits scan')
    memory_changes = safe(diff_memory_index, [], 'memory index diff')
    open_comments, open_comments_total, unresolved_comments_total = safe(
        scan_open_comments, ([], 0, 0), 'open comments scan'
    )
    hygiene_status = safe(read_hygiene_status, None, 'hygiene status read')
    freshness_summary = safe(read_freshness_summary, None, 'freshness summary')
    inbox_cards = safe(scan_board_inbox, [], 'board inbox scan')
    recent_needs_mike_cards = safe(
        recent_needs_mike_from_processed, [], 'recent needs-mike processed-card scan'
    )
    todays_completions = safe(scan_todays_completions, [], "today's completions scan")

    board_md = render_board(
        changelog_entries, audit_files, memory_changes,
        open_comments, open_comments_total, unresolved_comments_total, hygiene_status,
        freshness_summary, inbox_cards, recent_needs_mike_cards,
        todays_completions,
    )

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        f.write(board_md)

    print(f'Board written to {OUT_PATH}')
    print(f'  changelog entries: {len(changelog_entries)}')
    print(f'  recent audits: {len(audit_files)}')
    print(f'  memory changes: {len(memory_changes)}')
    print(f'  open comments: {open_comments_total} across {len(open_comments)} page(s)')
    print(f'  unresolved marks: {unresolved_comments_total}')
    print(f'  inbox cards processed: {len(inbox_cards)}')
    print(f'  needs-mike cards still in 48h window: {len(recent_needs_mike_cards)}')
    print(f"  completions dated today: {len(todays_completions)}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
