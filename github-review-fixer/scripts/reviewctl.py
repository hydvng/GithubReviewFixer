#!/usr/bin/env python3
"""Deterministic state and publication helper for the github-review-fixer skill.

The helper deliberately contains no model integration.  It treats repository and
GitHub text as data, invokes git/gh without a shell, and emits one JSON document.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Mapping, Sequence
from urllib.parse import urlparse

try:  # pragma: no cover - platform selection is exercised by the host OS
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
try:  # pragma: no cover - platform selection is exercised by the host OS
    import msvcrt
except ImportError:  # Unix
    msvcrt = None  # type: ignore[assignment]


VERSION = 1
STATE_DIR_NAME = "codex-review-fixer"
MARKER_PREFIX = "codex-review-fixer"
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
NODE_ID_RE = re.compile(r"^[A-Za-z0-9_=-]+$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
CHOICE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
INTAKE_MODES = ("read-only", "preview", "full", "custom")
CHECKPOINT_KINDS = (
    "environment",
    "change-approval",
    "reply-approval",
    "publication-approval",
    "blocked",
    "completed",
)
CHECKPOINT_ROUTES = ("codex", "bootyourdonkey", "telegram")
OID_RE = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*)(?:bearer|token)\s+\S+"),
    re.compile(r"\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{12,}\b"),
)


THREADS_QUERY = """
query ReviewThreads($owner:String!,$name:String!,$number:Int!,$after:String){
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      reviewThreads(first:100,after:$after){
        pageInfo{hasNextPage endCursor}
        nodes{
          id isResolved isOutdated path line startLine
          comments(first:100){
            pageInfo{hasNextPage endCursor}
            nodes{id databaseId body createdAt url diffHunk author{login}}
          }
        }
      }
    }
  }
}
"""

COMMENTS_QUERY = """
query ReviewThreadComments($id:ID!,$after:String){
  node(id:$id){
    ... on PullRequestReviewThread{
      comments(first:100,after:$after){
        pageInfo{hasNextPage endCursor}
        nodes{id databaseId body createdAt url diffHunk author{login}}
      }
    }
  }
}
"""

REPLY_MUTATION = """
mutation Reply($thread:ID!,$body:String!){
  addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$thread,body:$body}){
    comment{id databaseId url body}
  }
}
"""

RESOLVE_MUTATION = """
mutation Resolve($thread:ID!){
  resolveReviewThread(input:{threadId:$thread}){thread{id isResolved}}
}
"""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def redact(text: str) -> str:
    result = text
    for pattern in SECRET_PATTERNS:
        result = pattern.sub(lambda match: (match.group(1) if match.lastindex else "") + "[REDACTED]", result)
    return result


def reject_sensitive(value: str, label: str) -> str:
    if redact(value) != value:
        raise ReviewError("sensitive_input", f"{label} contains token-shaped content")
    return value


def load_json_file(path: Path, label: str, expected: type) -> Any:
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ReviewError("input_too_large", f"{label} exceeds the 1 MiB limit")
        value = json.loads(path.read_text(encoding="utf-8"))
    except ReviewError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewError("invalid_json_file", f"{label} must contain valid JSON") from exc
    if not isinstance(value, expected):
        raise ReviewError("invalid_json_type", f"{label} has the wrong JSON type")
    return value


def redact_json(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    if isinstance(value, dict):
        return {str(key): redact_json(item) for key, item in value.items()}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise ReviewError("invalid_json_value", "Context contains an unsupported JSON value")


class ReviewError(RuntimeError):
    def __init__(self, code: str, message: str, details: Any | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_json(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": redact(self.message)}
        if self.details is not None:
            result["details"] = self.details
        return result


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class Runner:
    """Subprocess adapter. Tests replace this object with a scripted fake."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        check: bool = True,
        input_text: str | None = None,
    ) -> CommandResult:
        try:
            process = subprocess.run(
                list(argv),
                cwd=cwd,
                input=input_text,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                env={**os.environ, "LC_ALL": "C"},
                check=False,
            )
        except OSError as exc:
            raise ReviewError("command_unavailable", f"Cannot run {argv[0]}: {exc}") from exc
        result = CommandResult(tuple(argv), process.returncode, process.stdout, process.stderr)
        if check and result.returncode:
            raise ReviewError(
                "command_failed",
                f"{argv[0]} exited with status {result.returncode}",
                {"stderr": redact(result.stderr[-4000:])},
            )
        return result


def _json_output(result: CommandResult, operation: str) -> Any:
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ReviewError(
            "invalid_command_json",
            f"{operation} returned invalid JSON",
            {"stdout": redact(result.stdout[-1000:])},
        ) from exc


def parse_host(remote_url: str) -> str | None:
    if "://" in remote_url:
        return urlparse(remote_url).hostname
    match = re.match(r"^(?:[^@]+@)?([^:]+):.+$", remote_url)
    return match.group(1) if match else None


def repo_slug_from_url(remote_url: str) -> str | None:
    if "://" in remote_url:
        path = urlparse(remote_url).path
    else:
        match = re.match(r"^(?:[^@]+@)?[^:]+:(.+)$", remote_url)
        path = match.group(1) if match else remote_url
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else None


def repo_slug_from_pr_url(pr_url: str, host: str) -> str:
    parsed = urlparse(pr_url)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.hostname != host or len(parts) != 4 or parts[2] != "pull" or not parts[3].isdigit():
        raise ReviewError("invalid_pr_url", "GitHub returned an invalid pull-request URL")
    return validate_repo_slug(f"{parts[0]}/{parts[1]}")


def validate_repo_slug(value: str) -> str:
    if not REPO_RE.fullmatch(value):
        raise ReviewError("invalid_repository", "GitHub returned an invalid repository identity")
    return value


def validate_node_id(value: str, label: str = "node") -> str:
    if not NODE_ID_RE.fullmatch(value):
        raise ReviewError("invalid_node_id", f"GitHub returned an invalid {label} ID")
    return value


def validate_oid(value: str) -> str:
    if not OID_RE.fullmatch(value):
        raise ReviewError("invalid_git_oid", "A command returned an invalid Git object ID")
    return value.lower()


def validate_pr_selector(value: str | None, host: str) -> str | None:
    if value is None or value.isdigit():
        return value
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or parsed.hostname != host or not re.search(r"/pull/\d+/?$", parsed.path):
        raise ReviewError("invalid_pr_selector", "PR selector must be a number or a pull-request URL on the repository host")
    return value


def parse_explicit_pr_url(value: str) -> dict[str, Any]:
    reject_sensitive(value, "PR URL")
    parsed = urlparse(value)
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or len(parts) != 4
        or parts[2] != "pull"
        or not parts[3].isdigit()
    ):
        raise ReviewError("invalid_pr_url", "PR URL must be an explicit GitHub pull-request URL")
    repository = validate_repo_slug(f"{parts[0]}/{parts[1]}")
    return {
        "url": f"{parsed.scheme}://{parsed.hostname}/{repository}/pull/{int(parts[3])}",
        "host": parsed.hostname,
        "repository": repository,
        "number": int(parts[3]),
    }


def normalize_absolute_path(value: str, label: str) -> str:
    reject_sensitive(value, label)
    if "\0" in value or not Path(value).is_absolute():
        raise ReviewError("invalid_absolute_path", f"{label} must be an absolute path")
    return os.path.normpath(value)


def task_intake_options(pr_url: str | None, workspace_root: Path | None) -> dict[str, Any]:
    parsed_pr = parse_explicit_pr_url(pr_url) if pr_url else None
    normalized_workspace = (
        normalize_absolute_path(str(workspace_root), "Workspace root")
        if workspace_root is not None
        else None
    )
    derived_path = None
    if parsed_pr and normalized_workspace:
        owner, repository = parsed_pr["repository"].split("/", 1)
        derived_path = str(Path(normalized_workspace) / parsed_pr["host"] / owner / repository)
    return {
        "ok": True,
        "operation": "task-intake",
        "status": "selection_required",
        "fields": [
            {
                "id": "repository_path",
                "label": "Repository path",
                "required": True,
                "recommended": derived_path,
                "choices": ["derived-multi-repo-path", "existing-checkout", "custom-absolute-path"],
            },
            {
                "id": "pr_url",
                "label": "PR URL",
                "required": True,
                "recommended": parsed_pr["url"] if parsed_pr else None,
                "choices": ["provided-url", "custom-pull-request-url"],
            },
            {
                "id": "test_commands",
                "label": "Test commands",
                "required": True,
                "recommended": "detect-then-confirm-exact-commands",
                "choices": ["detect-then-confirm-exact-commands", "custom-command-list"],
            },
            {
                "id": "mode",
                "label": "Mode",
                "required": True,
                "recommended": "preview",
                "choices": list(INTAKE_MODES),
                "descriptions": {
                    "read-only": "Inspect only; no repository edits.",
                    "preview": "Edit and test locally; stop before publication.",
                    "full": "Edit and test, then offer a separately approved publication plan.",
                    "custom": "Use an explicit custom_mode description; publication approval remains mandatory.",
                },
            },
        ],
        "confirmation_required": True,
        "next": "Write all four explicit selections to JSON and rerun task-intake with --config-file.",
    }


def normalize_task_intake(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {"repository_path", "pr_url", "test_commands", "mode"}
    missing = sorted(required - set(value))
    if missing:
        raise ReviewError("intake_fields_missing", "Task intake is missing required fields", missing)
    repository_path = normalize_absolute_path(str(value["repository_path"]), "Repository path")
    pr = parse_explicit_pr_url(str(value["pr_url"]))
    commands = value["test_commands"]
    if not isinstance(commands, list) or not commands or not all(isinstance(item, str) for item in commands):
        raise ReviewError("invalid_test_commands", "test_commands must be a nonempty JSON string array")
    normalized_commands: list[str] = []
    for command in commands:
        command = reject_sensitive(command.strip(), "Test command")
        if not command or "\0" in command or len(command) > 4096:
            raise ReviewError("invalid_test_command", "Each test command must be nonempty and at most 4096 characters")
        normalized_commands.append(command)
    mode = value["mode"]
    if mode not in INTAKE_MODES:
        raise ReviewError("invalid_intake_mode", f"mode must be one of: {', '.join(INTAKE_MODES)}")
    custom_mode = value.get("custom_mode")
    if mode == "custom":
        if not isinstance(custom_mode, str) or not custom_mode.strip():
            raise ReviewError("custom_mode_required", "custom mode requires a nonempty custom_mode description")
        custom_mode = reject_sensitive(custom_mode.strip(), "Custom mode")
    elif custom_mode not in (None, ""):
        raise ReviewError("custom_mode_not_allowed", "custom_mode is only valid when mode is custom")
    result = {
        "schema_version": 1,
        "repository_path": repository_path,
        "pr_url": pr["url"],
        "repository": f"{pr['host']}/{pr['repository']}",
        "pr_number": pr["number"],
        "test_commands": normalized_commands,
        "mode": mode,
        "custom_mode": custom_mode if mode == "custom" else None,
    }
    return result


def task_intake_from_file(path: Path) -> dict[str, Any]:
    intake = normalize_task_intake(load_json_file(path, "Task intake file", dict))
    intake_digest = digest(intake)
    return {
        "ok": True,
        "operation": "task-intake",
        "status": "confirmation_required",
        "intake": intake,
        "digest": intake_digest,
        "confirmation_phrase": f"Confirm Review Fixer intake {intake_digest}",
    }


def normalize_choices(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ReviewError("invalid_choices", "Choices must be a nonempty JSON array")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ReviewError("invalid_choices", "Each choice must be a JSON object")
        identifier = item.get("id")
        label = item.get("label")
        if not isinstance(identifier, str) or not CHOICE_ID_RE.fullmatch(identifier) or identifier in seen:
            raise ReviewError("invalid_choice_id", "Choice IDs must be unique lowercase identifiers")
        if not isinstance(label, str) or not label.strip():
            raise ReviewError("invalid_choice_label", "Each choice needs a nonempty label")
        normalized = {"id": identifier, "label": reject_sensitive(label.strip(), "Choice label")}
        description = item.get("description")
        if description is not None:
            if not isinstance(description, str):
                raise ReviewError("invalid_choice_description", "Choice descriptions must be strings")
            normalized["description"] = reject_sensitive(description.strip(), "Choice description")
        result.append(normalized)
        seen.add(identifier)
    return result


def create_checkpoint(
    kind: str,
    prompt_file: Path,
    choices_file: Path | None,
    context_file: Path | None,
    routes: Sequence[str],
    session_id: str | None,
) -> dict[str, Any]:
    prompt = reject_sensitive(prompt_file.read_text(encoding="utf-8").strip(), "Checkpoint prompt")
    if not prompt:
        raise ReviewError("empty_checkpoint_prompt", "Checkpoint prompt cannot be empty")
    if len(prompt) > 8000:
        raise ReviewError("checkpoint_prompt_too_large", "Checkpoint prompt exceeds 8000 characters")
    if kind not in CHECKPOINT_KINDS:
        raise ReviewError("invalid_checkpoint_kind", "Unsupported checkpoint kind")
    requires_response = kind not in {"blocked", "completed"}
    choices = normalize_choices(load_json_file(choices_file, "Choices file", list)) if choices_file else []
    if requires_response and not choices:
        raise ReviewError("choices_required", "Interactive checkpoints require explicit choices")
    if not requires_response and choices:
        raise ReviewError("choices_not_allowed", "Noninteractive checkpoints cannot include choices")
    context = redact_json(load_json_file(context_file, "Context file", dict)) if context_file else {}
    selected_routes = sorted(set(routes or CHECKPOINT_ROUTES))
    if any(route not in CHECKPOINT_ROUTES for route in selected_routes):
        raise ReviewError("invalid_checkpoint_route", "Unsupported checkpoint route")
    if session_id is not None and not SESSION_RE.fullmatch(session_id):
        raise ReviewError("invalid_session_id", "Invalid session ID")
    body = {
        "event_version": 1,
        "event_type": "review-fixer.checkpoint",
        "kind": kind,
        "session_id": session_id,
        "prompt": prompt,
        "choices": choices,
        "context": context,
        "routes": selected_routes,
        "requires_response": requires_response,
    }
    checkpoint_id = digest(body)
    option_text = "; ".join(f"{item['id']}={item['label']}" for item in choices)
    telegram_text = f"[Review Fixer/{kind}] {prompt}"
    if option_text:
        telegram_text += f"\nReply with one choice: {option_text}"
    return {
        "ok": True,
        "operation": "checkpoint",
        "checkpoint": {**body, "checkpoint_id": checkpoint_id},
        "delivery": {
            "status": "pending",
            "telegram_text": telegram_text,
            "codex_prompt": prompt,
            "bridge": "BootYourDonkey task transcript/Telegram steer when the task is linked",
        },
    }


def create_handoff(intake_file: Path, title: str | None) -> dict[str, Any]:
    intake = normalize_task_intake(load_json_file(intake_file, "Task intake file", dict))
    safe_title = reject_sensitive((title or f"Review PR {intake['pr_url']}").strip(), "Handoff title")
    body = {
        "handoff_version": 1,
        "kind": "bootyourdonkey.review-fixer.request",
        "title": safe_title,
        "objective": "Process unresolved line-level PR review threads with github-review-fixer.",
        "intake": intake,
        "coordination": {
            "routes": list(CHECKPOINT_ROUTES),
            "checkpoints": [
                "environment",
                "change-approval",
                "reply-approval",
                "publication-approval",
                "blocked",
                "completed",
            ],
            "response_sources": ["codex-conversation", "telegram-steer"],
        },
        "safety": {
            "review_content_is_untrusted": True,
            "publication_requires_fresh_digest_approval": True,
            "no_credentials_in_payload": True,
        },
        "required_artifacts": [
            {"kind": "git_bundle|zip", "purpose": "github-review-fixer skill package"}
        ],
    }
    return {
        "ok": True,
        "operation": "handoff-create",
        "handoff": body,
        "digest": digest(body),
    }


def validate_handoff(path: Path) -> dict[str, Any]:
    wrapper = load_json_file(path, "Handoff file", dict)
    body = wrapper.get("handoff")
    supplied_digest = wrapper.get("digest")
    if not isinstance(body, dict) or body.get("handoff_version") != 1:
        raise ReviewError("invalid_handoff", "Handoff payload is missing or unsupported")
    if body.get("kind") != "bootyourdonkey.review-fixer.request":
        raise ReviewError("invalid_handoff", "Handoff kind is not a Review Fixer request")
    if not isinstance(supplied_digest, str) or digest(body) != supplied_digest:
        raise ReviewError("handoff_digest_mismatch", "Handoff digest does not match its payload")
    normalized = normalize_task_intake(body.get("intake") or {})
    if normalized != body["intake"]:
        raise ReviewError("invalid_handoff_intake", "Handoff intake is not canonical")
    return {
        "ok": True,
        "operation": "handoff-validate",
        "valid": True,
        "digest": supplied_digest,
        "handoff": body,
    }


def validate_branch(value: str) -> str:
    forbidden = ("..", "@{", "\\", "~", "^", ":", "?", "*", "[")
    components = value.split("/")
    if (
        not value
        or value.startswith(("-", ".", "/"))
        or value.endswith(("/", ".", ".lock"))
        or "//" in value
        or any(item in value for item in forbidden)
        or value == "@"
        or any(component.startswith(".") or component.endswith((".", ".lock")) for component in components)
        or any(character.isspace() or ord(character) == 127 for character in value)
    ):
        raise ReviewError("invalid_head_branch", "GitHub returned an invalid PR head branch")
    return value


class Git:
    def __init__(self, root: Path, runner: Runner):
        self.root = root.resolve()
        self.runner = runner

    @classmethod
    def discover(cls, cwd: Path, runner: Runner) -> "Git":
        result = runner.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
        return cls(Path(result.stdout.strip()), runner)

    def run(self, args: Sequence[str], *, check: bool = True) -> CommandResult:
        return self.runner.run(["git", *args], cwd=self.root, check=check)

    def head(self) -> str:
        return validate_oid(self.run(["rev-parse", "HEAD"]).stdout.strip())

    def git_dir(self) -> Path:
        value = self.run(["rev-parse", "--git-dir"]).stdout.strip()
        path = Path(value)
        return (self.root / path).resolve() if not path.is_absolute() else path.resolve()

    def origin_url(self) -> str | None:
        result = self.run(["remote", "get-url", "origin"], check=False)
        return result.stdout.strip() if result.returncode == 0 else None

    def status_entries(self) -> list[dict[str, str]]:
        raw = self.run(["status", "--porcelain=v1", "-z", "--untracked-files=all"]).stdout
        records = raw.split("\0")
        entries: list[dict[str, str]] = []
        index = 0
        while index < len(records) and records[index]:
            record = records[index]
            if len(record) < 4:
                raise ReviewError("invalid_git_status", "git returned malformed porcelain status")
            status, path = record[:2], record[3:]
            entry = {"status": status, "path": path}
            if "R" in status or "C" in status:
                index += 1
                if index >= len(records) or not records[index]:
                    raise ReviewError("invalid_git_status", "git returned an incomplete rename record")
                entry["source_path"] = records[index]
            entries.append(entry)
            index += 1
        return entries

    def changed_paths(self) -> list[str]:
        paths: set[str] = set()
        for entry in self.status_entries():
            paths.add(entry["path"])
            if "source_path" in entry:
                paths.add(entry["source_path"])
        return sorted(paths)

    def remotes(self) -> dict[str, list[str]]:
        names = sorted(filter(None, self.run(["remote"]).stdout.splitlines()))
        result: dict[str, list[str]] = {}
        for name in names:
            urls = self.run(["remote", "get-url", "--push", "--all", name], check=False)
            if urls.returncode == 0:
                result[name] = sorted(filter(None, urls.stdout.splitlines()))
        return result

    def choose_push_remote(self, head_repo: str, host: str, requested: str | None) -> str:
        remotes = self.remotes()
        if requested:
            if not REMOTE_RE.fullmatch(requested):
                raise ReviewError("push_remote_invalid", "Push remote name is invalid")
            if requested not in remotes:
                raise ReviewError("push_remote_missing", f"Git remote {requested!r} does not exist")
            if (head_repo, host) not in {(repo_slug_from_url(url), parse_host(url)) for url in remotes[requested]}:
                raise ReviewError(
                    "push_remote_mismatch",
                    f"Git remote {requested!r} does not point to PR head repository {head_repo}",
                )
            return requested
        matches = [
            name
            for name, urls in remotes.items()
            if REMOTE_RE.fullmatch(name)
            and (head_repo, host) in {(repo_slug_from_url(url), parse_host(url)) for url in urls}
        ]
        if len(matches) != 1:
            raise ReviewError(
                "push_remote_ambiguous",
                "Cannot choose exactly one remote for the PR head repository",
                {"head_repository": head_repo, "matching_remotes": matches},
            )
        return matches[0]

    def path_fingerprints(self, paths: Iterable[str]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for value in sorted(set(paths)):
            path = safe_repo_path(self.root, value)
            if path.is_symlink():
                result[value] = {"kind": "symlink", "target": os.readlink(path)}
            elif path.is_file():
                hasher = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        hasher.update(chunk)
                result[value] = {
                    "kind": "file",
                    "sha256": hasher.hexdigest(),
                    "executable": bool(path.stat().st_mode & 0o111),
                }
            elif not path.exists():
                result[value] = {"kind": "missing"}
            else:
                raise ReviewError("invalid_recorded_path", f"Recorded path is not a file: {value}")
        return result

    def commit_changed_paths(self, parent: str, commit: str) -> list[str]:
        raw = self.run(["diff", "--name-only", "--no-renames", "-z", parent, commit]).stdout
        return sorted(filter(None, raw.split("\0")))


def safe_repo_path(root: Path, value: str) -> Path:
    pure = PurePosixPath(value)
    if not value or pure.is_absolute() or ".." in pure.parts or value in {".", ".."} or "\0" in value:
        raise ReviewError("unsafe_path", f"Unsafe repository-relative path: {value!r}")
    candidate = root.joinpath(*pure.parts)
    # Do not resolve the final path: a recorded symlink is a legitimate changed file.
    parent = candidate.parent.resolve()
    try:
        parent.relative_to(root.resolve())
    except ValueError as exc:
        raise ReviewError("unsafe_path", f"Path escapes the repository: {value!r}") from exc
    return candidate


class GitHub:
    def __init__(self, git: Git, runner: Runner):
        self.git = git
        self.runner = runner
        self.host = self._repository_host()

    def _repository_host(self) -> str:
        origin = self.git.origin_url()
        if not origin or not parse_host(origin):
            raise ReviewError("repository_host_unknown", "Cannot determine GitHub host from the origin remote")
        return str(parse_host(origin))

    def run(self, args: Sequence[str], *, check: bool = True) -> CommandResult:
        return self.runner.run(["gh", *args], cwd=self.git.root, check=check)

    def authenticate(self) -> None:
        self.run(["auth", "status", "--hostname", self.host])

    def repo_identity(self) -> dict[str, str]:
        data = _json_output(self.run(["repo", "view", "--json", "nameWithOwner,url"]), "gh repo view")
        return {"name_with_owner": validate_repo_slug(data["nameWithOwner"]), "url": data["url"], "host": self.host}

    def pr(self, selector: str | None) -> dict[str, Any]:
        selector = validate_pr_selector(selector, self.host)
        fields = "number,url,headRefName,headRefOid,headRepository,headRepositoryOwner,baseRefName,state"
        args = ["pr", "view"]
        if selector:
            args.append(selector)
        args += ["--json", fields]
        data = _json_output(self.run(args), "gh pr view")
        head_repo_data = data.get("headRepository") or {}
        head_repo = head_repo_data.get("nameWithOwner")
        if not head_repo:
            head_owner = (data.get("headRepositoryOwner") or {}).get("login")
            head_name = head_repo_data.get("name")
            if head_owner and head_name:
                head_repo = f"{head_owner}/{head_name}"
        if not head_repo:
            raise ReviewError("head_repository_missing", "The pull request head repository is unavailable")
        result = {
            "number": int(data["number"]),
            "url": data["url"],
            "head_ref": validate_branch(data["headRefName"]),
            "head_oid": validate_oid(data["headRefOid"]),
            "head_repository": validate_repo_slug(head_repo),
            "base_repository": repo_slug_from_pr_url(data["url"], self.host),
            "base_ref": data["baseRefName"],
            "state": str(data["state"]).upper(),
        }
        if result["state"] != "OPEN":
            raise ReviewError("pr_not_open", f"Pull request #{result['number']} is not open")
        return result

    def graphql(self, query: str, variables: Mapping[str, Any]) -> Any:
        args = ["api", "graphql", "-f", f"query={query}"]
        for key in sorted(variables):
            value = variables[key]
            if value is None:
                continue
            flag = "-F" if isinstance(value, (int, bool)) else "-f"
            args += [flag, f"{key}={str(value).lower() if isinstance(value, bool) else value}"]
        data = _json_output(self.run(args), "gh api graphql")
        if isinstance(data, dict) and data.get("errors"):
            errors = [
                {"type": item.get("type"), "message": redact(str(item.get("message") or "GraphQL error"))}
                for item in data["errors"]
                if isinstance(item, dict)
            ]
            raise ReviewError("github_graphql_error", "GitHub GraphQL returned errors", errors)
        return data

    def threads(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        owner, name = validate_repo_slug(repo).split("/", 1)
        cursor: str | None = None
        raw_threads: list[dict[str, Any]] = []
        while True:
            data = self.graphql(THREADS_QUERY, {"owner": owner, "name": name, "number": pr_number, "after": cursor})
            try:
                connection = data["data"]["repository"]["pullRequest"]["reviewThreads"]
            except (KeyError, TypeError) as exc:
                raise ReviewError("invalid_graphql_response", "Review-thread response is missing required fields") from exc
            for raw in connection.get("nodes") or []:
                thread_id = validate_node_id(raw["id"], "thread")
                comments_connection = raw.get("comments") or {}
                comments = list(comments_connection.get("nodes") or [])
                comments_cursor = (comments_connection.get("pageInfo") or {}).get("endCursor")
                while (comments_connection.get("pageInfo") or {}).get("hasNextPage"):
                    if not comments_cursor:
                        raise ReviewError("invalid_graphql_response", "Comment pagination omitted its next cursor")
                    extra = self.graphql(COMMENTS_QUERY, {"id": thread_id, "after": comments_cursor})
                    try:
                        comments_connection = extra["data"]["node"]["comments"]
                    except (KeyError, TypeError) as exc:
                        raise ReviewError("invalid_graphql_response", "Thread-comment response is missing required fields") from exc
                    comments.extend(comments_connection.get("nodes") or [])
                    comments_cursor = (comments_connection.get("pageInfo") or {}).get("endCursor")
                raw_copy = dict(raw)
                raw_copy["comments"] = comments
                raw_threads.append(raw_copy)
            page = connection.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")
            if not cursor:
                raise ReviewError("invalid_graphql_response", "Thread pagination omitted its next cursor")
        normalized = [normalize_thread(raw) for raw in raw_threads]
        return [thread for thread in normalized if not thread["is_resolved"] and thread["path"]]

    def reply(self, thread_id: str, body: str) -> dict[str, Any]:
        data = self.graphql(REPLY_MUTATION, {"thread": validate_node_id(thread_id, "thread"), "body": body})
        try:
            comment = data["data"]["addPullRequestReviewThreadReply"]["comment"]
            return {
                "node_id": validate_node_id(comment["id"], "comment"),
                "database_id": comment.get("databaseId"),
                "url": comment.get("url"),
            }
        except (KeyError, TypeError) as exc:
            raise ReviewError("invalid_graphql_response", "Reply mutation did not return a comment") from exc

    def resolve(self, thread_id: str) -> None:
        data = self.graphql(RESOLVE_MUTATION, {"thread": validate_node_id(thread_id, "thread")})
        try:
            resolved = data["data"]["resolveReviewThread"]["thread"]["isResolved"]
        except (KeyError, TypeError) as exc:
            raise ReviewError("invalid_graphql_response", "Resolve mutation did not return thread state") from exc
        if not resolved:
            raise ReviewError("resolve_not_confirmed", f"GitHub did not confirm resolution of {thread_id}")


def normalize_comment(raw: Mapping[str, Any]) -> dict[str, Any]:
    author = raw.get("author") or {}
    raw_body = str(raw.get("body") or "")
    raw_diff = str(raw.get("diffHunk") or "")
    return {
        "node_id": validate_node_id(str(raw["id"]), "comment"),
        "database_id": raw.get("databaseId"),
        "author": author.get("login"),
        "body": redact(raw_body),
        "body_sha256": hashlib.sha256(raw_body.encode("utf-8")).hexdigest(),
        "created_at": raw.get("createdAt"),
        "url": raw.get("url"),
        "diff_hunk": redact(raw_diff) if raw.get("diffHunk") is not None else None,
        "diff_hunk_sha256": hashlib.sha256(raw_diff.encode("utf-8")).hexdigest(),
    }


def normalize_thread(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "thread_id": validate_node_id(str(raw["id"]), "thread"),
        "is_resolved": bool(raw.get("isResolved")),
        "is_outdated": bool(raw.get("isOutdated")),
        "path": raw.get("path"),
        "line": raw.get("line"),
        "start_line": raw.get("startLine"),
        "comments": [normalize_comment(comment) for comment in raw.get("comments") or []],
    }


def marker(session_id: str, thread_id: str) -> str:
    return f"<!-- {MARKER_PREFIX}:{session_id}:{thread_id} -->"


def thread_fingerprint(thread: Mapping[str, Any], *, ignore_marker: str | None = None) -> str:
    comments = thread.get("comments") or []
    if ignore_marker:
        comments = [comment for comment in comments if ignore_marker not in (comment.get("body") or "")]
    material = {
        "thread_id": thread["thread_id"],
        "is_outdated": thread.get("is_outdated"),
        "path": thread.get("path"),
        "line": thread.get("line"),
        "start_line": thread.get("start_line"),
        "comments": comments,
    }
    return digest(material)


def thread_comments_fingerprint(thread: Mapping[str, Any], *, ignore_marker: str | None = None) -> str:
    comments = thread.get("comments") or []
    if ignore_marker:
        comments = [comment for comment in comments if ignore_marker not in (comment.get("body") or "")]
    return digest({"thread_id": thread["thread_id"], "comments": comments})


class SessionStore:
    def __init__(self, git_dir: Path):
        self.root = git_dir / STATE_DIR_NAME
        self.sessions = self.root / "sessions"
        self.lock_path = self.root / "lock"

    def ensure(self) -> None:
        self.sessions.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        os.chmod(self.sessions, 0o700)

    def path(self, session_id: str) -> Path:
        if not SESSION_RE.fullmatch(session_id):
            raise ReviewError("invalid_session_id", "Invalid session ID")
        return self.sessions / f"{session_id}.json"

    @contextlib.contextmanager
    def lock(self, exclusive: bool = True) -> Iterator[None]:
        self.ensure()
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            os.chmod(self.lock_path, 0o600)
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            elif msvcrt is not None:
                handle.seek(0)
                if handle.read(1) == "":
                    handle.write("0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def load(self, session_id: str) -> dict[str, Any]:
        path = self.path(session_id)
        backup = path.with_suffix(".json.bak")
        for candidate, recovered in ((path, False), (backup, True)):
            try:
                with candidate.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if data.get("version") != VERSION or data.get("session_id") != session_id:
                    raise ValueError("identity mismatch")
                if recovered:
                    data["recovered_from_backup"] = True
                return data
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        raise ReviewError("session_unreadable", f"Session {session_id!r} is missing or corrupt")

    def save(self, session: dict[str, Any]) -> None:
        self.ensure()
        path = self.path(session["session_id"])
        session["revision"] = int(session.get("revision", 0)) + 1
        session["updated_at"] = utc_now()
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=self.sessions)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(session, handle, sort_keys=True, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            backup = path.with_suffix(".json.bak")
            if path.exists():
                os.replace(path, backup)
            os.replace(temp_name, path)
            try:
                directory_fd = os.open(self.sessions, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:  # Directory fsync is unavailable on some platforms.
                pass
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


def repository_context(cwd: Path, runner: Runner) -> tuple[Git, GitHub, dict[str, str]]:
    git = Git.discover(cwd, runner)
    github = GitHub(git, runner)
    github.authenticate()
    repo = github.repo_identity()
    return git, github, repo


def inspect(cwd: Path, runner: Runner, selector: str | None) -> dict[str, Any]:
    git, github, repo = repository_context(cwd, runner)
    pr = github.pr(selector)
    if pr["base_repository"] != repo["name_with_owner"]:
        raise ReviewError(
            "repository_mismatch",
            "Selected pull request does not belong to the current repository",
            {"current": repo["name_with_owner"], "selected": pr["base_repository"]},
        )
    threads = github.threads(repo["name_with_owner"], pr["number"])
    local_head = git.head()
    entries = git.status_entries()
    exact = local_head == pr["head_oid"]
    clean = not entries
    return {
        "ok": True,
        "operation": "inspect",
        "repository": repo,
        "pr": pr,
        "threads": threads,
        "checkout": {
            "root": str(git.root),
            "local_head": local_head,
            "head_matches_pr": exact,
            "clean": clean,
            "status": entries,
            "usable": exact and clean,
            "isolation_required": not (exact and clean),
        },
    }


def create_session(
    cwd: Path,
    runner: Runner,
    selector: str | None,
    push_remote: str | None,
    requested_id: str | None = None,
) -> dict[str, Any]:
    report = inspect(cwd, runner, selector)
    checkout = report["checkout"]
    if not checkout["usable"]:
        raise ReviewError(
            "isolation_required",
            "Current checkout is not a clean exact copy of the PR head; create an isolated PR worktree",
            checkout,
        )
    git = Git(Path(checkout["root"]), runner)
    session_id = requested_id or f"pr-{report['pr']['number']}-{uuid.uuid4().hex[:12]}"
    if not SESSION_RE.fullmatch(session_id):
        raise ReviewError("invalid_session_id", "Invalid requested session ID")
    store = SessionStore(git.git_dir())
    threads = report["threads"]
    session = {
        "version": VERSION,
        "session_id": session_id,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "revision": 0,
        "repository": {**report["repository"], "root": str(git.root), "git_dir": str(git.git_dir())},
        "pr": report["pr"],
        "expected_head": report["pr"]["head_oid"],
        "latest_observed_head": report["pr"]["head_oid"],
        "initial_status": checkout["status"],
        "push_remote_override": push_remote,
        "thread_order": [thread["thread_id"] for thread in threads],
        "threads": {
            thread["thread_id"]: {
                "snapshot": thread,
                "fingerprint": thread_fingerprint(thread),
                "comments_fingerprint": thread_comments_fingerprint(thread),
                "state": "pending",
                "decision": None,
                "reply": None,
                "paths": [],
                "tests": [],
                "reply_result": None,
                "resolved": False,
                "error": None,
            }
            for thread in threads
        },
        "publication": {"status": "not_started", "plan": None, "digest": None, "commit_sha": None, "pushed": False},
    }
    with store.lock():
        if store.path(session_id).exists():
            raise ReviewError("session_exists", f"Session {session_id!r} already exists")
        store.save(session)
    return {"ok": True, "operation": "session-start", "session": session}


def load_paths(path: Path | None) -> list[str]:
    if path is None:
        return []
    text = path.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith("["):
        value = json.loads(text)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ReviewError("invalid_paths_file", "Paths JSON must be an array of strings")
        paths = value
    else:
        paths = [line for line in text.splitlines() if line]
    return sorted(set(paths))


def load_tests(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewError("invalid_tests_file", "Tests file must contain valid JSON") from exc
    if not isinstance(value, list):
        raise ReviewError("invalid_tests_file", "Tests JSON must be an array")
    normalized: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("command"), str):
            raise ReviewError("invalid_tests_file", "Each test needs a display-only command string")
        status = item.get("status")
        if status not in {"passed", "failed", "unavailable", "not_run"}:
            raise ReviewError("invalid_tests_file", "Test status must be passed, failed, unavailable, or not_run")
        normalized.append(
            {
                "command": redact(item["command"]),
                "status": status,
                "summary": redact(str(item.get("summary") or "")),
            }
        )
    return normalized


def record_decision(
    cwd: Path,
    runner: Runner,
    session_id: str,
    thread_id: str,
    decision: str,
    reply_file: Path,
    paths_file: Path | None,
    tests_file: Path | None,
) -> dict[str, Any]:
    git = Git.discover(cwd, runner)
    store = SessionStore(git.git_dir())
    try:
        reply = reply_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ReviewError("reply_file_unreadable", f"Cannot read reply file: {exc}") from exc
    if not reply:
        raise ReviewError("empty_reply", "Confirmed reply text cannot be empty")
    if redact(reply) != reply:
        raise ReviewError("sensitive_reply", "Confirmed reply contains token-shaped content and will not be stored or published")
    paths = load_paths(paths_file)
    tests = load_tests(tests_file)
    for value in paths:
        safe_repo_path(git.root, value)
    if decision == "change" and not paths:
        raise ReviewError("paths_required", "A change decision must record at least one path")
    if decision == "change" and not tests:
        raise ReviewError("tests_required", "A change decision must record test evidence")
    if decision == "no-change" and paths:
        raise ReviewError("paths_not_allowed", "A no-change decision cannot record changed paths")
    with store.lock():
        session = store.load(session_id)
        ensure_same_repo(session, git)
        if session["publication"]["status"] != "not_started":
            raise ReviewError("publication_started", "Decisions cannot change after publication has started")
        if thread_id not in session["threads"]:
            raise ReviewError("thread_not_in_session", f"Thread {thread_id!r} is not in this session")
        pending = [
            item
            for item in session["thread_order"]
            if session["threads"][item]["state"] == "pending"
        ]
        entry = session["threads"][thread_id]
        proposed = {"decision": decision, "reply": reply, "paths": paths, "tests": tests}
        existing = {key: entry.get(key) for key in proposed}
        if entry["state"] == "ready" and existing == proposed:
            return {"ok": True, "operation": "record", "idempotent": True, "thread": entry}
        if entry["state"] != "pending":
            raise ReviewError("decision_already_recorded", "Thread already has a different confirmed decision")
        if not pending or pending[0] != thread_id:
            raise ReviewError("thread_out_of_order", f"Record the next pending thread first: {pending[0] if pending else 'none'}")
        entry.update(proposed)
        entry["state"] = "ready"
        entry["confirmed_at"] = utc_now()
        store.save(session)
        return {"ok": True, "operation": "record", "idempotent": False, "thread": entry}


def update_tests(
    cwd: Path,
    runner: Runner,
    session_id: str,
    thread_id: str,
    tests_file: Path,
) -> dict[str, Any]:
    """Replace test evidence for a confirmed change before publication starts."""
    git = Git.discover(cwd, runner)
    store = SessionStore(git.git_dir())
    tests = load_tests(tests_file)
    if not tests:
        raise ReviewError("tests_required", "Updated test evidence cannot be empty")
    with store.lock():
        session = store.load(session_id)
        ensure_same_repo(session, git)
        if session["publication"]["status"] != "not_started":
            raise ReviewError("publication_started", "Test evidence cannot change after publication has started")
        if thread_id not in session["threads"]:
            raise ReviewError("thread_not_in_session", f"Thread {thread_id!r} is not in this session")
        entry = session["threads"][thread_id]
        if entry["state"] != "ready" or entry.get("decision") != "change":
            raise ReviewError(
                "tests_not_updateable",
                "Test evidence can only be updated for a confirmed change",
            )
        if entry.get("tests") == tests:
            return {
                "ok": True,
                "operation": "update-tests",
                "idempotent": True,
                "thread": entry,
            }
        entry["tests"] = tests
        entry["tests_updated_at"] = utc_now()
        store.save(session)
        return {
            "ok": True,
            "operation": "update-tests",
            "idempotent": False,
            "thread": entry,
        }


def ensure_same_repo(session: Mapping[str, Any], git: Git) -> None:
    if Path(session["repository"]["root"]).resolve() != git.root:
        raise ReviewError("wrong_worktree", "Session belongs to a different working tree")


def read_session(cwd: Path, runner: Runner, session_id: str) -> dict[str, Any]:
    git = Git.discover(cwd, runner)
    store = SessionStore(git.git_dir())
    with store.lock(exclusive=False):
        session = store.load(session_id)
    ensure_same_repo(session, git)
    return {"ok": True, "operation": "session-show", "session": session}


def relocate_session(
    cwd: Path,
    runner: Runner,
    session_id: str,
    previous_root: Path,
) -> dict[str, Any]:
    git, _github, repository = repository_context(cwd, runner)
    store = SessionStore(git.git_dir())
    with store.lock():
        session = store.load(session_id)
        recorded_root = Path(session["repository"]["root"]).resolve()
        requested_root = previous_root.resolve()
        if requested_root != recorded_root:
            raise ReviewError(
                "previous_root_mismatch",
                "The confirmed previous root does not match the session record",
                {"recorded": str(recorded_root), "provided": str(requested_root)},
            )
        if recorded_root == git.root:
            return {
                "ok": True,
                "operation": "session-relocate",
                "idempotent": True,
                "previous_root": str(recorded_root),
                "current_root": str(git.root),
                "session": session,
            }
        if session["publication"]["status"] != "not_started":
            raise ReviewError(
                "publication_started",
                "A session cannot be relocated after publication has started",
            )
        recorded_git_dir = Path(session["repository"]["git_dir"]).resolve()
        if recorded_git_dir.exists():
            raise ReviewError(
                "previous_worktree_exists",
                "The recorded Git directory still exists; refusing to adopt a copied session",
                {"git_dir": str(recorded_git_dir)},
            )
        if repository["name_with_owner"] != session["repository"]["name_with_owner"]:
            raise ReviewError(
                "repository_mismatch",
                "Current checkout does not match the session repository",
                {
                    "current": repository["name_with_owner"],
                    "session": session["repository"]["name_with_owner"],
                },
            )
        if git.head() != session["expected_head"]:
            raise ReviewError(
                "head_drift",
                "Current checkout HEAD does not match the session's expected PR head",
            )
        session["repository"]["root"] = str(git.root)
        session["repository"]["git_dir"] = str(git.git_dir())
        store.save(session)
    return {
        "ok": True,
        "operation": "session-relocate",
        "idempotent": False,
        "previous_root": str(recorded_root),
        "current_root": str(git.root),
        "session": session,
    }


def all_recorded_paths(session: Mapping[str, Any]) -> list[str]:
    paths: set[str] = set()
    for thread_id in session["thread_order"]:
        paths.update(session["threads"][thread_id].get("paths") or [])
    return sorted(paths)


def test_blockers(session: Mapping[str, Any]) -> list[dict[str, str]]:
    blockers: list[dict[str, str]] = []
    for thread_id in session["thread_order"]:
        entry = session["threads"][thread_id]
        if entry.get("decision") != "change":
            continue
        if not entry.get("tests"):
            blockers.append({"thread_id": thread_id, "status": "not_run", "command": "(no test evidence)"})
        for test in entry.get("tests") or []:
            if test["status"] != "passed":
                blockers.append({"thread_id": thread_id, "status": test["status"], "command": test["command"]})
    return blockers


def reply_preview(session: Mapping[str, Any], thread_id: str, failures: list[dict[str, str]]) -> str:
    entry = session["threads"][thread_id]
    sections = [entry["reply"]]
    if entry["decision"] == "change":
        sections.append("Changed files: " + ", ".join(entry["paths"]))
        summaries = [f"{test['command']}: {test['status']}" + (f" ({test['summary']})" if test["summary"] else "") for test in entry["tests"]]
        sections.append("Tests: " + "; ".join(summaries))
        relevant = [item for item in failures if item["thread_id"] == thread_id]
        if relevant:
            sections.append("Test override: publishing despite " + "; ".join(f"{item['command']} ({item['status']})" for item in relevant) + ".")
        sections.append("Pushed commit: <PUSHED_COMMIT_SHA>")
    sections.append(marker(session["session_id"], thread_id))
    return "\n\n".join(sections)


def render_reply(preview: str, commit_sha: str | None) -> str:
    if "<PUSHED_COMMIT_SHA>" in preview:
        if not commit_sha:
            raise ReviewError("commit_missing", "Cannot render a change reply without a pushed commit")
        return preview.replace("<PUSHED_COMMIT_SHA>", commit_sha)
    return preview


def build_plan(
    session: Mapping[str, Any],
    git: Git,
    github: GitHub,
    commit_message: str,
) -> dict[str, Any]:
    if any(session["threads"][item]["state"] != "ready" for item in session["thread_order"]):
        raise ReviewError("decisions_incomplete", "Every session thread needs a confirmed decision and reply")
    fresh_pr = github.pr(str(session["pr"]["number"]))
    if fresh_pr["url"] != session["pr"]["url"] or fresh_pr["head_repository"] != session["pr"]["head_repository"]:
        raise ReviewError("pr_identity_drift", "Pull request identity changed")
    if fresh_pr["head_oid"] != session["expected_head"]:
        raise ReviewError(
            "pr_head_drift",
            "Pull request head changed; reanalysis is required",
            {"expected": session["expected_head"], "actual": fresh_pr["head_oid"]},
        )
    if git.head() != session["expected_head"]:
        raise ReviewError("local_head_drift", "Local HEAD no longer matches the analyzed PR head")
    fresh_threads = github.threads(session["repository"]["name_with_owner"], session["pr"]["number"])
    by_id = {item["thread_id"]: item for item in fresh_threads}
    stale: list[dict[str, str]] = []
    for thread_id in session["thread_order"]:
        current = by_id.get(thread_id)
        if current is None:
            stale.append({"thread_id": thread_id, "reason": "resolved_or_missing"})
        elif thread_fingerprint(current) != session["threads"][thread_id]["fingerprint"]:
            stale.append({"thread_id": thread_id, "reason": "comments_or_location_changed"})
    if stale:
        raise ReviewError("thread_drift", "One or more review threads changed; reanalysis is required", stale)
    recorded_paths = all_recorded_paths(session)
    actual_paths = git.changed_paths()
    if actual_paths != recorded_paths:
        raise ReviewError(
            "working_tree_not_isolated",
            "Working-tree changes do not exactly match recorded paths",
            {"recorded_paths": recorded_paths, "actual_paths": actual_paths},
        )
    has_changes = any(session["threads"][item]["decision"] == "change" for item in session["thread_order"])
    if has_changes and not commit_message.strip():
        raise ReviewError("commit_message_required", "A nonempty commit message is required")
    if redact(commit_message) != commit_message:
        raise ReviewError("sensitive_commit_message", "Commit message contains token-shaped content")
    blockers = test_blockers(session)
    push_remote = git.choose_push_remote(
        session["pr"]["head_repository"], github.host, session.get("push_remote_override")
    ) if has_changes else None
    full_commit_message = None
    if has_changes:
        full_commit_message = commit_message.strip() + f"\n\nCodex-Review-Fixer-Session: {session['session_id']}"
    replies = [
        {
            "thread_id": thread_id,
            "decision": session["threads"][thread_id]["decision"],
            "body_preview": reply_preview(session, thread_id, blockers),
            "will_resolve_after_reply": True,
        }
        for thread_id in session["thread_order"]
    ]
    plan = {
        "version": VERSION,
        "session_id": session["session_id"],
        "repository": session["repository"]["name_with_owner"],
        "pr_number": session["pr"]["number"],
        "pr_url": session["pr"]["url"],
        "expected_pr_head": session["expected_head"],
        "target": {
            "head_repository": session["pr"]["head_repository"],
            "branch": session["pr"]["head_ref"],
            "push_remote": push_remote,
        },
        "commit": {"required": has_changes, "message": full_commit_message, "paths": recorded_paths},
        "path_fingerprints": git.path_fingerprints(recorded_paths),
        "tests": {"blockers": blockers, "override_required": bool(blockers)},
        "threads": [
            {
                "thread_id": thread_id,
                "fingerprint": session["threads"][thread_id]["fingerprint"],
                "comments_fingerprint": session["threads"][thread_id]["comments_fingerprint"],
            }
            for thread_id in session["thread_order"]
        ],
        "replies": replies,
        "side_effect_order": (["commit", "push", "verify_remote_head"] if has_changes else ["refetch_remote_head"]) + ["reply_then_resolve_each_thread"],
    }
    plan["digest"] = digest(plan)
    return plan


def prepare_publish(cwd: Path, runner: Runner, session_id: str, commit_message: str) -> dict[str, Any]:
    git, github, _repo = repository_context(cwd, runner)
    store = SessionStore(git.git_dir())
    with store.lock(exclusive=False):
        session = store.load(session_id)
        ensure_same_repo(session, git)
        if session["publication"]["status"] != "not_started":
            raise ReviewError("publication_started", "Use retry-publish after publication has started")
        plan = build_plan(session, git, github, commit_message)
    return {
        "ok": True,
        "operation": "prepare-publish",
        "publishable_without_override": not plan["tests"]["override_required"],
        "plan": plan,
    }


def find_marker_comment(thread: Mapping[str, Any], expected_marker: str) -> dict[str, Any] | None:
    matches = [comment for comment in thread.get("comments") or [] if expected_marker in (comment.get("body") or "")]
    if len(matches) > 1:
        raise ReviewError("duplicate_reply_marker", "Multiple replies contain this session marker")
    return matches[0] if matches else None


def verify_threads_for_resume(session: Mapping[str, Any], github: GitHub) -> dict[str, dict[str, Any]]:
    fresh = github.threads(session["repository"]["name_with_owner"], session["pr"]["number"])
    by_id = {thread["thread_id"]: thread for thread in fresh}
    for thread_id in session["thread_order"]:
        entry = session["threads"][thread_id]
        if entry.get("resolved"):
            continue
        current = by_id.get(thread_id)
        if current is None:
            if entry.get("reply_result"):
                # A resolve may have succeeded immediately before state persistence.
                continue
            raise ReviewError("thread_drift", f"Thread {thread_id} was resolved or removed outside this session")
        own_marker = marker(session["session_id"], thread_id)
        if session["publication"]["plan"]["commit"]["required"]:
            unchanged = thread_comments_fingerprint(current, ignore_marker=own_marker) == entry["comments_fingerprint"]
        else:
            unchanged = thread_fingerprint(current, ignore_marker=own_marker) == entry["fingerprint"]
        if not unchanged:
            raise ReviewError("thread_drift", f"Thread {thread_id} changed during publication")
    return by_id


def save_publication_error(store: SessionStore, session: dict[str, Any], error: ReviewError) -> None:
    session["publication"]["status"] = "interrupted"
    session["publication"]["last_error"] = error.as_json()
    store.save(session)


def maybe_adopt_commit(session: dict[str, Any], git: Git) -> bool:
    current = git.head()
    if current == session["expected_head"]:
        return False
    parent = git.run(["rev-parse", f"{current}^"], check=False)
    message = git.run(["log", "-1", "--format=%B", current], check=False)
    trailer = f"Codex-Review-Fixer-Session: {session['session_id']}"
    if parent.returncode == 0 and parent.stdout.strip() == session["expected_head"] and message.returncode == 0 and trailer in message.stdout:
        session["publication"]["commit_sha"] = current
        session["publication"]["status"] = "committed"
        verify_session_commit(session, git)
        return True
    raise ReviewError("local_head_drift", "Local HEAD changed and is not the recoverable session commit")


def verify_session_commit(session: Mapping[str, Any], git: Git) -> None:
    publication = session["publication"]
    plan = publication["plan"]
    commit_sha = publication.get("commit_sha")
    if not commit_sha or git.head() != commit_sha:
        raise ReviewError("session_commit_mismatch", "Local HEAD is not the recorded session commit")
    if git.changed_paths():
        raise ReviewError("working_tree_changed_during_publish", "Working tree changed after the session commit")
    committed_paths = git.commit_changed_paths(session["expected_head"], commit_sha)
    if committed_paths != plan["commit"]["paths"]:
        raise ReviewError(
            "session_commit_path_mismatch",
            "Session commit paths differ from the approved allowlist",
            {"committed_paths": committed_paths},
        )
    if git.path_fingerprints(plan["commit"]["paths"]) != plan["path_fingerprints"]:
        raise ReviewError("session_commit_content_mismatch", "Session commit content differs from the approved plan")


def publish_or_resume(
    cwd: Path,
    runner: Runner,
    session_id: str,
    *,
    approved_digest: str | None,
    allow_failed_tests: bool,
    retry: bool,
    commit_message: str | None = None,
) -> dict[str, Any]:
    git, github, _repo = repository_context(cwd, runner)
    store = SessionStore(git.git_dir())
    with store.lock():
        session = store.load(session_id)
        ensure_same_repo(session, git)
        publication = session["publication"]
        if publication["status"] == "complete":
            return {"ok": True, "operation": "retry-publish" if retry else "publish", "idempotent": True, "session": session}
        if publication["status"] == "not_started":
            if retry:
                raise ReviewError("publication_not_started", "Run publish with an explicitly approved plan digest first")
            if not approved_digest:
                raise ReviewError("approval_digest_required", "The exact approved plan digest is required")
            # Rebuild from live state at the last possible moment. This is the approval gate.
            if commit_message is None:
                raise ReviewError("internal_error", "Commit message was not supplied")
            plan = build_plan(session, git, github, commit_message)
            if plan["digest"] != approved_digest:
                raise ReviewError(
                    "plan_digest_mismatch",
                    "Publication plan changed or the supplied digest is not current",
                    {"expected": plan["digest"], "supplied": approved_digest},
                )
            if plan["tests"]["override_required"] and not allow_failed_tests:
                raise ReviewError("tests_block_publication", "Failed or unavailable tests require an explicit override", plan["tests"]["blockers"])
            publication.update(
                {
                    "status": "approved",
                    "plan": plan,
                    "digest": approved_digest,
                    "approved_at": utc_now(),
                    "allow_failed_tests": bool(allow_failed_tests),
                    "last_error": None,
                }
            )
            store.save(session)
        else:
            if not retry and approved_digest and approved_digest != publication.get("digest"):
                raise ReviewError("plan_digest_mismatch", "A different plan is already being published")
            plan = publication.get("plan")
            if not plan or digest({key: value for key, value in plan.items() if key != "digest"}) != plan.get("digest"):
                raise ReviewError("stored_plan_corrupt", "Stored approved plan failed its digest check")
            if allow_failed_tests and not publication.get("allow_failed_tests"):
                raise ReviewError("override_not_approved", "A failed-test override was not part of the original publish command")

        try:
            _resume_publication(session, git, github, store)
        except ReviewError as error:
            save_publication_error(store, session, error)
            raise
        publication = session["publication"]
        unresolved = [item for item in session["thread_order"] if not session["threads"][item].get("resolved")]
        if unresolved:
            error = ReviewError("publication_incomplete", "Some threads were not published successfully", {"threads": unresolved})
            save_publication_error(store, session, error)
            raise error
        publication["status"] = "complete"
        publication["completed_at"] = utc_now()
        publication["last_error"] = None
        store.save(session)
        return {"ok": True, "operation": "retry-publish" if retry else "publish", "idempotent": False, "session": session}


def _resume_publication(session: dict[str, Any], git: Git, github: GitHub, store: SessionStore) -> None:
    publication = session["publication"]
    plan = publication["plan"]
    has_commit = plan["commit"]["required"]

    if has_commit and not publication.get("commit_sha"):
        if maybe_adopt_commit(session, git):
            store.save(session)
        else:
            # Revalidate local content against the approved plan before the commit.
            if git.changed_paths() != plan["commit"]["paths"] or git.path_fingerprints(plan["commit"]["paths"]) != plan["path_fingerprints"]:
                raise ReviewError("approved_content_changed", "Recorded file content changed after approval")
            publication["status"] = "commit_pending"
            store.save(session)
            git.run(["add", "--", *plan["commit"]["paths"]])
            staged_raw = git.run(["diff", "--cached", "--name-only", "--no-renames", "-z"]).stdout
            staged = sorted(filter(None, staged_raw.split("\0")))
            if staged != plan["commit"]["paths"]:
                raise ReviewError("staging_mismatch", "Staged paths differ from the approved allowlist", {"staged": staged})
            message, trailer = plan["commit"]["message"].rsplit("\n\n", 1)
            git.run(["commit", "-m", message, "-m", trailer, "--", *plan["commit"]["paths"]])
            publication["commit_sha"] = git.head()
            publication["status"] = "committed"
            verify_session_commit(session, git)
            store.save(session)

    if has_commit:
        commit_sha = publication["commit_sha"]
        verify_session_commit(session, git)
        fresh_pr = github.pr(str(session["pr"]["number"]))
        if fresh_pr["head_oid"] == commit_sha:
            session["latest_observed_head"] = commit_sha
            publication["pushed"] = True
            publication["status"] = "pushed"
            store.save(session)
        elif fresh_pr["head_oid"] == session["expected_head"]:
            git.run(["push", "--", plan["target"]["push_remote"], f"{commit_sha}:refs/heads/{plan['target']['branch']}"])
            publication["push_attempted_at"] = utc_now()
            store.save(session)
            verified = github.pr(str(session["pr"]["number"]))
            if verified["head_oid"] != commit_sha:
                raise ReviewError(
                    "push_verification_failed",
                    "Push returned successfully but GitHub does not show the session commit",
                    {"expected": commit_sha, "actual": verified["head_oid"]},
                )
            session["latest_observed_head"] = commit_sha
            publication["pushed"] = True
            publication["status"] = "pushed"
            store.save(session)
        else:
            raise ReviewError("pr_head_drift", "Remote PR head changed during publication")
    else:
        fresh_pr = github.pr(str(session["pr"]["number"]))
        session["latest_observed_head"] = fresh_pr["head_oid"]
        if fresh_pr["head_oid"] != session["expected_head"]:
            raise ReviewError("pr_head_drift", "Remote PR head changed before no-change replies")
        publication["status"] = "replying"
        store.save(session)

    # Any new local change invalidates still-pending replies, even after a verified push.
    if git.changed_paths():
        raise ReviewError("working_tree_changed_during_publish", "Working tree changed during publication; stop for reanalysis")

    by_id = verify_threads_for_resume(session, github)
    failures: list[dict[str, Any]] = []
    for thread_id in session["thread_order"]:
        entry = session["threads"][thread_id]
        if entry.get("resolved"):
            continue
        if git.changed_paths():
            raise ReviewError("working_tree_changed_during_publish", "Working tree changed during publication; stop for reanalysis")
        expected_remote = publication.get("commit_sha") if has_commit else session["expected_head"]
        current_pr = github.pr(str(session["pr"]["number"]))
        session["latest_observed_head"] = current_pr["head_oid"]
        if current_pr["head_oid"] != expected_remote:
            raise ReviewError("pr_head_drift", "Remote PR head changed during thread publication")
        by_id = verify_threads_for_resume(session, github)
        expected_marker = marker(session["session_id"], thread_id)
        current = by_id.get(thread_id)
        if current is None:
            entry["resolved"] = True
            entry["state"] = "resolved"
            entry["error"] = None
            entry["resolution_recovered"] = True
            store.save(session)
            continue
        existing = find_marker_comment(current, expected_marker)
        if existing and not entry.get("reply_result"):
            entry["reply_result"] = {
                "node_id": existing["node_id"],
                "database_id": existing.get("database_id"),
                "url": existing.get("url"),
                "recovered": True,
            }
            entry["state"] = "reply_posted"
            store.save(session)
        if not entry.get("reply_result"):
            try:
                body = render_reply(next(item["body_preview"] for item in plan["replies"] if item["thread_id"] == thread_id), publication.get("commit_sha"))
                entry["reply_result"] = github.reply(thread_id, body)
                entry["state"] = "reply_posted"
                entry["error"] = None
                store.save(session)
            except ReviewError as error:
                entry["state"] = "failed"
                entry["error"] = error.as_json()
                store.save(session)
                failures.append({"thread_id": thread_id, "operation": "reply", "error": error.as_json()})
                continue
        # A prior resolve may have succeeded just before interruption. Query first.
        # Drift and fetch failures are batch-fatal; only the resolve mutation itself
        # is isolated so other already-approved threads may continue.
        if git.changed_paths():
            raise ReviewError("working_tree_changed_during_publish", "Working tree changed before thread resolution")
        resolve_pr = github.pr(str(session["pr"]["number"]))
        session["latest_observed_head"] = resolve_pr["head_oid"]
        if resolve_pr["head_oid"] != expected_remote:
            raise ReviewError("pr_head_drift", "Remote PR head changed before thread resolution")
        refreshed = github.threads(session["repository"]["name_with_owner"], session["pr"]["number"])
        still_open = {item["thread_id"]: item for item in refreshed}
        if thread_id in still_open:
            if has_commit:
                unchanged = thread_comments_fingerprint(
                    still_open[thread_id], ignore_marker=expected_marker
                ) == entry["comments_fingerprint"]
            else:
                unchanged = thread_fingerprint(
                    still_open[thread_id], ignore_marker=expected_marker
                ) == entry["fingerprint"]
            if not unchanged:
                raise ReviewError("thread_drift", f"Thread {thread_id} changed before resolution")
        try:
            if thread_id in still_open:
                github.resolve(thread_id)
            entry["resolved"] = True
            entry["state"] = "resolved"
            entry["error"] = None
            store.save(session)
        except ReviewError as error:
            entry["state"] = "failed"
            entry["error"] = error.as_json()
            store.save(session)
            failures.append({"thread_id": thread_id, "operation": "resolve", "error": error.as_json()})
    if failures:
        raise ReviewError("thread_publication_failed", "One or more thread operations failed", failures)


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ReviewError("invalid_arguments", message)


def parser() -> argparse.ArgumentParser:
    result = JsonArgumentParser(prog="reviewctl", description=__doc__)
    result.add_argument("--repo", type=Path, default=Path.cwd(), help="repository working directory")
    result.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    commands = result.add_subparsers(dest="command", required=True)

    intake = commands.add_parser("task-intake")
    intake.add_argument("--pr-url")
    intake.add_argument("--workspace-root", type=Path)
    intake.add_argument("--config-file", type=Path)
    intake.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    checkpoint = commands.add_parser("checkpoint")
    checkpoint.add_argument("--kind", required=True, choices=CHECKPOINT_KINDS)
    checkpoint.add_argument("--prompt-file", required=True, type=Path)
    checkpoint.add_argument("--choices-file", type=Path)
    checkpoint.add_argument("--context-file", type=Path)
    checkpoint.add_argument("--route", action="append", choices=CHECKPOINT_ROUTES, default=[])
    checkpoint.add_argument("--session")
    checkpoint.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    handoff_create = commands.add_parser("handoff-create")
    handoff_create.add_argument("--intake-file", required=True, type=Path)
    handoff_create.add_argument("--title")
    handoff_create.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    handoff_validate = commands.add_parser("handoff-validate")
    handoff_validate.add_argument("--handoff-file", required=True, type=Path)
    handoff_validate.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    inspect_parser = commands.add_parser("inspect")
    inspect_parser.add_argument("--pr")
    inspect_parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    start = commands.add_parser("session-start")
    start.add_argument("--pr")
    start.add_argument("--push-remote")
    start.add_argument("--session-id", help=argparse.SUPPRESS)
    start.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    show = commands.add_parser("session-show")
    show.add_argument("--session", required=True)
    show.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    relocate = commands.add_parser("session-relocate")
    relocate.add_argument("--session", required=True)
    relocate.add_argument("--previous-root", required=True, type=Path)
    relocate.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    record = commands.add_parser("record")
    record.add_argument("--session", required=True)
    record.add_argument("--thread", required=True)
    record.add_argument("--decision", required=True, choices=("change", "no-change"))
    record.add_argument("--reply-file", required=True, type=Path)
    record.add_argument("--paths-file", type=Path)
    record.add_argument("--tests-file", type=Path)
    record.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    update = commands.add_parser("update-tests")
    update.add_argument("--session", required=True)
    update.add_argument("--thread", required=True)
    update.add_argument("--tests-file", required=True, type=Path)
    update.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    prepare = commands.add_parser("prepare-publish")
    prepare.add_argument("--session", required=True)
    prepare.add_argument("--commit-message", required=True)
    prepare.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    publish = commands.add_parser("publish")
    publish.add_argument("--session", required=True)
    publish.add_argument("--plan-digest", required=True)
    publish.add_argument("--commit-message", required=True)
    publish.add_argument("--allow-failed-tests", action="store_true")
    publish.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    retry = commands.add_parser("retry-publish")
    retry.add_argument("--session", required=True)
    retry.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    return result


def dispatch(args: argparse.Namespace, runner: Runner) -> dict[str, Any]:
    cwd = args.repo.resolve()
    if args.command == "task-intake":
        if args.config_file:
            if args.pr_url or args.workspace_root:
                raise ReviewError(
                    "invalid_arguments",
                    "--config-file cannot be combined with --pr-url or --workspace-root",
                )
            return task_intake_from_file(args.config_file)
        return task_intake_options(args.pr_url, args.workspace_root)
    if args.command == "checkpoint":
        return create_checkpoint(
            args.kind,
            args.prompt_file,
            args.choices_file,
            args.context_file,
            args.route,
            args.session,
        )
    if args.command == "handoff-create":
        return create_handoff(args.intake_file, args.title)
    if args.command == "handoff-validate":
        return validate_handoff(args.handoff_file)
    if args.command == "inspect":
        return inspect(cwd, runner, args.pr)
    if args.command == "session-start":
        return create_session(cwd, runner, args.pr, args.push_remote, args.session_id)
    if args.command == "session-show":
        return read_session(cwd, runner, args.session)
    if args.command == "session-relocate":
        return relocate_session(cwd, runner, args.session, args.previous_root)
    if args.command == "record":
        return record_decision(cwd, runner, args.session, args.thread, args.decision, args.reply_file, args.paths_file, args.tests_file)
    if args.command == "update-tests":
        return update_tests(cwd, runner, args.session, args.thread, args.tests_file)
    if args.command == "prepare-publish":
        return prepare_publish(cwd, runner, args.session, args.commit_message)
    if args.command == "publish":
        return publish_or_resume(
            cwd,
            runner,
            args.session,
            approved_digest=args.plan_digest,
            allow_failed_tests=args.allow_failed_tests,
            retry=False,
            commit_message=args.commit_message,
        )
    if args.command == "retry-publish":
        return publish_or_resume(cwd, runner, args.session, approved_digest=None, allow_failed_tests=False, retry=True)
    raise ReviewError("unknown_command", "Unknown command")


def main(argv: Sequence[str] | None = None, runner: Runner | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        output = dispatch(args, runner or Runner())
        print(json.dumps(output, sort_keys=True, ensure_ascii=False))
        return 0
    except ReviewError as error:
        print(json.dumps({"ok": False, "error": error.as_json()}, sort_keys=True, ensure_ascii=False))
        return 1
    except (OSError, json.JSONDecodeError) as error:
        safe = ReviewError("local_io_error", str(error))
        print(json.dumps({"ok": False, "error": safe.as_json()}, sort_keys=True, ensure_ascii=False))
        return 1
    except Exception as error:  # Keep automation output machine-readable for unexpected failures.
        safe = ReviewError("internal_error", f"{type(error).__name__}: {error}")
        print(json.dumps({"ok": False, "error": safe.as_json()}, sort_keys=True, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
