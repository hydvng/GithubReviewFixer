# Helper workflow and JSON interface

Run all commands from the PR worktree. `--repo PATH` may precede the subcommand. Every command emits
exactly one JSON document; success has `"ok": true`, and failure has `"ok": false` plus a stable
error `code`, safe `message`, and optional `details`. Failure exits nonzero. `--json` is accepted for
clarity but output is always JSON.

Before entering a PR worktree, generate and confirm the four-field task intake as described in
[coordination.md](coordination.md). Inspection does not replace intake confirmation.

## Read-only inspection

```sh
python3 "$SKILL/scripts/reviewctl.py" --repo "$PWD" inspect \
  --intake-file /tmp/intake.json --confirmed-intake-digest DIGEST --json
```

Example (abridged):

```json
{"ok":true,"operation":"inspect","pr":{"number":123,"head_oid":"abc..."},"checkout":{"head_matches_pr":true,"clean":true,"usable":true},"threads":[{"thread_id":"PRRT_x","is_outdated":false,"path":"src/x.py","line":8,"comments":[]}]}
```

Inspection authenticates through `gh auth status`, verifies the checkout and PR against the bound
intake, paginates all threads and every thread's comments, and returns unresolved line-level
threads. It also returns the PR body, conversation comments, review summaries, and status checks as
read-only analysis context. An outdated thread remains actionable even when its current line is null.

## Reviewer mode

Reviewer mode uses a bound intake with `"mode":"review"` and `"test_commands":[]`. Start a
durable, read-only review snapshot and retrieve its merge-base diff:

```sh
python3 "$SKILL/scripts/reviewctl.py" --repo "$PWD" review-start \
  --intake-file /tmp/intake.json --confirmed-intake-digest DIGEST --json
python3 "$SKILL/scripts/reviewctl.py" --repo "$PWD" review-diff \
  --session REVIEW_SESSION --json
```

The findings file is a JSON array with at most ten entries. An empty array is valid. Every entry
has these required fields; `start_line` is optional for a multiline right-side range:

```json
[
  {
    "path": "src/widget.py",
    "line": 42,
    "start_line": 40,
    "title": "Retry loses the original deadline",
    "severity": "warning",
    "confidence": 0.93,
    "category": "correctness",
    "problem": "Each retry resets the timeout and can exceed the caller's deadline.",
    "reproduction": "Use three slow failures with a one-second total deadline; the call runs for roughly three seconds.",
    "fix": "Compute one monotonic deadline before the loop and pass only the remaining duration.",
    "evidence": "The added loop passes timeout_seconds unchanged on every attempt at lines 40-42."
  }
]
```

Severity is `suggestion`, `warning`, or `blocker`; confidence must be `0.80` or greater; category is
a short lowercase identifier. Findings are sorted deterministically by severity, confidence, path,
and line. The helper rejects malformed fields, likely duplicates, secrets, unchanged paths, deleted
or left-side-only lines, and locations outside the current diff. It renders each comment into the
four required sections rather than trusting preformatted Markdown.

Write a nonempty UTF-8 summary and prepare the exact publication plan:

```sh
python3 "$SKILL/scripts/reviewctl.py" --repo "$PWD" prepare-review \
  --session REVIEW_SESSION --findings-file /tmp/findings.json \
  --summary-file /tmp/review-summary.md --json
```

This is read-only. It revalidates PR identity, base/head/merge-base, feedback and thread snapshots,
working-tree cleanliness, changed paths, diff lines, and finding policy. Show the complete plan and
obtain explicit approval of its digest. With no intervening operation, publish the identical inputs:

```sh
python3 "$SKILL/scripts/reviewctl.py" --repo "$PWD" publish-review \
  --session REVIEW_SESSION --plan-digest DIGEST \
  --findings-file /tmp/findings.json --summary-file /tmp/review-summary.md --json
```

Publication sends one GitHub pull-request review with a fixed `COMMENT` event, a summary body, and
all inline comments pinned to the reviewed head commit. It never sends `APPROVE` or
`REQUEST_CHANGES`; “approval” here is the user's required authorization to publish the exact
comment plan. If the request is interrupted, retry only the stored approved plan:

```sh
python3 "$SKILL/scripts/reviewctl.py" --repo "$PWD" retry-review \
  --session REVIEW_SESSION --json
```

The summary carries a session marker. Retry lists existing reviews first and adopts a matching
review, including an ambiguous POST that succeeded remotely, before considering another request.

## Start and inspect durable state

```sh
python3 "$SKILL/scripts/reviewctl.py" --repo "$PWD" session-start \
  --intake-file /tmp/intake.json --confirmed-intake-digest DIGEST --json
python3 "$SKILL/scripts/reviewctl.py" --repo "$PWD" session-start --push-remote fork \
  --intake-file /tmp/intake.json --confirmed-intake-digest DIGEST --json
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

`session-start` requires a bound edit-capable intake, a clean working tree, and local HEAD equal to
the remote PR head. `read-only` cannot start a session. When more
than one or no Git remote maps to the PR head repository, restart with the user-selected
`--push-remote`. State is private-mode JSON below the worktree Git directory.

## Run only approved tests and record evidence

The intake test commands are the initial allowlist. To add a risk-driven command, write the full
superset as a JSON string array, preview it, show the digest, obtain explicit approval, then apply it:

```sh
python3 "$SKILL/scripts/reviewctl.py" --repo "$PWD" prepare-test-plan \
  --session SESSION --commands-file /tmp/test-commands.json --json
python3 "$SKILL/scripts/reviewctl.py" --repo "$PWD" approve-test-plan \
  --session SESSION --commands-file /tmp/test-commands.json --plan-digest DIGEST --json
```

Commands cannot be removed during a session. Run an exact approved command without a shell:

```sh
python3 "$SKILL/scripts/reviewctl.py" --repo "$PWD" run-test \
  --session SESSION --command "python3 -m unittest tests.test_widget" \
  --timeout-seconds 300 --json
```

The result includes an evidence ID, argv, exit code, timestamps, duration, output hashes, redacted
tails, and whether the command changed repository state. Missing executables and timeouts become
`unavailable` evidence; nonzero exits or repository mutation become `failed`. Tests are not run
after publication starts. Ordinary helper commands time out after 60 seconds, pushes after 120
seconds, and a test timeout must be between 1 and 1800 seconds.

## Record one confirmed thread

Write the user-confirmed reply draft to a UTF-8 file. For a change, write paths as a JSON string
array and tests as a JSON array of evidence IDs:

```json
["4dc4e3...", "218f90..."]
```

New fix sessions also require a Mira-inspired assessment for every non-deferred decision:

```json
{
  "category": "correctness",
  "severity": "warning",
  "confidence": 0.94,
  "evidence_grade": "proven",
  "rationale": "The reported branch bypass is visible in the current PR-head implementation.",
  "related_paths": ["src/x.py", "src/caller.py", "tests/test_x.py"],
  "regression_risks": ["A broader guard could reject callers that omit this optional field."],
  "verification_scope": ["Focused regression test", "Affected caller and error-path inspection"],
  "duplicate_of": null
}
```

`severity` accepts `nitpick`, `suggestion`, `warning`, or `blocker`; confidence is between zero and
one; evidence is `proven`, `plausible`, or `unsupported`. `related_paths` must include the thread
path and every changed path. Use `duplicate_of` only for another thread in the same session. Risks
and verification scope are explicit arrays, including empty arrays when genuinely none are known.
The helper rejects an `unsupported` assessment paired with a `change` decision. It retains lower
confidence, plausible, and nitpick assessments rather than hiding them, then highlights them in the
final approval plan. Sessions created before this policy remain compatible and disclose missing
assessments in that plan.

Choose an explicit resolution policy. The complete rendered reply is confirmed later in the
publication preview:

```sh
python3 "$SKILL/scripts/reviewctl.py" --repo "$PWD" record \
  --session SESSION --thread PRRT_x --decision change \
  --resolution resolve \
  --reply-file /tmp/reply.txt --paths-file /tmp/paths.json --tests-file /tmp/tests.json \
  --assessment-file /tmp/assessment.json --json
```

For a decline, omit paths and tests:

```sh
python3 "$SKILL/scripts/reviewctl.py" --repo "$PWD" record \
  --session SESSION --thread PRRT_y --decision no-change --resolution leave-open \
  --reply-file /tmp/reason.txt --assessment-file /tmp/assessment.json --json
```

Use `--resolution leave-open` to reply without resolving. To defer one thread while publishing
others, omit reply, paths, tests, and resolution:

```sh
python3 "$SKILL/scripts/reviewctl.py" --repo "$PWD" record \
  --session SESSION --thread PRRT_z --decision defer --json
```

Example:

```json
{"ok":true,"operation":"record","idempotent":false,"thread":{"state":"ready","decision":"change","paths":["src/x.py"]}}
```

The helper enforces queue order, nonempty replies, explicit resolution policy, path safety,
machine-recorded test evidence, structured assessment consistency, and local-only state. Repeating the
identical record is idempotent; changing a confirmed record requires a new session.

If a confirmed change is retested before publication, replace only its machine-recorded test evidence:

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
{"ok":true,"operation":"prepare-publish","publishable_without_override":true,"plan":{"digest":"6f...","commit":{"required":true,"paths":["src/x.py"]},"target":{"push_remote":"origin","branch":"feature"},"tests":{"blockers":[],"override_required":false},"deferred_threads":[],"replies":[{"thread_id":"PRRT_x","body_preview":"Applied...\n\nPushed commit: <PUSHED_COMMIT_SHA>\n\n<!-- codex-review-fixer:SESSION:PRRT_x -->","body_template_sha256":"12...","will_resolve_after_reply":true}]}}
```

Preparation refetches the PR and threads, blocks changed or newly added unresolved threads and
changed PR body/conversation/review-summary feedback, refreshes CI status checks,
fingerprints file content, checks the exact changed-path
allowlist, includes each assessment plus aggregate quality flags, and computes the digest without
changing repository, session decisions, or GitHub. The
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

Publication is available only to `full` or an explicitly publish-capable custom intake. It stages
only approved paths, creates one commit, pushes that exact SHA, verifies the PR head, posts marked
replies, and applies each thread's `resolve` or `leave-open` policy. Deferred threads are untouched.
A no-change plan starts with a fresh remote-head/thread check and skips commit and push.

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
those actions. PR-head drift, new unresolved threads or comments, changed non-line review feedback,
changed resolution state without a session reply,
or new local edits stop recovery for reanalysis.

Common blocking codes include `intake_digest_mismatch`, `mode_forbids_edits`,
`mode_forbids_publish`, `isolation_required`, `working_tree_not_isolated`, `pr_head_drift`,
`pr_feedback_drift`, `thread_drift`, `test_command_not_approved`, `test_evidence_corrupt`,
`plan_digest_mismatch`, `tests_block_publication`, `push_verification_failed`, and
`publication_incomplete`. Command errors, reviewer data, diffs, and test evidence redact
token-shaped content while retaining hashes for drift detection; replies and commit messages with
token-shaped content are rejected before storage or publication.
