"""Stable block identity and quote-verified mark anchoring for soma-review.

The source Markdown remains authoritative.  A sibling block map remembers opaque
block ids across parses, while every mark carries enough evidence (its quote and
the hash of the block text it was measured against) to prove that a binding is
still correct.  A block id is therefore a lookup hint, never sufficient proof.

This module is deliberately stdlib-only and mostly pure.  The server owns route
resolution; callers pass parsed blocks and sidecar rows in and receive updated
maps/rows back.
"""
from __future__ import annotations

import base64
import copy
import difflib
import fcntl
import hashlib
import json
import os
import secrets
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterable


MAP_VERSION = 2
PARSER_VERSION = 1
_TEXT_KINDS = {"paragraph", "list", "blockquote", "heading"}
_CODE_KINDS = {"code", "film"}


def norm(text: Any) -> str:
    """Parser-canonical text used for matching and Unicode-codepoint offsets."""
    return " ".join(unicodedata.normalize("NFC", str(text or "")).split())


def _heading_path(block: dict[str, Any]) -> tuple[str, ...]:
    value = block.get("heading_path") or []
    if isinstance(value, str):
        return (value,)
    return tuple(str(part) for part in value)


def fingerprint(block: dict[str, Any]) -> str:
    return "\x1f".join((
        str(block.get("kind") or ""),
        "\x1e".join(_heading_path(block)),
        norm(block.get("text")),
    ))


def block_text_sha(text_or_block: Any) -> str:
    text = norm(text_or_block.get("text")) if isinstance(text_or_block, dict) else norm(text_or_block)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_sha256(src_bytes: bytes) -> str:
    return hashlib.sha256(src_bytes).hexdigest()


def mint_id() -> str:
    """Mint an opaque 128-bit id.

    URL-safe base64 represents 128 bits in 22 characters.  The earlier design
    brief called this "base32", but 22 base32 characters cannot hold 128 bits;
    preserving the entropy and the ratified 22-character shape is safer.
    """
    token = base64.urlsafe_b64encode(secrets.token_bytes(16)).decode("ascii").rstrip("=")
    return "blk_" + token


def _record(block: dict[str, Any], block_id: str, order: int) -> dict[str, Any]:
    return {
        "id": block_id,
        "kind": str(block.get("kind") or ""),
        "heading_path": list(_heading_path(block)),
        "text": norm(block.get("text")),
        "order": order,
    }


def _digest(blocks: Iterable[dict[str, Any]]) -> str:
    payload = "\n".join(fingerprint(block) for block in blocks)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _compatible(old: dict[str, Any], new: dict[str, Any]) -> tuple[bool, float]:
    old_kind = old.get("kind")
    new_kind = new.get("kind")
    if old_kind == new_kind or {old_kind, new_kind} <= _CODE_KINDS:
        return True, 0.60
    if old_kind in _TEXT_KINDS and new_kind in _TEXT_KINDS:
        return True, 0.75
    return False, 1.01


@dataclass
class MatchResult:
    pairs: dict[int, int] = field(default_factory=dict)  # new index -> old index
    resurrected: dict[int, int] = field(default_factory=dict)  # new index -> retired index
    ambiguous_new: set[int] = field(default_factory=set)


def match(old_blocks: list[dict[str, Any]], retired: list[dict[str, Any]],
          new_blocks: list[dict[str, Any]]) -> MatchResult:
    """Deterministically pair a fresh parse with live/retired map records."""
    result = MatchResult()
    old_fps = [fingerprint(block) for block in old_blocks]
    new_fps = [fingerprint(block) for block in new_blocks]
    used_old: set[int] = set()
    used_new: set[int] = set()

    # Pass 1: order-preserving exact runs.  autojunk=False is load-bearing for
    # long documents containing repeated horizontal rules or boilerplate rows.
    matcher = difflib.SequenceMatcher(None, old_fps, new_fps, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            continue
        for old_i, new_i in zip(range(i1, i2), range(j1, j2)):
            result.pairs[new_i] = old_i
            used_old.add(old_i)
            used_new.add(new_i)

    # SequenceMatcher is allowed to choose either occurrence of duplicate text.
    # Keep such a pairing only when immediate neighbours make the occurrence
    # unique on both sides.  Otherwise a deletion of one identical paragraph can
    # silently transfer the surviving id to its twin.
    def context_key(fps: list[str], index: int) -> tuple[str | None, str, str | None]:
        return (fps[index - 1] if index else None, fps[index],
                fps[index + 1] if index + 1 < len(fps) else None)

    old_counts = {fp: old_fps.count(fp) for fp in set(old_fps)}
    new_counts = {fp: new_fps.count(fp) for fp in set(new_fps)}
    for new_i, old_i in list(result.pairs.items()):
        fp = new_fps[new_i]
        if old_counts.get(fp, 0) == 1 and new_counts.get(fp, 0) == 1:
            continue
        old_key = context_key(old_fps, old_i)
        new_key = context_key(new_fps, new_i)
        if old_key == new_key and old_key not in {
            context_key(old_fps, i) for i, value in enumerate(old_fps)
            if value == fp and i != old_i
        } and new_key not in {
            context_key(new_fps, i) for i, value in enumerate(new_fps)
            if value == fp and i != new_i
        }:
            continue
        del result.pairs[new_i]
        used_old.discard(old_i)
        used_new.discard(new_i)
        result.ambiguous_new.add(new_i)

    # Pass 2: exact, globally unique move rescue.  Repeated/low-entropy blocks
    # deliberately fall through rather than being paired by position.
    old_by_fp: dict[str, list[int]] = {}
    new_by_fp: dict[str, list[int]] = {}
    for i, fp in enumerate(old_fps):
        if i not in used_old:
            old_by_fp.setdefault(fp, []).append(i)
    for i, fp in enumerate(new_fps):
        if i not in used_new:
            new_by_fp.setdefault(fp, []).append(i)
    for fp in sorted(set(old_by_fp) & set(new_by_fp)):
        olds = old_by_fp[fp]
        news = new_by_fp[fp]
        if len(olds) != 1 or len(news) != 1:
            continue
        new_i, old_i = news[0], olds[0]
        candidate = new_blocks[new_i]
        if candidate.get("kind") == "hr" or len(norm(candidate.get("text"))) < 24:
            continue
        result.pairs[new_i] = old_i
        used_old.add(old_i)
        used_new.add(new_i)

    # Pass 3: same-section fuzzy matches (including moves).  A proposal must be
    # clearly better than its runner-up on both the old and new sides.
    proposals: list[tuple[float, int, int, bool]] = []
    remaining_old = [i for i in range(len(old_blocks)) if i not in used_old]
    remaining_new = [i for i in range(len(new_blocks)) if i not in used_new]
    score_table: dict[tuple[int, int], tuple[float, float, bool]] = {}
    for new_i in remaining_new:
        new = new_blocks[new_i]
        for old_i in remaining_old:
            old = old_blocks[old_i]
            if _heading_path(old) != _heading_path(new):
                continue
            ok, threshold = _compatible(old, new)
            if not ok:
                continue
            score = difflib.SequenceMatcher(
                None, norm(old.get("text")), norm(new.get("text")), autojunk=False
            ).ratio()
            score_table[(new_i, old_i)] = (score, threshold, old.get("kind") != new.get("kind"))

    for new_i in remaining_new:
        candidates = sorted(
            ((score, old_i, threshold, cross_kind)
             for (n_i, old_i), (score, threshold, cross_kind) in score_table.items()
             if n_i == new_i),
            key=lambda item: (-item[0], item[1]),
        )
        if not candidates:
            continue
        best_score, old_i, threshold, cross_kind = candidates[0]
        runner = candidates[1][0] if len(candidates) > 1 else 0.0
        reverse_scores = sorted(
            (score for (n_i, o_i), (score, _threshold, _cross) in score_table.items()
             if o_i == old_i), reverse=True,
        )
        reverse_runner = reverse_scores[1] if len(reverse_scores) > 1 else 0.0
        if best_score >= threshold and best_score - runner >= 0.10 and best_score - reverse_runner >= 0.10:
            proposals.append((best_score, new_i, old_i, cross_kind))
        elif best_score >= threshold:
            result.ambiguous_new.add(new_i)

    for _score, new_i, old_i, _cross_kind in sorted(proposals, key=lambda p: (-p[0], p[2], p[1])):
        if new_i in used_new or old_i in used_old:
            result.ambiguous_new.add(new_i)
            continue
        result.pairs[new_i] = old_i
        used_old.add(old_i)
        used_new.add(new_i)

    # Pass 4: restore a retired id only for a unique exact fingerprint on both
    # sides.  This is what makes a revert/branch flip restore mark identity.
    retired_by_fp: dict[str, list[int]] = {}
    unmatched_new_by_fp: dict[str, list[int]] = {}
    for i, block in enumerate(retired):
        retired_by_fp.setdefault(fingerprint(block), []).append(i)
    for i, fp in enumerate(new_fps):
        if i not in used_new:
            unmatched_new_by_fp.setdefault(fp, []).append(i)
    for fp in sorted(set(retired_by_fp) & set(unmatched_new_by_fp)):
        r_indexes = retired_by_fp[fp]
        n_indexes = unmatched_new_by_fp[fp]
        if len(r_indexes) == 1 and len(n_indexes) == 1:
            result.resurrected[n_indexes[0]] = r_indexes[0]
            used_new.add(n_indexes[0])

    return result


def position_map(old_text: str, new_text: str) -> list[tuple[str, int, int, int, int]]:
    return list(difflib.SequenceMatcher(
        None, norm(old_text), norm(new_text), autojunk=False
    ).get_opcodes())


def remap_point(point: int, replacements: list[tuple[str, int, int, int, int]],
                is_start: bool) -> int:
    """Map one old codepoint boundary into the new string.

    Exact insertion boundaries are left-biased for both endpoints: an insert at
    a mark's start becomes covered because its old end moves, while an insert at
    the exclusive end remains outside.
    """
    for tag, i1, i2, j1, j2 in replacements:
        if tag == "insert":
            if point == i1:
                return j1
            continue
        if i1 <= point <= i2:
            if tag == "equal":
                return j1 + (point - i1)
            if point == i1:
                return j1
            if point == i2:
                return j2
            return j1 if is_start else j2
    if replacements:
        _tag, _i1, i2, _j1, j2 = replacements[-1]
        if point >= i2:
            return j2 + (point - i2)
    return point


def _all_occurrences(haystack: str, needle: str) -> list[int]:
    if not needle:
        return []
    out = []
    start = 0
    while True:
        found = haystack.find(needle, start)
        if found < 0:
            return out
        out.append(found)
        start = found + 1


def _find_quote(mark: dict[str, Any], blocks: list[dict[str, Any]],
                old_block: dict[str, Any] | None = None) -> tuple[dict[str, Any], int, int] | None:
    quote = norm(mark.get("quote"))
    if not quote:
        return None
    preferred_path = tuple(mark.get("heading_path") or [])

    def candidates(pool: Iterable[dict[str, Any]]) -> list[tuple[dict[str, Any], int, int]]:
        found: list[tuple[dict[str, Any], int, int]] = []
        for block in pool:
            text = norm(block.get("text"))
            for start in _all_occurrences(text, quote):
                found.append((block, start, start + len(quote)))
        return found

    def context_verified(candidate: tuple[dict[str, Any], int, int]) -> bool:
        if not old_block or (old_block.get("prev_fp") is None and old_block.get("next_fp") is None):
            return True
        index = blocks.index(candidate[0])
        prev_fp = fingerprint(blocks[index - 1]) if index else None
        next_fp = fingerprint(blocks[index + 1]) if index + 1 < len(blocks) else None
        return (old_block.get("prev_fp"), old_block.get("next_fp")) == (prev_fp, next_fp)

    if preferred_path:
        same_path = candidates(block for block in blocks if _heading_path(block) == preferred_path)
        if len(same_path) == 1:
            return same_path[0] if context_verified(same_path[0]) else None
        if len(same_path) > 1:
            return None
    everywhere = candidates(blocks)
    if len(everywhere) != 1:
        return None
    candidate = everywhere[0]
    # If this id belonged to one of several identical old blocks, a globally
    # unique quote after deletion is not proof that the surviving twin is the
    # same conceptual block.  Require matching neighbours before rescuing it.
    return candidate if context_verified(candidate) else None


def _resolve_on_block(mark: dict[str, Any], block: dict[str, Any],
                      old_block: dict[str, Any] | None = None) -> tuple[int, int | None] | None:
    text = norm(block.get("text"))
    quote = norm(mark.get("quote"))
    start = mark.get("from")
    end = mark.get("to")

    if end is None and quote and text == quote:
        return 0, None
    if isinstance(start, int) and isinstance(end, int) and 0 <= start <= end <= len(text):
        if text[start:end] == quote:
            return start, end

    # The id survived a fuzzy block match.  Remap offsets through the same text
    # diff, but accept the result only when the quote proves it.
    if old_block is not None and isinstance(start, int) and isinstance(end, int):
        edits = position_map(old_block.get("text", ""), text)
        new_start = remap_point(start, edits, True)
        new_end = remap_point(end, edits, False)
        if 0 <= new_start <= new_end <= len(text) and text[new_start:new_end] == quote:
            return new_start, new_end

    occurrences = _all_occurrences(text, quote)
    if len(occurrences) == 1:
        found = occurrences[0]
        return found, found + len(quote)
    return None


def resolve(mark: dict[str, Any], blocks: list[dict[str, Any]],
            old_blocks_by_id: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Resolve through block id -> legacy anchor -> quote -> unresolved."""
    by_id = {block.get("id"): block for block in blocks if block.get("id")}
    block_id = mark.get("block_id")
    if block_id in by_id:
        block = by_id[block_id]
        bound = _resolve_on_block(mark, block, (old_blocks_by_id or {}).get(block_id))
        if bound is not None:
            return {"status": "bound", "block": block, "from": bound[0], "to": bound[1]}

    anchor = mark.get("anchor")
    if anchor:
        anchored = [block for block in blocks if block.get("anchor") == anchor]
        if len(anchored) == 1:
            block = anchored[0]
            quote = norm(mark.get("quote"))
            if not quote or quote == norm(block.get("text")):
                return {"status": "bound", "block": block, "from": 0, "to": None}

    rescued = _find_quote(mark, blocks, (old_blocks_by_id or {}).get(block_id))
    if rescued:
        block, start, end = rescued
        return {"status": "bound", "block": block, "from": start, "to": end,
                "reattached": "auto-exact"}

    return {"status": "unresolved", "reason": "quote-not-found-or-ambiguous"}


def _degenerate(src_bytes: bytes, old_count: int, new_blocks: list[dict[str, Any]]) -> str | None:
    if old_count and len(new_blocks) * 2 < old_count:
        return "block-count-dropped-over-50-percent"
    text = unicodedata.normalize("NFC", src_bytes.decode("utf-8", errors="replace"))
    if text.count("```") % 2:
        return "unterminated-fence"
    if old_count and not text.strip():
        return "empty-source"
    return None


def reconcile(map_json: dict[str, Any] | None, src_bytes: bytes,
              parsed_blocks: list[dict[str, Any]], marks: list[dict[str, Any]] | None = None,
              now: str | None = None) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Reconcile one parse and return (new map, updated rows, report)."""
    previous = copy.deepcopy(map_json or {})
    old_blocks = list(previous.get("blocks") or [])
    retired = list(previous.get("retired") or [])
    digest = _digest(parsed_blocks)
    src_sha = source_sha256(src_bytes)
    report = {"minted": 0, "retired": 0, "rescued": 0, "ambiguous": 0,
              "unresolved_marks": 0, "changed": False}

    problem = _degenerate(src_bytes, len(old_blocks), parsed_blocks)
    if problem:
        report.update({"blocked": problem, "unresolved_marks": sum(
            1 for mark in (marks or []) if not mark.get("deleted") and mark.get("block_id")
        )})
        return previous, copy.deepcopy(marks or []), report

    fast = bool(previous
                and previous.get("version") == MAP_VERSION
                and previous.get("source_sha256") == src_sha
                and previous.get("parser_version") == PARSER_VERSION
                and len(old_blocks) == len(parsed_blocks)
                and previous.get("fingerprint_digest") == digest)

    if fast:
        new_records = old_blocks
        pairing = MatchResult(pairs={i: i for i in range(len(parsed_blocks))})
    else:
        pairing = match(old_blocks, retired, parsed_blocks)
        new_records: list[dict[str, Any]] = []
        restored_retired = set(pairing.resurrected.values())
        for new_i, block in enumerate(parsed_blocks):
            if new_i in pairing.pairs:
                block_id = old_blocks[pairing.pairs[new_i]]["id"]
            elif new_i in pairing.resurrected:
                block_id = retired[pairing.resurrected[new_i]]["id"]
                report["rescued"] += 1
            else:
                block_id = mint_id()
                report["minted"] += 1
            new_records.append(_record(block, block_id, new_i))

        matched_old = set(pairing.pairs.values())
        stamp = now or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        kept_retired = [copy.deepcopy(row) for i, row in enumerate(retired) if i not in restored_retired]
        for old_i, old in enumerate(old_blocks):
            if old_i in matched_old:
                continue
            row = copy.deepcopy(old)
            row["prev_fp"] = fingerprint(old_blocks[old_i - 1]) if old_i else None
            row["next_fp"] = fingerprint(old_blocks[old_i + 1]) if old_i + 1 < len(old_blocks) else None
            row["retired_at"] = stamp
            kept_retired.append(row)
            report["retired"] += 1
        retired = kept_retired
        report["ambiguous"] = len(pairing.ambiguous_new)

    generation = int(previous.get("generation") or 0)
    changed = not fast
    new_map = {
        "version": MAP_VERSION,
        "source_sha256": src_sha,
        "generation": generation + (1 if changed else 0),
        "parser_version": PARSER_VERSION,
        "fingerprint_digest": digest,
        "blocks": new_records,
        "retired": retired,
    }

    runtime_blocks = []
    for parsed, record in zip(parsed_blocks, new_records):
        combined = dict(parsed)
        combined.update(record)
        combined["norm_text"] = record["text"]
        runtime_blocks.append(combined)

    old_by_id = {}
    for old_i, block in enumerate(old_blocks):
        if not block.get("id"):
            continue
        contextual = copy.deepcopy(block)
        contextual["prev_fp"] = fingerprint(old_blocks[old_i - 1]) if old_i else None
        contextual["next_fp"] = fingerprint(old_blocks[old_i + 1]) if old_i + 1 < len(old_blocks) else None
        old_by_id[block["id"]] = contextual
    updated_marks = []
    for original in marks or []:
        mark = copy.deepcopy(original)
        before_binding = (
            mark.get("block_id"), mark.get("from"), mark.get("to"),
            mark.get("quote"), mark.get("block_text_sha"), mark.get("unresolved"),
        )
        # Page-level comments, liveness probes, and completion verdict state are
        # distinct records, not detached block marks.
        if mark.get("block_id") is None and not mark.get("anchor") and not mark.get("quote"):
            updated_marks.append(mark)
            continue
        outcome = resolve(mark, runtime_blocks, old_by_id)
        if outcome["status"] == "bound":
            block = outcome["block"]
            start, end = outcome["from"], outcome["to"]
            current_quote = norm(block.get("text")) if end is None else norm(block.get("text"))[start:end]
            previous_id = mark.get("block_id")
            mark.update({
                "block_id": block["id"],
                "from": start,
                "to": end,
                "quote": current_quote,
                "origin_quote": mark.get("origin_quote") or current_quote,
                "block_text_sha": block_text_sha(block),
                "heading_path": list(_heading_path(block)),
                "unresolved": False,
            })
            mark.pop("unresolved_reason", None)
            if outcome.get("reattached"):
                hops = int(mark.get("hops") or 0) + 1
                mark["hops"] = hops
                mark["reattached"] = outcome["reattached"]
                mark["drifted"] = hops > 3
                report["rescued"] += 1
            elif previous_id and previous_id != block["id"]:
                mark["reattached"] = "auto-exact"
            after_binding = (
                mark.get("block_id"), mark.get("from"), mark.get("to"),
                mark.get("quote"), mark.get("block_text_sha"), mark.get("unresolved"),
            )
            if before_binding != after_binding or not mark.get("quote_verified_at"):
                mark["quote_verified_at"] = now or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        else:
            mark["unresolved"] = True
            mark["unresolved_reason"] = outcome["reason"]
            report["unresolved_marks"] += 1
        updated_marks.append(mark)

    report["changed"] = changed
    return new_map, updated_marks, report


@contextmanager
def file_lock(path: str, exclusive: bool = True):
    """Cross-process lock with a stable inode next to a replaceable data file."""
    lock_path = path + ".lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_map(path: str) -> dict[str, Any] | None:
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: str, data: bytes, mode: int = 0o600) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + f".{os.getpid()}.",
                               suffix=".tmp", dir=directory)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_dir(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save_map(path: str, mapping: dict[str, Any], keep_generations: int = 5) -> None:
    """Atomically save a map and retain the previous five generations."""
    previous = None
    if os.path.isfile(path):
        with open(path, "rb") as handle:
            previous = handle.read()
        try:
            old_generation = int(json.loads(previous).get("generation") or 0)
        except (ValueError, AttributeError):
            old_generation = 0
        atomic_write(f"{path}.gen{old_generation}", previous)
    payload = (json.dumps(mapping, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    atomic_write(path, payload)
    generations = []
    prefix = os.path.basename(path) + ".gen"
    for name in os.listdir(os.path.dirname(path)):
        if name.startswith(prefix):
            try:
                generations.append((int(name[len(prefix):]), os.path.join(os.path.dirname(path), name)))
            except ValueError:
                continue
    for _generation, old_path in sorted(generations, reverse=True)[keep_generations:]:
        os.unlink(old_path)
