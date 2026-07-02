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
routing, whitelist, markdown rendering, comment API, dispatch). `v2/mdblocks.py` — the
dependency-free markdown → block-list parser + inline HTML renderer (not full CommonMark;
handles headings, paragraphs, lists incl. nesting, GFM pipe tables, fenced code, blockquotes,
hr, links/bold/italic/inline-code — the subset the estate's docs actually use).
`v2/dispatch-prompt-template.md` — the editable prompt sent to `cc-dispatch` when Mike clicks
"Send to Dee" on a page.

**Run it:** `SOMA_REVIEW_PORT=8090 /opt/homebrew/bin/python3 v2/server.py` (or via the launchd
service below). URL: **http://localhost:8090/page/estate/MORNING-REVIEW-2026-07-02.md**
(root `/` redirects to the home page). No external Python packages — stdlib only
(`http.server`, `json`, `re`, `subprocess`). Zero `npm`/`pip install` step.

## What v2 is

A local interactive review app: estate markdown rendered as linked in-app pages (relative
`.md` links are rewritten to `/page/...` routes so navigation never leaves the app), every
block (paragraph/heading/list/table/etc.) gets a hover comment affordance, comments persist
server-side, and a page can be dispatched to a `cc-dispatch` worker ("Send to Dee") that reads
the comments and replies/acts inline. Replaces scroll-through-chat review.

## Whitelisted roots

Configured in `v2/server.py::WHITELIST_ROOTS` — currently `_estate/`, `business-ops/`,
`SOMA/`, plus a synthetic `nightly/<worktree-slug>` route that auto-discovers
`~/Projects/.nightly-*/NIGHTLY-REPORT.md` worktrees at request time (no server restart needed
when a new nightly worktree appears). `business-ops/SPEND-INVENTORY-DRAFT.md` and
`LEDGER.csv`-adjacent files are in-scope and serve fine — they're sensitive-local (never
leave the machine, this server only binds `127.0.0.1`), not secret-from-Mike.

Path resolution rejects anything outside its root (`os.path.normpath` + prefix check) and
anything that isn't a `.md` file. Unresolvable/off-whitelist links render inert (`#unresolved`)
rather than navigating away or 500ing.

## Home page + nav

Home = `_estate/MORNING-REVIEW-2026-07-02.md`. Sidebar (`v2/server.py::NAV_ENTRIES`) lists:
Morning Review, Productivity Opportunities, Business Plan, Doc-Proofing Plan, Overnight
Manifest, SOMA App Standard, Vision Interview — plus an auto-generated "Nightly Reports"
section from the `.nightly-*` discovery above.

**These are hardcoded to the 2026-07-02 morning review artifacts.** When the estate does its
next morning review cycle, update `NAV_ENTRIES` and `HOME_PAGE` in `v2/server.py` to point at
the new dated files (or generalize to a manifest — not done yet, see Gotchas).

## Comment sidecar format

One JSONL file per page at `_estate/review-feedback/<page-slug>.jsonl`, where `page-slug` is
the route path with `/` → `_` (e.g. `estate/MORNING-REVIEW-2026-07-02.md` →
`estate_MORNING-REVIEW-2026-07-02.md.jsonl`). Append-only for new comments; status updates
rewrite the whole file (small files, fine). Each line:

```json
{
  "id": "uuid",
  "page": "estate/MORNING-REVIEW-2026-07-02.md",
  "anchor": "b12-7077ff901e",
  "snapshot": "first ~80 chars of the block text at comment time",
  "author": "mike",
  "text": "comment body",
  "timestamp": "2026-07-02T14:52:23Z",
  "status": "queued",
  "thread_id": "uuid (same as id for the root comment; replies share it)"
}
```

`anchor` is `null` for page-level (bottom-of-page discussion) comments. The anchor is derived
from heading-path + block-index + a hash of the first 40 chars of block text
(`mdblocks.py::_make_anchor`) — stable across reloads of an unchanged doc; if the doc is
edited, `snapshot` is what lets a human (or Claude) figure out what the comment was about even
if the anchor no longer matches anything.

## Comment API

All endpoints are JSON, served by `v2/server.py`'s `Handler`. No auth — binds `127.0.0.1` only.

- `GET /api/comments?page=<route>` → array of comment objects for that page (root + replies
  interleaved, sorted client-side by timestamp).
- `POST /api/comments` `{page, anchor, snapshot, text, author?}` → creates a new root comment,
  `status: "queued"`, `thread_id` = its own id. Returns the created object, `201`.
- `POST /api/comments/reply` `{page, thread_id, text, author?, status?}` → appends a reply into
  an existing thread (used by the dispatched worker to answer Mike). `author` defaults
  `"claude"`; pass `"dee"` or another persona name to be explicit. `status` defaults `"seen"`.
- `POST /api/comments/status` `{page, id, status}` → updates one comment's status in place.
  `status` must be one of `queued|seen|in-progress|done`.
- `POST /api/dispatch` `{page}` → fires `cc-dispatch review-comments-<slug> <prompt-file>`
  (see below), returns `{ok, task_name, pid}` immediately (fire-and-forget; does not block on
  completion).
- `GET /page/<route>` → rendered HTML page. `GET /healthz` → `{ok: true, ts}`.

## Send to Dee (dispatch)

Each page with a "Send to Dee" button (any whitelisted route) POSTs `/api/dispatch`, which:
1. Writes the filled `v2/dispatch-prompt-template.md` (page path, fs path, sidecar path, API
   base URL substituted in) to `_estate/review-feedback/.dispatch-prompt-<slug>.md`.
2. Shells out to `~/.local/bin/cc-dispatch review-comments-<slug> <prompt-file>` with an
   **explicitly widened `PATH`** (prepends `/opt/homebrew/bin:/opt/homebrew/sbin`) — see
   Gotchas, this is load-bearing.
3. `cc-dispatch` forks, runs a Claude worker with filesystem access, which reads the sidecar
   JSONL, marks comments `seen` → does the work → replies via `POST /api/comments/reply` →
   marks `done` (or `in-progress` if it deliberately deferred to Mike), and writes its own
   audit report to `~/Projects/SOMA/audits/`.

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
- **`NAV_ENTRIES` and `HOME_PAGE` are dated to 2026-07-02.** They don't auto-discover "today's
  morning review" — someone (Mike or Claude) has to update `v2/server.py` when a new morning
  review cycle starts. A future version could generalize this to scan `_estate/*.md` by
  modification date, but that wasn't built (kept for time — see What was cut).
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

## Authorship

v2 built 2026-07-02 by Dee (Claude Sonnet 5, engineering-lead/COO role) per Mike's spec
(same date, verbatim in the dispatching prompt): "single interactive experience at a URL;
links go to other pages, not other documents; every page has feedback mechanisms; comments
must reach Claude with full context." Branch `v2-collab-pages`. This is SOMA's "collaboration
space" reference implementation, dogfooded on the 2026-07-02 morning review itself.
