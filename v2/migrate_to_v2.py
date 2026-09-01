#!/usr/bin/env python3
"""Reversible one-shot migration of soma-review sidecars to anchor schema v2."""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import socket
import sys
import time
from typing import Any

import blockmap
from mdblocks import parse_markdown


PROJECTS_ROOT = os.path.expanduser('~/Projects')
CANONICAL_FEEDBACK_ROOT = os.path.join(PROJECTS_ROOT, '_estate', 'review-feedback')
WORKSPACES_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'workspaces.json')


def service_is_up(port: int = 8090) -> bool:
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.25):
            return True
    except OSError:
        return False


def load_rows(path: str) -> list[dict[str, Any]]:
    rows = []
    with open(path, 'r', encoding='utf-8') as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except ValueError as exc:
                raise RuntimeError(f'{path}:{line_number}: malformed JSON: {exc}') from exc
    return rows


def workspace_for_sidecar(path: str, root: str) -> str:
    relative = os.path.relpath(path, root)
    parts = relative.split(os.sep)
    return parts[0] if len(parts) > 1 else 'estate'


def resolve_source(page: str, workspace: str) -> str:
    with open(WORKSPACES_CONFIG, 'r', encoding='utf-8') as handle:
        config = json.load(handle)[workspace]
    if '/' not in page:
        raise RuntimeError(f'invalid page route: {page!r}')
    prefix, rest = page.split('/', 1)
    for route_prefix, relative_root in config.get('roots', []):
        if prefix != route_prefix:
            continue
        root = os.path.normpath(os.path.join(PROJECTS_ROOT, relative_root))
        source = os.path.normpath(os.path.join(root, rest))
        if not source.startswith(root + os.sep) or not os.path.isfile(source):
            break
        return source
    raise RuntimeError(f'cannot resolve {workspace}:{page}')


def map_path_for(sidecar: str) -> str:
    return sidecar[:-len('.jsonl')] + '.blocks.json'


def _binding(block: dict[str, Any], migrated_from_sha: str, recovered: str | None,
             stamp: str) -> dict[str, Any]:
    quote = blockmap.norm(block['text'])
    fields = {
        'schema': 2,
        'block_id': block['id'],
        'from': 0,
        'to': None,
        'quote': quote,
        'origin_quote': quote,
        'block_text_sha': blockmap.block_text_sha(block),
        'heading_path': list(block.get('heading_path') or []),
        'quote_verified_at': stamp,
        'unresolved': False,
        'migrated_from_sha': migrated_from_sha,
        'source_sha': migrated_from_sha,
    }
    if recovered:
        fields['recovered'] = recovered
    return fields


def migrate_rows(rows: list[dict[str, Any]], blocks: list[dict[str, Any]],
                 source_sha: str, stamp: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_anchor = {block.get('anchor'): block for block in blocks if block.get('anchor')}
    counts = {'bound_exact': 0, 'bound_snapshot': 0, 'unresolved': 0,
              'null_anchor': 0, 'already_v2': 0}
    unresolved_rows = []
    migrated = []
    for original in rows:
        row = dict(original)
        if row.get('schema') == 2:
            counts['already_v2'] += 1
            migrated.append(row)
            continue
        anchor = row.get('anchor')
        if anchor is None:
            row.update({
                'schema': 2, 'block_id': None, 'from': None, 'to': None,
                'quote': None, 'origin_quote': None, 'block_text_sha': None,
                'heading_path': None, 'unresolved': False,
                'migrated_from_sha': source_sha, 'source_sha': source_sha,
            })
            counts['null_anchor'] += 1
            migrated.append(row)
            continue

        block = by_anchor.get(anchor)
        recovered = None
        if block is None:
            prefix = blockmap.norm(row.get('snapshot'))[:60]
            candidates = [candidate for candidate in blocks
                          if prefix and blockmap.norm(candidate.get('text')).startswith(prefix)]
            if len(candidates) == 1:
                block = candidates[0]
                recovered = 'snapshot-prefix'

        if block is not None:
            row.update(_binding(block, source_sha, recovered, stamp))
            counts['bound_snapshot' if recovered else 'bound_exact'] += 1
        else:
            quote = blockmap.norm(row.get('snapshot'))
            row.update({
                'schema': 2,
                'block_id': None,
                'from': None,
                'to': None,
                'quote': quote,
                'origin_quote': quote,
                'block_text_sha': None,
                'heading_path': None,
                'unresolved': True,
                'unresolved_reason': 'no-match-at-migration',
                'migrated_from_sha': source_sha,
                'source_sha': source_sha,
            })
            counts['unresolved'] += 1
            unresolved_rows.append({
                'id': row.get('id'), 'page': row.get('page'), 'quote': quote,
            })
        migrated.append(row)
    return migrated, {'counts': counts, 'unresolved_rows': unresolved_rows}


def _backup_once(path: str, stamp: str) -> str:
    existing = sorted(glob.glob(path + '.pre-v2.*.bak'))
    if existing:
        return existing[0]
    backup = path + f'.pre-v2.{stamp}.bak'
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as out, open(path, 'rb') as source:
            shutil.copyfileobj(source, out)
            out.flush()
            os.fsync(out.fileno())
    except Exception:
        if os.path.exists(backup):
            os.unlink(backup)
        raise
    return backup


def migrate_sidecar(path: str, feedback_root: str, dry_run: bool,
                    backup_stamp: str, row_stamp: str) -> dict[str, Any]:
    rows = load_rows(path)
    if not rows:
        return {'sidecar': path, 'skipped': 'empty'}
    pages = {row.get('page') for row in rows if row.get('page')}
    if len(pages) != 1:
        raise RuntimeError(f'{path}: expected one page, found {sorted(pages)}')
    page = next(iter(pages))
    workspace = workspace_for_sidecar(path, feedback_root)
    source = resolve_source(page, workspace)
    with open(source, 'rb') as handle:
        src_bytes = handle.read()
    _title, blocks = parse_markdown(src_bytes.decode('utf-8'))
    mapping, _marks, map_report = blockmap.reconcile(None, src_bytes, blocks, [], now=row_stamp)
    for block, record in zip(blocks, mapping['blocks']):
        block['id'] = record['id']
    migrated, report = migrate_rows(rows, blocks, blockmap.source_sha256(src_bytes), row_stamp)
    report.update({'sidecar': path, 'page': page, 'workspace': workspace,
                   'source': source, 'map_report': map_report})
    if all(row.get('schema') == 2 for row in rows):
        report['skipped'] = 'already-v2'
        return report
    if dry_run:
        return report

    map_path = map_path_for(path)
    with blockmap.file_lock(map_path):
        with blockmap.file_lock(path):
            report['backup'] = _backup_once(path, backup_stamp)
            blockmap.save_map(map_path, mapping)
            payload = ''.join(json.dumps(row, ensure_ascii=False) + '\n'
                              for row in migrated).encode('utf-8')
            blockmap.atomic_write(path, payload)
    return report


def markdown_report(reports: list[dict[str, Any]], dry_run: bool) -> str:
    totals = {'bound_exact': 0, 'bound_snapshot': 0, 'unresolved': 0,
              'null_anchor': 0, 'already_v2': 0}
    unresolved = []
    for report in reports:
        for key, value in report.get('counts', {}).items():
            totals[key] = totals.get(key, 0) + value
        unresolved.extend(report.get('unresolved_rows') or [])
    lines = [
        '# soma-review anchoring migration report', '',
        f'_Mode: {"dry run" if dry_run else "applied"}_', '',
        f'- Exact legacy anchors bound: {totals["bound_exact"]}',
        f'- Recovered by unique snapshot prefix: {totals["bound_snapshot"]}',
        f'- Unresolved as of migration: {totals["unresolved"]}',
        f'- Null-anchor/page-state rows preserved: {totals["null_anchor"]}',
        f'- Rows already on v2: {totals["already_v2"]}',
        '',
    ]
    if unresolved:
        lines.extend([
            '## Unresolved rows', '',
            'These rows were not deleted. They will reattach automatically if their exact text returns.', '',
        ])
        for row in unresolved:
            lines.append(f'- `{row.get("id")}` · `{row.get("page")}` — {json.dumps(row.get("quote"), ensure_ascii=False)}')
        lines.append('')
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--feedback-root', default=CANONICAL_FEEDBACK_ROOT)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--report')
    args = parser.parse_args(argv)
    root = os.path.abspath(os.path.expanduser(args.feedback_root))
    canonical = os.path.abspath(CANONICAL_FEEDBACK_ROOT)
    if not args.dry_run and root == canonical and service_is_up():
        print('refusing migration: soma-review is listening on port 8090', file=sys.stderr)
        return 2
    paths = sorted(glob.glob(os.path.join(root, '**', '*.jsonl'), recursive=True))
    backup_stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    row_stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    reports = [migrate_sidecar(path, root, args.dry_run, backup_stamp, row_stamp)
               for path in paths]
    text = markdown_report(reports, args.dry_run)
    if args.report:
        blockmap.atomic_write(os.path.abspath(args.report), (text + '\n').encode('utf-8'))
    print(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
