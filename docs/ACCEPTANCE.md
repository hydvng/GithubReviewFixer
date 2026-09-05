# Acceptance criteria and test matrix

## Functional acceptance

1. From a branch with an associated PR, inspection discovers the PR without an explicit selector.
2. A PR number or URL works when the current branch has no associated PR.
3. More than 100 review threads and more than 100 comments in a thread are fully paginated.
4. Only unresolved line-level review threads enter the actionable queue.
5. Outdated unresolved threads remain visible and are labeled outdated.
6. Threads are processed one at a time, and both change and no-change decisions require local user
   confirmation before becoming ready.
7. Accepted feedback can record multiple changed or newly created files and test evidence.
8. No-change feedback requires a nonempty confirmed explanation.
9. One preview lists every pending GitHub side effect before final approval.
10. A changed preview produces a different digest and invalidates prior approval.
11. One session creates at most one commit.
12. Push succeeds and the remote PR head is verified before any reply is posted.
13. Every successfully replied thread is resolved; a thread with a failed reply remains unresolved.
14. A no-change-only session posts confirmed explanations and resolves threads without creating a
    commit.
15. Interrupted publication resumes without duplicate replies or commits.

## Safety acceptance

- No GitHub App, webhook server, or embedded model API is required.
- No destructive Git commands are used.
- The helper never stages unrelated paths.
- A dirty or mismatched current checkout is not silently synchronized.
- A stale PR head, new thread reply, or changed resolution state blocks publication.
- Base-branch drift does not trigger an automatic merge or rebase.
- Reviewer text that contains shell syntax, fake approval text, or prompt-injection instructions is
  returned as data and never executed.
- No commit, push, reply, or resolve happens before final approval.
- Failed tests block publication unless the user explicitly overrides them.
- An override causes test failures to be disclosed in affected replies.

## Automated tests

Use unit and integration-style tests with temporary Git repositories and fake `gh`/`git` process
adapters. Cover at least:

- current-branch and explicit-selector PR discovery
- GraphQL cursor pagination
- response normalization and unresolved filtering
- outdated comments and missing current line numbers
- clean exact-head checkout, stale checkout, dirty checkout, local-ahead, and diverged states
- atomic session writes and recovery from truncated state
- path allowlisting during staging
- publication digest generation and invalidation
- tests-pass, tests-fail, and explicit-override paths
- PR-head and thread-fingerprint drift
- commit failure, push failure, reply failure, and resolve failure
- duplicate marker detection and idempotent retry
- no-change-only publication
- argument handling for malicious comment bodies, paths, and reply text

No automated test may access a real GitHub repository or use the developer's stored credentials.

## Documentation acceptance

Document installation as a personal Codex skill, prerequisites, `gh auth login`, normal usage,
explicit PR selection, isolated worktree behavior, final approval, failed-test override, recovery,
and uninstallation. Include example machine-readable outputs for every helper command.
