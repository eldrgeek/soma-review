---
district: soma-core
status: active
depends_on: [cc-dispatch]
capabilities: [document-review, anchored-comments, local-collab-pages]
last_reviewed: 2026-07-02
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
pre-2026-07-02 rows that lack the field) or `"edit"` (suggested-edit, has `proposed`).

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
  for a suggested-edit. Returns the created object, `201`.
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
