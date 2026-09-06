"""Live mark-layer node emission — Playmaker fromProseMarkdown port.

SOMA agreed-model item 6a: Playmaker and soma-review share one engine.
Playmaker's adapter is `fromProseMarkdown`
(`playmaker/src/mark-layer-engine/adapters/proseMarkdown.ts`). This module
is the in-repo faithful port of that shared model, and it is the sole live
emitter for DOM stamps, create/resolve minting, and edit-rebind.

The historical Python twin (`mark_layer_adapter.to_mark_layer_nodes`) is a
debug/bridge alias. Live callers must use `emit_live_mark_layer_nodes` /
`from_prose_markdown`. Twin is not the live default: set
`SOMA_REVIEW_MARK_LAYER_TWIN=1` only to force the twin name on debug
routes. Optional `SOMA_REVIEW_MARK_LAYER_ENGINE=js` consumes the in-repo
JS `fromProseMarkdown` via node (parity / Playmaker-shaped consume), not
the twin.

Playmaker's TypeScript package is not a runtime dependency of this stdlib
server. When a sibling checkout is present, `tests/test_engine_parity.py`
and `tests/test_mark_layer_engine.py` can still compare. Ids match the
documented Playmaker mint: `{prefix}-{sha1(prefix:text)[:10]}` with a
`-{n}` occurrence suffix for the 2nd+ `(prefix, text)` in one parse.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from typing import Any

from mdblocks import norm, segment_sentences

_HEADING_RE = re.compile(r'^#{1,6}\s+')
ENGINE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mark-layer-engine')
FROM_PROSE_JS = os.path.join(ENGINE_DIR, 'fromProseMarkdown.mjs')

_LAST_LIVE_SOURCE = 'fromProseMarkdown'


def mark_layer_twin_enabled() -> bool:
    """Debug/bridge only. Default off — live stamps do not use the twin."""
    return os.environ.get('SOMA_REVIEW_MARK_LAYER_TWIN', '0').strip().lower() in (
        '1', 'true', 'yes',
    )


def mark_layer_js_engine_enabled() -> bool:
    """Opt-in consume of the in-repo JS fromProseMarkdown (not the twin)."""
    return os.environ.get('SOMA_REVIEW_MARK_LAYER_ENGINE', '').strip().lower() in (
        'js', 'node', 'fromprosemarkdown',
    )


def last_live_emitter_source() -> str:
    """Which emitter `emit_live_mark_layer_nodes` last used."""
    return _LAST_LIVE_SOURCE


def _content_id(seen: dict[str, int], prefix: str, text: str) -> str:
    digest = hashlib.sha1(f'{prefix}:{text}'.encode('utf-8')).hexdigest()[:10]
    key = f'{prefix}:{digest}'
    occurrence = seen.get(key, 0)
    seen[key] = occurrence + 1
    base = f'{prefix}-{digest}'
    return base if occurrence == 0 else f'{base}-{occurrence}'


def _paragraph_node(seen: dict[str, int], text: str) -> dict[str, Any]:
    node_id = _content_id(seen, 'pmpara', text)
    return {
        'id': node_id,
        'kind': 'paragraph',
        'fragments': [{'id': _content_id(seen, f'{node_id}-frag', text), 'text': text}],
    }


def _blank_node(seen: dict[str, int], text: str) -> dict[str, Any]:
    node_id = _content_id(seen, 'pmln', text)
    return {
        'id': node_id,
        'kind': 'blank',
        'fragments': [{'id': _content_id(seen, f'{node_id}-frag', text), 'text': text}],
    }


def _sentence_nodes(seen: dict[str, int], paragraph_text: str) -> list[dict[str, Any]]:
    normalized = norm(paragraph_text)
    nodes = []
    offset = 0
    for _start, _end, text in segment_sentences(normalized):
        node_id = _content_id(seen, 'pmsent', text)
        nodes.append({
            'id': node_id,
            'kind': 'sentence',
            'fragments': [{'id': _content_id(seen, f'{node_id}-frag', text), 'text': text}],
            'attrs': {'offset': offset},
        })
        offset += len(text)
    return nodes


def from_prose_markdown(md: str) -> list[dict[str, Any]]:
    """Playmaker `fromProseMarkdown` port — the live shared-model emitter.

    One `paragraph` node per blank-line-separated block (headings kept
    whole), each non-heading paragraph split into sibling `sentence`
    nodes, blank separators as `blank` nodes. Ids are the Playmaker
    content-hash mint. This is not the debug twin.
    """
    if not md:
        return []
    nodes: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    blocks = re.split(r'(\n{2,})', md)
    for block in blocks:
        if not block:
            continue
        if re.fullmatch(r'\n{2,}', block):
            nodes.append(_blank_node(seen, block))
            continue
        nodes.append(_paragraph_node(seen, block))
        if _HEADING_RE.match(block):
            continue
        nodes.extend(_sentence_nodes(seen, block))
    return nodes


def from_prose_markdown_js(md: str, timeout: float = 15.0) -> list[dict[str, Any]]:
    """Run the in-repo JS fromProseMarkdown CLI. Raises on tooling miss."""
    node = shutil.which('node')
    if not node or not os.path.isfile(FROM_PROSE_JS):
        raise RuntimeError('node or fromProseMarkdown.mjs missing')
    result = subprocess.run(
        [node, FROM_PROSE_JS],
        input=md,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=ENGINE_DIR,
    )
    if result.returncode != 0:
        raise RuntimeError(f'fromProseMarkdown.mjs failed: {result.stderr}')
    payload = json.loads(result.stdout)
    if isinstance(payload, dict) and isinstance(payload.get('nodes'), list):
        return payload['nodes']
    if isinstance(payload, list):
        return payload
    raise RuntimeError('fromProseMarkdown.mjs returned unexpected JSON')


def emit_live_mark_layer_nodes(md: str) -> list[dict[str, Any]]:
    """Sole live node emission for stamps / create / rebind.

    Default: `from_prose_markdown` (Playmaker shared-model port).
    `SOMA_REVIEW_MARK_LAYER_ENGINE=js` consumes the JS fromProseMarkdown.
    `SOMA_REVIEW_MARK_LAYER_TWIN=1` is debug-only and routes through the
    twin name; it is not the live default.
    """
    global _LAST_LIVE_SOURCE
    if mark_layer_twin_enabled():
        from mark_layer_adapter import to_mark_layer_nodes  # noqa: PLC0415
        _LAST_LIVE_SOURCE = 'twin'
        return to_mark_layer_nodes(md)
    if mark_layer_js_engine_enabled():
        try:
            nodes = from_prose_markdown_js(md)
        except Exception:  # noqa: BLE001 — live must not 500 a page
            _LAST_LIVE_SOURCE = 'fromProseMarkdown'
            return from_prose_markdown(md)
        _LAST_LIVE_SOURCE = 'fromProseMarkdown-js'
        return nodes
    _LAST_LIVE_SOURCE = 'fromProseMarkdown'
    return from_prose_markdown(md)
