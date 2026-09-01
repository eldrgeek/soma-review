# Anchoring model — implementation brief

_2026-09-01. Produced by a 137-agent read-only audit + adversarial design pass
(Mike Wolf + Claude Opus 5, CCc). Ratified constraints came from the Mark Layer
design session; see auto-memory `project_playmaker_mark_layer.md` and
https://claude.ai/code/artifact/53ff1d56-1e18-4e3f-ba4b-9fedd2a3ccc7_

**Ratified by Mike (2026-09-01):** blocks get stable per-block ids; a mark anchors
to {blockId, from, to}; every edit produces a position map through which mark
ranges are remapped; a mark whose range dies DETACHES with its quote and is shown,
never silently dropped; marks stay in the existing per-page JSONL sidecar;
`soma-review` is the home (not a third system).

**Method caveat, stated plainly:** the refutation pass was instructed to default to
`refuted=true`, so 129/129 attempts "succeeded" and that count is not a score. The
value is in the specific counterexamples, which the synthesis below absorbed.

---

# Implementation brief — replacing soma-review's anchoring model

Measured baseline (run just now against live data): 49 records in 11 sidecars; 35 anchored, 14 null-anchor. Of the 35, **16 still resolve, 19 are already orphaned, 6 of those 19 are recoverable by unique snapshot-prefix match, 13 are not.** Zero duplicate `(kind, heading_path, norm(text))` fingerprints exist on any mark-bearing page today (WORKQUEUE.md is 541 blocks, 0 dupes); duplicates do exist elsewhere in the estate (`_estate/FRONTIER.md`, `_estate/SITREP.md`, `_estate/pulse-archive/*`), so ambiguity must fail closed but will be rare.

---

## 1. Final semantics

### The one change that carries the design

**The mark carries its own evidence. The block map is a cache, not the authority.**

Every mark stores `quote` — the full normalized text it covered — and `block_text_sha` — the sha256 of the normalized block text those offsets were measured against. `block_id` and `from`/`to` are *hints*. On every resolve, the system verifies `slice(from,to) == quote`; if that fails it searches for `quote` inside the matched block; if that fails the mark is unresolved. This single rule kills the whole class of refutations where a mark stayed "in bounds" and silently covered different text (C2/C3/C5/C6, A1 variant-2, B5, E3, F2, G3).

Everything below is subordinate to it.

### Rules, restated

**A1 — REFUTED, fixed.** The id is not "underivable"; it is **opaque to consumers** and **assigned by a documented, deterministic, auditable matcher**. `blk_` + 22 base32 chars of 128 random bits, minted only by reconcile, never parsed by any consumer. The refutation is correct that derivation cannot be eliminated — it is relocated into the matcher, where it is testable. `dispatch-prompt-template.md:13` must stop calling it a parseable composite.

**A2 — kept, hardened.** Map at `<feedback_dir>/<page_slug>.blocks.json`, sibling to the `.jsonl`. Marks do not move. Three additions the refutations force: (a) the map records `source_sha256` of the exact byte buffer that was parsed, so a stale pairing is detectable; (b) writes go through `flock` + unique tmp + `fsync(file)` + `os.replace` + `fsync(dir)` — the current `write_all_comments` at `v2/server.py:317-324` uses a fixed `path + '.tmp'` and no fsync at all, which is a live corruption race; (c) the last 5 generations are kept as `<slug>.blocks.json.gen<N>` so a bad reconcile is auditable and revertible.

**A3 — kept, amended.** Schema:
```
{version:2, source_sha256, generation:<int>, parser_version:<int>,
 blocks:[{id, kind, heading_path, text, order}],
 retired:[{id, kind, heading_path, text, order, prev_fp, next_fp, retired_at}]}
```
`retired` gains `order` and neighbour fingerprints — without them, two retired blocks with identical text are indistinguishable and a returning block re-attaches the wrong mark. `retired` is pruned only when no non-deleted mark references the id AND the entry is older than 90 days.

**A4 — kept, corrected.** Reconcile is the sole minter, but it must be the sole *resolver* too: `v2/tours.py:117` and `v2/cursor_intake.py` currently derive anchors independently via `mdblocks._make_anchor` (`v2/mdblocks.py:213`). They get a read-only lookup API. The critical section is **read file → hash → parse → reconcile → write map**, all inside one `flock` on the map file — not the in-process `threading.Lock` at `v2/server.py:293`, which does not span `generate_board.py`, `cursor_intake.py`, or a second server instance.

**A5 — REFUTED, fixed.** Fast path requires `sha256(parsed buffer) == map.source_sha256` **AND** `map.parser_version == PARSER_VERSION` **AND** `len(parsed) == len(map.blocks)` **AND** a digest over per-block fingerprints matches. All four are computable from the parse already in hand, cost no I/O, and fail closed into a full reconcile. A sha-only guard makes any corrupt or half-merged map self-certifying forever.

**A6 — REFUTED, fixed.** Ids are never reused *for a different block*. They **are** resurrected for the same block: if an unmatched NEW block's fingerprint exactly equals a `retired` entry's fingerprint and the match is unique on both sides, the retired id is restored. This is what makes `git checkout` / `git revert` / a nightly regenerator restoring prior content non-destructive, which the strict no-reuse rule made permanent.

**B1 — REFUTED, fixed.** Matcher inputs are: the block map's `blocks` + `retired`, the fresh parse, **and the quotes of currently-unresolved marks**. No git, no timestamps, no client state; works with `.git` absent. The orphan-quote pool is what lets a mark come back when its text returns.

**B2 — REFUTED on uniqueness, kept as normalization.** `norm(b) = ' '.join(b['text'].split())` on NFC-normalized input, frozen behind `parser_version`. The fingerprint `kind \x1f heading_path \x1f norm(text)` is a **match candidate, not a key**. Measured: 0 collisions on all 11 mark-bearing pages, but ~20% of estate `.md` files contain at least one collision, so the matcher must never assume uniqueness.

**B3 — kept, two fixes.** `difflib.SequenceMatcher(None, old_fps, new_fps, autojunk=False)` — autojunk drops any element appearing in >1% of a ≥200-element sequence, which silently deletes every `---` from every `equal` run once a document has 6+ of them. Positional pairing inside `equal` opcodes.

**B4 — REFUTED, amended.** Fuzzy pass is scoped to the same `heading_path` across the whole document, not to the opcode gap — gap restriction structurally cannot see a moved block, and moves are the common case in WORKQUEUE.md. Accept only if `best >= 0.6` **and** `best - runner_up >= 0.10`; ambiguous → no match. `SequenceMatcher(None, a, b, autojunk=False)` — note the ratified text `SequenceMatcher(norm_old, norm_new)` passes `norm_old` as `isjunk` and always returns 0.0.

**B5 — REFUTED, narrowed.** Global move rescue fires only when the fingerprint is unique among still-unmatched OLD **and** still-unmatched NEW, and the block is not low-entropy (`kind != 'hr'` and `len(norm(text)) >= 24`). Otherwise the block is retired and its marks go to quote rescue. Ascending-order consumption over duplicates is a coin flip and is removed.

**B6 — kept.** Pure, deterministic, ties by ascending OLD index then NEW index.

**B7 — kept, demoted.** Self-match is the identity function; it is a smoke test, not the safety property. The safety property is M1 in §3.

**B8 — REFUTED, amended.** Kind is part of the pass-1 fingerprint. In passes 2 and 3, kind must match **except** within the text-preserving set `{paragraph, list, blockquote, heading}`, where a cross-kind match is permitted at higher threshold (0.75) and **drops offsets to whole-block**. `code`↔`film` are treated as one class (identical stored text). Everything else is a hard bar. Additionally: if a parse is degenerate (block count drops >50% vs the last good generation, or a fence is unterminated at EOF), reconcile **writes nothing and retires nothing** — an unterminated fence collapses the rest of the file into one code block and would otherwise mass-retire.

**B9 — REFUTED, replaced.** Mint/retire counts are block counts and are blind to what marks did. The reported and logged figure is **`unresolved_marks`** — non-deleted marks not currently bound to a live block — plus `minted`/`retired`/`rescued`/`ambiguous`. `unresolved_marks > 0` is a page-level banner, not a number in a log.

**C1 — kept, tightened.** `{block_id, from, to}`, `to` exclusive, Unicode **codepoint** indices into `norm(b)`; client uses `Array.from`. Input is NFC-normalized server-side before offsets are ever computed (the corpus is 100% NFC today; nothing enforces it). Selection boundaries snap outward to grapheme-cluster edges via `Intl.Segmenter` at capture time.

**C2/C3/C4 — kept.** Position map from the same diff; closed-form endpoint remap; inclusive-start / exclusive-end gravity. But the result is **advisory**: it is accepted only if it verifies against `quote`.

**C5 — kept, with the missing token.** Remapped offsets are persisted in the same reconcile, together with the new `block_text_sha`. Double-application is impossible because a remap only runs when `mark.block_text_sha != sha(norm(current block))`.

**C6 — kept.** Exact-fingerprint matches skip remapping; the verify step still runs (it is a string compare, not a diff).

**D1/D2 — REFUTED, replaced with a complete enumeration.** A mark is unresolved when and only when:
1. its block matched but `quote` is not findable in the new text (covers collapse-to-empty);
2. its block was retired and quote rescue found zero matches;
3. its block was retired and quote rescue found more than one match (**ambiguous** — never guess);
4. no block matched and no retired entry matched;
5. the source document no longer resolves (`resolve_page` raises) — the whole page's marks are unresolved-page-missing.
Anything else is a bug, not a fifth cause.

**D3 — kept, semantics corrected.** `quote` is captured at mark creation and refreshed **only** on a verified remap. It is the last *verified* covered text, not "the text immediately before the killing edit" — the app observes states, not edits, and can be many edits behind. The record stores `quote_verified_at`. It is not the 80-char `snapshot`, whose semantics are already inconsistent across writers (`v2/server.py:843` stores full block source, `:896` stores 80 chars, `:1004` stores a row id, `v2/tours.py:336` stores a page title).

**D4 — kept, invariant restated.** Every non-deleted mark renders somewhere. The assertion is `rendered_inline + page_level + unresolved_section == non_deleted − intentionally_hidden`, where `intentionally_hidden` is exactly the cursor-dispatched set filtered at `v2/server.py:662`. The flat equality in the ratified rule is false today in both directions (`:662` hides dispatched rows; `:703` renders Mike's soft-deleted rows).

**D5 — REFUTED in part, split.** Automatic re-attachment is permitted **only** on an exact, unique, full-`quote` match, and is labelled `reattached: "auto-exact"` in the record and in the UI. Fuzzy re-attachment of an unresolved mark requires a human, writes `reattached: "human"`, and that binding is sticky until the human changes it.

**D6 — REFUTED, inverted.** Unresolved status is **not sticky**. It is recomputed from scratch on every reconcile. A transient bad parse, a `git checkout`, or a torn read must not permanently condemn a mark — and under the degenerate-parse gate (B8) such a parse never gets to condemn one in the first place. Only a human's explicit re-attachment is sticky.

**E1 — kept.** Split: the winning fragment keeps the id; other fragments are new. No id on two blocks in one parse — asserted, not assumed.

**E2/E3/E4 — kept, E3 widened.** Rescue searches **all blocks in the current parse**, ordered: same `heading_path` first, then whole document. Exactly one exact occurrence of `quote` → re-anchor at the found offsets, `rescued: true`. Zero or more than one → unresolved. Scoping rescue to "blocks minted in this reconcile" makes it unreachable after a crash and blind to a plain section move.

**E5 — REFUTED, replaced.** Hop bounding per reconcile is meaningless because reconciles compose. Instead: every rescue increments `hops`, and the mark's `origin_quote` (immutable, set at creation) is retained. `hops > 3` marks the mark `drifted: true` and it is shown for human confirmation rather than silently followed further.

**F1 — REFUTED, fixed.** The 14 null-anchor rows are **not one category**: 6 `dee-liveness-probe` comments, 3 Mike page-level comments, and **5 `type:"verdict"` rows keyed by `row_id: completion:<path>`** which are resolved by `v2/tours.py` review_state, not by anchor at all. Migration partitions on `type`, never strips `row_id`, and the acceptance test is per-row (every null-anchor row still renders where it rendered), not a count — the probe writes a new one on every run.

**F2 — REFUTED, fixed.** Whole-block marks use the sentinel `to: null` (meaning WHOLE), not a literal length that the next nightly regeneration falsifies. `quote` is `norm(block['text'])` — for `table`, `code`, `film` and `hr` that is the **parser's** text, not raw source (`v2/mdblocks.py` builds table text as header + `row: [...]` reprs). That is correct because every match is against parse output, never against file bytes; the field is documented as parser-normalized.

**F3 — REFUTED, git dropped.** Time-travel is unusable: `_estate/BOARD.md` and `_estate/PORTFOLIO.md` are gitignored (`.gitignore:105-106`), several rows predate their file's first commit, and the anchor covers only `text[:40]` so a historical hit can reconstruct the wrong block. Order is now: (a) anchor still resolves → 16 rows; (b) unique `snapshot[:60]` prefix match → 6 rows; (c) unresolved → 13 rows. **Same 22/35 recovery as the git path, with no git.**

**F4 — REFUTED, fixed.** The 13 detach, are never deleted, never nearest-neighbour guessed — and detachment is **not terminal**. The migration report says: "13 unresolved as of source_sha `<hash>`; they re-attach automatically if their text returns."

**F5 — REFUTED, fixed.** Backup is `<name>.jsonl.pre-v2.<utc>.bak` written with `O_EXCL`; an existing backup is never overwritten. Rows stamped `schema: 2` plus `migrated_from_sha`. Idempotent, re-runnable.

**F6 — rationale wrong, fix kept anyway.** Verified: **no row on disk lacks `anchor` or `snapshot`**; exactly one row (`bb4edb9f` in `estate_OVERNIGHT-2026-07-01.md.jsonl`) lacks `type` and `deleted`, and it is the thread head, so `v2/server.py:1826-1827` cannot KeyError today. Use `.get()` everywhere regardless. The real fix at `:1826` is not the subscript — it is that the reply blindly copies the parent's anchor. See G2.

**G1 — REFUTED, restated.** One resolution **chain**, not an if/else dispatch: `block_id` → `anchor` → `quote` → unresolved bucket. A row with a `block_id` that no longer resolves must still be allowed to fall through to its anchor and its quote. Dispatching on field presence creates a loss mode neither scheme had.

**G2 — kept, strengthened.** Every write path (`POST /api/comments` at `v2/server.py:1691-1712`, `/api/comments/reply` at `:1824-1839`, verdict/review buttons at `:1003-1049`) emits `block_id`, `from`, `to`, `quote`, `block_text_sha`. `anchor` keeps being emitted through the whole transition and is never nulled. A reply **re-resolves at write time** rather than copying `existing[0]`; if the thread root is unresolved, the reply is created unresolved and shown as such.

**G3 — REFUTED, softened.** 409 only when `block_id` is unknown **and** `quote` is not findable anywhere in the current parse. If `block_id` is stale but `quote` resolves uniquely, the server re-anchors and returns 201 with `reanchored: true`. Offsets that fail quote verification are **not silently clamped** — the mark is stored whole-block with `offsets_dropped: true`. Silent clamping manufactures the born-dead mark the rule exists to prevent.

**G4 — REFUTED, fixed.** `generate_board.py:194 scan_open_comments` and `cursor_intake.py:29 _pending_items` filter on **`unresolved`** (quote-verified), not on anchor equality. An unresolved mark is *never* hidden from the human — it is only withheld from machine dispatch, and the board reports both counts separately.

**G5 — REFUTED, fixed.** The Grok card ships `quote` + `heading_path` + the opaque `block_id` **together**. `block_id` stays (it is the only handle back to the sidecar row and the only exactly-verifiable locator); the template says it is opaque and must not be decoded. The worker must report **detached** (zero matches) **and ambiguous** (>1 match) rather than editing.

### Explicitly unsolved

1. **Wholesale nightly regeneration.** `generate_board.py:584` rewrites `_estate/BOARD.md` from scratch every night from live streams; its content genuinely changes. Content matching cannot preserve identity for text that no longer exists. Marks on BOARD.md and PORTFOLIO.md will keep detaching nightly. The only real fix is generator-emitted stable ids (open question #3), which makes the generators writers of the identity ledger. **Not solved in this pass.**
2. **TOCTOU.** The app does not own the `.md`. A write validated at T can be dead at T+1ms. `quote`-carrying marks make the loss recoverable; they do not close the window. **Unsolvable in this architecture.**
3. **`row_id` drift.** `generate_portfolio.py:263` truncates content-derived slugs at 60 chars; 34 of 57 live idea row-ids sit at that cap; renaming a project silently orphans its verdict. **Out of scope, still broken.**
4. **Grapheme clusters in already-stored offsets.** New captures snap; old ones may split a cluster. Cosmetic.

---

## 2. Ordered edit list

**Step 0 — `v2/tests/` (before any production edit).** New package, stdlib `unittest`. See §3.

**Step 1 — new file `v2/blockmap.py`.** The whole model, importable and testable with zero server dependencies:
- `PARSER_VERSION = 1`, `norm(text)`, `fingerprint(block)`, `mint_id()`
- `match(old_blocks, retired, new_blocks) -> MatchResult` — passes 1/2/3 + retired-resurrection, pure and deterministic
- `position_map(norm_old, norm_new)`, `remap_point(p, replacements, is_start)`
- `reconcile(map_json, src_bytes, marks) -> (new_map, mark_updates, report)` — pure; caller does I/O
- `load_map(path)`, `save_map(path, m)` — `flock`, unique tmp (`pid`+`uuid`), `fsync` file and dir, generation rotation
- `resolve(mark, blocks) -> ('bound', block, from, to) | ('unresolved', reason)` — the chain from G1 plus quote verification
- degenerate-parse gate (B8)

**Step 2 — `v2/mdblocks.py`.** Keep `_make_anchor` (`:213`) unchanged for the transition. Add `unicodedata.normalize('NFC', src)` at the top of `parse_markdown` (`:218`). Keep `snapshot` for back-compat; add `b['norm_text'] = norm(b['text'])` at `:367-368`.

**Step 3 — `v2/server.py` storage layer (`:296-345`).** Replace the `threading.Lock` at `:293` with an `flock`-based per-sidecar lock used by *every* reader and writer. `write_all_comments` (`:317`): unique tmp name, `fsync` before `os.replace`, `fsync` the directory after — the fixed `path + '.tmp'` is a live cross-process corruption race. `append_comment` (`:326`): write a leading newline guard and `fsync` — a torn append currently merges with the next row and `read_comments:311-315` swallows both.

**Step 4 — `v2/server.py:render_page` (`:1216-1227`).** Read the file, hash the exact buffer, parse, call `blockmap.reconcile` under the lock, get ids. Pass `block['id']` into `render_block_html`.

**Step 5 — `v2/server.py:render_block_html` (`:1173-1209`).** Add `data-block-id`. Keep `data-anchor` and `data-snapshot` for the transition.

**Step 6 — `PAGE_JS` (`:659-730`).** Replace `loadThreadsIntoDOM`'s two-bucket logic with the three-bucket chain: bound / page-level / **unresolved**. Delete the `(byAnchor[anchor] || [])` silent-drop at `:703`. Add an "Unresolved marks (N)" section rendering quote + author + text + last-known heading path. Assert `rendered + pageLevel + unresolved + hidden === nonDeleted` in a dev-mode console check.

**Step 7 — `PAGE_JS` write paths (`:768`, `:843`, `:896`, `:1003-1049`).** Emit `block_id`, `from`, `to`, `quote`, `block_text_sha`. Use `Array.from` for all slicing. Snap selection to grapheme clusters. Stop overloading `snapshot` — `:843` (full source), `:896` (80 chars), `:1004` (row id fallback) all become explicit fields.

**Step 8 — `v2/server.py` POST handlers.** `/api/comments` (`:1691-1712`): validate + re-anchor per G3; store `source_sha`. `/api/comments/reply` (`:1824-1839`): re-resolve at write time, `.get()` not subscript, propagate unresolved.

**Step 9 — `v2/cursor_intake.py`.** `_pending_items` (`:29`) excludes unresolved. `file_intake_card` (`:70`, `:101`, `:110-112`) emits `quote` + `heading_path` + opaque id, never a bare id.

**Step 10 — `v2/tours.py`.** Replace `mdblocks` re-derivation (`:117`) with `blockmap` lookup; targets become `[data-block-id="..."]` (`:174`, `:190`, `:202`, `:213`, `:223`, `:236`).

**Step 11 — `v2/generate_board.py:194 scan_open_comments`.** Resolve each row against the current parse; report `open` and `unresolved` as two numbers.

**Step 12 — `v2/dispatch-prompt-template.md:13`.** Rewrite per G5.

**Step 13 — `v2/migrate_to_v2.py`.** §4.

---

## 3. First test suite

`v2/tests/`, stdlib `unittest`, no network, no launchd. Run: `python3 -m unittest discover v2/tests`.

### Tier 1 — the safety property (metamorphic, highest priority)

**M1 — `test_never_silently_moves`.** *The* invariant. For a corpus of (doc_before, edit, doc_after) pairs, for every mark: after reconcile, either `rendered_text(mark) == quote(mark)` exactly, or `mark.unresolved is True` with `quote` intact. There is no third outcome. Enforces: a mark covers the same text, or it detaches with its quote. Never different text.

**M2 — `test_edit_generator_fuzz`.** Property test over generated edits — insert-above, delete, split at a sentence boundary, merge two paragraphs, move a section, reword a head, reword a tail, reflow whitespace, rename a heading, duplicate a block, reverse section order — applied to real estate docs. Assert M1 holds for every generated case. This is the pass that catches everything the ratified rules got wrong.

**M3 — `test_composition_equals_chain`.** For `v0 → v1 → v2`, the mark set from reconciling both hops equals the mark set from reconciling `v0 → v2` **or** the collapsed hop yields unresolved. Never a different bound text. Enforces: an unobserved intermediate edit cannot silently mis-anchor.

**M4 — `test_round_trip_restores`.** `v0 → v1 → v0` (git revert, branch flip, generator restore). Every mark bound at the first `v0` is bound to the same text at the second `v0`. Enforces retired-id resurrection (A6 fix) and non-sticky unresolved (D6 fix).

**M5 — `test_duplicate_text_never_guesses`.** Documents with byte-identical blocks under one heading; delete one, reorder them, split around them. Every mark either binds to the block whose quote and neighbours match, or is unresolved-ambiguous. Never binds to the wrong twin. Uses real `_estate/FRONTIER.md` shapes.

### Tier 2 — reconcile correctness

**R1 — `test_self_match_is_identity`.** Reconcile a parse against a map built from the same bytes: zero mints, zero retirements, byte-identical ids. Run over all 11 mark-bearing pages plus the 541-block WORKQUEUE.md.

**R2 — `test_deterministic`.** Matcher run twice on identical input returns identical output. Ties break as specified.

**R3 — `test_no_write_when_unchanged`.** Two renders, no file change: zero writes, zero generation bumps.

**R4 — `test_degenerate_parse_writes_nothing`.** Truncated file, unterminated fence, empty file: reconcile refuses to retire anything and leaves the map untouched.

**R5 — `test_id_uniqueness`.** No id appears on two blocks in one parse; no retired id is minted for a different fingerprint.

### Tier 3 — offsets

**O1 — `test_remap_boundary_cases`.** The 11 cases: insert before/after/at-from/at-to/inside; delete before/head-overlap/tail-overlap/covering/exact; replace-shorter. Closed form from C3.

**O2 — `test_gravity`.** `(10,20)` + insert 3 at 10 → `(10,23)`; same insert at 20 → `(10,20)`.

**O3 — `test_astral_offsets`.** Marks spanning non-BMP characters round-trip Python↔JS-semantics identically (`Array.from` equivalence). Uses the 11 astral chars in `FLEET-TOOLING-REVIEW-2026-07-04.md` and the 7 in `WORKQUEUE.md`.

**O4 — `test_quote_beats_offsets`.** A mark whose stored offsets are in-bounds but point at different text is re-found by quote or goes unresolved — never rendered at the stale offsets.

### Tier 4 — storage and API

**S1 — `test_no_lost_update`.** Two processes: one does read-modify-write, the other appends mid-flight. Both rows survive. Currently fails.

**S2 — `test_torn_append_isolated`.** A truncated final line loses only that line, not the following one, and is reported rather than silently skipped.

**S3 — `test_crash_between_writes`.** Kill between map write and sidecar write, in both orders; restart; assert M1 still holds and no mark is lost.

**S4 — `test_render_count_invariant`.** `rendered + pageLevel + unresolved + hidden == non_deleted`, for every page, including half-migrated sidecars.

**S5 — `test_write_validation`.** A POST with a dead `block_id` but a live `quote` returns 201 re-anchored; with neither, 409; offsets that fail verification are stored whole-block, never clamped.

### Tier 5 — migration

**G1t — `test_migration_golden`.** Against a copy of the real 11 sidecars: exactly 16 bound-exact, 6 bound-by-snapshot, 13 unresolved, 14 null-anchor rows unchanged, 5 completion verdicts keep `row_id`.

**G2t — `test_migration_idempotent`.** Run twice; second run produces no diff and does not touch the backup.

---

## 4. Migration for the 49 records

`v2/migrate_to_v2.py`, offline, one-shot, idempotent, reversible.

1. **Refuse to run while the service is up** — check port 8090 and exit non-zero. Not an assertion; a gate.
2. For each of the 11 sidecars: take the `flock`, back up to `<name>.jsonl.pre-v2.<utc>.bak` with `O_EXCL` (never overwrite; a partial-run re-run must not clobber the pristine copy).
3. Parse the source `.md`, hash it, build the initial block map, write it.
4. For each row, all field access via `.get()`:
   - **null `anchor`** (14 rows) → `block_id: null`, `from/to: null`, `quote: null`. Preserve `row_id` on the 5 completion verdicts verbatim. No other change.
   - **anchor resolves** (16 rows) → `block_id` = that block's new id, `from: 0`, `to: null` (WHOLE), `quote = norm(block.text)`, `block_text_sha`.
   - **anchor dead, unique `snapshot[:60]` prefix match** (6 rows) → same, plus `recovered: "snapshot-prefix"`.
   - **neither** (13 rows) → `unresolved: true`, `unresolved_reason: "no-match-at-migration"`, `quote = snapshot`, `origin_quote = snapshot`. Never deleted, never nearest-neighbour attached.
5. Stamp every row `schema: 2` and `migrated_from_sha: <source sha>`. Skip already-stamped rows.
6. Emit a report naming each of the 13 by id, page, and quote, and stating they re-attach automatically if their text returns.

Rollback: stop the service, restore the `.bak` files, delete the `.blocks.json` files, revert the code. Note honestly that rollback restores v1 anchors computed against `.md` content that may have moved in the interim.

---

## 5. What needs a restart

**Without touching the running service:**
- The whole test suite (Tier 1–3 are pure functions over `v2/blockmap.py` and `v2/mdblocks.py`).
- Writing `v2/blockmap.py`, `v2/migrate_to_v2.py`, and the tests.
- A dry-run migration against a copy of `_estate/review-feedback/`.
- Editing `v2/dispatch-prompt-template.md`.
- `v2/generate_board.py` and `v2/generate_portfolio.py` — separate processes, next cron run picks them up.

**Requires a restart (`launchctl kickstart -k gui/501/com.mikewolf.soma-review`):**
- Everything in `v2/server.py`, `v2/mdblocks.py`, `v2/cursor_intake.py`, `v2/tours.py` — all imported at process start.

**Ordering:** land `blockmap.py` + tests → run the migration with the service stopped → restart into the new server. Do not migrate with the service running: `append_comment` at `:326` and the migration's whole-file rewrite share no lock across processes, and the migration will silently delete any comment written during its run.

---

## 6. The single biggest remaining risk

**`_estate/BOARD.md` is regenerated from scratch every night by `generate_board.py:584` and is gitignored (`.gitignore:105`). Its content genuinely changes, so no content-derived matcher can preserve identity across a regeneration — all 6 of its marks are already orphaned and will keep orphaning nightly.** This is the estate's home page and the page Mike is most likely to comment on. The new model makes the loss *visible and recoverable* (the mark stays on screen in the Unresolved section with its quote, and re-attaches automatically if the text returns) rather than silent — which is a real improvement, but it does not make the mark stay put.

The only fix is having the generators emit stable ids for the rows they already know the identity of (`generate_portfolio.py:263` already builds `project-<name>` / `idea-<slug>`), which makes them writers of the identity ledger and breaks the single-minter rule. That is a genuine architectural decision and it is Mike's, not mine — but until it is made, the Unresolved section on BOARD.md will accumulate a new entry roughly every night, and the risk is that it becomes noise Mike stops reading, which is exactly the failure this redesign exists to prevent.

---

## 7. Implementation status — shipped 2026-09-01

Codex implemented this design in `v2/blockmap.py` and integrated it through rendering, comment
creation/replies, dispatch, tours, and Board generation. One representation detail changed from
the draft: ids contain 22 URL-safe base64 characters after `blk_`, which carries the full 128
random bits; 22 base32 characters cannot carry 128 bits.

The migration was rehearsed on a copy, proven idempotent, then applied offline to the live 11
sidecars. Result: 49/49 rows preserved; 16 exact-anchor bindings, 6 snapshot-prefix recoveries,
13 unresolved rows, and 14 page-level rows. The service was restarted and verified at
`http://localhost:8090`: health passes, the Board exposes stable block ids, all six of its
unresolved marks appear in the recovery section, and there are no browser console errors.

Automated verification currently consists of 12 focused tests covering block identity,
determinism, insertion/deletion offset remapping, astral code points, ambiguous-quote refusal,
deleted-quote detachment, stale-id quote repair, and torn-append isolation. The live API also
refused an invalid binding with HTTP 409 without adding a sidecar row. This is not a claim that
every proposed Tier 1–5 test above has been implemented; the crash-injection and two-process
race cases remain valuable hardening work.
