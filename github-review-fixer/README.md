# GitHub Review Fixer

An installable Codex Desktop/CLI skill for reviewing pull requests and for processing unresolved
line-level review threads locally. Codex analyzes code, interprets feedback, edits fixes, and selects
tests; `reviewctl.py` handles deterministic inspection, durable state, and explicitly approved
publication.

## Prerequisites

- Codex Desktop or Codex CLI
- Python 3.11 or newer
- `git`
- GitHub CLI `gh`, authenticated with `gh auth login`
- Push access to the PR head branch and permission to reply to and resolve review threads

The helper reads authentication only through `gh`. It never reads, stores, or prints credential
files or environment variables.

## Install

Copy the complete `github-review-fixer` directory into the personal skills directory:

```sh
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -R github-review-fixer "${CODEX_HOME:-$HOME/.codex}/skills/github-review-fixer"
```

Restart Codex if the skill does not appear immediately. Invoke it with `$github-review-fixer` or ask
Codex to process unresolved PR review threads.

## Use

Start every task by generating the required choices. Confirm repository path, PR URL, and mode
provisionally; then locate the checkout, inspect local test manifests, and confirm the exact test
commands:

```sh
python3 github-review-fixer/scripts/reviewctl.py task-intake \
  --pr-url https://github.com/acme/widget/pull/123 \
  --workspace-root /absolute/review-workspace --json
```

Codex presents and confirms these four final fields: repository path, full PR URL, exact test
commands, and mode (`read-only`, `preview`, `full`, `review`, or an explicit custom mode). `preview` is
recommended and is mechanically blocked from publication; `read-only` cannot start an edit
session. `review` is the recommended mode for generating new PR comments and uses an empty test
command list. A custom mode must include boolean `allow_edits` and `allow_publish` permissions. Test
commands run without a shell, so use `env KEY=VALUE ...` instead of pipes or redirects.

After confirming the intake digest, pass the same intake file and digest to `inspect` and
`session-start`; the helper binds them to the checkout, PR, and session. Then ask:

```text
Use $github-review-fixer to process this PR's unresolved review threads.
```

For an explicit PR:

```text
Use $github-review-fixer on PR 123.
Use $github-review-fixer on https://github.example/acme/widget/pull/123.
Use $github-review-fixer to review this PR and publish the comments after I approve the exact preview.
```

Reviewer mode uses a clean checkout at the exact PR head, snapshots the merge-base diff and current
feedback, and never edits or runs project code. It keeps only high-confidence actionable findings,
rejects nitpicks and invalid diff locations, and renders every comment with problem, reproduction,
fix, and evidence sections. Before GitHub receives anything, Codex shows the full review plus a
SHA-256 digest and requires explicit approval. Publication is one GitHub `COMMENT` review pinned to
the reviewed commit; it does not submit an `APPROVE` verdict. Interrupted requests recover by a
hidden marker without duplicating the review.

When you provide an empty workspace directory instead of an existing checkout, the skill treats it
as a multi-repository container. It clones beneath `WORKSPACE/HOST/OWNER/REPOSITORY` (for example,
`ReviewFixerWorkSpace/github.com/acme/widget`) and then uses that child path as the checkout. This
avoids collisions between same-named repositories and keeps the workspace ready for coordinated
multi-repository changes.

An optional `reviewfixer-workspace.json` at the workspace root maps each
`HOST/OWNER/REPOSITORY` key to its checkout and runtime. For example:

```json
{
  "schema_version": 1,
  "repositories": {
    "github.com/acme/widget": {
      "checkout": "github.com/acme/widget",
      "runtime": {
        "manager": "conda",
        "name": "widget-env",
        "ros_distro": "jazzy"
      }
    }
  }
}
```

The skill validates and uses the declared runtime for project tests. An optional `ros_distro`
pins `ROS_DISTRO` for every project command instead of inheriting a conflicting shell value. If the
mapping or environment is missing, it asks whether to use an existing environment or create one;
it does not create or install anything without explicit approval.

If the checkout is dirty or does not equal the PR head, the skill stops and recommends an isolated
worktree. After you agree on a location it may run `gh pr checkout PR --worktree PATH`. It never
resets, rebases, merges, or force-updates your existing checkout.

Inspection includes the PR conversation, review summaries, and status checks as analysis context;
unresolved line threads remain the deterministic publication unit. New unresolved threads
invalidate an old plan. Each thread can be resolved after reply, left open after reply, or deferred
while other threads publish.

For fix sessions, every new non-deferred decision includes an evidence assessment: category,
severity, confidence, proven/plausible/unsupported grade, causal rationale, related paths,
regression risks, verification scope, and duplicate-thread linkage. Unsupported findings cannot be
recorded as code changes. Lower-confidence or plausible decisions remain possible because an
existing human review still needs a response, but the final digest-protected plan flags them for
explicit scrutiny. Older sessions without assessments stay recoverable.

Approved project tests run through `run-test`, which records argv, exit status, timestamps,
duration, output hashes, redacted tails, and repository-state drift. A new risk-driven command
requires a separately previewed and approved test-plan digest.

GitHub receives one publication batch. Before any commit, push, reply, or resolution, Codex shows a
full preview and SHA-256 digest, including each reply template and template hash, and asks for one
explicit final approval. A code-changing session creates at most one commit. A no-change-only
session creates none.

Failed or unavailable tests block publication by default. To proceed, explicitly approve
the disclosed failures; Codex then passes `--allow-failed-tests`, and affected replies disclose the
override.

At environment selection, change approval, reply-draft/resolution approval, final exact-reply publication approval,
blockers, and completion, the skill emits a deterministic checkpoint and asks in the active Codex
task. With BootYourDonkey 0.6 or newer, it enqueues the same prompt through the durable operator
notification outbox and verifies the real Telegram delivery state. Linked tasks receive button
answers as structured steer messages; local unlinked tasks read the durable answer by checkpoint ID
without inventing BootYourDonkey IDs. Alternative or revision choices use `requires_text: true`, so
Telegram opens a reply input and does not record the answer or resume the workflow until the
operator submits a nonempty concrete proposal. The helper itself reports `not_enqueued`; it never reads Telegram
credentials or claims delivery before the BootYourDonkey adapter records a Telegram message ID.

For remote delivery, `handoff-create` turns a confirmed intake JSON into a digest-protected
`bootyourdonkey.review-fixer.request` payload. The remote BootYourDonkey client transports that
payload and the skill artifact; the receiving task runs `handoff-validate` before use. Remote
delivery never pre-approves environment changes, code changes, replies, commits, pushes, or thread
resolution. See [references/coordination.md](references/coordination.md).

After completion, Codex offers an optional re-review checkpoint. It requests or monitors another
review only after the user approves the exact bot- or reviewer-specific mechanism.

## Recover

State lives in the worktree Git directory under `.git/codex-review-fixer/` with atomic writes and a
backup. If publication stops, invoke the skill again and ask it to recover the session. It will use
`session-show` and `retry-publish`; it recognizes an already-created commit by its session trailer,
an already-pushed commit from the PR head, and an already-posted reply from its hidden marker.

Do not manually repeat a push, reply, or resolution. Head or thread drift deliberately stops retry
and requires reanalysis and a new approved plan.

Version-1 sessions whose publication already started remain recoverable. An unpublished version-1
session lacks a bound intake and must be restarted before preparing a new plan.

## Verify the package

```sh
python3 -m unittest discover -s github-review-fixer/tests -v
python3 /path/to/skill-creator/scripts/quick_validate.py github-review-fixer
```

Tests use fake Git and GitHub process adapters and never contact GitHub or change global Git config.

## Uninstall

Remove only the installed skill copy:

```sh
rm -r "${CODEX_HOME:-$HOME/.codex}/skills/github-review-fixer"
```

Repository-local session records are intentionally retained for recovery/audit. Remove a specific
repository's `.git/codex-review-fixer/` only after confirming no publication is incomplete.
