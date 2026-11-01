"""Generic CLI transport extracted from the MIT embedded implementation.

No workflow policy, sibling skill lookup, review brief, target recipe, or intake.
Profile support requires fresh host evidence; this package asserts no live support.
"""

import argparse


import fcntl


import hashlib


import json


import math


import os


import re


import shutil


import signal


import stat


import subprocess


import sys


import tempfile


import time


import uuid


from dataclasses import dataclass


from pathlib import Path


from typing import Mapping, Optional


DEPTH_VAR = "CROSS_REVIEW_DEPTH"


REVIEWERS = {
    "codex": {"cli": "codex", "model": "gpt-6.1-sol", "effort": "low",
              "env_prefixes": ("CODEX_", "OPENAI_"), "priced": True},
    "claude-code": {"cli": "claude", "model": "claude-opus-5-5", "effort": "medium",
                    "env_prefixes": ("ANTHROPIC_", "CLAUDE_CONFIG_DIR",
                                     "CLAUDE_CODE_USE_", "CLAUDE_CODE_OAUTH_TOKEN"),
                    "priced": True},
    "antigravity": {"cli": "agy", "model": "gemini-4-argon", "effort": "medium",
                    "env_prefixes": ("AGY_", "GEMINI_API_KEY"), "priced": False},
}


EFFORTS = ("low", "medium", "high", "xhigh", "max")


ENV_ALLOWLIST = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TERM", "LANG", "TZ",
    "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
    "XDG_RUNTIME_DIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy",
    "https_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
    "__CF_USER_TEXT_ENCODING",
)


_ENV_NAME_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


EXIT_INVALID_INPUT = 2


EXIT_NESTED = 20


EXIT_NO_SESSION = 24


USAGE_FILE = "usage.json"


USAGE_SCHEMA_VERSION = 1


PRICES_AS_OF = "2026-10-10"


PRICES = {
    "gpt-6.1-sol": (2.00, 0.10, 10.00),
    "gpt-6-sol": (2.00, 0.20, 10.00),
    "gpt-6-astra": (10.00, 1.00, 50.00),
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-5.6-sol": (4.00, 0.40, 20.00),
    "gpt-5.6-terra": (2.00, 0.20, 12.00),
    "gpt-5.6-luna": (0.20, 0.02, 1.20),
    "gpt-5.5": (5.00, 0.50, 30.00),
    "gpt-5.3-codex": (1.75, 0.175, 14.00),
}


CLAUDE_PRICES = {
    "claude-fable-5-1": (10.00, 12.50, 0.25, 50.00),
    "claude-opus-5-5": (4.00, 5.00, 0.20, 20.00),
    "claude-sonnet-5-5": (2.00, 2.50, 0.10, 10.00),
    "claude-haiku-5-5": (0.10, 0.125, 0.01, 0.50),
}


TOKEN_KEYS = ("input", "cached_input", "cache_write", "output", "reasoning")


_CODEX_USAGE_FIELDS = (
    ("input", "input_tokens"),
    ("cached_input", "cached_input_tokens"),
    ("cache_write", "cache_write_input_tokens"),
    ("output", "output_tokens"),
    ("reasoning", "reasoning_output_tokens"),
)


_SKILL_PATH_RE = re.compile(r"([A-Za-z0-9._-]+)/SKILL\.md\b")


_CLAUDE_POLICY = (
    "--permission-mode", "manual", "--permission-prompts", "none",
    "--tools", "Read,Grep,Glob",
    "--allowedTools", "Read,Grep,Glob",
    "--disallowedTools", "Write,Edit,NotebookEdit,Bash,Agent,Skill,mcp__*",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--safe-mode",
    "--restricted",
    "--setting-sources", "",
)


_AGY_POLICY = ("--disable-slash-commands", "--print-timeout", "0")


_CODEX_RUN_SANDBOX = ("-s", "read-only")


_CODEX_RESUME_SANDBOX = ("-c", 'sandbox_mode="read-only"')


_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


_UUID_RE = re.compile(r"\A%s\Z" % _UUID)


class CrossReviewError(Exception):
    """Error that maps to a specific process exit code."""

    def __init__(self, code: int, message: str, run_dir: Optional[Path] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        # Set once a run directory exists, so callers can still find partial artifacts.
        self.run_dir = run_dir


@dataclass(frozen=True)
class VerifiedProfilePolicy:
    """Trusted host evidence, never parsed from child output or assignment text.

    No production policy is shipped: fresh/resume evidence remains pending until
    live probes. A host may construct this only after verifying bounded actions,
    config/plugin isolation and denial of raw CLI/SDK/MCP delegation routes. The
    adapter is cooperative containment, not an OS boundary against malicious code.
    Only local workspace writes are represented; outward actions are unavailable.
    """
    family: str
    repo: str
    fresh_evidence: str
    resume_evidence: str
    local_actions: tuple
    delegation_blocked: bool
    config_isolated: bool


@dataclass(frozen=True)
class ProfileContext:
    """Host-owned dispatch identity carried to the child environment.

    task_id and parent_id identify host records, not CLI session UUIDs. policy is
    required only for execution; no assignment text can upgrade this context.
    """
    task_id: str
    parent_id: str
    role: str
    profile: str
    policy: Optional[VerifiedProfilePolicy] = None


def _profile_policy(reviewer, repo, profile, context, resume=False):
    if profile not in ("read-only", "execution"):
        raise CrossReviewError(EXIT_INVALID_INPUT, "UNAVAILABLE: unsupported profile")
    if context is not None:
        if not isinstance(context, ProfileContext):
            raise CrossReviewError(EXIT_INVALID_INPUT, "invalid trusted profile context")
        for name in ("task_id", "parent_id"):
            _require_text(name, getattr(context, name))
        roles = ("consultation", "review") if profile == "read-only" else ("execution",)
        if context.profile != profile or context.role not in roles:
            raise CrossReviewError(EXIT_INVALID_INPUT, "role/profile mismatch")
    if profile == "read-only":
        return
    policy = context.policy if context else None
    if (reviewer != "codex" or not isinstance(policy, VerifiedProfilePolicy)
            or policy.family != reviewer or policy.repo != str(Path(repo).resolve())
            or not isinstance(policy.local_actions, tuple)
            or not policy.local_actions
            or any(action not in ("read", "write-workspace") for action in policy.local_actions)
            or "write-workspace" not in policy.local_actions
            or policy.delegation_blocked is not True or policy.config_isolated is not True
            or not isinstance(policy.fresh_evidence, str) or not policy.fresh_evidence.strip()
            or (resume and (not isinstance(policy.resume_evidence, str)
                            or not policy.resume_evidence.strip()))):
        raise CrossReviewError(EXIT_INVALID_INPUT,
                               "UNAVAILABLE: execution needs verified scoped host policy")


def validate_actual_identity(model, effort, session_id, observed):
    """Validate provider evidence; a requested resume needs complete identity.

    observed uses actual_model, actual_effort and cli_session_id. Self-report and
    requested argv are not provider evidence. A non-None session_id pins a resume:
    every observed field must be present, known, and match its requested value.
    Fresh runs may report unknown fields, which cannot establish resumable identity.
    Dispatcher must call this before accepting completion or persisting evidence.
    """
    if not isinstance(observed, Mapping):
        raise CrossReviewError(EXIT_NO_SESSION, "invalid provider identity evidence")
    result = {}
    for key, expected in (("actual_model", model), ("actual_effort", effort),
                          ("cli_session_id", session_id)):
        value = observed.get(key)
        if value is None or value == "unknown":
            if session_id is not None:
                raise CrossReviewError(EXIT_NO_SESSION,
                                       "incomplete resume identity: %s" % key)
            result[key] = "unknown"
        elif not isinstance(value, str) or not value.strip():
            raise CrossReviewError(EXIT_NO_SESSION, "invalid provider %s" % key)
        elif expected is not None and value != expected:
            raise CrossReviewError(EXIT_NO_SESSION, "%s mismatch" % key)
        else:
            result[key] = value
    return result


def ensure_not_nested(env: Mapping[str, str]) -> None:
    """Refuse to run inside a reviewer process; an empty value still counts."""
    if DEPTH_VAR in env or "CROSS_AGENT_DELEGATED" in env:
        raise CrossReviewError(
            EXIT_NESTED,
            "You are already the cross-reviewer; do the review yourself "
            "instead of delegating it again.",
        )


def validate_pass_env(names) -> list:
    """Return the --pass-env names after checking each is a plain variable name."""
    names = list(names or [])
    for name in names:
        if not isinstance(name, str) or not _ENV_NAME_RE.match(name):
            raise CrossReviewError(EXIT_INVALID_INPUT,
                                   "--pass-env needs a variable name, got %r" % (name,))
    return names


def reviewer_environment(env: Mapping[str, str], reviewer: str = "codex",
                         pass_env=(), context=None) -> dict:
    """Return the reviewer child's environment: the allowlist only, depth marker set.

    Kept: ENV_ALLOWLIST, LC_* locale variables, variables starting with the agent's
    env_prefixes, and the explicit pass_env names. Everything else in the host
    environment, including unrelated secrets, stays out of the reviewer process.
    """
    prefixes = REVIEWERS[reviewer]["env_prefixes"]
    extra = set(validate_pass_env(pass_env))
    child = {key: value for key, value in env.items()
             if key in ENV_ALLOWLIST or key in extra or key.startswith("LC_")
             or key.startswith(prefixes)}
    child[DEPTH_VAR] = env.get(DEPTH_VAR, "1")
    child["CROSS_AGENT_DELEGATED"] = "1"
    if context is not None:
        _profile_policy(reviewer, context.policy.repo if context.policy else Path.cwd(),
                        context.profile, context)
        child.update({"CROSS_AGENT_TASK_ID": context.task_id,
                      "CROSS_AGENT_PARENT_ID": context.parent_id,
                      "CROSS_AGENT_ROLE": context.role,
                      "CROSS_AGENT_PROFILE": context.profile})
    return child


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise CrossReviewError(EXIT_INVALID_INPUT, "%s must be a non-empty string" % name)


def _require_agent(reviewer: str) -> None:
    if reviewer not in REVIEWERS:
        raise CrossReviewError(EXIT_INVALID_INPUT,
                               "agent must be one of: %s" % ", ".join(sorted(REVIEWERS)))


def _validate_common(reviewer: str, model: str, effort: str) -> None:
    _require_agent(reviewer)
    _require_text("model", model)
    _require_text("effort", effort)
    # The fixed set also keeps effort safe inside the Codex -c TOML string.
    if effort not in EFFORTS:
        raise CrossReviewError(EXIT_INVALID_INPUT,
                               "effort must be one of: %s" % ", ".join(EFFORTS))
    # An option-like value would be read by the CLI as a flag.
    if model.startswith("-"):
        raise CrossReviewError(EXIT_INVALID_INPUT, "model looks like a command-line option")


def _is_uuid(value) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.match(value))


_GIT_REDIRECT_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR",
                      "GIT_CEILING_DIRECTORIES", "GIT_INDEX_FILE")


def _git_line(repo: Path, *args: str) -> Optional[str]:
    """Return the first stdout line of a git command in repo, or None on any failure."""
    env = {k: v for k, v in os.environ.items() if k not in _GIT_REDIRECT_VARS}
    try:
        proc = subprocess.run(["git", "-C", str(repo)] + list(args),
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, env=env, check=False)
    except OSError:
        return None
    lines = os.fsdecode(proc.stdout).splitlines()
    if proc.returncode != 0 or not lines or not lines[0].strip():
        return None
    return lines[0]


def _on_disk_path(path: Path) -> Optional[str]:
    """Return the path as the file system spells it (case on macOS), if available."""
    if not hasattr(fcntl, "F_GETPATH"):
        return None
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
    except OSError:
        return None
    finally:
        os.close(fd)
    return os.fsdecode(raw.split(b"\0", 1)[0]) or None


def _find_dot_git(repo: Path) -> Optional[Path]:
    for directory in [repo] + list(repo.parents):
        candidate = directory / ".git"
        if os.path.lexists(candidate):
            return candidate
    return None


def _parse_dot_git(dot_git: Path):
    """Return (toplevel, common_dir) from a .git entry without running git, else None."""
    top = dot_git.parent
    if dot_git.is_dir():
        return top, dot_git.resolve()
    try:
        text = dot_git.read_text("utf-8").strip()
    except (OSError, ValueError):
        return None
    if not text.startswith("gitdir:"):
        return None
    git_dir = (top / text[len("gitdir:"):].strip()).resolve()
    common = git_dir
    try:
        pointer = (git_dir / "commondir").read_text("utf-8").strip()
    except FileNotFoundError:
        pointer = ""
    except (OSError, ValueError):
        return None
    if pointer:
        common = (git_dir / pointer).resolve()
    return top, common


def codex_untrusted_paths(repo: Path) -> list:
    """Paths whose Codex project config must not load.

    A trusted path, or a worktree of a trusted repository, would otherwise load
    .codex/config.toml from the reviewed repository. The set holds the resolved
    repo and its on-disk spelling, the worktree root, the Git common directory's
    parent (the main worktree root for a normal repository), and the common
    directory itself when it is not named .git (bare or separate layouts). Paths
    come from git and from parsing .git directly; a .git entry that yields no
    path at all fails closed.
    """
    repo = Path(repo).resolve()
    paths = [str(repo)]
    on_disk = _on_disk_path(repo)
    if on_disk:
        paths.append(on_disk)

    found = []
    top = _git_line(repo, "rev-parse", "--show-toplevel")
    common = _git_line(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common is None:
        # Git before 2.31 has no --path-format; a relative answer is relative to repo.
        common = _git_line(repo, "rev-parse", "--git-common-dir")
        common = str(repo / common) if common else None
    if top:
        found.append((Path(top).resolve(), None))
    if common:
        found.append((None, Path(common).resolve()))

    dot_git = _find_dot_git(repo)
    parsed = _parse_dot_git(dot_git) if dot_git is not None else None
    if parsed:
        found.append(parsed)
    if dot_git is not None and not found:
        raise CrossReviewError(
            EXIT_INVALID_INPUT,
            "cannot determine the Git layout of %s (%s is unreadable); refusing to run "
            "the Codex reviewer without marking it untrusted" % (repo, dot_git))

    for top_path, common_path in found:
        if top_path is not None:
            paths.append(str(top_path))
        if common_path is not None:
            paths.append(str(common_path.parent))
            if common_path.name != ".git":
                paths.append(str(common_path))
    return list(dict.fromkeys(paths))


def _codex_untrusted_override(paths) -> str:
    """Build the -c value that marks every path as an untrusted Codex project."""
    entries = []
    for path in paths:
        if any(ord(ch) < 0x20 or ch == "\x7f" or 0xD800 <= ord(ch) <= 0xDFFF
               for ch in path):
            raise CrossReviewError(EXIT_INVALID_INPUT,
                                   "repository path contains control characters "
                                   "or undecodable bytes")
        # json.dumps yields a valid TOML basic string for a path without control
        # characters; ensure_ascii=False keeps non-BMP characters out of surrogate escapes.
        entries.append('%s={trust_level="untrusted"}' % json.dumps(path, ensure_ascii=False))
    if not entries:
        raise CrossReviewError(EXIT_INVALID_INPUT, "no repository path to mark untrusted")
    return "projects={%s}" % ", ".join(entries)


def identity_diff(previous, current):
    return {key: {"previous": previous.get(key), "current": current.get(key)}
            for key in sorted(set(previous) | set(current)) if previous.get(key) != current.get(key)}


def build_run_command(reviewer: str, repo: Path, run_dir: Path,
                      model: str, effort: str, session_id: str,
                      untrusted=None, *, profile="read-only", context=None) -> list:
    """Build the argv for a fresh review run.

    Codex reads the brief from stdin ("-") and writes the final message to
    run_dir/review.md. Claude reads the brief from stdin, runs with cwd=repo,
    and uses the runner-generated session_id. Antigravity reads the brief from stdin,
    runs with cwd=repo, and reports its own conversation id, so session_id is unused.
    untrusted lists the paths forced to an untrusted Codex project; None computes them
    from repo.
    """
    _validate_common(reviewer, model, effort)
    _profile_policy(reviewer, repo, profile, context, resume=False)
    if not Path(repo).is_dir():
        raise CrossReviewError(EXIT_INVALID_INPUT, "repo is not a directory: %s" % repo)

    if reviewer == "antigravity":
        return _agy_command(model, effort)
    if reviewer == "codex":
        if untrusted is None:
            untrusted = codex_untrusted_paths(repo)
        return (["codex", "exec", "--json"] + (list(_CODEX_RUN_SANDBOX) if profile == "read-only" else ["-s", "workspace-write"])
                + ["-m", model, "-c", 'model_reasoning_effort="%s"' % effort,
                   "-c", _codex_untrusted_override(untrusted),
                   "-C", str(repo), "-o", str(Path(run_dir) / "review.md"), "-"])

    if not _is_uuid(session_id):
        raise CrossReviewError(EXIT_INVALID_INPUT, "session_id must be a UUID")
    return (["claude", "-p", "--output-format", "json", "--model", model, "--effort", effort]
            + list(_CLAUDE_POLICY)
            + ["--session-id", session_id])


def build_resume_command(reviewer: str, repo: Path, run_dir: Path,
                         model: str, effort: str, session_id: str,
                         untrusted=None, *, profile="read-only", context=None) -> list:
    """Build the argv for a follow-up in an existing reviewer session.

    The caller runs it with cwd=repo. Codex resume has no -s/-C flags and would
    inherit a permissive sandbox, so read-only is forced through -c sandbox_mode.
    Never uses --last: the session is always the explicit UUID. untrusted works as
    in build_run_command.
    """
    _validate_common(reviewer, model, effort)
    _profile_policy(reviewer, repo, profile, context, resume=True)
    if not _is_uuid(session_id):
        raise CrossReviewError(EXIT_NO_SESSION, "session id is missing or not a UUID")

    if reviewer == "antigravity":
        return _agy_command(model, effort) + ["--conversation", session_id]
    if reviewer == "codex":
        if untrusted is None:
            untrusted = codex_untrusted_paths(repo)
        return (["codex", "exec", "resume", "--json", "-m", model,
                 "-c", 'model_reasoning_effort="%s"' % effort]
                + (list(_CODEX_RESUME_SANDBOX) if profile == "read-only" else ["-c", 'sandbox_mode="workspace-write"'])
                + ["-c", _codex_untrusted_override(untrusted)]
                + ["-o", str(Path(run_dir) / "review.md"), session_id, "-"])

    return (["claude", "-p", "--output-format", "json", "--model", model, "--effort", effort]
            + list(_CLAUDE_POLICY)
            + ["--resume", session_id])


def _agy_command(model: str, effort: str) -> list:
    # No -p: it takes the next token as the prompt, and the brief comes on stdin.
    return (["agy", "--output-format", "json", "--model", model, "--effort", effort]
            + list(_AGY_POLICY))


def _count(mapping, key) -> int:
    """Read a count already checked by _valid_counts; absent or null means 0."""
    value = mapping.get(key)
    return 0 if value is None else value


def _valid_counts(mapping, required, optional) -> bool:
    """True when every required count is present and every present count is valid.

    A valid count is a non-negative int. An optional count may be absent or null,
    but a present malformed value makes the whole record unknown instead of 0.
    """
    def ok(value):
        return type(value) is int and value >= 0

    return (isinstance(mapping, dict)
            and all(ok(mapping.get(key)) for key in required)
            and all(mapping.get(key) is None or ok(mapping[key]) for key in optional))


def _zero_tokens() -> dict:
    return {key: 0 for key in TOKEN_KEYS}


def parse_codex_events(log: str) -> dict:
    """Read the codex --json event stream mixed into run.log.

    Lines that are not JSON objects (stderr diagnostics, the runner header) are
    skipped. Only the first thread.started sets the session, so an id that shows up
    later cannot replace it; message text is JSON-escaped inside one line and never
    forms an event. Skills are the directory names of SKILL.md files that a shell
    command touched. tokens is None when no turn.completed carried usage, or when
    one of them lacked valid input/output counts or held a malformed optional count;
    usage_error then says why.
    """
    session, seen_thread, tokens, turns, skills = None, False, _zero_tokens(), 0, set()
    usage_error = None
    for line in (log or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "thread.started" and not seen_thread:
            seen_thread = True
            thread = event.get("thread_id")
            session = thread if _is_uuid(thread) else None
        elif kind == "turn.completed":
            usage = event.get("usage")
            # input and output are required; cache and reasoning counts may be absent.
            if not _valid_counts(usage, ("input_tokens", "output_tokens"),
                                 ("cached_input_tokens", "cache_write_input_tokens",
                                  "reasoning_output_tokens")):
                usage_error = "turn.completed with missing or invalid token counts"
                continue
            turns += 1
            for key, field in _CODEX_USAGE_FIELDS:
                tokens[key] += _count(usage, field)
        elif kind == "item.completed" and isinstance(event.get("item"), dict):
            item = event["item"]
            if item.get("type") == "command_execution":
                skills.update(_SKILL_PATH_RE.findall(str(item.get("command", ""))))
    return {"session": session,
            "tokens": tokens if turns and usage_error is None else None,
            "skills": sorted(skills), "usage_error": usage_error}


def codex_cost(model: str, tokens: Mapping[str, int]) -> Optional[float]:
    """Approximate USD for a Codex run from PRICES; None for a model not in the table.

    input already includes cached tokens and output already includes reasoning
    tokens, so neither subset is added again.
    """
    price = PRICES.get(model)
    if price is None:
        return None
    input_price, cached_price, output_price = price
    uncached = max(tokens["input"] - tokens["cached_input"], 0)
    total = (uncached * input_price + tokens["cached_input"] * cached_price
             + tokens["output"] * output_price) / 1_000_000
    return round(total, 4)


def claude_cost(model: str, tokens: Mapping[str, int]) -> Optional[float]:
    """Approximate USD for a Claude run from CLAUDE_PRICES; None for an unknown model.

    input already includes cache hits and cache writes, so they are billed at their
    own prices and subtracted from the base-price input.
    """
    price = CLAUDE_PRICES.get(model)
    if price is None:
        return None
    input_price, write_price, hit_price, output_price = price
    base = max(tokens["input"] - tokens["cached_input"] - tokens["cache_write"], 0)
    total = (base * input_price + tokens["cache_write"] * write_price
             + tokens["cached_input"] * hit_price + tokens["output"] * output_price) / 1_000_000
    return round(total, 4)


def parse_claude_output(raw: str):
    """Split claude --output-format json into (review text, usage), or (None, None).

    usage is {"tokens": dict or None, "cost_usd": float or None, "session": str or
    None}; session is the reported session_id when it is a UUID. A failure while
    reading the metrics never costs the review text: usage then holds only Nones.
    """
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        return None, None
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), str):
        return None, None
    try:
        usage = _claude_usage(payload)
    except Exception:  # metrics must never fail a review
        usage = {"tokens": None, "cost_usd": None, "session": None}
    return payload["result"], usage


def _claude_usage(payload: dict) -> dict:
    """Sum every model in modelUsage; cost is the CLI-reported total_cost_usd at list price."""
    tokens = None
    models = payload.get("modelUsage")
    if isinstance(models, dict) and models:
        tokens = _zero_tokens()
        for entry in models.values():
            # inputTokens and outputTokens are required; cache and thinking may be absent.
            if not _valid_counts(entry, ("inputTokens", "outputTokens"),
                                 ("cacheReadInputTokens", "cacheCreationInputTokens",
                                  "thinkingTokens")):
                tokens = None
                break
            cached = _count(entry, "cacheReadInputTokens")
            written = _count(entry, "cacheCreationInputTokens")
            tokens["input"] += _count(entry, "inputTokens") + cached + written
            tokens["cached_input"] += cached
            tokens["cache_write"] += written
            tokens["output"] += _count(entry, "outputTokens")
            tokens["reasoning"] += _count(entry, "thinkingTokens")
    cost = payload.get("total_cost_usd")
    # The range check also rejects NaN and Infinity, which json.loads accepts.
    if type(cost) not in (int, float) or not 0 <= cost < float("inf"):
        cost = None
    session = payload.get("session_id")
    return {"tokens": tokens, "cost_usd": None if cost is None else round(cost, 4),
            "session": session if _is_uuid(session) else None}


_AGY_USAGE_FIELDS = (
    ("input", "input_tokens"),
    ("cached_input", "cache_read_tokens"),
    ("output", "output_tokens"),
    ("reasoning", "thinking_tokens"),
)


def parse_antigravity_output(raw: str):
    """Split agy --output-format json into (review text, info), or (None, None).

    info is {"tokens", "cost_usd", "session", "status", "error", "denied"}; cost_usd is
    always None because agy reports no cost, session is the conversation_id when it is
    a UUID, and denied lists the display names of the tool calls agy refused. text is
    None unless status is SUCCESS with a non-empty response: a denied tool call ends
    the turn with SUCCESS and an empty response. A failure while reading the metrics
    never costs the review text.
    """
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    response = payload.get("response")
    status = payload.get("status")
    text = (response if status == "SUCCESS" and isinstance(response, str)
            and response.strip() else None)
    info = {"tokens": None, "cost_usd": None, "session": None,
            "status": status if isinstance(status, str) else None,
            "error": None, "denied": []}
    try:
        usage = payload.get("usage")
        # input and output are required; cache and thinking may be absent. output
        # already includes thinking tokens: total_tokens is input plus output.
        if _valid_counts(usage, ("input_tokens", "output_tokens"),
                         ("cache_read_tokens", "thinking_tokens")):
            info["tokens"] = _zero_tokens()
            for key, field in _AGY_USAGE_FIELDS:
                info["tokens"][key] = _count(usage, field)
        session = payload.get("conversation_id")
        info["session"] = session if _is_uuid(session) else None
        if payload.get("error") is not None:
            info["error"] = str(payload["error"])
        denied = payload.get("denied_actions")
        if isinstance(denied, list):
            info["denied"] = [
                str(item.get("display_name") or item.get("action"))
                if isinstance(item, dict) else str(item) for item in denied]
    except Exception:  # metrics must never fail a review
        pass
    return text, info


def _valid_total(total) -> bool:
    if not isinstance(total, dict):
        return False
    tokens, cost = total.get("tokens"), total.get("cost_usd")
    return (isinstance(tokens, dict)
            and all(type(tokens.get(key)) is int and tokens[key] >= 0 for key in TOKEN_KEYS)
            # The range check also rejects NaN and Infinity, which json.loads accepts.
            and (cost is None or (type(cost) in (int, float) and 0 <= cost < float("inf")))
            and type(total.get("runs")) is int and total["runs"] >= 1
            and type(total.get("complete")) is bool)


def _prior_total(previous):
    """The session_total of a parsed previous usage.json when it is valid, else None."""
    prior = previous.get("session_total") if isinstance(previous, dict) else None
    return prior if _valid_total(prior) else None


def _known(tokens, cost_usd, priced: bool) -> bool:
    """True when a run's figures are complete; an unpriced reviewer needs tokens only."""
    return tokens is not None and (cost_usd is not None or not priced)


def resume_usage(tokens, cost_usd, previous, session, expected_session,
                 reviewer: str = "claude-code"):
    """Split cumulative resume figures into (tokens, cost_usd, reason) of this run.

    claude -p --resume and agy --conversation report usage for the whole session, so
    this run is the reported figures minus the previous session_total. The result is
    (None, None, reason) when that difference cannot be trusted; reason is None when
    the reported figures themselves are missing. An unpriced reviewer's cost stays None.
    """
    cli, priced = REVIEWERS[reviewer]["cli"], REVIEWERS[reviewer]["priced"]
    if session is not None and session != expected_session:
        return None, None, "%s resumed into a different session" % cli
    prior = _prior_total(previous)
    # An incomplete total may hold zero placeholders for unknown tokens.
    if prior is None or (priced and prior["cost_usd"] is None) or not prior["complete"]:
        return None, None, "previous session total unavailable"
    if not _known(tokens, cost_usd, priced):
        return None, None, None
    run_tokens = {key: tokens[key] - prior["tokens"][key] for key in TOKEN_KEYS}
    run_cost = cost_usd - prior["cost_usd"] if priced else None
    if (run_cost is not None and run_cost < 0) or any(value < 0 for value in run_tokens.values()):
        return None, None, "%s reported totals below the previous session total" % cli
    return run_tokens, None if run_cost is None else round(run_cost, 4), None


def _resume_total(tokens, cost_usd, previous, priced: bool) -> dict:
    """session_total of a claude or agy resume: the CLI-reported cumulative figures."""
    prior = _prior_total(previous)
    return {"tokens": dict(tokens) if tokens is not None else _zero_tokens(),
            "cost_usd": cost_usd,
            "runs": prior["runs"] + 1 if prior is not None else 1,
            "complete": _known(tokens, cost_usd, priced)}


def _session_total(tokens, cost_usd, previous_run, previous, priced: bool = True) -> dict:
    complete = _known(tokens, cost_usd, priced)
    total_tokens = dict(tokens) if tokens is not None else _zero_tokens()
    total_cost, runs = cost_usd, 1
    if previous_run is not None:
        prior = previous.get("session_total") if isinstance(previous, dict) else None
        if _valid_total(prior):
            for key in TOKEN_KEYS:
                total_tokens[key] += prior["tokens"][key]
            if prior["cost_usd"] is not None:
                total_cost = (total_cost or 0.0) + prior["cost_usd"]
            runs += prior["runs"]
            complete = complete and prior["complete"]
        else:
            complete = False
    return {"tokens": total_tokens,
            "cost_usd": None if total_cost is None else round(total_cost, 4),
            "runs": runs, "complete": complete}


def _target_total(cost_usd, rounds_previous, round_no) -> dict:
    """Cumulative cost of every run of the review target, rounds and resumes alike.

    rounds_previous is the parsed usage.json of the run this one continues (a
    followup round or a resume), or None for the first round of a target.
    """
    prior = rounds_previous.get("target_total") if isinstance(rounds_previous, dict) else None
    valid = (isinstance(prior, dict) and type(prior.get("runs")) is int and prior["runs"] >= 1
             and type(prior.get("complete")) is bool
             and (prior.get("cost_usd") is None
                  or (type(prior["cost_usd"]) in (int, float)
                      and 0 <= prior["cost_usd"] < float("inf"))))
    if rounds_previous is None:
        return {"cost_usd": cost_usd, "runs": 1, "rounds": round_no,
                "complete": cost_usd is not None}
    if not valid:
        return {"cost_usd": cost_usd, "runs": 1, "rounds": round_no, "complete": False}
    total = prior["cost_usd"]
    if cost_usd is not None:
        total = (total or 0.0) + cost_usd
    return {"cost_usd": None if total is None else round(total, 4),
            "runs": prior["runs"] + 1, "rounds": round_no,
            "complete": prior["complete"] and cost_usd is not None}


def build_usage(reviewer: str, model: str, effort: str, tokens, cost_usd, skills,
                previous_run=None, previous=None, session=None,
                expected_session=None, round_no: int = 1,
                rounds_previous=None) -> dict:
    """Assemble the usage.json record of one run.

    cost_usd is the CLI-reported cost and is used for claude only; the codex cost
    comes from PRICES, and the antigravity cost is always unknown. previous is the
    parsed usage.json of previous_run, or None when it is missing or unreadable, which
    marks the session total incomplete. On a claude or antigravity resume tokens and
    cost_usd are the cumulative session figures the CLI reports; session and
    expected_session are the reported and the resumed session ids (see resume_usage).
    """
    priced = REVIEWERS[reviewer]["priced"]
    # Claude and agy report cumulative usage on resume; codex reports this run only.
    if reviewer != "codex" and previous_run is not None:
        session_total = _resume_total(tokens, cost_usd, previous, priced)
        tokens, cost_usd, _ = resume_usage(tokens, cost_usd, previous, session,
                                           expected_session, reviewer)
    else:
        session_total = None
    if reviewer == "codex":
        cost_usd = codex_cost(model, tokens) if tokens is not None else None
        basis = "price-table" if cost_usd is not None else "unknown"
        skills, note = sorted(skills), "detected from SKILL.md reads"
    elif reviewer == "antigravity":
        cost_usd, basis = None, "unknown"
        skills, note = [], "not detected"
    else:
        basis = "cli-list" if cost_usd is not None else "unknown"
        if cost_usd is None and tokens is not None:
            cost_usd = claude_cost(model, tokens)
            basis = "price-table" if cost_usd is not None else "unknown"
        skills, note = [], "disabled by policy"
    return {
        "schema_version": USAGE_SCHEMA_VERSION,
        "reviewer": reviewer,
        "model": model,
        "effort": effort,
        "skills": skills,
        "skills_note": note,
        "tokens": tokens,
        "cost_usd": cost_usd,
        "cost_basis": basis,
        "prices_as_of": PRICES_AS_OF if basis == "price-table" else None,
        "previous_run": None if previous_run is None else str(previous_run),
        "session_total": session_total or _session_total(tokens, cost_usd, previous_run,
                                                         previous, priced),
        "round": round_no,
        "target_total": _target_total(cost_usd, rounds_previous, round_no),
    }


def _ensure_private_dir(path: Path) -> None:
    """Accept only a real directory owned by us with mode 0700."""
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise CrossReviewError(EXIT_INVALID_INPUT, "unsafe run root (not a real directory): %s" % path)
    if st.st_uid != os.getuid():
        raise CrossReviewError(EXIT_INVALID_INPUT, "unsafe run root (foreign owner): %s" % path)
    if stat.S_IMODE(st.st_mode) != 0o700:
        raise CrossReviewError(EXIT_INVALID_INPUT,
                               "unsafe run root (mode %o, expected 700): %s"
                               % (stat.S_IMODE(st.st_mode), path))


def _read_private_file(path: Path, limit: int) -> bytes:
    """Read a run artifact only if it is a regular file we own (no symlinks)."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            raise OSError("not a regular file owned by the current user")
        return os.read(fd, limit)
    finally:
        os.close(fd)


def _read_usage(run_dir: Path) -> Optional[dict]:
    """Return the parsed usage.json of a run, or None when it is missing or invalid."""
    try:
        data = json.loads(_read_private_file(Path(run_dir) / USAGE_FILE, 1 << 16)
                          .decode("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


AGY_SETTINGS = Path(".gemini") / "antigravity-cli" / "settings.json"


def check_agy_isolation(env: Mapping[str, str]) -> None:
    """Refuse an Antigravity run when agy would run commands without asking.

    Headless agy runs every command in the user's permissions.allow, so a non-empty
    list, or a settings file that cannot be read or parsed, is exit 2 before any brief
    is sent. A missing file is an empty list. Messages name the file, never its content.
    """
    path = Path(env.get("HOME") or os.path.expanduser("~")) / AGY_SETTINGS
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CrossReviewError(EXIT_INVALID_INPUT, "cannot read agy settings %s: %s"
                               % (path, exc.strerror or type(exc).__name__))
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        data = None
    permissions = data.get("permissions", {}) if isinstance(data, dict) else None
    allow = permissions.get("allow", []) if isinstance(permissions, dict) else None
    if not isinstance(allow, list):
        raise CrossReviewError(EXIT_INVALID_INPUT,
                               "cannot parse permissions.allow in agy settings %s" % path)
    if allow:
        raise CrossReviewError(
            EXIT_INVALID_INPUT,
            "agy settings %s allow %d command(s) without asking (permissions.allow); "
            "empty that list before an Antigravity review" % (path, len(allow)))
