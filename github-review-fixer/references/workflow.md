# Helper workflow and JSON interface

Run all commands from the PR worktree. `--repo PATH` may precede the subcommand. Every command emits
exactly one JSON document; success has `"ok": true`, and failure has `"ok": false` plus a stable
error `code`, safe `message`, and optional `details`. Failure exits nonzero. `--json` is accepted for
clarity but output is always JSON.

Before entering a PR worktree, generate and confirm the four-field task intake as described in
[coordination.md](coordination.md). Inspection does not replace intake confirmation.

## Read-only inspection

```sh
python3 "$SKILL/scripts/reviewctl.py" --repo "$PWD" inspect --json
python3 "$SKILL/scripts/reviewctl.py" --repo "$PWD" inspect --pr 123 --json
```

Example (abridged):

```json
{"ok":true,"operation":"inspect","pr":{"number":123,"head_oid":"abc..."},"checkout":{"head_matches_pr":true,"clean":true,"usable":true},"threads":[{"thread_id":"PRRT_x","is_outdated":false,"path":"src/x.py","line":8,"comments":[]}]}
```

Inspection authenticates through `gh auth status`, discovers the current-branch PR when `--pr` is
omitted, paginates all threads and every thread's comments, and returns only unresolved line-level
threads. An outdated thread remains actionable even when its current line is null.

## Start and inspect durable state

```sh
python3 "$SKILL/scripts/reviewctl.py" --repo "$PWD" session-start --json
python3 "$SKILL/scripts/reviewctl.py" --repo "$PWD" session-start --pr URL --push-remote fork --json
python3 "$SKILL/scripts/reviewctl.py" --repo "$PWD" session-show --session SESSION --json
```

If the entire worktree is moved before publication starts, adopt its existing session only with the
previous absolute root supplied explicitly:

```sh
python3 "$SKILL/scripts/reviewctl.py" --repo "$PWD" session-relocate \
  --session SESSION --previous-root /old/absolute/worktree --json
```

Relocation is refused after publication starts, while the recorded Git directory still exists, or
when repository identity or HEAD no longer matches. Repeating a completed relocation is idempotent.

Examples:

```json
{"ok":true,"operation":"session-start","session":{"session_id":"pr-123-a1b2c3d4e5f6","thread_order":["PRRT_x"],"publication":{"status":"not_started"}}}
{"ok":true,"operation":"session-show","session":{"session_id":"pr-123-a1b2c3d4e5f6","revision":3}}
```

`session-start` requires a clean working tree and local HEAD equal to the remote PR head. When more
than one or no Git remote maps to the PR head repository, restart with the user-selected
`--push-remote`. State is private-mode JSON below the worktree Git directory.

## Record one confirmed thread

Write the user-confirmed reply to a UTF-8 file. For a change, write paths as a JSON string array
(recommended because Git filenames may contain unusual characters) and tests as a JSON array:

```json
[
  {"command":"python3 -m unittest tests.test_widget","status":"passed","summary":"12 tests passed"}
]
```

Allowed statuses are `passed`, `failed`, `unavailable`, and `not_run`. Commands are display-only
evidence and are never executed by the helper.

```sh
python3 "$SKILL/scripts/reviewctl.py" --repo "$PWD" record \
  --session SESSION --thread PRRT_x --decision change \
  --reply-file /tmp/reply.txt --paths-file /tmp/paths.json --tests-file /tmp/tests.json --json
```

For a decline, omit paths and tests:

```sh
python3 "$SKILL/scripts/reviewctl.py" --repo "$PWD" record \
  --session SESSION --thread PRRT_y --decision no-change --reply-file /tmp/reason.txt --json
```

Example:

```json
{"ok":true,"operation":"record","idempotent":false,"thread":{"state":"ready","decision":"change","paths":["src/x.py"]}}
```

The helper enforces queue order, nonempty replies, path safety, and local-only state. Repeating the
identical record is idempotent; changing a confirmed record requires a new session.

If a confirmed change is retested before publication, replace only its display-only test evidence:

```sh
python3 "$SKILL/scripts/reviewctl.py" --repo "$PWD" update-tests \
  --session SESSION --thread PRRT_x --tests-file /tmp/tests.json --json
```

This is allowed only for a ready `change` decision while publication is `not_started`. It does not
change the confirmed decision, reply, or paths. Repeating identical evidence is idempotent; any
replacement changes the next publication digest and is shown in the final approval preview.

## Preview and approval

```sh
python3 "$SKILL/scripts/reviewctl.py" --repo "$PWD" prepare-publish \
  --session SESSION --commit-message "Address PR review" --json
```

Example (abridged):

```json
{"ok":true,"operation":"prepare-publish","publishable_without_override":true,"plan":{"digest":"6f...","commit":{"required":true,"paths":["src/x.py"]},"target":{"push_remote":"origin","branch":"feature"},"tests":{"blockers":[],"override_required":false},"replies":[{"thread_id":"PRRT_x","body_preview":"Applied...\n\nPushed commit: <PUSHED_COMMIT_SHA>\n\n<!-- codex-review-fixer:SESSION:PRRT_x -->","will_resolve_after_reply":true}]}}
```

Preparation refetches the PR and threads, fingerprints file content, checks the exact changed-path
allowlist, and computes the digest without changing repository, session decisions, or GitHub. The
commit placeholder is the only runtime substitution and becomes the verified pushed SHA.

After the user explicitly approves this exact digest, with no intervening edit or test rerun:

```sh
python3 "$SKILL/scripts/reviewctl.py" --repo "$PWD" publish \
  --session SESSION --plan-digest DIGEST --commit-message "Address PR review" --json
```

If and only if the user separately accepts disclosed test failures, add
`--allow-failed-tests`. Example success:

```json
{"ok":true,"operation":"publish","idempotent":false,"session":{"publication":{"status":"complete","commit_sha":"def...","pushed":true}}}
```

Publication stages only approved paths, creates one commit, pushes that exact SHA, verifies the PR
head, posts marked replies, and resolves only successfully replied threads. A no-change plan starts
with a fresh remote-head/thread check and skips commit and push.

## Retry

```sh
python3 "$SKILL/scripts/reviewctl.py" --repo "$PWD" retry-publish --session SESSION --json
```

Example failure and recovery:

```json
{"ok":false,"error":{"code":"thread_publication_failed","message":"One or more thread operations failed","details":[{"thread_id":"PRRT_x","operation":"resolve"}]}}
{"ok":true,"operation":"retry-publish","idempotent":false,"session":{"publication":{"status":"complete"}}}
```

Retry uses only the persisted approved plan. It does not need or accept a new digest because it
cannot alter that plan. It discovers a commit by its session trailer, a push by the current PR head,
a reply by its hidden marker, and a completed resolution from the thread state. It never duplicates
those actions. PR-head drift, new thread comments, changed resolution state without a session reply,
or new local edits stop recovery for reanalysis.

Common blocking codes include `isolation_required`, `working_tree_not_isolated`, `pr_head_drift`,
`thread_drift`, `plan_digest_mismatch`, `tests_block_publication`, `push_verification_failed`, and
`publication_incomplete`. Command errors, reviewer data, diffs, and test evidence redact
token-shaped content while retaining hashes for drift detection; replies and commit messages with
token-shaped content are rejected before storage or publication.
