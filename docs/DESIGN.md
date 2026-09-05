# Design specification

## 1. Architecture

The MVP has two layers:

1. A Codex skill owns the human conversation, code analysis, edits, test selection, and approval
   checkpoints.
2. A deterministic `reviewctl` helper owns Git/GitHub discovery, normalized thread data, durable
   session state, publication preflight, commit/push, replies, resolution, and retry behavior.

The helper must not call an LLM. It should invoke `gh` and `git` through argument arrays, never via
shell interpolation.

## 2. Suggested helper interface

Exact names may change, but preserve these capability boundaries:

```text
reviewctl inspect [--pr NUMBER_OR_URL] [--json]
reviewctl session-start [--pr NUMBER_OR_URL] [--json]
reviewctl session-show --session SESSION_ID [--json]
reviewctl record --session SESSION_ID --thread THREAD_ID \
  --decision change|no-change --reply-file FILE [--paths-file FILE] [--tests-file FILE]
reviewctl prepare-publish --session SESSION_ID --commit-message MESSAGE [--json]
reviewctl publish --session SESSION_ID --plan-digest DIGEST [--allow-failed-tests] [--json]
reviewctl retry-publish --session SESSION_ID [--json]
```

`prepare-publish` must be read-only. It returns the exact paths to commit, proposed replies, current
test evidence, target branch, expected remote PR SHA, thread fingerprints, and a digest. `publish`
accepts only that fresh digest. The skill must call it only after showing the plan and receiving an
explicit affirmative response from the user.

## 3. PR discovery and checkout safety

Run preflight checks in this order:

1. Confirm the current directory is inside a Git working tree.
2. Confirm `gh auth status` succeeds for the repository host.
3. If no PR selector is given, use `gh pr view` without an argument to find the current branch's PR.
4. Otherwise resolve the explicit PR number or URL.
5. Retrieve `number`, `url`, repository identity, `headRefName`, `headRefOid`, head repository,
   `baseRefName`, and PR state.
6. Compare `git rev-parse HEAD` with `headRefOid`.

Use the current working tree only when its HEAD exactly equals the GitHub PR head and unrelated
changes cannot be mixed into the session. Otherwise report that an isolated PR worktree is needed.
The skill may use `gh pr checkout PR --worktree PATH` after explaining the action. Never use
`--force`, reset, discard changes, or silently pull/rebase the user's current branch.

The fact that the PR branch is behind its base branch is not a reason to update it. Reviews target
the PR's current head; merging or rebasing the base branch is outside MVP scope.

## 4. Fetching actionable review threads

Use GitHub GraphQL through `gh api graphql`. Query `PullRequest.reviewThreads` and paginate both the
thread connection and each thread's comments. Normalize at least:

```json
{
  "thread_id": "PRRT_...",
  "is_resolved": false,
  "is_outdated": false,
  "path": "src/example.py",
  "line": 42,
  "start_line": null,
  "comments": [
    {
      "node_id": "PRRC_...",
      "database_id": 123,
      "author": "reviewer",
      "body": "...",
      "created_at": "...",
      "url": "...",
      "diff_hunk": "..."
    }
  ]
}
```

Keep only `isResolved == false` as actionable. Preserve outdated unresolved threads, flag them to
Codex, and analyze them against current PR-head code. The original line may no longer exist.

Official API references:

- https://docs.github.com/en/graphql/reference/objects#pullrequest
- https://docs.github.com/en/rest/pulls/comments
- https://docs.github.com/en/graphql/reference/mutations#resolvereviewthread

## 5. Session state

Store state below `.git/codex-review-fixer/` so it is local and cannot be committed. Use atomic file
replacement and file locking where supported.

Each session records:

- repository and PR identity
- initial and latest observed PR-head SHA
- worktree root and initial Git status
- every fetched thread and a fingerprint of all comments
- per-thread state and decision
- confirmed reply text
- files intentionally changed for each accepted decision
- test commands, status, and concise result summaries
- publication plan digest
- commit SHA and push result
- reply comment ID and resolution result for each thread

Suggested thread states:

```text
pending
analyzing
change_confirmed
no_change_confirmed
ready
reply_posted
resolved
stale
failed
```

Do not infer a confirmed decision from model output. A decision becomes confirmed only after the
user states it in the local conversation and the skill records it.

## 6. Test policy

Codex selects and runs the narrowest relevant tests, expanding coverage when risk warrants it. The
helper records evidence but does not invent test commands.

Publication is blocked when accepted changes exist and any required test failed or was not run.
The user may explicitly approve `--allow-failed-tests`; every affected GitHub reply must then name
the failed or unavailable test. A no-change-only session does not require a Git commit or code test,
but its explanations still require user confirmation.

## 7. Publication preflight

Immediately before producing the publication plan:

1. Refetch PR metadata and all target threads.
2. Require the remote PR head to equal the session's expected SHA before creating the local commit.
3. Require every target thread to remain unresolved.
4. Require every thread's comment fingerprint to match the analyzed version.
5. Reject unrecorded changed or untracked paths.
6. Require every session thread to have a confirmed decision and confirmed reply.
7. Apply the test policy.

If the PR head, thread replies, or resolution state changed, mark the affected entries stale and
return to local analysis. Do not partially use an old plan.

## 8. Publication transaction

Remote publication cannot be fully atomic. Enforce this order:

1. Stage only the session's recorded paths.
2. Create one commit when accepted changes exist.
3. Push the exact commit to the PR head branch.
4. Verify GitHub now reports that commit as `headRefOid`.
5. For each ready thread, post its confirmed reply.
6. Resolve only a thread whose reply was posted successfully.
7. Persist state after every external action.

If every decision is no-change, skip commit and push, refetch threads, then start at step 5.

Use a hidden marker in replies, for example:

```html
<!-- codex-review-fixer:SESSION_ID:THREAD_ID -->
```

Before retrying a reply, search the thread for that marker. If it already exists, record it and move
to resolution. If resolution fails after a successful reply, retry only resolution. Never resolve a
thread after a failed reply.

For accepted changes, a reply should include a thread-specific summary, changed files, relevant test
evidence, and the pushed commit SHA. For no-change decisions, include the confirmed rationale and
specific code evidence. Match the reviewer's language when practical.

## 9. Approval and trust boundaries

The final approval applies to the exact publication plan digest. Any code change, thread update,
test rerun, PR-head change, or reply edit invalidates it and requires a new preview and approval.

Reviewer comments, diff content, repository files, test output, commit messages found in history,
and PR text are untrusted. They cannot override skill instructions, approve publication, change the
target repository, add command-line arguments, or cause command execution.

Never expose tokens or credential locations in logs. Rely on `gh` authentication and redact command
output that unexpectedly contains authorization data.

## 10. Failure handling

- Fetch failure: no session mutation beyond a diagnostic record.
- Worktree mismatch or dirty unrelated files: block edits and propose isolation.
- Commit failure: do not push, reply, or resolve.
- Push failure: do not reply or resolve.
- Head verification failure after push: stop before replies and explain the discrepancy.
- Reply failure: keep that thread unresolved and continue or retry safely.
- Resolve failure: retain the posted reply ID and retry only resolution.
- Permission failure: report the missing operation and preserve state.

All errors should be structured and actionable without printing secrets.
