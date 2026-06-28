"""``polyp.runner.common`` -- shared helpers for the Python runner scripts.

Wraps the side effects every architect/worker entrypoint needs: cbq
subprocess calls, git operations, S3 uploads, Slack notifications,
log files. Each helper is dependency-light (subprocess + stdlib);
no Python SDK for any of the external services.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence
from urllib import error as urllib_error
from urllib import request as urllib_request

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Time / logging
# ----------------------------------------------------------------------------


def iso_now() -> str:
    """ISO-8601 UTC timestamp with seconds resolution and Z suffix.

    Matches the bash scripts' ``date -Is`` output shape so logs line up
    when both shell and Python entrypoints run during the migration.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def stamp() -> str:
    """``YYYYMMDD-HHMMSS`` UTC stamp for run-id construction."""
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def log_line(line: str, *, index_path: Optional[Path] = None) -> None:
    """Print to stdout AND append to ``index_path`` (if given), with
    the bash-style ``[<iso>] <line>`` prefix.

    Matches the ``tee -a "$INDEX"`` pattern the bash scripts use.
    """
    msg = f"[{iso_now()}] {line}"
    print(msg, flush=True)
    if index_path is not None:
        try:
            index_path.parent.mkdir(parents=True, exist_ok=True)
            with open(index_path, "a", buffering=1) as f:
                f.write(msg + "\n")
        except OSError as exc:
            logger.debug("log_line: write to %s failed: %s", index_path, exc)


# ----------------------------------------------------------------------------
# cbq wrapper
# ----------------------------------------------------------------------------


def cbq_bin() -> str:
    """Resolve the cbq binary. ``$CBQ`` wins, else ``~/bin/cbq``, else PATH."""
    explicit = os.environ.get("CBQ")
    if explicit:
        return explicit
    home_bin = Path.home() / "bin" / "cbq"
    if home_bin.exists():
        return str(home_bin)
    return shutil.which("cbq") or "cbq"


def cbq(
    *args: str,
    check: bool = False,
    capture: bool = True,
    quiet: bool = False,
) -> subprocess.CompletedProcess:
    """Run ``cbq <args>``. Returns the CompletedProcess.

    ``capture=True`` returns stdout/stderr as text; ``capture=False``
    streams to the parent stdout (useful for ``cbq verdict --commit``
    which prints progress).
    """
    argv = [cbq_bin(), *args]
    if quiet:
        logger.debug("cbq %s", " ".join(args))
    else:
        logger.info("cbq %s", " ".join(args))
    return subprocess.run(
        argv,
        check=check,
        text=True,
        capture_output=capture,
        env=os.environ,
    )


def cbq_field(id_: str, field: str) -> Optional[str]:
    """Return ``cbq show <id> --field <field>`` stripped, or None on failure."""
    try:
        result = cbq("show", id_, "--field", field, quiet=True)
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


def cbq_touch(id_: str) -> None:
    """Best-effort claim heartbeat; never raises."""
    try:
        cbq("touch", id_, "--quiet", quiet=True)
    except (FileNotFoundError, subprocess.SubprocessError):
        pass


# ----------------------------------------------------------------------------
# git helpers
# ----------------------------------------------------------------------------


def git(*args: str, cwd: Optional[Path] = None, check: bool = False,
        capture: bool = True) -> subprocess.CompletedProcess:
    """Run a git command. Returns the CompletedProcess.

    ``cwd`` defaults to the process cwd so callers that ``os.chdir`` first
    don't have to thread it through.
    """
    return subprocess.run(
        ["git", *args],
        check=check,
        text=True,
        capture_output=capture,
        cwd=str(cwd) if cwd else None,
    )


def git_reset_to_main(*, cwd: Optional[Path] = None) -> None:
    """Fast-forward to a clean ``origin/main`` checkout, discarding state."""
    git("fetch", "origin", "main", "--quiet", cwd=cwd)
    git("checkout", "main", "--quiet", cwd=cwd)
    git("reset", "--hard", "origin/main", "--quiet", cwd=cwd)


def git_remote_branch_exists(branch: str, *, cwd: Optional[Path] = None) -> bool:
    """True iff ``origin/<branch>`` exists on the remote."""
    result = git("ls-remote", "--heads", "origin", branch, cwd=cwd)
    return result.returncode == 0 and branch in (result.stdout or "")


def git_remote_branch_sha(branch: str, *, cwd: Optional[Path] = None) -> Optional[str]:
    """Return the 7-char SHA of ``origin/<branch>``, or None if absent."""
    result = git("ls-remote", "--heads", "origin", branch, cwd=cwd)
    if result.returncode != 0:
        return None
    line = (result.stdout or "").strip().split("\n")[0]
    if not line:
        return None
    return line.split()[0][:7]


def git_show_file(ref: str, path: str, *, cwd: Optional[Path] = None) -> Optional[str]:
    """Return ``git show <ref>:<path>`` contents, or None if absent."""
    result = git("show", f"{ref}:{path}", cwd=cwd)
    if result.returncode != 0:
        return None
    return result.stdout


# ----------------------------------------------------------------------------
# S3 + Slack
# ----------------------------------------------------------------------------


def s3_cp(local: Path | str, remote: str, *, quiet: bool = True) -> bool:
    """Upload ``local`` to ``remote`` via ``aws s3 cp``. Returns success."""
    argv = ["aws", "s3", "cp", str(local), remote]
    if quiet:
        argv.append("--quiet")
    try:
        result = subprocess.run(argv, text=True, capture_output=True)
    except FileNotFoundError:
        logger.debug("s3_cp: aws CLI not on PATH; skipping upload to %s", remote)
        return False
    if result.returncode != 0:
        logger.debug(
            "s3_cp: failed %s -> %s rc=%d: %s",
            local, remote, result.returncode, result.stderr,
        )
        return False
    return True


def slack_post(text: str, *, hook_url: Optional[str] = None) -> None:
    """Post a single text message to a Slack incoming-webhook. Never raises.

    ``hook_url`` defaults to ``$SLACK_WEBHOOK_URL``. Use
    :func:`route_slack_hook` to pick a kind-specific override first.
    """
    url = hook_url or os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        return
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib_request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=10) as resp:
            resp.read()
    except (urllib_error.URLError, OSError, TimeoutError) as exc:
        logger.debug("slack_post failed: %s", exc)


def route_slack_hook(kind: Optional[str]) -> Optional[str]:
    """Pick the right Slack webhook for a task kind.

    ``loadtest`` routes to ``$SLACK_INFRA_BENCH_WEBHOOK_URL`` when set;
    everything else falls through to the default ``$SLACK_WEBHOOK_URL``.
    """
    default = os.environ.get("SLACK_WEBHOOK_URL")
    if kind == "loadtest":
        infra = os.environ.get("SLACK_INFRA_BENCH_WEBHOOK_URL")
        if infra:
            return infra
    return default


# ----------------------------------------------------------------------------
# Task metadata + paths
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskInfo:
    """Resolved cbq row metadata. Built once per runner invocation."""

    id: str
    slug: str
    kind: str
    tag: str            # "#<id>"
    branch: str         # "exp/<id>-<slug>"
    workdir: Path
    results_dir: str    # repo-relative results path
    run_id: str         # "<id>-<slug>-<stamp>" — used in log names / Slack
    log_path: Path
    s3_log_url: str
    index_path: Path
    host: str


def resolve_task(
    id_: str,
    *,
    workdir: Path,
    log_dir: Path,
    index_path: Path,
    s3_prefix: str,
    results_root: str,
    phase: Optional[str] = None,
) -> TaskInfo:
    """Look up slug/kind from cbq and assemble the per-run paths.

    ``phase`` (when set, e.g. ``"analyze"``) is woven into the run-id
    so analyze logs don't collide with code logs for the same row.
    """
    slug = cbq_field(id_, "slug")
    if not slug:
        raise SystemExit(f"ERROR: no row for {id_}")
    kind = cbq_field(id_, "kind") or "research"
    branch = cbq_field(id_, "branch") or f"exp/{id_}-{slug}"
    st = stamp()
    run_id = f"{id_}-{slug}-{st}" if phase is None else f"{id_}-{slug}-{phase}-{st}"
    log_path = log_dir / f"{run_id}.jsonl"
    s3_log_url = f"{s3_prefix.rstrip('/')}/{run_id}.jsonl"
    results_dir = f"{results_root.rstrip('/')}/{id_}-{slug}"
    return TaskInfo(
        id=id_,
        slug=slug,
        kind=kind,
        tag=f"#{id_}",
        branch=branch,
        workdir=workdir,
        results_dir=results_dir,
        run_id=run_id,
        log_path=log_path,
        s3_log_url=s3_log_url,
        index_path=index_path,
        host=socket.gethostname(),
    )


# ----------------------------------------------------------------------------
# Retry loop
# ----------------------------------------------------------------------------


def append_log(path: Path, text: str) -> None:
    """Append text to a log file with line-buffered semantics."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", buffering=1) as f:
        if not text.endswith("\n"):
            text = text + "\n"
        f.write(text)


def env_int(name: str, default: int) -> int:
    """Read an int from env, falling back to ``default`` on missing/invalid."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@contextmanager
def cwd(path: Path) -> Iterator[None]:
    """``os.chdir`` to ``path`` for the with-block; restore on exit."""
    prev = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


__all__ = [
    "TaskInfo",
    "append_log",
    "cbq",
    "cbq_bin",
    "cbq_field",
    "cbq_touch",
    "cwd",
    "env_int",
    "git",
    "git_remote_branch_exists",
    "git_remote_branch_sha",
    "git_reset_to_main",
    "git_show_file",
    "iso_now",
    "log_line",
    "resolve_task",
    "route_slack_hook",
    "s3_cp",
    "slack_post",
    "stamp",
]
