# Spec: Todoist reconcile + cleanup for financial-agent-mcp

Status: proposed
Owner: finance MCP server
Motivation: over time the Todoist Finance project (`<FINANCE_PROJECT_ID>`) can
drift — tasks get created but never retired, so stale leftovers accumulate
alongside the legitimate ones. Root cause: there is **no server-side LIST
capability** to see what is actually in the project. Everything today is keyed by
`surface_key` and only reconciled one-task-at-a-time by reading a known task id.

All file:line citations are against the maps gathered 2026-06-25. Code paths:
- `src/financial_agent/todoist_outbox.py` (HTTP + ledger)
- `src/financial_agent/server.py` (MCP tool wrappers)
- `src/financial_agent/onboarding.py` (candidate apply/reject)
- `src/financial_agent/surface_queue.py` (surface item builders)
- `src/financial_agent/config.py` (gate config)
- `src/financial_agent/schema.py` (tables)

## Ground-truth corrections to the proposal assumptions

1. **The client is ALREADY on `/api/v1`.** Proposal item 3 assumes the server is
   on `rest/v2` and HTTP 410. It is not. `TODOIST_BASE_URL =
   "https://api.todoist.com/api/v1"` (todoist_outbox.py:31); every call
   (`_write_request` 34-50, `_read_task` 59-83) concatenates that base. There is
   **no** `rest/v2` string anywhere in `todoist_outbox.py`. Item 3 collapses to a
   short verification/audit task plus the *new* LIST call, which must use the v1
   paginated list shape. See section 3.

2. **There is no DELETE function today.** "deleted" is only inferred from a 404
   on read (todoist_outbox.py, `TASK_NOT_FOUND` sentinel at :56, set in
   `_read_task` on `exc.code == 404`). Apply mode of the new tool needs a new
   `_delete_request` helper. See section 1.

3. **There is no persisted candidate -> Todoist task link** (MAP 4).
   `charge_onboarding_candidates` has no `todoist_task_id` and no surface_key
   column; its only outbound link is `existing_obligation_id`. Charge-onboarding
   candidates are **never** surfaced to Todoist by the current surfacing path —
   `surface_queue` only emits `data-sync-failed:`, `obligation-due:`,
   `followup:`, `goal:`, `estimate-review:`, `snapshot-due:` keys. So the "56
   onboarding tasks" in the drift were created by something OUTSIDE the
   `todoist_emissions` path (manual `create_todoist_task` or a legacy script).
   This shapes item 4 (we are designing a NEW surfacing behavior, not fixing an
   existing per-candidate emitter) and item 1 (orphan detection cannot rely on a
   stored candidate->task id).

4. **Only `_create_surface_task` attaches the `fa-auto` label and the `[fa:...]`
   marker.** Verified in code: `send_review_batch` (the daily ritual review task
   + subtasks) and `create_todoist_task` (free-form one-offs) both write tasks
   with **no `fa-auto` label and no `[fa:...]` marker**. The 56 onboarding tasks
   in the drift came through `create_todoist_task` / a legacy script, so they
   ALSO carry neither discriminator. Consequence: "no marker" is NOT a safe
   delete signal — it matches the 3 ritual reminders and every hand-made user
   task. The cleanup model in section 1 is built around this fact (three explicit
   non-overlapping delete rules, never "delete anything unmanaged").

5. **The v1 LIST/DELETE contract is UNVERIFIED.** The list endpoint, its
   pagination envelope, and the hard-DELETE verb are external Todoist contracts,
   not code-cited facts. They MUST be confirmed against current Todoist unified
   v1 docs before implementation (section 3, blocker). Treat every v1 list/delete
   detail below as provisional until that audit lands.

### Pre-implementation verification (BLOCKING — do before any code)

These two checks gate the whole tool; do them first and record the answers in
section 3 before writing the helpers:

- **V-LIVE — sample the real tasks.** Pull a live sample of the 56 + 8 drift
  tasks and record, per task: does it carry the `fa-auto` label? a `[fa:...]`
  marker? what is the exact title prefix? The spec ASSERTS these tasks have no
  discriminator; confirm it. The delete model (below) depends on which concrete
  signal is actually present. Do not run apply until this is confirmed.
- **V-V1 — confirm the v1 contract** (section 3): (a) the project-filter param
  name on `GET /tasks`, (b) the pagination envelope + cursor param name, (c)
  whether a hard `DELETE /tasks/<id>` exists or only `POST /tasks/<id>/close`,
  (d) whether the active-tasks list can EVER return completed/checked tasks.

---

## 1. `reconcile_todoist_project` (CORE)

### Purpose
Add the missing server-side LIST capability and a classify/clean pass over the
whole Finance project. This is the root-gap fix.

### New HTTP helpers (todoist_outbox.py)

```
def _list_tasks_request(token, project_id, *, cursor=None, timeout=30) -> dict:
    # GET https://api.todoist.com/api/v1/tasks?project_id=<id>[&cursor=<cursor>]
    # headers identical to _read_task (Bearer, Content-Type, User-Agent)
    # returns parsed JSON: {"results": [ {task}, ... ], "next_cursor": str|None}
```

v1 list endpoints are paginated: response is `{"results": [...], "next_cursor":
<str|null>}`. Caller loops, passing `cursor=next_cursor` until `next_cursor` is
null/empty. This is the default `read_func` for the new tool (a list reader, not
the single-task `_read_task`).

```
def _delete_request(token, task_id, *, timeout=30, max_retries=5) -> bool:
    # DELETE https://api.todoist.com/api/v1/tasks/<id>
    # 204/200 -> True; 404 -> True (already gone, idempotent)
    # 429 -> read Retry-After header (default exponential backoff
    #        0.5,1,2,4,8s capped), sleep, retry up to max_retries
    # other 4xx/5xx after retries exhausted -> raise
```

Default `delete_func` for the new tool's apply mode. Note Todoist update is a
POST (todoist_outbox.py:281-300), but delete is a real DELETE verb; confirm the
v1 delete verb during the audit (section 3, V-V1) — if v1 only supports
`POST /tasks/<id>/close`, swap the verb but keep the `(token, task_id) -> bool`
signature.

**Rate limiting (rev 9).** This tool deletes ~70 tasks one-by-one against Todoist
per-user request limits. `_delete_request` MUST detect HTTP 429, honor
`Retry-After`, and back off/retry before giving up; only a post-retry failure
counts as `actions.failed`. Document a per-run delete ceiling
(`MAX_DELETES_PER_RUN = 200`): once hit, stop deleting, set `truncated_deletes:
true` in the report, and require a re-run to finish. This protects against a
runaway loop and against tripping a hard rate-limit ban mid-cleanup.

### Impl: `reconcile_todoist_project_for_db` (todoist_outbox.py)

```
def reconcile_todoist_project_for_db(
    conn,
    *,
    as_of_date: str,
    apply: bool = False,
    write_enabled: bool | None = None,
    token: str | None = None,
    project_id: str | None = None,
    env_path: str | None = None,
    list_func=_list_tasks_request,
    delete_func=_delete_request,
) -> dict[str, Any]:
```

### MCP wrapper (server.py)
Follow the standard boilerplate (server.py:719-729): open sqlite, `row_factory =
Row`, call impl, `conn.commit()` (apply mode resolves ledger rows -> write),
`finally: conn.close()`.

```
@mcp.tool()
def reconcile_todoist_project(
    as_of_date: str,
    apply: bool = False,
    db_path: str | None = None,
) -> dict:
```

### Gating
Mirror `surface_to_todoist` (todoist_outbox.py:466-616). Resolve gate from
`get_finance_config()` when `write_enabled is None`: `todoist_write_enabled`,
`todoist_api_token`, `todoist_project_id` (config.py:69-86).
`live = bool(write_enabled and token and project_id)`.
- LIST requires `token + project_id` (reads are allowed even when
  `todoist_write_enabled` is off — config comment, config.py:86). So the
  **report** (dry-run) needs only token+project; it does NOT require
  `todoist_write_enabled`.
- **Apply (delete/resolve) requires `live` true**, i.e.
  `todoist_write_enabled` AND token AND project. If `apply=true` but not live:
  return the report with `applied=false` and `reason:"awaiting-integration"`,
  perform NO deletes and NO ledger writes.
- `apply` defaults to `false`: dry-run by default.

### Deletion model — three explicit, non-overlapping rules (revs 1, 2)

Safety invariant (HARD): **a task is deletable ONLY if it matches one of the
three positive delete rules below. Everything else is `kept` and is NEVER
deleted.** "No marker / no emission" is explicitly NOT a delete signal, because
the ritual reminders (`send_review_batch`) and manual tasks (`create_todoist_task`)
have no marker and no `fa-auto` label (ground-truth correction 4). The old
`orphan = no marker AND no emission` rule deleted those; it is removed.

The delete set is the union of exactly these three rules:

- **(a) fa-auto orphan** — task carries the `FA_AUTO_LABEL = "fa-auto"`
  (todoist_outbox.py:212) but has NO open `todoist_emissions` row mapping to its
  id (marker absent, or marker present whose emission is closed/missing). This is
  a task we created and then lost track of. Deletable.
- **(b) known-pattern legacy/onboarding cleanup** — task title matches a
  configured legacy-mess pattern, currently the exact prefix `"Onboard charge:"`
  (the 56 onboarding tasks). This is the ONLY rule that can delete a task lacking
  `fa-auto`, and it is deliberately narrow: it must match a closed allowlist of
  literal title prefixes (`LEGACY_CLEANUP_PREFIXES`), confirmed against the live
  sample in V-LIVE before apply is enabled. Deletable.
- **(c) duplicate extra copies** — see duplicate rule below; only the non-kept
  copies, and only when the surviving copy is itself managed or fa-auto.

If a task carries no `fa-auto`, no `[fa:...]` marker, and matches no
`LEGACY_CLEANUP_PREFIXES` entry, it is `kept` (covers the 3 ritual reminders and
all hand-made user tasks). This is the resolution of the core contradiction
(rev 2): rule (a) protects ritual/manual tasks, rule (b) still removes the 56
onboarding tasks by their literal title prefix.

### Classification algorithm
For each task returned by paginated LIST over `project_id`:

1. Extract marker: `surface_key = extract_surface_key(task.get("description"))`
   (todoist_outbox.py:221-237). Read the `fa-auto` label off the task's labels.
2. Join against `todoist_emissions` by `surface_key` (PK; schema.py:254-261).
3. Classify (precedence, first match wins:
   managed > stale-applied > duplicate > fa_auto_orphan > kept):
   - **managed** — has a marker AND a `todoist_emissions` row with
     `status='open'` AND that row's `todoist_task_id == task["id"]`. No action.
   - **stale-applied** — title matches a `LEGACY_CLEANUP_PREFIXES` entry
     (currently `"Onboard charge:"`) whose underlying candidate is no longer
     active. Look up the candidate (see "candidate join" below); if its status is
     in `DECIDED_STATUSES` (applied/rejected/deferred/merged/split,
     onboarding.py:46-48) the task is stale-applied — this is delete rule (b).
     If the title matches the prefix but no candidate matches, it is still a
     legacy-pattern task: classify `stale_applied` with `candidate_id: null` and
     `action: needs_review` (the prefix alone does not auto-delete an unmatched
     task; see candidate join).
   - **duplicate** — two or more tasks share the same `content_hash_for(content,
     description)` (todoist_outbox.py:240-244) OR the same extracted
     `surface_key`. Survivor selection (rev 11): **keep the copy whose id matches
     an open emission row; if none matches, keep the NUMERICALLY smallest id
     (`int(task["id"])`, not lexical — Todoist ids are large numeric strings).**
     Mark the rest `duplicate`. A duplicate copy is delete rule (c) ONLY if the
     survivor is itself `managed` or `fa-auto`; if the survivor is a `kept`
     user/ritual task, the "duplicates" are also `kept`.
   - **fa_auto_orphan** — carries `fa-auto` label but no open emission row maps
     to `task["id"]`. Delete rule (a).
   - **kept** — none of the above (no `fa-auto`, no managed emission, no legacy
     prefix). NEVER deleted. Covers ritual reminders and manual tasks.
   - **ledger-orphan** (ledger-side finding, NOT a listed-task classification) —
     an emission row with `status='open'` whose `todoist_task_id` was NOT seen in
     the LIST results. See "Ledger-orphan resolution" below — only acted on when
     the LIST was fully drained and non-truncated, and resolved to the new
     `retired` status, not `deleted_by_user` (rev 3, rev 8).

#### Candidate join (no stored link — MAP 4)
There is no `candidate.todoist_task_id`. To classify `stale-applied`, derive the
candidate by:
- title heuristic: parse merchant/display name out of `"Onboard charge: <name>
  not modeled"`; match against `charge_onboarding_candidates.display_name` /
  `merchant_key`.
This join is heuristic and lossy. Report each stale-applied task with
`match_confidence: "heuristic"` and the matched `candidate_id` (or null). In
apply mode, only delete stale-applied tasks whose candidate was matched AND is in
`DECIDED_STATUSES`; leave unmatched ones in the report as `needs_review` (never
deleted on the first run). The `LEGACY_CLEANUP_PREFIXES` allowlist plus the
V-LIVE live-sample confirmation are what make rule (b) safe: the prefix is a
narrow literal, and the unmatched-but-prefix case escalates to `needs_review`
rather than auto-deleting. (See Open questions, now resolved below.)

### Apply actions (only when `apply=true` and `live`)

**Precondition — truncation forces report-only (rev 7).** Duplicate
classification and ledger-orphan inference are both UNSOUND on a partial view: on
a truncated LIST you could delete the surviving copy of a duplicate whose other
copy is on an undrained page, or resolve a ledger row for a task that merely
lives on a page you never fetched. Therefore: **if `truncated:true` (page cap
hit) OR any LIST page request failed, force report-only — perform ZERO deletes
and ZERO ledger resolutions, set `applied:false`, `status:"truncated"`, and
return a hard flag.** Apply is permitted only over a fully-drained, fully
successful LIST.

**Precondition — delete gate (rev 1, HARD).** No task is deleted unless it falls
in the explicit delete set: rule (a) `fa_auto_orphan`, rule (b) matched+decided
`stale_applied`, or rule (c) `duplicate` extra copy with a managed/fa-auto
survivor. Any `kept` task is never passed to `delete_func`. This is asserted by
`test_kept_tasks_never_deleted`.

- delete set (a)/(b)/(c): `delete_func(token, task["id"])` (with 429 backoff and
  the `MAX_DELETES_PER_RUN` ceiling). On success, if the task had a marker with
  an open emission row, also `mark_emission_status(conn, surface_key,
  'retired')` (NOT `deleted_by_user` — see rev 3).
- ledger-orphan (only when LIST fully drained + non-truncated): resolve the open
  ledger row to `'retired'` via `mark_emission_status` (no HTTP; task already
  gone). Using `retired` (not `deleted_by_user`) keeps the surface_key eligible
  for recreation if the underlying item is due again (rev 3, rev 8).
- `kept`, `managed`: no-op.

#### New emission status: `retired` (rev 3)
`deleted_by_user` means "the human resolved this, never recreate" and is honored
by `surface_to_todoist` as a permanent suppressor (MAP 1). Reusing it for
auto-retire permanently kills any recurring `surface_key` — the singleton
`onboarding-digest` would never reappear after the candidate count returns above
0, and any retired-then-due-again `obligation-due:` item would be silently
suppressed forever. Introduce a distinct status `retired` with semantics: "we
removed the surfaced task, but the underlying need may recur — recreate normally
if it surfaces again." `surface_to_todoist`'s recreate-suppression query must
exclude `completed` and `deleted_by_user` ONLY; `retired` rows do NOT block
recreation. Add `retired` to the status set in `mark_emission_status` /
`schema.py` CHECK constraint if one exists.

#### Ledger-orphan resolution (rev 8)
Resolve an open emission to `retired` ONLY when ALL hold: (1) the LIST completed
without truncation, (2) every page request succeeded (no transient list failure),
(3) the task id was confirmed absent from the full drained result set. If any
LIST page failed or truncation occurred, do NOT resolve any ledger row — a
transient list gap must never be read as "user deleted it." Never use
`deleted_by_user` here.

### Return JSON shape

```json
{
  "status": "ok" | "awaiting-integration" | "truncated",
  "integration_enabled": true,
  "applied": false,
  "truncated": false,
  "truncated_deletes": false,
  "as_of_date": "2026-06-25",
  "project_id": "<FINANCE_PROJECT_ID>",
  "listed": 96,
  "counts": {
    "managed": 24,
    "stale_applied": 4,
    "duplicate": 6,
    "fa_auto_orphan": 60,
    "kept": 2,
    "needs_review": 3
  },
  "ledger_findings": {
    "ledger_orphan": 2
  },
  "actions": {
    "deleted": 0,
    "ledger_resolved": 0,
    "skipped_not_live": 0,
    "failed": 0
  },
  "tasks": [
    {
      "task_id": "12345",
      "content": "Onboard charge: Acme not modeled",
      "surface_key": null,
      "has_fa_auto": false,
      "classification": "stale_applied",
      "candidate_id": "cand_abc",
      "candidate_status": "applied",
      "match_confidence": "heuristic",
      "action": "deleted" | "would_delete" | "needs_review" | "kept",
      "reason": "candidate applied 2026-06-20"
    }
  ],
  "reason": "awaiting-integration"   // only when status != ok
}
```

`would_delete` is emitted in dry-run; `deleted` in apply mode. `actions.*` are 0
in dry-run.

**Count invariant (rev 10).** `ledger_orphan` is by definition NOT in the listed
set (those tasks are absent from LIST), so it lives under a separate
`ledger_findings` block, NOT under `counts`. The hard invariant the report must
satisfy:
`listed == managed + stale_applied + duplicate + fa_auto_orphan + kept`
(plus `completed` only if V-V1 confirms the active list can return checked
tasks — see rev 6). `needs_review` is a per-task `action`, not a disjoint
classification bucket, so it is reported as an action tally, not summed into
`listed`. Assert this invariant in `test_report_count_invariant`.

### Idempotency
- LIST is read-only.
- DELETE is idempotent: `_delete_request` treats 404 as success.
- A second apply run over a cleaned project finds nothing to delete (counts drop
  to managed-only). Re-running dry-run never mutates.
- Ledger resolution uses `mark_emission_status` (UPDATE by PK, idempotent).

### Edge cases
- Empty project: `listed:0`, all counts 0, `status:"ok"`.
- LIST pagination: must drain all pages before classifying duplicates (a dup can
  span page boundaries). Cap total pages (`MAX_LIST_PAGES = 50`); if hit, set
  `truncated:true` and force report-only (rev 7) — apply does nothing.
- Task with marker but emission row points at a *different* task id => the marked
  task is a `duplicate` (orphaned re-create) only if it also carries `fa-auto`;
  the emission's real task is managed. A marked-but-fa-auto-less mismatch is
  `kept`.
- Marker present but no emission row at all + `fa-auto` => `fa_auto_orphan`
  (rule a; marker written by a now-deleted ledger row). Report
  `classification:"fa_auto_orphan", had_marker:true`. Marker present, no
  emission, NO `fa-auto` => `kept` (do not delete).
- Completed/checked tasks (rev 6): **the v1 active-tasks list almost certainly
  returns ACTIVE tasks only** — completed tasks are a separate endpoint. Until
  V-V1 confirms the active list can return checked tasks, there is NO `completed`
  bucket and no `test_completed_tasks_excluded` (both would test an impossible
  input). If V-V1 confirms checked tasks CAN appear, add a `completed` bucket
  (no action) and include it in the count invariant; otherwise omit it entirely.
  Resolve this in section 3 before coding.
- `delete_func` raises after 429 backoff exhausted or on 5xx: count under
  `actions.failed`, item `action:"failed"`, continue; do not abort the whole run.
- A LIST page request fails: the LIST is incomplete -> force report-only exactly
  like truncation (rev 7/8): zero deletes, zero ledger resolutions,
  `status:"truncated"`, `truncated:true`.
- Token present but project_id missing: cannot LIST -> `status:
  "awaiting-integration", reason:"missing project_id"`.

### Test plan (hermetic — pytest)
All tests inject `list_func` and `delete_func`; no live HTTP. Pattern mirrors
existing `read_func`/`send_func` injection (todoist_outbox.py:371-463, 466-616).

- `test_list_paginates`: fake `list_func` returns two pages
  (`next_cursor` then null); assert all tasks classified, cursor passed through.
- `test_classify_managed`: seed `todoist_emissions` open row matching a listed
  task's marker+id; assert `managed`, no delete called.
- `test_kept_tasks_never_deleted` (HIGHEST VALUE — revs 1-2): seed (i) a
  marker-less, fa-auto-less manual task (as `create_todoist_task` writes) and
  (ii) a `send_review_batch`-style ritual task (no marker, no `fa-auto`). Run
  apply (live). Assert BOTH classify `kept`, `delete_func` is NEVER called for
  them, `actions.deleted` excludes them. This directly guards the data-loss path.
- `test_classify_fa_auto_orphan`: listed task WITH `fa-auto` label, no open
  emission; dry-run => `would_delete`; apply (live) => `delete_func` called once,
  `deleted:1`.
- `test_no_fa_auto_no_pattern_is_kept`: task with no `fa-auto`, no marker, no
  legacy prefix => `kept`, never deleted even in apply.
- `test_classify_stale_applied`: seed `charge_onboarding_candidates` row status
  `applied`; listed "Onboard charge: X" task (rule b prefix); apply => deleted;
  assert candidate untouched.
- `test_stale_applied_unmatched_is_needs_review`: title has `Onboard charge:`
  prefix but matches no candidate => `needs_review`, never deleted even in apply.
- `test_classify_duplicate_numeric_tiebreak` (rev 11): three listed tasks same
  `content_hash_for`, none matching an open emission, ids `"100"`, `"99"`,
  `"1000"`; assert survivor is `"99"` (numeric min), NOT `"100"` (lexical min);
  apply deletes `"100"` and `"1000"` only.
- `test_duplicate_keeps_emission_match`: among duplicates one matches the open
  emission id; assert it survives regardless of numeric order; apply deletes the
  others.
- `test_duplicate_of_kept_survivor_not_deleted`: duplicates whose survivor is a
  `kept` user task (no fa-auto, no marker) => duplicates also `kept`, no delete.
- `test_ledger_orphan_resolved_retired`: open emission whose task id absent from
  a fully-drained, non-truncated LIST; apply =>
  `mark_emission_status(...,'retired')` (NOT `deleted_by_user`), no delete_func
  call.
- `test_apply_blocked_when_truncated` (rev 7): page cap hit so `truncated:true`;
  `apply=True` + live => zero `delete_func` calls, zero ledger resolutions,
  `status:"truncated"`, `applied:false`, report still populated.
- `test_apply_blocked_when_list_page_fails` (rev 8): one LIST page request
  raises => same report-only behavior as truncation; no deletes, no ledger
  resolution.
- `test_report_count_invariant` (rev 10): assert
  `listed == managed + stale_applied + duplicate + fa_auto_orphan + kept`, and
  that `ledger_orphan` appears under `ledger_findings`, not `counts`.
- `test_dry_run_default_no_writes`: `apply` default false => `delete_func` never
  called, emissions table unchanged, `applied:false`.
- `test_apply_requires_live`: `apply=True` but `write_enabled=False` =>
  `status:"awaiting-integration"`, no delete, report still populated.
- `test_delete_404_idempotent`: `delete_func` returns True on 404; classified
  task counted `deleted`.
- `test_delete_429_backoff` (rev 9): `delete_func` raises 429 once (with
  `Retry-After`) then succeeds; assert it retries, the task is counted `deleted`,
  and the run does not fail.
- `test_delete_ceiling_truncates` (rev 9): more deletable tasks than
  `MAX_DELETES_PER_RUN`; assert deletes stop at the ceiling and
  `truncated_deletes:true`.
- `test_delete_failure_isolated`: one `delete_func` raises (post-retry); others
  still processed; `actions.failed:1`.
- `test_completed_tasks_excluded` (rev 6): INCLUDE ONLY IF V-V1 confirms the
  active list can return checked tasks. If V-V1 says active list is
  active-only (expected), DROP this test and the `completed` bucket — it would
  test an impossible input.

---

## 2. Auto-cleanup at the source

### Problem
Tasks are created but never retired (root cause). When a candidate is
applied/rejected, or an obligation retired, any linked Todoist task should be
removed on the next surface run.

### Constraint from the code (MAP 4)
There is no candidate->task link and candidates are not surfaced via
`todoist_emissions` today. So "linked Todoist task" for a candidate is, in
practice, the transitive `obligation-due:<obligation_id>:<due_date>` emission
that exists only *after* apply created an obligation. Direct candidate tasks (the
"Onboard charge:" ones) are out-of-band and only reachable via item 1's project
reconcile.

### Design: mark-for-removal flag, drained on surface run
Rather than deleting inline (apply path impls do not commit; server wrapper
commits — MAP 2), set a tombstone the next `surface_to_todoist` run honors.

1. Schema add (schema.py, inside `ensure_app_schema`): new nullable column on
   `todoist_emissions`:
   `retire_requested_at TEXT` (NULL default). `ALTER TABLE ... ADD COLUMN` guard
   (idempotent IF NOT EXISTS pattern already used in `ensure_app_schema`).

2. New helpers (todoist_outbox.py). Two entry points — exact and prefix —
   because `obligation-due` keys carry a per-instance date suffix
   (`obligation-due:<obligation_id>:<due_date>`) and cannot be retired by an
   exact `surface_key` (rev 4):
   ```
   def request_emission_retire(conn, surface_key) -> dict:
       # exact match
       # UPDATE todoist_emissions SET retire_requested_at=? WHERE surface_key=? AND status='open'
       # returns {matched: surface_key, retire_requested: rowcount}

   def request_emission_retire_prefix(conn, surface_key_prefix) -> dict:
       # prefix match (e.g. "obligation-due:<obligation_id>:")
       # UPDATE todoist_emissions SET retire_requested_at=?
       #   WHERE surface_key LIKE ? || '%' AND status='open'
       # returns {prefix: surface_key_prefix, retire_requested: rowcount}
   ```
   Use the exact helper for singletons (`onboarding-digest`); use the prefix
   helper for `obligation-due:<obligation_id>:` so EVERY due-date instance of that
   obligation is retired in one call. Both only touch `status='open'` rows and set
   intent (`retire_requested_at`); the actual delete happens in the drain (step 4)
   and flips status to `retired`, not `deleted_by_user`.

3. Hook points (call within the existing server wrappers, which already commit):
   - `record_charge_onboarding_decision` (server.py:777-801, impl
     onboarding.py:614-673): when resulting status is `rejected`/`deferred`,
     after the decision, if the candidate has an `existing_obligation_id`, call
     `request_emission_retire_prefix(conn,
     f"obligation-due:{existing_obligation_id}:")` to retire all due-date
     instances (rev 4).
   - `apply_charge_onboarding_candidate` (server.py:838+, impl onboarding.py:706+):
     on apply, the candidate becomes an obligation — the "Onboard charge:" task
     is now stale. Mark for retire any emission whose surface_key references the
     candidate; for the out-of-band "Onboard charge:" task there is no emission,
     so this case is covered only by item 1's `stale_applied` path. Document that
     gap explicitly.
   - Obligation retirement (status -> `inactive`/`dormant_suppressed`,
     schema.py:17-21): wherever obligation status is set inactive, call
     `request_emission_retire_prefix(conn,
     f"obligation-due:{obligation_id}:")`. **Owner (resolves OQ2):** locate the
     function that flips obligations to `inactive`/`dormant_suppressed` (candidate
     owner: the `suppress_dormant_estimates` background step — grep
     `dormant_suppressed`/`set ... status` in background.py / obligations code)
     and attach the hook there. This is a BLOCKER: without the owning function the
     `obligation-due:` retire path has nowhere to attach and the feature is
     half-built. Confirm the owner before coding section 2.

4. Drain in `surface_to_todoist` (todoist_outbox.py:466-616): before the
   per-item loop, when `live`, query open emissions with
   `retire_requested_at IS NOT NULL`, call `delete_func(token, task_id)` (the new
   `_delete_request`, with 429 backoff), then
   `mark_emission_status(...,'retired')` (rev 3 — NOT `deleted_by_user`, so the
   surface_key can recreate when it next becomes due). Add `retired` to the return
   summary dict (alongside `created/updated/skipped/resolved/failed`,
   todoist_outbox.py return shape).

### Gating / idempotency
- `request_emission_retire` is a pure local UPDATE — runs regardless of
  `todoist_write_enabled` (just sets intent). Safe in dry-run.
- The DELETE drain only fires when `surface_to_todoist` is `live`. Idempotent:
  once status flips to `retired`, the row is excluded from the `status='open'`
  retire query. Critically, `retired` (unlike `deleted_by_user`) does NOT block
  the recreate path, so the same surface_key resurfaces normally when due again.

### Edge cases
- Retire requested but task already deleted by user: `_delete_request` 404 =>
  success, still flip ledger to `retired`.
- Candidate rejected then re-accepted (`reset`->`proposed`, onboarding.py:56-63):
  clearing the tombstone — on apply/accept, also clear `retire_requested_at`
  (`SET retire_requested_at=NULL`) so a re-surfaced item is not immediately
  retired.

### Test plan
- `test_request_emission_retire_sets_flag`: seed open emission; call helper;
  assert column set, rowcount 1; non-open row untouched.
- `test_request_emission_retire_prefix_multi_instance` (rev 4): seed TWO open
  emissions `obligation-due:OB1:2026-06-01` and `obligation-due:OB1:2026-07-01`
  plus an unrelated `obligation-due:OB2:2026-06-01`; call
  `request_emission_retire_prefix(conn, "obligation-due:OB1:")`; assert BOTH OB1
  rows flagged, OB2 untouched, rowcount 2.
- `test_reject_decision_marks_retire`: build candidate with
  `existing_obligation_id`, seed two matching `obligation-due:<id>:<date>`
  emissions; call `record_charge_onboarding_decision_for_db(..., {"action":
  "reject"})` via wrapper; assert prefix retire flagged both.
- `test_surface_drains_retire_live`: seed retire-flagged emission; run
  `surface_to_todoist(..., write_enabled=True, send_func=fake,
  delete_func=fake)`; assert delete called, status `retired`, summary `retired:1`.
- `test_retire_then_recreate_resurfaces` (rev 3 — resurrection guard): retire a
  recurring `obligation-due:OB1:2026-06-01` emission (status -> `retired`); then
  drive a NEW due instance `obligation-due:OB1:2026-07-01` through
  `surface_to_todoist`; assert it IS created (the `retired` row does NOT suppress
  recreation). Contrast: a `deleted_by_user` row WOULD suppress — assert that too.
- `test_digest_retire_then_resurface` (rev 3): retire singleton
  `onboarding-digest` when N->0 (status `retired`); then N>0 again; assert the
  digest is recreated, not permanently suppressed.
- `test_surface_drain_skipped_when_not_live`: same seed, `write_enabled=False`;
  no delete, flag remains, `retired:0`.
- `test_reset_clears_retire`: flag set, then accept/reset path clears it.

---

## 3. Verify/migrate Todoist client to `/api/v1`

### Finding (MAP 1)
**Already done.** `TODOIST_BASE_URL = "https://api.todoist.com/api/v1"`
(todoist_outbox.py:31). All three current calls are v1:
`POST /api/v1/tasks`, `POST /api/v1/tasks/<id>`, `GET /api/v1/tasks/<id>`. No
`rest/v2` string exists in the module. The proposal's "HTTP 410 on rest/v2"
concern does not apply to the current code.

### Remaining work (small) — but V-V1 is a hard prerequisite (rev 5)

The list endpoint, its pagination envelope, and the DELETE verb are EXTERNAL
Todoist contracts, presented in section 1 as fact with no code citation (the
codebase has never listed or deleted). The entire tool hinges on them. Confirm
against current Todoist unified v1 docs BEFORE implementation and record the
answers here. Use the `find-docs` skill / Todoist API reference; do not assume.

1. **Audit grep** (CI guard, hermetic test): assert no occurrence of
   `rest/v2` or `api.todoist.com/rest` anywhere under `src/`. Add
   `test_no_v2_rest_endpoints` that greps the source tree and fails on any hit.
2. **V-V1(a) project filter param**: confirm the exact query param name to
   filter `GET /tasks` by project (the spec assumes `project_id=`). Record it.
3. **V-V1(b) pagination envelope + cursor param**: confirm the response envelope
   (spec assumes `{"results":[...], "next_cursor":...}`) AND the request param
   name to pass the cursor (spec assumes `cursor=`). `_list_tasks_request` MUST
   match the confirmed shape, not a bare array. Record both.
4. **V-V1(c) delete verb**: confirm a hard `DELETE /api/v1/tasks/<id>` exists. If
   v1 only offers `POST /tasks/<id>/close` (complete, not delete), decide:
   close-semantics still removes the task from the active list, which is
   sufficient for cleanup — but it then appears in the completed endpoint, not
   gone. Choose hard delete if available; keep `delete_func` signature stable.
   Record which verb is real.
5. **V-V1(d) completed-in-list (rev 6)**: confirm whether `GET /tasks` (active
   list) can EVER return completed/checked tasks. In Todoist, completed tasks are
   a separate endpoint, so the active list is almost certainly active-only. If
   confirmed active-only, DROP the `completed` bucket and
   `test_completed_tasks_excluded` from section 1 (they test an impossible
   input). If it can return checked tasks, keep the bucket and add it to the
   count invariant. Record the answer.

### Test plan
- `test_no_v2_rest_endpoints`: source grep guard (described above), hermetic.
- `test_list_request_parses_v1_pagination`: feed a fake urlopen returning the v1
  `{results,next_cursor}` body; assert helper returns the dict unchanged and the
  paginating caller follows `next_cursor`. (Transport test can monkeypatch
  `urllib.request.urlopen`; classification tests inject `list_func` and skip
  transport entirely.)

---

## 4. Onboarding surfacing: digest, not one-task-per-candidate

### Finding (MAP 4)
Today the surface path emits NO candidate tasks at all (`surface_queue` keys do
not include candidates). The 56 onboarding tasks in the drift came from an
out-of-band creator (manual `create_todoist_task` or a legacy script). So this
item is a NEW surfacing behavior to add deliberately, plus a rule to never
fan-out one task per candidate.

### Design: single digest item
Add a new surface item builder `_onboarding_digest_surface_item(conn,
as_of_date)` in `surface_queue.py`, included in `build_surface_items`'s fixed
order (surface_queue.py:203-207). Behavior:
- Count active candidates: status in `ACTIVE_STATUSES`
  (`discovered`,`proposed`,`in_review`; onboarding.py:42) via
  `list_charge_onboarding_queue_for_db`.
- If count == 0: emit nothing.
- Else emit ONE item:
  - `surface_key = "onboarding-digest"` (stable, singleton — so the emission
    ledger updates the same task each day rather than creating new ones; the
    `content_hash` changes as the count changes -> `_update_surface_task`,
    todoist_outbox.py:281-300).
  - `content = "<N> charges to review"`.
  - `description`: short list of up to K (e.g. 5) highest `priority_score`
    candidate display names + a pointer to run the review. Marker
    `[fa:onboarding-digest]` appended by `_emission_description`.
  - optional: a second item `onboarding-high-confidence` only when there are
    candidates above a confidence threshold, listing just those.

Because `surface_key` is the singleton `"onboarding-digest"`, idempotency is
automatic via `todoist_emissions` (one row, updated in place). When count drops
to 0, the digest item is no longer emitted; the open task should be retired —
reuse item 2's retire flag (`request_emission_retire(conn, "onboarding-digest")`
when count hits 0). The drain flips it to `retired`, NOT `deleted_by_user`, so
when candidates reappear (N>0) the digest is recreated rather than permanently
suppressed (rev 3; covered by `test_digest_retire_then_resurface`).

### Gating / idempotency
- Built read-only by `build_surface_items`; actual push gated by
  `surface_to_todoist` `live` (todoist_outbox.py).
- Singleton key => never fans out. Hash-based update only when N or the listed
  names change.

### Edge cases
- N changes daily: `content_hash` differs => `_update_surface_task` updates the
  one task. No churn of create/delete.
- Count 0 after being >0: emit nothing AND request retire for
  `onboarding-digest` so the stale task is removed next live run.
- User completed the digest task in Todoist: emission flips to `completed` via
  `reconcile_todoist_completions`; `surface_to_todoist` then treats it as
  `resolved` and never recreates (todoist_outbox.py resolved disposition).

### Test plan
- `test_digest_single_item_for_many_candidates`: seed 56 active candidates;
  assert `build_surface_items` yields exactly ONE `onboarding-digest` item with
  content "56 charges to review".
- `test_digest_absent_when_zero`: no active candidates => no digest item.
- `test_digest_updates_not_recreates`: run surface twice with changing N; assert
  second run is `updated` (same `todoist_task_id`), not `created`.
- `test_digest_retire_when_zero`: after N->0, assert retire requested for
  `onboarding-digest`.
- `test_high_confidence_split_optional`: candidates above threshold produce the
  separate `onboarding-high-confidence` item.

---

## 5. Schedule the daily loop

### Design
Once `reconcile_todoist_project` exists, the daily Todoist routine is:
1. `reconcile_todoist_project(as_of_date, apply=true)` — clean drift first.
2. `surface_due_items_to_todoist(as_of_date)` — push today's items (which now
   includes the onboarding digest from item 4 and drains retire tombstones from
   item 2).

Cron: `10 8 * * *` (08:10 local, matching the proposal).

**Scheduler is an UNVERIFIED dependency (rev 12).** The spec asserts "the repo's
existing scheduled-job mechanism / `get_job_health` surface" but no map
establishes that a cron/routine registration mechanism actually exists.
`get_job_health` reports job health; it is NOT evidence that a registrar exists.
Before relying on it: confirm whether the repo has a routine/cron registration
path (grep for an existing scheduled-job table, a routines registry, or an
external scheduler invoking the MCP tools). If one exists, cite it and register
the two-step routine there. If none exists, treat scheduling as OUT OF SCOPE for
this spec — ship the two tools as idempotent, manually/externally invokable, and
file the scheduler as separate follow-up work. Do NOT invent a new scheduler
inside this spec, and do NOT let an unverified scheduler block the rollout of
items 1-4.

The two MCP tools run in sequence; step 2 must run even if step 1 reports
`partial`/`failed`/`truncated` (mirror background.py per-step isolation,
background.py:103-116).

### Gating
- Both steps require `live` for writes. If `todoist_write_enabled` is off, the
  routine still runs but both tools return `awaiting-integration` and mutate
  nothing — safe no-op. This matches the existing opt-in posture
  (`todoist_write_enabled` default off, config.py:86; background surface step
  defaults `write_enabled=False`, background.py:391-396).

### Idempotency
- reconcile apply is idempotent (section 1). surface is idempotent via the
  emission ledger (todoist_outbox.py). Re-running the cron mid-day is safe.

### Test plan
- `test_daily_routine_order`: assert reconcile runs before surface.
- `test_daily_routine_continues_on_reconcile_failure`: reconcile raises/returns
  failed; surface still invoked.
- `test_daily_routine_noop_when_integration_off`: both steps return
  `awaiting-integration`, zero deletes, zero creates.

---

## Migration / rollout

1. Schema: add `todoist_emissions.retire_requested_at` via the idempotent
   `ALTER TABLE` guard in `ensure_app_schema` (schema.py). Backward compatible
   (nullable, defaults NULL). Also add the new `retired` status value (rev 3): if
   `todoist_emissions.status` has a CHECK constraint enumerating allowed values,
   extend it to include `retired`; otherwise just document `retired` in the
   status vocabulary. Audit every query that filters `status` for recreate
   suppression — `retired` must be EXCLUDED from suppression (it recreates),
   while `completed` and `deleted_by_user` remain suppressors.
2. Ship `reconcile_todoist_project` in **dry-run only** first
   (`apply` default false). Run it once against the live project and eyeball the
   `tasks[]` report before enabling apply.
3. One-time cleanup: run `reconcile_todoist_project(apply=true)` with
   `todoist_write_enabled=on` to clear the stale orphans and legacy tasks. Verify
   `listed` drops to the managed/kept set.
4. Enable auto-cleanup hooks (item 2) and the onboarding digest (item 4) only
   after the one-time cleanup, so the digest's singleton task starts from a clean
   project.
5. Add the `10 8 * * *` cron last, once steps 2-4 verified.
6. Keep `TODOIST_WRITE_ENABLED` off in any CI/test env; all tests are hermetic
   with injected `list_func`/`send_func`/`delete_func`/`read_func`.

## Resolved decisions (formerly open questions)

These were open questions; the critique elevated the safety-critical ones to
blockers. Resolutions are now baked into the sections above.

1. **Stale-applied join is heuristic — RESOLVED.** `needs_review` for any
   `Onboard charge:`-prefix task with no candidate match; delete only
   matched+decided. The prefix alone never auto-deletes. (Section 1 candidate
   join.)
2. **Who owns obligation retirement — BLOCKER, must resolve before coding
   section 2.** No single function in the maps flips obligation status to
   `inactive`/`dormant_suppressed`, so the `obligation-due:` retire hook has
   nowhere to attach yet. Find the owner (candidate: `suppress_dormant_estimates`
   background step) and attach `request_emission_retire_prefix` there. Until
   located, section 2's obligation-retire path is half-built. (Section 2 hook
   points.)
3. **Delete vs close in Todoist v1 — folded into V-V1(c).** Confirm hard
   `DELETE /api/v1/tasks/<id>`; signature stable regardless. (Section 3.)
4. **Cleanup safety model — RESOLVED, this is the core (revs 1-2).** Marker
   absence is NOT a delete signal (it matches ritual + manual tasks). Deletion is
   gated on the three explicit rules: (a) `fa-auto` orphan, (b)
   `LEGACY_CLEANUP_PREFIXES` matched+decided, (c) duplicate extra copies of a
   managed/fa-auto survivor. Everything else is `kept`. Confirmed against
   `FA_AUTO_LABEL = "fa-auto"` (todoist_outbox.py:212) and the fact that only
   `_create_surface_task` attaches it (ground-truth correction 4). (Section 1
   deletion model.)

## Blocking pre-work checklist (do before any code)

- [x] V-LIVE — CONFIRMED (live API + a pre-cleanup dump of the project):
      managed tasks carry BOTH a `[fa:]` marker AND the `fa-auto` label; the
      legacy `"Onboard charge:"` tasks have no labels and no markers (so ONLY
      rule (b)'s prefix catches them); ritual reminders + legacy recurring tasks
      carry a non-`fa-auto` label (no `fa-auto`, no marker) and are therefore
      `kept`. `fa-auto` is present on EXACTLY the managed tasks. =>
      `LEGACY_CLEANUP_PREFIXES = ["Onboard charge:"]`. The three-rule deletion
      model is validated against real data.
- [x] V-V1 — CONFIRMED (live curl against the project): project
      filter param is `project_id=`; LIST envelope is `{"results":[...],
      "next_cursor": <str|null>}` with request cursor param `cursor=`; a hard
      `DELETE /api/v1/tasks/<id>` exists and returns 204; `GET /api/v1/tasks`
      (active list) returned active tasks only (every result `checked:false`; no
      completed tasks present) => treat as active-only: DROP the `completed`
      bucket and `test_completed_tasks_excluded` (rev 6 resolved).
- [ ] OQ2 owner: locate the function that sets obligations
      `inactive`/`dormant_suppressed`; attach the prefix-retire hook. (STILL OPEN
      — resolve via code search during item 2 implementation.)

## Test-plan gaps now covered (cross-reference)

- Markerless manual + ritual task never deleted in apply:
  `test_kept_tasks_never_deleted` (revs 1-2, highest value).
- Retire -> recreate resurrection: `test_retire_then_recreate_resurfaces`,
  `test_digest_retire_then_resurface` (rev 3).
- Prefix retire across instances: `test_request_emission_retire_prefix_multi_instance`
  (rev 4).
- Apply-while-truncated / failed page: `test_apply_blocked_when_truncated`,
  `test_apply_blocked_when_list_page_fails` (revs 7-8).
- 429 backoff + delete ceiling: `test_delete_429_backoff`,
  `test_delete_ceiling_truncates` (rev 9).
- Numeric vs lexical duplicate tie-break: `test_classify_duplicate_numeric_tiebreak`
  (rev 11).
- Count invariant: `test_report_count_invariant` (rev 10).
