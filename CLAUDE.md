---
district: soma-core
status: active
depends_on: [cc-dispatch]
capabilities: [document-review, sentence-marks, anchored-comments, local-collab-pages]
last_reviewed: 2026-09-01
---

# soma-review — v1 static VPS-feedback intake (`index.html`) PLUS v2 local interactive review server (`v2/`)

**v2 is the active surface for estate document review.** v1 (`index.html`, root of this repo)
is retained as-is — a static app that posts to `soma-infer` on the VPS with per-item status
badge polling, deployed via Netlify. Do not conflate the two: v1 never runs locally and never
touches `_estate/review-feedback/`; v2 never touches Netlify or the VPS.

**Where work happens (v2):** `v2/server.py` — the whole app (stdlib-only Python HTTP server:
routing, whitelist, markdown rendering, comment API, dispatch, workspaces). `v2/mdblocks.py` —
the dependency-free markdown → block-list parser + inline HTML renderer (not full CommonMark;
handles headings, paragraphs, lists incl. nesting, GFM pipe tables, fenced code, blockquotes,
hr, links/bold/italic/inline-code/bare-URL-autolink — the subset the estate's docs actually
use). `v2/dispatch-prompt-template.md` — the editable prompt sent to `cc-dispatch` when Mike
clicks "Send to Dee" on a page. `v2/workspaces.json` — project-workspace config (roots, nav,
home page, feedback dir per workspace); reloaded fresh on every request, no restart needed
to edit it.

**Run it:** `SOMA_REVIEW_PORT=8090 /opt/homebrew/bin/python3 v2/server.py` (or via the launchd
service below). URL: **http://localhost:8090/page/estate/MORNING-REVIEW-2026-07-02.md**
(root `/` redirects to the home page; `/w/<workspace>/` redirects to that workspace's home
page). No external Python packages — stdlib only (`http.server`, `json`, `re`, `subprocess`).
Zero `npm`/`pip install` step.

## What v2 is

A local interactive review app: estate markdown rendered as linked in-app pages (relative
`.md` links are rewritten to `/page/...` routes so navigation never leaves the app), every
block (paragraph/heading/list/table/etc.) gets a hover comment affordance AND is directly
click-to-edit (edit-as-comment — see below), comments persist server-side with edit/delete
support, multiple project workspaces are switchable from the sidebar, and a page can be
dispatched to a `cc-dispatch` worker ("Send to Dee") that reads the comments and
replies/acts inline. Replaces scroll-through-chat review.

## Workspaces

Config-driven, `v2/workspaces.json`, loaded fresh on every request (`load_workspaces()` —
edit the JSON, reload the page, no server restart needed). Each workspace defines: `roots`
(route-prefix → fs-path-relative-to-`~/Projects` pairs, i.e. its own whitelist), `nav`
(sidebar links), `home` (default page), `feedback_dir` (where its comment sidecars live),
and optionally `nightly`/`nightly_filter` (opt into the `.nightly-*` worktree auto-discovery,
filtered by a regex on the worktree slug so e.g. the `playmaker` workspace only picks up
`.nightly-capture-*`/`.nightly-izzy`-shaped worktrees, not unrelated ones).

Shipped workspaces: `estate` (default — `_estate/`, `business-ops/`, `SOMA/`), `playmaker`
(`playmaker/`), `platform` (`SOMA/`, `_shared/`), `legends` (`legends-membership-site/`).

**URL scheme:** the default workspace (`estate`) keeps the original unprefixed routes
(`/page/...`, `/raw/...`, `/api/...`) for backward compatibility with the one sidecar file
that predates workspaces. Every other workspace is addressed via `/w/<workspace>/page/...`,
`/w/<workspace>/raw/...`, `/w/<workspace>/api/...`. `Handler._split_workspace()` strips the
`/w/<slug>` prefix (or defaults to `estate` if absent) at the top of both `do_GET`/`do_POST`;
every downstream function (`resolve_page`, `resolve_raw`, `sidecar_path`, `run_dispatch`,
render functions) takes an explicit `workspace=` kwarg threaded from there. `/` and
`/w/<workspace>/` both redirect to that workspace's `home` page. Sidebar has a workspace
switcher pill-row above the nav list (`render_workspace_switcher()`); switching workspaces
navigates to that workspace's home page.

Per-workspace feedback dirs: `estate` keeps using `_estate/review-feedback/` directly (no
subdir, backward compat); the others use `_estate/review-feedback/<workspace>/` (auto-created
on first access via `get_workspace()`).

## Whitelisted roots

Per-workspace now (`workspaces.json::<ws>.roots`), not a single global list. Each entry:
`[route_prefix, path_relative_to_~/Projects]`. `estate`'s roots are `_estate/`,
`business-ops/`, `SOMA/`, plus (workspace-gated) a synthetic `nightly/<worktree-slug>` route
that auto-discovers `~/Projects/.nightly-*/NIGHTLY-REPORT.md` worktrees at request time (no
server restart needed when a new nightly worktree appears). `business-ops/SPEND-INVENTORY-DRAFT.md`
and `LEDGER.csv`-adjacent files are in-scope and serve fine — they're sensitive-local (never
leave the machine, this server only binds `127.0.0.1`), not secret-from-Mike.

Path resolution rejects anything outside its workspace's roots (`os.path.normpath` + prefix
check). `resolve_page()` additionally requires `.md`; `resolve_raw()` (see Links below) allows
any file type under the whitelist for read-only static serving.

## Links (fixed 2026-07-02 — see Gotchas for the pre-fix bug)

Every link form Mike's docs actually use now renders and resolves correctly, inside
paragraphs, list items (incl. nested), and table cells alike (all three flow through the same
`mdblocks.render_inline()` → `link_resolver()` pipeline):

- **`[label](relative.md)`** → rewritten to `{workspace_prefix}/page/<route>` if the target
  resolves inside the current workspace's whitelist; internal-link styling.
- **Bare `https://...` URLs in running text** (not markdown-link syntax) — e.g.
  `**Preview:** https://foo.netlify.app` in the morning review — are now autolinked
  (`mdblocks.py::_BARE_URL_RE`, applied after markdown-link stashing so a URL already used as
  a real link's href is never double-linked). External-link styling, `target="_blank"`.
- **`[label](file.md#anchor)`** → anchor fragment preserved through the route rewrite.
- **Non-`.md` local paths** (`LEDGER.csv`, images, etc.) that exist under a whitelisted root
  → served read-only via a new `/raw/<route>` (or `/w/<ws>/raw/<route>`) route
  (`resolve_raw()`, MIME-typed via `_RAW_MIME`, `Content-Disposition: inline`).
- **Non-`.md` local paths that don't exist yet** (e.g. `LEDGER.csv` referenced in
  `business-ops/BUSINESS-PLAN-2026-07.md` before the file was created) → rendered with
  `.unavailable-link` (greyed, strikethrough, `cursor: not-allowed`, `#unavailable` href,
  `title` attr explaining why) instead of silently dead-linking or crashing.
- **`.md` links that resolve on disk but sit outside every whitelisted root for the current
  workspace** → same `.unavailable-link` treatment with an explanatory title, distinct from
  "file not found."

`LinkKind` (`server.py`) is the 3-way classification (`internal-link` / `external-link` /
`unavailable-link`) that `make_link_resolver()`'s `resolver()` returns; `mdblocks.render_inline`
accepts either that 3-tuple or a legacy 2-tuple `(href, is_internal_bool)` for backward
compatibility with any other caller.

## Home page + nav

Per-workspace now (`workspaces.json::<ws>.{home,nav}`), not global constants. `estate`'s home
is still `_estate/MORNING-REVIEW-2026-07-02.md`, nav still lists Morning Review, Productivity
Opportunities, Business Plan, Doc-Proofing Plan, Overnight Manifest, SOMA App Standard, Vision
Interview — plus an auto-generated "Nightly Reports" section.

**These are still hardcoded to the 2026-07-02 morning review artifacts** for the `estate`
workspace. When the estate does its next morning review cycle, update `workspaces.json`'s
`estate.home`/`estate.nav` to point at the new dated files (or generalize to a manifest — not
done yet, see Gotchas).

## Ringer list (2026-09-04) — the machine half of agreed-model 12b

Fork Q1 was ruled Alternative B on 2026-09-03: a bracket of assent covers everything between
two of Mike's marks, revisions included. So a revision can reach the trunk file with his assent
and without his attention. Item 12b of `SOMA/shared-cognition/mdp-agreed-model.md` is the
counterweight — every round closes with a generated list naming each such revision back to him,
so the list cannot be empty by omission. Writer-composed ringer lists fail exactly there: the
first hand-written one, on `mdp-proposal.md`, omitted the entry the writer was most invested in.

`compute_ringer_list(route, workspace, reader='mike')` derives the list from the same sidecar
rows the review surface already writes, plus the durable block map for document order.
`render_ringer_section()` renders it into every `?view=v3` page **server-side**, and
`GET /api/ringer?page=<route>` returns the same data as JSON. Server-side is the point: a list
that appears only when a client script succeeds is a list that can go missing silently.

Two halves, one list:
- **swallowed** (machine) — a `type: 'edit'` row (the `replace` flag is a panel label on the
  same record, not a different kind) by an author other than the reader, inside the bracket,
  on a block the reader never marked, that he never settled or reverted himself.
- **flagged** (writer) — any row carrying `ringer: true` (+ optional `reason`, stored as
  `ringer_reason`): a sentence the writer believes he should *not* have agreed with. Listed
  with or without a bracket.

The bracket is deliberately generous, and the bias is stated rather than hidden: **over-report
rather than under-report.** A ringer he already knew about costs him a glance; one that was
never named costs him the ruling.
- lower edge = the top of the document, always. Reading is top-down, so reaching a mark at
  block N means passing every block above it.
- upper edge = the deepest of (his own marks, his `last_read_block` from a reader-signal row).
  `bracket.basis` says which set it.
- no marks and no reader signal = no bracket. The section then says so explicitly instead of
  showing an empty list, which would read as a clean round.
- a revision whose block no longer exists (`position: null`) is listed anyway — that is the
  least reviewable state a change can be in, not a reason to drop it.

Known limitation, measured on the real corpus: **a rewritten block can move.** On
`mdp-proposal.md` the `replace` of Mike's central question now resolves to block 29 of 35,
below a bracket whose edge is block 21, so the machine half loses it. `outside_bracket` counts
these and the rendered section says so in prose; the writer-flagged half is the backstop, and
that specific row carries `ringer: true` for exactly this reason. A positional bracket compared
against a document that has since been rewritten is the structural weakness here — the durable
fix is to anchor the bracket to block ids as they were at read time, which needs the reader
signal to carry the block order it saw.

`/api/marks/merge` now records `resolved_by` (from the request's `author`, default `mike`), so
settling one's own change does not clear it from the list. Rows written before 2026-09-04 have
no `resolved_by`, so a revision settled before then still shows as swallowed — over-reporting,
per the stated bias.

Attention has a time dimension and a scope, both found by an adversarial pass (Skip,
2026-09-04) and both fixed before this shipped:
- A mark only suppresses a revision if the mark came **after** it. Without that, one agree in
  round one silenced every later revision to that block, in every round, forever. Same-second
  rows are ordered by sidecar append index, the tie-break `/api/read-state` already uses.
- A **reply** is re-bound to its thread root's block, so it is attention on that thread, not on
  the block: answering a decision card says nothing about a separate revision to the same
  sentence. A reply clears the revision it answers, and it still moves the bracket edge
  (it proves he reached the block) — those are two different sets, `answered_threads` and
  `reached`, and conflating them cost the real page 20 blocks of bracket.
- A **soft-deleted edit row is not a soft-deleted change**: the trunk write happened at create
  time and delete does not revert it, so deleting the row used to remove the change from every
  list while leaving it in the document. Deleted edits carrying a `commit` are listed as
  **withdrawn, still in the trunk**.
- `resolved_by` no longer defaults to `mike`. An unnamed resolver is not credited as the
  reader, or the list clears itself. It is client-asserted, exactly like every `author` on this
  surface — a record of who claimed to resolve, not an authenticated fact.

### The trunk gap (2026-09-04) — the second witness

The sidecar half above is complete only for changes that came through this surface. A worker
that edits the `.md` file directly leaves no row (and `v2/dispatch-prompt-template.md` still
*instructs* exactly that), so the list came back clean while the reader's text had changed
under him — the same silent swallow one layer down, a list complete *if the writer behaved*.

`compute_trunk_gap(route, workspace, reader)` uses git as the second witness. Every change
applied through the surface is committed by `_git_commit_file` and records its sha on its row,
so **commits touching the document in this round, minus the shas the sidecar claims, is exactly
the set of changes made behind the surface's back.** Those become ringer rows with
`why: 'unattributed'`, labelled *trunk change with no sidecar row*, with the same word-level
del/ins diff the other rows use. An uncommitted working-tree edit is listed too — that is the
state a half-finished direct edit is actually in.

The round window runs from the reader's second-most-recent reader-signal (the close of his
previous round), or from the earliest sidecar row if he has signalled at most once. It opens
one second early, because sidecar stamps are second-resolution and the bias is over-report.

Two things this got wrong first, both worth keeping in mind:
- **Git's date parser does not honour a trailing `Z`** and reads the stamp as local time,
  which shifted the window by the machine's UTC offset — four hours here, i.e. a whole round
  going unreported. `_git_since()` hands git an explicit `+0000` stamp built with
  `calendar.timegm` (`time.mktime` minus `time.timezone` is an hour out under DST).
- **A commit under Mike's git identity proves nothing.** This laptop's `user.name` is
  Mike Wolf, so every agent that commits here commits under his name. The first version
  excluded "his own" commits and would have suppressed 7 of 7 real findings on
  `soma/shared-cognition/mdp-agreed-model.md`. The flag is now carried onto the row as a
  caveat and excludes nothing.

When the check cannot run at all — the document is not in a git repo, or git fails — the
section says so in those words. Silence there would be the page claiming a clean round it
never verified, which is the exact over-claim 12b exists to stop.

First run against the live agreed-model document: the sidecar half said `Ringer list (0)`;
the trunk witness found **7 unrecorded rewrites of the ruling text Mike was reading**, every
one of them a direct file write by an agent during the 09-03/09-04 runs.

**The laundering hole, found by Skip and Codex independently and closed the same run.**
`_git_commit_file` runs `git add <file>` — it stages the whole document, not the span it just
wrote. So a direct edit left dirty in the working tree is swept into the *next* surface commit,
whose sha a row then claims, and the gap check would have marked it accounted. That is not an
edge case: an agent that edits the trunk directly is exactly an agent that does not commit.
`_trunk_residue()` now checks a claimed commit's added words against what the claiming rows
proposed; three or more words of residue (less is diff noise) rings the commit anyway, saying
the commit carried text no row proposed.

Other fixes from the same adversarial pass, each with a regression test:
- A **`gave-up` signal does not close a round** (fork Q2 puts it mid-round), so only `done`
  signals set the window's lower edge. Counting give-up would have called everything before it
  checked.
- The **revert commit sha is now stored on the row** (`revert_commit`). It was not, so Mike
  reverting a change — his most deliberate act of attention — rang itself back at him as an
  unrecorded trunk change, forever.
- An **untracked document** reports `untracked`, not a clean round: `git status` says `??` for
  the whole file, so a direct edit to it is invisible to both halves.
- **Truncation is reported.** `-n50` is applied after `--since`, so a busy round could drop the
  commit that mattered; the section now says the window was cut.
- **`_git_since` returns `None`** on an unparseable stamp and forces `unavailable`, instead of
  handing git a stamp it reads as local time — the exact four-hour shift the function exists to
  prevent.
- Diff header matching requires the **trailing space** (`--- `), or a deleted `---` front-matter
  fence or horizontal rule is read as a header and dropped; and a working-tree entry is emitted
  whenever the patch is non-empty, even when the changed lines are blank (deleting the blank
  line between two paragraphs merges them, which is a real edit).
- A sha shorter than **7 characters** never accounts for anything, and pathspecs are literal.

Still open, and named rather than hidden:
- **A squash, rebase or amend rewrites shas**, so a sidecar row's recorded sha can stop
  matching any commit in history and its change gets rung as unattributed. Over-report, so it
  fails in the safe direction, but it is noise.
- **Sentence-level attention vs block-level suppression.** Marks carry `from`/`to` offsets and
  edits carry `sentence_index`; neither is used. A mark on sentence 1 suppresses a revision to
  sentence 4 of the same block.
- **Block-id drift cuts both ways.** A retired id drops that mark from `reached`, which can
  collapse the bracket edge and silently drop many rows, not just one.
- **A round is not modelled.** The list is recomputed per render, so there is no persisted
  artifact showing that a given round closed with a given list.

### Three holes closed (2026-09-04, second pass)

All three were named by the pass that built the witness and left open for a later run.

- **A change that arrives by merge is seen.** `git log --since -- <path>` simplifies history: a
  merge TREESAME to a parent is dropped and the log follows the side branch instead, whose
  commit carries its own older date and falls outside the round. A writer who edits the document
  on a branch and merges it in after the round opened was therefore invisible. The log now runs
  `--full-history` and reads `%cI` (commit date), and merges — which `git show` prints with no
  diff at all — are diffed against their first parent. A merge that changed nothing along this
  line of history is dropped again, and a side commit plus the merge that carried it are
  fingerprinted on their (before, after) text so one change rings once, not twice.
- **A stranded surface write says whose it is.** `_git_commit_file` already returned its error
  and both callers threw it away. `apply_sentence_change` / `apply_sentence_revert` now return
  `commit_error` and the POST handlers persist it on the row. When the working tree is dirty and
  the dirty text is what those rows proposed, the section reads *in the trunk, uncommitted — this
  surface wrote it and the commit failed (<reason>)* instead of reporting the surface's own write
  as an edit of unknown origin on every render forever. If part of the dirty text is not
  accounted for by a failed row, only the residue is rung, at the same 3-word noise floor — a
  stranded write is not a laundering channel for whatever else is dirty in the same file.
- **The gap is cached.** Key: `(repo_root, rel, route, workspace, reader, since, HEAD sha,
  page mtime_ns+size, sidecar mtime_ns+size)`. Only the `ok` answer is cached; every other exit
  is a failure to look and must be retried, not pinned. Answers are handed out as deep copies.
  Measured on the live agreed-model page: 0.34s cold, 0.07s warm.

Five cracks the adversarial pass (Skip) found in that same change, all fixed before it shipped,
three of them routes to a page claiming a checked round it never checked:
- **A `git` call that failed is not a commit that changed nothing.** The per-commit `show`/`diff`
  dropped a `None` return, so an index lock or a 10s timeout during a render printed *"nothing
  reached the document behind this surface's back"*. Any unreadable commit now forces
  `unavailable` and names the shas, with the partial list still shown.
- **A deletion was inheriting the stranded-write label.** `_trunk_residue('')` is empty by
  arithmetic, not by evidence, so a stranger deleting a paragraph netted zero residue and read as
  the surface's own bookkeeping. Both sides of the diff must now be accounted for, the deleted
  side at a 1-word floor (the noise argument does not hold for a removal), and `commit_error`
  rows are scoped to the current round — the field is written once and never cleared, so an
  unscoped read let one failure years ago vouch for every later direct edit.
- **Dedupe only ever suppresses a merge against the commit it carried.** Fingerprinting any two
  commits alike swallowed a re-application after a revert (X→Y, Y→X, X→Y), leaving the page
  describing a document that ends in X while the file holds Y. An empty diff is not an identity.
- **A merge of a surface commit is accounted, not rung.** The sidecar records the side commit's
  sha and never the merge's, so on an estate that merges through `pr-merge-green` every merged PR
  carrying a recorded change would have rung as a stranger's edit.
- **The cache key carries a digest of the row set**, since `rows`/`all_rows` are parameters, and
  an answer computed with an unreadable HEAD is never stored — that key does not move when HEAD
  does.

Still open here: `-n50` is applied after `--since` and `--full-history` puts TREESAME merges into
that budget before discarding them, so a busy round can cut more real commits than before; the
section says the window was cut but not which shas. And `git log` walks HEAD only, so a change
committed on an unmerged branch is not seen — correct, because the reader's file is unchanged
too, but the prose does not distinguish that from "nothing touched this file".

Tests: `v2/tests/test_ringer_list.py` (19 cases) and `v2/tests/test_trunk_gap.py` (21 cases,
including the merge, stranded-write, cache and Skip-pass cases) pin the bracket edges,
the ways a revision
leaves the list, the two empty states, one regression per fix above, the server-rendered
section, and the JSON twin.

## "Waiting on you" inbox (2026-09-04) — the landing page

`/waiting` (and `/w/<ws>/waiting`, plus `/api/waiting` for the same data as JSON) is a derived
index of every document that has a comment sidecar, in every workspace. It exists because each
marked document lived at its own URL: Mike had to be told that URL by whoever wrote the doc,
and a document he had already ruled on looked, from outside, exactly like one holding an
unanswered ask.

`/` now redirects to `/waiting` instead of the workspace home page. The old behaviour lives at
`/home` (still per-workspace, still `workspaces.json::<ws>.home`), and each workspace home is
one click away in the sidebar. A "⏳ Waiting on you" link with a live count sits at the top of
the sidebar on every page.

Counting rules (`collect_waiting()`), deliberately two columns:
- **waiting on you** — open rows NOT authored by mike: asks he has not answered.
- **waiting on Dee** — open rows authored by mike: rulings nobody has acted on yet.
- **open** — `status` is not `done` and the row is not deleted. `reader-signal` rows
  (`done` / `gave-up`) are bookkeeping, never an ask; they show as a chip instead.
- An ask nobody has touched in 14 days is chipped **stale** and excluded from the headline
  count and the sidebar badge, so July's dead asks do not bury tonight's.

A row's route comes from each record's own `page` field — `page_slug()` is lossy (slashes
become underscores) and is never reversed. A row links to `?view=v3#b:<block-id>` of the
oldest open ask; the v3 view has a load-time handler for that fragment (see the v3 section).

Deep-link caveat, measured not assumed: the v3 view keeps re-laying-out for seconds after
`load` (mark rails, edit chips, term links) — MAC-STEWARD is 8418px tall at `load` and 6328px
once settled. A single jump landed 1193px above the target. The shipped handler re-centres on
every animation frame for 8s and surrenders on the reader's first scroll/click/key. In a
throttled headless measurement it still finished ~500px high; in a foreground browser the
reflow completes inside that window. Treat the fragment as "lands you at the ask", not as a
pixel guarantee.

Cost: the sidebar count re-reads every sidecar on every page render (17 files today, all
small). If the sidecar set grows much, cache it by directory mtime.

## Mark Layer review surface (2026-09-01)

Ordinary document pages now use a document-first review instrument modeled on the Playmaker
Mark Layer. The Markdown remains the primary surface: scrolling selects the sentence nearest
38% of the viewport and a 1.2-second dwell records it as read. Keyboard marks are `A` agree
(repeat up to x3), `?`/`/` clarify, `E` rewrite, `X` clear-and-rewrite/strike, `S`
acknowledge, `N` note, and `J`/`K` next/previous. Section endings offer agree-all or a
sentence-by-sentence walk. Ruling-shaped sections expose explicit Ratify / Not yet / Reject
controls; an agree mark never implies a ruling.

Paragraphs and blockquotes are split into sentence spans using Unicode code-point offsets in
the durable normalized block coordinate system. A list stays one stable block but each
rendered list item receives its own exact child range; tables, code, film, and other rich
blocks fall back to one addressable block unit. This preserves the original rendering while
making the review binding precise enough to survive edits through `blockmap.py` reconciliation.

Marks queue in `localStorage` until explicitly sent, the weighted threshold reaches 9, the
page is idle for 60 seconds, or the reviewer leaves. Weights are clarify 3; rewrite, strike,
and note 2; agree 1; acknowledge 0.5; ruling 9. Composing or editing pauses idle/leave sends.
Each mark is persisted through the existing comments endpoint with `type: "mark"`,
`mark_kind`, `strength`, optional `scope`, `reason`, `proposed`, and `sent_because`; after a
successful batch, a dispatchable page launches one review turn. Failed sends stay in browser
storage. Deleted and unresolved rows retain the existing audit/recovery behavior.

The surface is enabled by default and can be disabled per workspace with
`"mark_layer": false`. Tour-bearing pages keep the purpose-built Quinn flow. The Board and
Portfolio also retain their regenerate action inside the Mark Layer header.

## Edit-as-comment (2026-07-02)

**Decision: contenteditable-per-block (textarea swap), not CodeMirror 6.** CM6 is
ESM-module-based; vendoring a working single-file bundle without a build step (no `npm`
pipeline in this stdlib-only project — Node/npm on this Mac are scoped to the Hermes install,
not a general toolchain) would mean either shipping an unminified multi-file ESM tree with
import-map wiring, or standing up a build step this project deliberately doesn't have.
Mike's spec explicitly sanctioned "a contenteditable-per-block approach with careful diffing"
as an acceptable documented fallback — took it.

**How it works:** every block gets its raw markdown source embedded as base64 in
`data-source` (`render_block_html()` — base64 sidesteps HTML/JS string-escaping edge cases in
doc text: backticks, quotes, embedded newlines). Code and table blocks are excluded from
click-to-edit (`edit-eligible` CSS class gate) — their raw source has fence/pipe structure
that's easy to corrupt via a flat textarea edit and low-value to inline-edit; they still get
the normal comment affordance.

Click into any other block's body (`wireEditableBlocks()` in `PAGE_JS`) → `enterEditMode()`
swaps the rendered HTML for a `<textarea>` pre-filled with the decoded raw source
(`b64ToUtf8()`). On blur (or Cmd/Ctrl+Enter to commit explicitly, Escape to cancel without
saving), if the text changed, POSTs `{type: "edit", anchor, snapshot: <before>, proposed:
<after>}` to `/api/comments` — a suggested-edit comment in the same sidecar JSONL as regular
comments, distinguished by `type`. **The underlying `.md` file is never touched by the app** —
this only proposes; applying an edit is Claude-side work when a dispatched worker (or Mike)
decides to.

Edit-type comments render as an inline word-level diff (`renderDiffHtml()` / `wordDiff()` — a
small LCS-based diff, not a full diff library; fine for block-sized text) with
`<span class="diff-del">`/`<span class="diff-ins">` spans, same status badges as regular
comments.

**Enter opens a comment (separate interaction from edit-as-comment):** pressing Enter while
focused on a block's body (via `tabindex="0"`, not in edit mode) opens that block's inline
comment box (`wireEnterOpensComment()`) rather than entering edit mode — edit mode is
click-triggered, comment-box is Enter-triggered, so the two never fight for the same
keystroke. Inside an open comment box, plain Enter saves (Shift+Enter for a newline); the
page-level discussion box (always open, no toggle) saves on Cmd/Ctrl+Enter instead, since
plain Enter there would be too eager for a box that's already visible by default.

## Comment sidecar format

One JSONL file per page at `<workspace's feedback_dir>/<page-slug>.jsonl`, where `page-slug`
is the route path with `/` → `_` (e.g. `estate/MORNING-REVIEW-2026-07-02.md` →
`estate_MORNING-REVIEW-2026-07-02.md.jsonl`). Append-only for new comments; status/text/delete
updates rewrite the whole file (small files, fine). Each line:

```json
{
  "id": "uuid",
  "page": "estate/MORNING-REVIEW-2026-07-02.md",
  "type": "comment",
  "anchor": "b12-7077ff901e",
  "snapshot": "first ~80 chars of the block text at comment time (or 'before' text for edits)",
  "author": "mike",
  "text": "comment body",
  "timestamp": "2026-07-02T14:52:23Z",
  "status": "queued",
  "thread_id": "uuid (same as id for the root comment; replies share it)",
  "deleted": false,
  "edited_at": "optional — set when a mike-authored comment's text is edited via the pencil affordance",
  "deleted_at": "optional — set on soft-delete",
  "proposed": "only present when type == 'edit': the proposed after-text"
}
```

`anchor` is `null` for page-level (bottom-of-page discussion) comments. The anchor is derived
from heading-path + block-index + a hash of the first 40 chars of block text
(`mdblocks.py::_make_anchor`) — stable across reloads of an unchanged doc; if the doc is
edited, `snapshot` is what lets a human (or Claude) figure out what the comment was about even
if the anchor no longer matches anything. `type` is `"comment"` (default, back-compat with
pre-2026-07-02 rows that lack the field), `"edit"` (suggested-edit, has `proposed`),
`"verdict"`, or `"mark"` (Mark Layer metadata described above). Stable-binding fields
appended by Anchoring v2 are documented in that section below.

## Comment API

All endpoints are JSON, served by `v2/server.py`'s `Handler`. No auth — binds `127.0.0.1`
only. Every endpoint below is available both unprefixed (implicit `estate` workspace) and
under `/w/<workspace>/...` for any other workspace.

- `GET /api/comments?page=<route>` → array of comment objects for that page (root + replies
  interleaved, sorted client-side by timestamp). Includes soft-deleted rows (`deleted: true`)
  — the client filters what to show.
- `POST /api/comments` `{page, anchor, snapshot, text, author?, type?, proposed?}` → creates a
  new root comment, `status: "queued"`, `thread_id` = its own id, `deleted: false`. `type`
  defaults `"comment"`; pass `"edit"` with `proposed` set (and `snapshot` = the before-text)
  for a suggested-edit. Mark Layer passes `type: "mark"` plus `mark_kind`, stable range
  fields, `strength`, and optional `scope`/`reason`/`proposed`/`sent_because`. Returns the
  created object, `201`.
- `POST /api/comments/update` `{page, id, text}` → edits an existing comment's `text` in
  place, sets `edited_at`. **Author-gated: only comments with `author == "mike"` are editable**
  via this endpoint (403 otherwise) — a dispatched worker's own replies aren't meant to be
  silently rewritten by the same API a human uses.
- `POST /api/comments/delete` `{page, id}` → **soft-delete**: sets `deleted: true` and
  `deleted_at`, row stays in the JSONL (audit trail survives). Same `author == "mike"` gate,
  403 otherwise.
- `POST /api/comments/reply` `{page, thread_id, text, author?, status?}` → appends a reply into
  an existing thread (used by the dispatched worker to answer Mike). `author` defaults
  `"claude"`; pass `"dee"` or another persona name to be explicit. `status` defaults `"seen"`.
- `POST /api/comments/status` `{page, id, status}` → updates one comment's status in place.
  `status` must be one of `queued|seen|in-progress|done`.
- `POST /api/dispatch` `{page}` → fires `cc-dispatch review-comments-<ws->-<slug> <prompt-file>`
  (see below), returns `{ok, task_name, pid}` immediately (fire-and-forget; does not block on
  completion).
- `GET /page/<route>` → rendered HTML page. `GET /raw/<route>` → read-only static file (see
  Links above). `GET /api/workspaces` → `{slug: {label, home, url_prefix}}` for all configured
  workspaces. `GET /healthz` → `{ok: true, ts}`.

## Comments editable/deletable in the UI

Pencil (`&#9998;`) and trash (`&#128465;`) icons appear on `comment-item`s where
`author === "mike"`, not already deleted, and `type !== "edit"` (suggested-edit comments carry
their own diff view instead of freeform text — editing one doesn't make sense the same way;
delete still works on those if needed via the same author check, just no pencil).
Pencil → inline `<textarea>` replaces the comment body, commits on blur or Enter, Escape
reloads without saving. Trash → `confirm()` then soft-delete; deleted comments render with
`.deleted` (dimmed) styling and a "(deleted)" note rather than disappearing, so the audit
trail is visible in-UI too, not just in the JSONL.

## Voice-in

Mic button (🎤, `.mic-btn`) next to every comment/edit textarea's save button, wired via
`wireMic()` in `PAGE_JS` using the browser **Web Speech API**
(`window.SpeechRecognition || window.webkitSpeechRecognition` — Chrome-only in practice).
Click → records → transcript appended to the textarea for review before save (never
auto-submits — "human reads" stays in the loop per doctrine). Click again to stop early.
**Graceful hide**: if neither constructor exists on `window`, `wireMic()` sets
`btn.style.display = 'none'` and returns — no crash, no dead button, on any browser lacking
the API. No server-side audio handling of any kind; this is 100% client-side, browser-mediated
dictation.

## Send to Dee (dispatch)

Each page with a "Send to Dee" button (any route under the current workspace's own roots, or
its nightly reports if it opts into nightly discovery) POSTs `/api/dispatch` (or
`/w/<ws>/api/dispatch`), which:
1. Writes the filled `v2/dispatch-prompt-template.md` (page path, fs path, sidecar path, a
   **workspace-prefixed** API base URL substituted in) to
   `<workspace's feedback_dir>/.dispatch-prompt-<slug>.md`.
2. Shells out to `~/.local/bin/cc-dispatch review-comments-<ws-infix><slug> <prompt-file>`
   (task name gets a `<workspace>-` infix for any non-`estate` workspace, so task names don't
   collide across workspaces) with an **explicitly widened `PATH`** (prepends
   `/opt/homebrew/bin:/opt/homebrew/sbin`) — see Gotchas, this is load-bearing.
3. `cc-dispatch` forks, runs a Claude worker with filesystem access, which reads the sidecar
   JSONL, marks comments `seen` → does the work → replies via `POST {api_base}/api/comments/reply`
   (where `api_base` already includes the workspace prefix, so replies land in the right
   workspace's sidecar automatically) → marks `done` (or `in-progress` if it deliberately
   deferred to Mike), and writes its own audit report to `~/Projects/SOMA/audits/`.

Edit `v2/dispatch-prompt-template.md` directly to change what the dispatched worker is told to
do — it's a plain `.format()` template with `{page}`, `{page_fs_path}`, `{sidecar_path}`,
`{api_base}` placeholders.

## Service (launchd)

`~/Library/LaunchAgents/com.mikewolf.soma-review.plist` — `RunAtLoad` + `KeepAlive`,
**explicit interpreter path** `/opt/homebrew/bin/python3` (bare `python3` under launchd's
default PATH `/usr/bin:/bin:/usr/sbin:/sbin` resolves to Xcode CLT's Python 3.9.6, not
homebrew's). Logs: `~/Projects/SOMA/logs/soma-review.out.log` /
`~/Projects/SOMA/logs/soma-review.err.log`.

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mikewolf.soma-review.plist
launchctl kickstart -k gui/$(id -u)/com.mikewolf.soma-review   # restart after edits
curl -s http://localhost:8090/healthz
```

## Gotchas

- **launchd PATH gotcha, twice over.** The plist itself uses an explicit interpreter path (see
  above). But `run_dispatch()` in `server.py` ALSO has to widen `PATH` before shelling out to
  `cc-dispatch`, because `cc-dispatch`'s own `lib/runner.sh` calls bare `python3` internally —
  under launchd's minimal inherited PATH that resolves to the Xcode stub (3.9.6), and
  `lib/runner.py` uses `str | None` (PEP 604, needs 3.10+), so it crashes with
  `TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'`. Fixed by prepending
  `/opt/homebrew/bin:/opt/homebrew/sbin` to the child's env in `run_dispatch()`. If dispatch
  starts silently failing again, check `~/Projects/cc-dispatch/logs/<ts>-review-comments-*.log`
  first — this exact traceback is the signature.
- **`mdblocks.py` is not CommonMark.** It's tuned to what the estate's docs actually use.
  Nested list rendering is indent-based (2 spaces = one nesting level) and has not been
  stress-tested against deeply nested or mixed ordered/unordered lists. If a page renders
  oddly, check `v2/mdblocks.py::_render_list` first before assuming a comment-anchoring bug.
- **`estate.home`/`estate.nav` in `workspaces.json` are dated to 2026-07-02.** They don't
  auto-discover "today's morning review" — someone (Mike or Claude) has to update
  `workspaces.json` when a new morning review cycle starts. A future version could generalize
  this to scan `_estate/*.md` by modification date, but that wasn't built (kept for time —
  see What was cut).
- **Links were silently broken before 2026-07-02 (fixed).** Two independent bugs, found while
  fixing Mike's "links aren't clickable" report:
  1. Bare `https://...` URLs in running text (not `[label](url)` syntax) were never linked at
     all — `render_inline()` only ever matched markdown-link syntax. The morning review's
     `**Preview:** https://...netlify.app` lines (6 of them) rendered as inert text. Fixed by
     adding `mdblocks.py::_BARE_URL_RE` autolinking, applied *after* markdown-link stashing so
     a URL that's already a real link's href never gets double-linked.
  2. **Pre-existing, unrelated to the autolink work:** `render_inline`'s placeholder-unstash
     loop only ran one forward pass over `placeholders`, so a link whose *label* contained
     inline code (e.g. `` [`file.md`](file.md) ``, common in this codebase's docs) rendered
     the label as a bare placeholder index (literally the text "0") instead of the code span —
     because the code-span placeholder was nested inside the link's own stashed HTML, and a
     single pass can't resolve a placeholder that only appears after an earlier substitution.
     Fixed by looping the unstash pass until no `\x00N\x00` tokens remain (bounded by
     `len(placeholders)+1`). Reproduced and verified against
     `` Overnight run per [`OVERNIGHT-2026-07-01.md`](OVERNIGHT-2026-07-01.md). `` — this
     exact line is in `estate/OVERNIGHT-2026-07-01.md`'s source.
  Also added: non-`.md` local links now resolve to `/raw/<route>` (read-only static serve) if
  they exist under the whitelist, or `.unavailable-link` (never a silent dead link) if they
  don't — see "Links" section above for the full taxonomy.
- **Sidecar rewrite-on-status-update is O(n) per page.** `update_comment()` reads the whole
  JSONL, patches in memory, rewrites atomically (`.tmp` + `os.replace`). Fine at estate-review
  volume (dozens of comments per page); would need a real datastore past a few thousand.
- **No auth on the comment/dispatch API.** Server binds `127.0.0.1` only — anything that can
  reach localhost:8090 on this Mac can post/dispatch. Acceptable for a single-user local tool;
  revisit if this ever needs to be reachable off-box.
- **Task-name/slug `.md` collision (fixed 2026-07-02):** `run_dispatch()` originally built the
  `cc-dispatch` task name straight from the page slug, which already contains `.md`
  (`estate_MORNING-REVIEW-2026-07-02.md`) — `cc-dispatch` appends its own `.md` for the report
  filename, producing `...2026-07-02.log.log`-shaped names. Fixed by stripping a trailing
  `.md` from the slug before building `task_name`. Sidecar filenames themselves still keep the
  `.md` (that's fine, they're not passed through cc-dispatch's naming).

## What was cut (v2.1 pass, 2026-07-02)

- **CM6 vendoring** — see "Edit-as-comment" above for the full reasoning; contenteditable
  (textarea-swap) fallback shipped instead, as explicitly sanctioned by Mike's spec.
- **Rich diff view** — `wordDiff()`/`renderDiffHtml()` is a small LCS-based word-diff, not a
  real diff library (no move-detection, no line-level grouping for long blocks). Fine for
  block-sized text (a paragraph, a list item); would look noisy on a full-page edit.
- **Nightly-report auto-discovery per workspace is regex-filtered but not validated against
  real nightly worktree names for `playmaker`/`legends`** — the `nightly_filter` patterns in
  `workspaces.json` are a best guess (`"playmaker\\|capture\\|character-memory\\|landing\\|izzy"`
  for playmaker, `"legends"` for legends) based on what `.nightly-*` directories exist today;
  revisit if new nightly worktree naming conventions appear.
- **No workspace-creation UI** — new workspaces are added by hand-editing `workspaces.json`.
  Fine for the 4 shipped ones; would need a form + validation if this became self-serve.
- **Voice-in has no error surfacing beyond hiding the button** — if `SpeechRecognition` exists
  but permission is denied or the mic is unavailable, `rec.onerror` just stops the recording
  UI state; there's no toast explaining why. Acceptable for a v1 of voice-in; revisit if it
  becomes a friction point.

## Board + Portfolio (2026-07-03)

**Generated, never hand-curated.** Two documents compose from live streams and
overwrite themselves on every run — nobody edits `_estate/BOARD.md` or
`_estate/PORTFOLIO.md` by hand; they're regenerated nightly and on-demand.

**`v2/generate_board.py`** composes `_estate/BOARD.md` (now the `estate`
workspace's home page — `workspaces.json::estate.home`, replacing the
hardcoded morning-review default; Morning Review stays in the nav). Streams,
each independently fault-tolerant (`safe()` wrapper — one broken source never
kills the run):
1. `ESTATE.md` changelog entries (`### 2026-...` headers) from the last 48h.
2. New files in `SOMA/audits/` (mtime < 48h) — name + first `#` heading.
3. Memory index diff: `~/.claude/projects/-Users-mikewolf-Projects/memory/MEMORY.md`
   vs a cached copy in `_estate/board-state/MEMORY.md.cache` (added/changed
   `- [...]` lines only; cache advances every run).
4. Open review comments (`status` in `queued`/`seen`, not `deleted`) across
   every workspace's `_estate/review-feedback/**/*.jsonl`.
5. Status lines: `_estate/hygiene/LATEST-STATUS.txt` +
   `second-brain/scripts/freshness_report.py --summary` (best-effort, skipped
   on error/timeout — costs ~15s max, usually <1s).
6. **Board inbox** (`SOMA/board/inbox/`, contract below) — new cards surface
   once under "Shipped/changed" the run that processes them, then move to
   `inbox/processed/`. A `needs-mike: true` card additionally re-surfaces in
   "Needs Mike" on **every** regeneration for 48h post-processing (mtime-gated
   scan of `processed/`, `recent_needs_mike_from_processed()`) — a needs-mike
   item is real work-in-flight for Mike, and surfacing it exactly once was a
   real gap (caught during this build: a genuine cross-surface card vanished
   from the board on the very next regen before it was fixed).

Sections rendered: **Needs Mike** (open comments + needs-mike inbox cards +
changelog lines matching `Mike`/`decision`) → **Shipped/changed (48h)**
(changelog bodies + memory-index diff + this-run inbox cards) →
**Fleet status** (hygiene + freshness one-liners) → **Streams digest** (counts).

**`v2/generate_portfolio.py`** composes `_estate/PORTFOLIO.md`, Mike's
cancel/restart triage board. Sources:
1. `PROJECT-REGISTRY.json` — every project, grouped by lifecycle tag into
   **Active** (`active`/`active(live)`/`canonical`/`canonical(docs)`/`infra`/
   `infra-stable` — no verdict needed), **Parked/incubating/dormant**
   (`parked`/`incubating`/`archive-lean`/`archive-lean/incubating`/
   `fork-dup`/`fork-worktree`/`unmapped` — verdict needed; anything not
   explicitly classified defaults into this bucket rather than being
   silently dropped), and a small already-`archive`/`vendor` footnote
   (out of scope, no verdict UI). **No literal `dormant` tag exists in the
   registry today** — `parked`/`incubating`/`archive-lean`-shaped tags are the
   closest real signal (see the registry's own taxonomy, `PROJECT-REGISTRY.json`).
   Recency is reported as `branch` + dirty/clean, not a commit date — the
   registry has no "last commit" field and shelling `git log` per repo (~90
   repos) would make this slow; revisit if real recency becomes load-bearing.
2. `_estate/PRODUCTIVITY-OPPORTUNITIES-2026-07-02.md` — Tier 1 table rows +
   Tier 2/Tier 3/Watch-items bullets (with wrapped-line joining, since several
   bullets in that doc paragraph-wrap across lines) as ideas/open-item rows.
3. `_estate/audit-2026-07/raw-fleet-output-2026-07-01.json` — the directive
   that commissioned this build expected a `backlog.items` array here;
   **verified it does not exist in the file as shipped** (top-level keys are
   `summary`/`agentCount`/`logs`/`result`/`workflowProgress`/`totalTokens`/
   `totalToolCalls`; `result` has `districts`/`links`/`coverage`/`salvage`, no
   `backlog`). Parsed defensively (`parse_raw_fleet_backlog()` checks both
   `backlog.items` and `result.backlog.items`) — contributes 0 rows today,
   picks up automatically if a future regeneration of that file adds the
   shape.
4. `SOMA/SOMA-STATE.md` §5 ("What's broken or missing pieces" — `###` headings,
   skipping ones whose heading itself says FIXED/DONE/RUNNING) and §6
   ("What's designed but unbuilt" — a markdown table) — both best-effort.

**Verdict UI** — no new storage. Each verdict-needed row's rightmost cell gets
a `[[VERDICT:<row-id>]]` token; `mdblocks.py::_VERDICT_TOKEN_RE` +
`verdict_sub()` render it as four buttons (Keep/Restart/Cancel/Later,
`.verdict-btn` + `data-verdict`/`data-row-id`). `PAGE_JS::wireVerdictButtons()`
posts `{type:"verdict", verdict, row_id}` to the existing `POST /api/comments`
— `do_POST` accepts `verdict` as a third comment `type` alongside `comment`/
`edit`, stores `verdict` + `row_id` fields on the row, same JSONL sidecar.
Rendered distinctly in the comment thread (`renderCommentItem` → `is-verdict`
class, `badge-verdict-<verdict>` badge) and pre-marked on page load
(`loadThreadsIntoDOM()` finds the latest verdict comment per `row_id` and
writes a `✓ <verdict>` into that row's `.verdict-status` span — survives
reload without any extra endpoint). Verdict comments are excluded from the
pencil/trash (edit/delete) affordance, same as `type: "edit"`.

**"Regenerate board" button** — appears on both `estate/BOARD.md` and
`estate/PORTFOLIO.md` pages (`is_board_or_portfolio` check in `render_page()`).
`POST /api/board/regenerate` (`run_board_regenerate()`) shells both generator
scripts synchronously (each runs in well under a second) via `sys.executable`,
returns `{ok, board: {rc,stdout,stderr}, portfolio: {...}}`; the client
toasts and reloads the page on success.

**Nightly wiring** — `scripts/nightly-estate-hygiene.sh` step 5 runs both
generators (pinned to `$PY` = the same explicit-homebrew-python3 the rest of
the script uses — same launchd-PATH gotcha as everywhere else in this repo)
and folds a nonzero exit into the existing `problems[]`/`LATEST-STATUS.txt`/
overall-exit-code machinery, so a generator crash surfaces the same way a
failing launchd job does.

**Board inbox contract** (`SOMA/board/inbox/`, `processed/` subdir
auto-created): any surface — CDC, CCw, mobile/web via the email-dispatch path,
another CCc instance — can drop a `.md` or `.json` file there as a card. `.md`
cards: optional YAML-ish frontmatter block (`---\nneeds-mike: true\n---`) or a
bare `needs-mike: true` line anywhere in the first 20 lines; title = first `#`
heading found, else the filename. `.json` cards: `{"title": "...",
"needs-mike": true}` (or `needs_mike`, underscore accepted). Cards are
one-shot-consumed into the board (moved to `processed/` the run that reads
them) but a `needs-mike: true` card stays visible in "Needs Mike" for 48h
post-processing via the `processed/` mtime scan — see the needs-mike-persistence
note above. **Proven live during this build**: a parallel Dee/CDC session
independently wired the email→board intake path
(`claude@mike-wolf.com` → `claude-email-daemon` → files a card here) while
this session was building the generator — two cards
(`2026-07-03-e2e-intake-test-cross-surface-board-wiring.md`,
`2026-07-03-greg-call-prep.md`) landed in the inbox mid-session from that
independent path and were correctly picked up by `generate_board.py` on the
next run, no coordination between the two sessions required. That's the
intended cross-surface contract working as designed, not a test I staged.

**Verified end-to-end** (this build, 2026-07-03): both generators run clean
and idempotent (second run: 0 new memory-diff lines, 0 re-surfaced inbox
cards); `:8090/page/estate/BOARD.md` and `/PORTFOLIO.md` both 200; root `/`
redirects to `BOARD.md`; Playwright click on a Portfolio verdict button →
`{type:"verdict", verdict:"cancel", row_id:"project-ai-embassadors"}` landed
in `_estate/review-feedback/estate_PORTFOLIO.md.jsonl`, `✓ cancel` persisted
across a fresh page reload, comment-count pill appeared on the containing
table block; `POST /api/board/regenerate` returns `rc:0` for both scripts;
`nightly-estate-hygiene.sh` dry-run completed with the new step folded in, no
new failures. Test verdict comment soft-deleted after verification (not a
real Mike decision).

## Quinn tours of completed jobs (2026-07-03)

**Completed work is presented as guided tours, not bare receipt links** — the
same experience Greg gets from Quinn on Legends, running on the review surface.
The real soma-guide widget engine (CDN: `https://soma-guide.netlify.app/soma-guide.{js,css}`,
the exact build Legends loads) is mounted on tour-bearing pages with Quinn as
the persona (reused from Legends' reviewer — `personas.review` there; here she's
the primary persona, id `soma-review-quinn`, text mode, no ElevenLabs agent).

**`v2/tours.py`** is the whole feature. Per request (inherently idempotent — a
concurrent worker can keep adding completion pages), it globs
`_estate/completions/*.md` (excluding `COMPLETED.md`/`README.md`), parses each
with the SAME `mdblocks.parse_markdown` the renderer uses (so
`[data-anchor="…"]` step targets always match the rendered DOM), and builds one
3-step walkthrough per completion following the convention in
`_estate/completions/README.md`:
1. **What was done** — title block highlighted, opening paragraph narrated
   (the `**Date:** …` meta line is skipped).
2. **The receipts** — first `##` heading matching
   `receipt|evidence|commit|verif|proof` highlighted.
3. **See it live** — the block carrying the machine-parsed `**Live:** <url>`
   line highlighted; the link itself is already an external `target=_blank`
   autolink. No-live completions get a wrap-up step instead.

**Feature flag:** `workspaces.json::<ws>.tours == true` (estate only today) AND
the route must be tour-bearing (`estate/COMPLETED.md` or
`estate/completions/*.md`) — `tour_page_assets()` returns `None` everywhere
else and the page render is byte-identical. Asset build failures are caught in
`render_page()` and logged, never break rendering.

**Entry points:** (1) opening COMPLETED.md first-visit auto-opens Quinn's panel
offering the tour list (engine's introduce-once localStorage gate); (2) a
`▶ Tour` pill injected after every in-app link to a completion page (HELPER_JS
matches hrefs against a server-built `__SOMA_TOUR_INDEX__` route→tour-id map) —
click starts the tour in place, engine navigates to the completion page itself
(sessionStorage tour-state resume, same mechanism as Legends cross-page tours);
(3) deep link `?tour=<id>` on any tour-bearing page.

**Chat:** Quinn answers questions via the same VPS inference endpoint Bill uses
(`https://vpsmikewolf.duckdns.org/infer/ask`) with a knowledge pack compiled
from the tour set (one line per completed job). Voice affordances are hidden
via injected CSS (`.sg-btn-voice`, `.sg-io-voice`) since no ElevenLabs agent is
configured here; tours auto-advance on the engine's text-length fallback timer.

**Gotchas:**
- The engine is CDN-loaded — offline, the review pages still render fine (the
  module script just 404s; nothing else references it), but no Quinn.
- `_computeConfigHash` (stale-tour-state guard) hashes walkthrough/step ids, so
  a new completion page landing mid-tour invalidates saved tour state — safe,
  by design.
- The VPS infer endpoint was returning 500 on 2026-07-03 (Anthropic key out of
  credits — affects Legends' Ask-Bill too). Quinn degrades to "doesn't see that
  in the site content"; self-heals when credits are restored.
- v2 candidate: pre-generated audio narration via the `gen-tour-audio.mjs`
  hash-per-(agent|narration) path from soma-platform, once Quinn gets a
  voice/TTS route that makes sense locally.

Verified 2026-07-03 (Playwright, fresh headless context): 23 tours built from
the worker's completion set; auto-offer panel, 23 pills, pill-click cross-page
tour start, all 3 steps with correct highlights, live-link `target=_blank`,
deep-link start, commenting intact on tour pages, zero page errors. Evidence:
`_estate/evidence/quinn-tours/*.png`.

## Tour v2: live demo + verdict capture + review inbox (2026-07-03 evening, WQ-70)

Same-day v2 per Mike's directive ("continuous smooth flow… demo… approve or
recommend changes right there"). All in `v2/tours.py` + small `server.py` /
`generate_board.py` hooks, soma-review `79ad7ab`:

- **Live-demo step.** A completion page may carry a `## Demo` section with a
  machine-parsed `**Demo:** <product-url>?sg_tour=<walkthrough-id>` line
  (convention: `_estate/completions/README.md`). It becomes step 3 ("Live
  demo") in place of the plain See-it-live step: the highlighted block's
  autolink opens the REAL product page in a new tab and the product site's
  own guide starts the named walkthrough from the `sg_tour` param. The
  engine reads no URL params — each product site needs a small handler in
  its guide config (reference implementation:
  `legends-membership-site/js/legends-guide-config.js`, walkthrough
  `demo-scholarships-round2`, live). Playmaker does NOT embed soma-guide
  (V'Eric is homegrown), so Legends is the pilot surface.
- **Verdict capture.** Every completion tour ends on an `s9-verdict` step;
  HELPER_JS wraps the engine's `_renderWtStep` to mount ✅ Approve /
  ✏️ Recommend-changes buttons into `.sg-wt-ui` (auto-play is parked there —
  the tour can't auto-finish out from under the buttons; Mike ends with
  Finish). Approve POSTs a `type:"verdict", verdict:"approve"` row
  (`row_id: completion:<route>`) to the existing comments API. Recommend
  focuses the page-discussion box; on the next comment save (PAGE_JS's
  `postComment` now fires a `soma-comment-saved` CustomEvent) the comment
  text becomes BOTH a `recommend-changes` verdict row AND an RSI Development
  Request — `server.py::file_development_request()` writes
  `SOMA/rsi/requests/incoming/verdict-*.json` (app parsed from the page's
  `**Project:**` meta, reporter.role=admin, intent=idea) and runs
  `route_requests.py route` synchronously; the resulting board-card name is
  returned in the response (`_dr`) and confirmed in Quinn's panel. Verdict
  vocabulary in `do_POST` extended with `approve|recommend-changes`.
- **Review inbox.** COMPLETED.md's main list = UNREVIEWED completions only.
  `tours.py::review_state()` derives {route: verdict} from the latest
  non-deleted verdict row per sidecar JSONL at render time (never the .md as
  state); injected as `__SOMA_REVIEW_STATE__`; HELPER_JS moves reviewed
  `li`s into a collapsed `details.sg-reviewed` section (badge ✅/✏️ + the
  Tour pill travel with the item — reviewed stays tourable). Soft-deleting
  the verdict row returns the item to the inbox (verified). The Board's
  Done-today strip uses the same source (`generate_board.py::
  _reviewed_completion_names()` imports tours) → "N to review · M reviewed".

**Gotchas (v2):**
- `?tour=` deep links pre-consume the engine's introduce-once localStorage
  key (`soma-guide:soma-review-quinn:introduced`) — without it the 500ms
  first-visit auto-offer (`_openIdle`) stomps the deep-linked tour. The same
  race existed on Legends via its auto-greet: the `sg_tour` param does NOT
  survive the engine's own cross-page hops, so the greet fired on page 2 of
  the tour and killed it — guarded in the Legends config by checking the
  engine's `soma-guide-xp:legends-bill:wt-id`/`resume-id` sessionStorage keys
  (legends `b96574c7`).
- The wt panel container is `.sg-wt-ui` (there is no `.sg-wt`).
- Engine ready-gate = 2.5s poll then PROCEEDS (`READY_GATE_MS`) — a
  login-gated demo page cannot make the tour wait for auth; the convention
  is an instruction step ("sign in, then press Next"), documented in
  `_estate/completions/README.md`.
- The DR relay is best-effort by construction: routing failure lands in the
  verdict response's `_dr.error` and Quinn says so; the verdict row itself
  always persists. Router hardening that shipped with this: app strings
  like `SOMA (tools/monitoring)` are slugified in card FILENAMES
  (SOMA `9422b1a`) — a `/` in the app used to crash `write_card`.

Verified 2026-07-03 evening, 21/21 headless e2e (approve → sidecar → inbox
partition → soft-delete undo; recommend → comment → DR → board card →
panel confirmation) plus the live continuous flow: review-surface demo step →
new tab on legends-membership.netlify.app → 3-step cross-page product tour,
zero page errors. Evidence: `_estate/evidence/quinn-tours/DEMO-*.png`;
receipts: `_estate/completions/2026-07-03-demo-tour-verdict-flow.md`.

## Anchoring v2: stable block identities + preserved unresolved marks (2026-09-01)

Comments no longer depend on renderer-order anchors. `v2/blockmap.py` maintains one
`<page>.blocks.json` identity ledger beside each JSONL sidecar. Rendered blocks expose an
opaque `data-block-id`; a mark stores that id plus code-point offsets, the exact normalized
quote, and the block-text hash. Reconciliation keeps identities through ordinary edits and
moves, remaps offsets, and verifies the quote before rendering. If verification cannot be
proved, the mark is retained with `unresolved: true` and shown in the page's **Unresolved
marks** section. Never attach an uncertain mark to a merely nearby block.

Storage operations share stable `.lock` files and use atomic replacement for read-modify-write
paths. Comment creation validates bindings server-side: a stale id with one unique exact quote
is repaired; an absent or ambiguous quote returns HTTP 409 and writes nothing. Dispatch omits
unresolved marks and identifies attached marks by opaque id, heading path, and exact quote.

Migration is `python3 v2/migrate_to_v2.py --dry-run` followed, with the launchd service stopped,
by `python3 v2/migrate_to_v2.py`. It is idempotent and takes an exclusive pristine backup of each
sidecar as `<name>.jsonl.pre-v2.<utc>.bak`. The 2026-09-01 live migration preserved all 49 rows:
16 bound by existing anchor, 6 recovered from a unique snapshot prefix, 13 retained unresolved,
and 14 page-level rows unchanged. Eleven block maps and eleven backups were created.

Rollback: stop `com.mikewolf.soma-review`, restore each `.pre-v2.*.bak`, remove the corresponding
`.blocks.json` files, revert the anchoring code, and restart. Restored v1 anchors may themselves
be stale if source markdown changed after the backup.

Known limit: `_estate/BOARD.md` is regenerated from scratch. Content matching cannot guarantee
identity for rows that disappear or change substantially; those marks remain visible and may
reattach when their exact text returns. Generator-emitted semantic ids are the future fix.

## v3 view: Playmaker-model mark layer (2026-09-03)

A second page view ships alongside the classic sentence-dwell mark layer above, as a MARKED
IMPLEMENTATION: both views exist in the running server at once, toggled by `?view=v3` in the URL
(persisted to `localStorage['soma-review-view']` by the toggle control, which auto-redirects a
returning v3 preference forward when a URL carries no explicit `view`). Default is classic — a
fresh browser with no stored preference and no query param always renders classic. Both views
read and write the SAME per-page sidecar; a mark made in either view appears in the other (D1:
marks anchor to the block model, never to DOM position).

**What v3 is, model-wise:** `render_page(route_path, workspace, view=)` sets
`mark_layer = ... and view != 'v3'`, so v3 is literally "the dwell/keyboard mark layer switched
off" — which means the pre-mark-layer block-comment path (`wireBlockAffordances`,
`wireEditableBlocks`, `wireEnterOpensComment`, `loadThreadsIntoDOM`, all already in `PAGE_JS`,
unconditionally shipped, just unreached while `__MARK_LAYER__` is true) runs instead. Every
block is `contenteditable`-by-click already, per the existing "Edit-as-comment" section above:
click into a block, it reads plain while focused, blur diffs it against the source and persists
a `type: "edit"` comment (kind `edit` in v3's vocabulary) — v3 adds no new editing code, it just
un-gates code that already existed. New, v3-only work:

- **Level label** (finding 5, `mdp-as-ui-prior-art.md` Part 4): `compute_level(src, route_path)`
  reads a leading YAML-ish front-matter `level:` field if present; else pages under
  `SOMA/shared-cognition/` (or `soma/shared-cognition/` post-workspace-prefix) default to
  `"meta: the mark layer"` and everything else defaults to `"object"`. Rendered as a pill in the
  v3 header only (reading front matter never strips it from the classic render, so this cannot
  change a classic byte).
- **Marks panel** (`V3_JS`, right-side slide-in): fetches the existing `GET /api/comments`
  endpoint (no new read endpoint), reuses the classic mark-layer's `K` kind vocabulary
  (agree/clarify/rewrite/strike/note/ack/ruling — the Playmaker source/decision kinds that pass
  already ported from `playmaker/public/mark-layer.html`) plus `comment`/`edit`/`verdict` for the
  kinds unique to the always-editable v3 surface. Filter chips per kind, status pill
  open/resolved/**stale** per row, click a row (or the `.comment-count-pill` on a marked block —
  clicking the block body itself stays reserved for entering edit mode) opens a dialog. Resolve
  (`POST /api/comments/status {status:"done"}`) advances to the next open mark in the filtered,
  timestamp-ordered list (PM law); Reopen sets `status:"queued"`.
- **Stale detection** (finding 1): every comment already carries `block_id` + `block_text_sha`
  (set by `validated_binding()` for any block-anchored row, comments included, not just marks) —
  `V3_JS`'s `decorate()` compares that stored hash against the block-wrap's live
  `data-block-sha` and flags `stale` client-side. No new server field.
- **Closeable, resizable sidebar**: `V3_JS::wireSidebar()` injects a close button and a drag
  handle at runtime (DOM-only — no server-rendered markup changes), state in
  `localStorage['soma-review-sidebar-closed'|'soma-review-sidebar-width']`.
- **Passive `\`\`\`widget` blocks** (item 6, both hosts per
  `SOMA/shared-cognition/marked-document-widgets.md`, written in parallel by Mike the same day):
  `mdblocks.py::render_widget_block()` renders the fence body as raw HTML in a sandboxed iframe
  (`srcdoc`, `sandbox="allow-scripts"` only — no `allow-same-origin`, no network egress). Fence
  syntax follows that spec's `kind=`/`name=` attribute grammar (`parse_widget_attrs()`); bare
  \`\`\`widget defaults to `kind=passive name=inline-html`. `demo` and `active` kinds render a
  labeled "not yet supported" placeholder instead of crashing or silently misrendering — they are
  the two ACTIVE/interactive kinds that spec documents as next-step work, not built here. Widget
  CSS lives in the always-included `PAGE_CSS` (not view-gated) since a widget block is document
  content, renderable in either view.

**The only classic-page (no `?view` param) additions**, verified by `curl` diff against a
pre-change baseline: the shared view-toggle control (`render_view_toggle()`, required in both
views by spec item 1) plus its tiny bootstrap script, `window.__V3_VIEW__`/`__LEVEL_LABEL__`
globals, and the widget CSS rules (needed regardless of view). Every block, comment, and the
existing dwell mark layer are byte-identical otherwise. `v2/tests/test_v3_view.py` pins this
scope plus the edit-mark/stale/widget mechanics; 38/38 tests pass (24 pre-existing + 14 new).

**Not built (documented as next-step):** the `demo` and `active` widget kinds (`proposeMark`
contract, same-origin execution, capability declaration in the panel) — see the parallel design
spec above for the full three-kind contract both hosts (soma-review, Playmaker) are meant to
share once built.

## Authorship

v2 built 2026-07-02 by Dee (Claude Sonnet 5, engineering-lead/COO role) per Mike's spec
(same date, verbatim in the dispatching prompt): "single interactive experience at a URL;
links go to other pages, not other documents; every page has feedback mechanisms; comments
must reach Claude with full context." Branch `v2-collab-pages`. This is SOMA's "collaboration
space" reference implementation, dogfooded on the 2026-07-02 morning review itself.

v2.1 pass, same date, same author (Dee, Claude Sonnet 5): link-rendering bug fixes (bare-URL
autolink + nested-placeholder unstash bug + non-.md link handling), edit-as-comment
(contenteditable fallback), comment edit/delete (soft-delete) API + UI, project workspaces
(`estate`/`playmaker`/`platform`/`legends`), voice-in via Web Speech API. Verified end-to-end
against the live `estate` workspace's morning review doc plus all 3 new workspaces, using
Playwright against a local test-port instance of the server (see verification evidence in the
dispatching session's report).

Anchoring v2 implemented and migrated 2026-09-01 by Codex (GPT-5.6), continuing the design and
measured migration plan developed by Mike with Claude earlier that day. Verification covered
the pure matcher/remapper tests, API binding behavior, idempotent copied-data migration, the live
49-row migration, service health, and the rendered unresolved-marks surface.

v3 view (Playmaker-model mark layer, marks panel, level label, sidebar resize/close, passive
`\`\`\`widget` blocks) built 2026-09-03 by Dee (Claude Sonnet 5, engineering-lead/COO role) per
Mike's spec that day: ship it as a marked implementation alongside classic, default off, toggled
by URL param + localStorage. Verified against the live `mdp-as-ui-prior-art.md` page in a real
browser (Claude Browser pane): edit-mark creation + diff rendering, marks-panel filter/dialog,
Resolve status transition, level-label pill, widget iframe rendering; `curl` diff confirmed the
classic-view byte scope described above; multi-mark Resolve-advances-to-next driven end-to-end
(two open marks, resolving the first auto-opened the second's dialog, panel showed
RESOLVED/OPEN correctly); sidebar close/reopen click-tested end-to-end (close hid the sidebar,
computed width 0, "≡ Menu" affordance appeared; reopening restored it) — an initial float-based
close-button position bug was found and fixed (`position:absolute` instead) during this pass;
38/38 `pytest` (24 pre-existing + 14 new in `v2/tests/test_v3_view.py`). Not verified in this
pass: the sidebar's drag-to-resize mouse interaction itself (open/close was click-tested; the
`mousedown`/`mousemove` resize handle was exercised only by code inspection).
