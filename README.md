# GitHub Review Fixer

GitHub Review Fixer is an installable Codex Desktop/CLI skill for safely processing unresolved,
line-level pull-request review threads. It keeps inspection, local editing, testing, and remote
publication separate, and requires approval of a fresh digest before commit, push, replies, or
thread resolution.

The installable package and complete usage guide are in
[`github-review-fixer/`](github-review-fixer/README.md).

## Verify

```sh
python3 -m unittest discover -s github-review-fixer/tests -v
python3 /path/to/skill-creator/scripts/quick_validate.py github-review-fixer
```

The automated suite uses fakes for Git and GitHub interactions; it does not contact a real
repository or modify global Git configuration.

Design background and acceptance criteria are retained in [`docs/`](docs/).
