"""File soma-review staged work into SOMA/cursor-intake for Grok/Cursor sessions."""
import json
import os
import re
import time

PROJECTS_ROOT = os.path.expanduser('~/Projects')
CURSOR_INTAKE_ROOT = os.path.join(PROJECTS_ROOT, 'SOMA', 'cursor-intake')
CURSOR_INBOX = os.path.join(CURSOR_INTAKE_ROOT, 'inbox')
CURSOR_STAGED = os.path.join(CURSOR_INTAKE_ROOT, 'staged')
CURSOR_PROCESSED = os.path.join(CURSOR_INBOX, 'processed')


def _ensure_dirs():
    os.makedirs(CURSOR_INBOX, exist_ok=True)
    os.makedirs(CURSOR_STAGED, exist_ok=True)
    os.makedirs(CURSOR_PROCESSED, exist_ok=True)


def page_slug(route_path):
    slug = route_path.replace('/', '_')
    return re.sub(r'\.md$', '', slug)


def review_url(route_path, workspace_url_prefix=''):
    return f'http://localhost:8090{workspace_url_prefix}/page/{route_path}'


def _pending_items(comments):
    """Comments and agree-verdicts not yet sent to Cursor intake."""
    pending = []
    for c in comments:
        if c.get('deleted'):
            continue
        if c.get('cursor_dispatched'):
            continue
        ctype = c.get('type', 'comment')
        status = c.get('status', 'queued')
        verdict = c.get('verdict')
        if ctype == 'verdict' and verdict == 'agree':
            pending.append(c)
        elif ctype in ('comment', 'edit') and status in ('queued', 'seen'):
            pending.append(c)
    return pending


def refresh_staged_manifest(route_path, comments, workspace_url_prefix=''):
    """Rewrite staged/<slug>.md from current agree verdicts not yet dispatched."""
    _ensure_dirs()
    slug = page_slug(route_path)
    agrees = [
        c for c in _pending_items(comments)
        if c.get('type') == 'verdict' and c.get('verdict') == 'agree'
    ]
    path = os.path.join(CURSOR_STAGED, slug + '.md')
    lines = [
        f'# Staged for Grok — `{route_path}`',
        '',
        f'**Review:** {review_url(route_path, workspace_url_prefix)}',
        f'**Updated:** {time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}',
        '',
    ]
    if not agrees:
        lines.append('_Nothing staged — all agreed items sent, or no Agree verdicts yet._')
    else:
        lines.append(f'**{len(agrees)} agreed recommendation(s)** waiting for **Send to Grok**:')
        lines.append('')
        for c in agrees:
            row = c.get('row_id') or c.get('id', '?')
            snap = (c.get('snapshot') or '').strip()
            lines.append(f'- `{row}` — {snap[:120]}')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    return path


def file_intake_card(route_path, comments, persist_comments, workspace_url_prefix=''):
    """Bundle pending work into cursor-intake/inbox and mark sidecar rows dispatched.

    persist_comments(comments) must rewrite the page sidecar JSONL atomically.
    """
    _ensure_dirs()
    pending = _pending_items(comments)
    if not pending:
        return None, 'nothing pending for Cursor intake', 0

    ts = time.strftime('%Y-%m-%dT%H%M%SZ', time.gmtime())
    slug = page_slug(route_path)
    fname = f'{ts}-{slug}.md'
    card_path = os.path.join(CURSOR_INBOX, fname)

    agrees = [c for c in pending if c.get('type') == 'verdict' and c.get('verdict') == 'agree']
    comments_open = [c for c in pending if c.get('type') in ('comment', 'edit')]

    body = [f'# Cursor intake — {route_path}', '']
    if agrees:
        body.append('## Agreed recommendations')
        body.append('')
        for c in agrees:
            row = c.get('row_id') or '?'
            snap = (c.get('snapshot') or '').strip()
            body.append(f'### `{row}`')
            if snap:
                body.append(snap)
            body.append('')
    if comments_open:
        body.append('## Open comments')
        body.append('')
        for c in comments_open:
            anchor = c.get('anchor') or 'page-level'
            text = (c.get('text') or '').strip()
            body.append(f'- **{anchor}** ({c.get("author", "?")}): {text}')
        body.append('')

    body.append('## Instructions for Grok (Cursor)')
    body.append('')
    body.append(
        'Implement the agreed recommendations above. Use block comments on the review '
        'page to report progress. Do not cc-dispatch to Dee unless Mike explicitly asks.'
    )

    frontmatter = '\n'.join([
        '---',
        'source-surface: soma-review',
        'dispatch-target: cursor',
        'needs-mike: false',
        f'page: {route_path}',
        f'review-url: {review_url(route_path, workspace_url_prefix)}',
        'card-file: SOMA/cursor-intake/inbox/' + fname,
        '---',
        '',
    ])

    with open(card_path, 'w', encoding='utf-8') as f:
        f.write(frontmatter + '\n'.join(body) + '\n')

    dispatched_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    pending_ids = {p.get('id') for p in pending}
    for c in comments:
        if c.get('id') in pending_ids:
            c['cursor_dispatched'] = True
            c['cursor_dispatched_at'] = dispatched_at
            c['cursor_card'] = 'SOMA/cursor-intake/inbox/' + fname
    persist_comments(comments)

    refresh_staged_manifest(route_path, comments, workspace_url_prefix)
    rel = 'SOMA/cursor-intake/inbox/' + fname
    return rel, None, len(pending)