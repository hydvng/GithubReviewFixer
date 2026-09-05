# GitHub Review Fixer

An installable Codex Desktop/CLI skill for processing unresolved line-level pull-request review
threads locally. Codex interprets feedback, edits code, and selects tests; `reviewctl.py` handles
deterministic inspection, durable state, and explicitly approved publication.

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

Start every task by generating the required choices:

```sh
python3 github-review-fixer/scripts/reviewctl.py task-intake \
  --pr-url https://github.com/acme/widget/pull/123 \
  --workspace-root /absolute/review-workspace --json
```

Codex presents and confirms these four fields: repository path, full PR URL, exact test commands,
and mode (`read-only`, `preview`, `full`, or an explicit custom mode). `preview` is recommended and
stops before commit, push, reply, or resolution. Custom absolute paths and commands are supported;
missing or implicit values are not.

After confirming the intake digest, start Codex in the selected checkout. For the current branch,
ask:

```text
Use $github-review-fixer to process this PR's unresolved review threads.
```

For an explicit PR:

```text
Use $github-review-fixer on PR 123.
Use $github-review-fixer on https://github.example/acme/widget/pull/123.
```

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

Each thread is confirmed individually, but GitHub receives one publication batch. Before any
commit, push, reply, or resolution, Codex shows a full preview and SHA-256 digest and asks for one
explicit final approval of that exact plan. A code-changing session creates at most one commit. A
no-change-only session creates none.

Failed, unavailable, or unrun tests block publication by default. To proceed, explicitly approve
the disclosed failures; Codex then passes `--allow-failed-tests`, and affected replies disclose the
override.

At environment selection, change approval, exact reply/resolve approval, publication approval,
blockers, and completion, the skill emits a deterministic checkpoint and asks in the active Codex
task. A BootYourDonkey-linked task exposes the same prompt through its task transcript so the
configured Telegram bridge can notify you; Telegram answers return to that task as steer messages.
The helper does not read Telegram credentials or claim delivery by itself.

For remote delivery, `handoff-create` turns a confirmed intake JSON into a digest-protected
`bootyourdonkey.review-fixer.request` payload. The remote BootYourDonkey client transports that
payload and the skill artifact; the receiving task runs `handoff-validate` before use. Remote
delivery never pre-approves environment changes, code changes, replies, commits, pushes, or thread
resolution. See [references/coordination.md](references/coordination.md).

## Recover

State lives in the worktree Git directory under `.git/codex-review-fixer/` with atomic writes and a
backup. If publication stops, invoke the skill again and ask it to recover the session. It will use
`session-show` and `retry-publish`; it recognizes an already-created commit by its session trailer,
an already-pushed commit from the PR head, and an already-posted reply from its hidden marker.

Do not manually repeat a push, reply, or resolution. Head or thread drift deliberately stops retry
and requires reanalysis and a new approved plan.

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
