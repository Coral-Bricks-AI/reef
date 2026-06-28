"""``polyp.runner.worker_reconcile`` -- deterministic reconcile + schedule.

Ported from ``worker_reconcile.sh``. Run by the watcher at the top of
every poll, BEFORE it decides whether to wake the agentic shift. Pulls
the mechanical 90% of scheduling off the agent's critical path.

Phases:

- **Pre** — honor ``cbq stop``: SIGTERM the pid of any executing row
  this actor owns that has a stop request, SIGKILL after
  ``STOP_GRACE_SEC`` if still alive.
- **A** (always on) — for each slot whose pid is DEAD, run
  :mod:`polyp.runner.finalize_completed`. A clean completion finalizes
  via cbq + frees the slot + removes the worktree; a dead-not-clean
  job just frees the slot (worktree + cbq claim left for the shift).
- **B** (opt-in: ``WORKER_DET_SCHED=1``) — while GPUs are free and
  ready specs exist, claim + check out + launch the ones whose SHARED
  venv already exists, via ``smoke_and_launch.sh``. Anything that
  needs a venv build, a code fix, or fails its smoke is deferred
  back to the agentic shift.

Invoke as ``python -m polyp.runner.worker_reconcile [--dry-run]``.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, Optional

from polyp.runner.common import cbq, iso_now


BASE = Path(os.environ.get("WORKER_BASE", str(Path.home() / "worker" / "www")))
WORKER_HOME = BASE.parent
SLOTS_PATH = Path(os.environ.get("SLOTS", str(WORKER_HOME / "slots.json")))
LOCK_PATH = WORKER_HOME / ".slots.lock"
QVENVS = Path.home() / "queue" / "venvs"
JOBS_DIR = WORKER_HOME / "jobs"
INFLIGHT_DIR = WORKER_HOME / "inflight"

CBQ_KIND = (os.environ.get("CBQ_KIND") or "").strip()
CB_LEASE_RESOURCE = (os.environ.get("CB_LEASE_RESOURCE") or "").strip()
DET_SCHED = os.environ.get("WORKER_DET_SCHED", "0") == "1"
STOP_GRACE_SEC = int(os.environ.get("STOP_GRACE_SEC", "10"))
WORKER_ACTOR_FILTER = (os.environ.get("CBQ_ACTOR") or "").strip()

SMOKE_AND_LAUNCH = Path.home() / "bin" / "smoke_and_launch.sh"
FINALIZE_COMPLETED = Path.home() / "bin" / "finalize_completed.sh"


def _log(label: str, msg: str) -> None:
    print(f"[{iso_now()}] reconcile{label}: {msg}", flush=True)


def _kind_args() -> list[str]:
    return ["--kind", CBQ_KIND] if CBQ_KIND else []


def _read_slots() -> dict:
    try:
        with open(SLOTS_PATH, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def free_slot_for_job(job: str) -> None:
    """Atomically null every slot entry whose ``.job == job``.

    Mirrors the shared flock pattern from finalize_completed.sh so
    reconcile and the helpers serialize read-modify-writes the same way.
    """
    WORKER_HOME.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "w") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        try:
            data = _read_slots()
            changed = False
            for k, v in list(data.items()):
                if isinstance(v, dict) and v.get("job") == job:
                    data[k] = None
                    changed = True
            if changed:
                tmp = SLOTS_PATH.with_suffix(SLOTS_PATH.suffix + ".tmp")
                with open(tmp, "w") as f:
                    json.dump(data, f, indent=1)
                tmp.replace(SLOTS_PATH)
        finally:
            fcntl.flock(lockf, fcntl.LOCK_UN)


# ----------------------------------------------------------------------------
# Pre-phase: honor stop intent
# ----------------------------------------------------------------------------


def honor_stop_intent(*, dry: bool) -> None:
    """SIGTERM (then SIGKILL) the pid of any executing row with a stop request."""
    actor_args = ["--claimed-by", WORKER_ACTOR_FILTER] if WORKER_ACTOR_FILTER else []
    res = cbq("stop-pending", *actor_args, "--json", quiet=True)
    if res.returncode != 0:
        return
    body = (res.stdout or "").strip()
    if not body or body == "[]":
        return
    try:
        ids = json.loads(body)
    except json.JSONDecodeError:
        return

    slots = _read_slots()
    for id_ in ids:
        if not id_:
            continue
        job = None
        pid = 0
        for v in slots.values():
            if not isinstance(v, dict):
                continue
            j = v.get("job") or ""
            if isinstance(j, str) and j.startswith(f"{id_}-"):
                job = j
                pid = int(v.get("pid") or 0)
                break
        if not job:
            _log("", f"stop-pending {id_}: no live slot — leave for shift (orphan path)")
            continue
        if pid <= 0:
            continue
        if not _pid_alive(pid):
            _log("", f"stop-pending {id_}: pid {pid} already dead — heal path will free slot")
            continue
        if dry:
            _log("/dry",
                 f"WOULD SIGTERM pid={pid} for stop-requested {id_} ({job}); "
                 f"grace={STOP_GRACE_SEC}s then SIGKILL")
            continue
        _log("", f"honoring stop request for {id_}: SIGTERM pid={pid} (job={job})")
        _signal_group(pid, "TERM")
        waited = 0
        while waited < STOP_GRACE_SEC and _pid_alive(pid):
            time.sleep(1)
            waited += 1
        if _pid_alive(pid):
            _log("",
                 f"stop-pending {id_}: still alive after {STOP_GRACE_SEC}s — "
                 f"SIGKILL pid={pid}")
            _signal_group(pid, "KILL")


def _signal_group(pid: int, sig: str) -> None:
    """SIGTERM/SIGKILL the process AND its pgid for reliable subtree death."""
    ps = subprocess.run(["ps", "-o", "pgid=", "-p", str(pid)],
                        text=True, capture_output=True)
    pgid_str = (ps.stdout or "").strip()
    sig_num = {"TERM": 15, "KILL": 9}.get(sig, 15)
    if pgid_str:
        try:
            os.killpg(int(pgid_str), sig_num)
            return
        except (ProcessLookupError, PermissionError, ValueError, OSError):
            pass
    try:
        os.kill(pid, sig_num)
    except (ProcessLookupError, PermissionError):
        pass


# ----------------------------------------------------------------------------
# Phase A: finalize cleanly-completed dead jobs
# ----------------------------------------------------------------------------


def finalize_dead_clean(*, dry: bool) -> None:
    slots = _read_slots()
    seen: dict[str, dict] = {}
    for v in slots.values():
        if not isinstance(v, dict):
            continue
        j = v.get("job")
        if not j:
            continue
        seen.setdefault(j, v)

    for job, entry in seen.items():
        pid = int(entry.get("pid") or 0)
        if _pid_alive(pid):
            continue
        id_, _, slug = job.partition("-")
        if not id_ or not slug:
            continue
        res_dir = JOBS_DIR / job / "ml" / "eval" / "experiments" / "results" / job

        if dry:
            results = res_dir / "results.json"
            progress = res_dir / "progress.log"
            clean = (
                results.exists()
                and progress.exists()
                and _progress_has_done(progress)
            )
            if clean:
                _log("/dry", f"WOULD finalize (clean completion): {job}")
            else:
                _log("/dry",
                     f"WOULD free slot (dead, not cleanly complete — "
                     f"crash/user-kill); leave worktree+claim for shift: {job}")
            continue

        if FINALIZE_COMPLETED.exists():
            res = subprocess.run([str(FINALIZE_COMPLETED), id_, slug],
                                 text=True, capture_output=True)
            rc = res.returncode
        else:
            rc = 127
        if rc == 0:
            _log("", f"finalized clean completion: {job} (slot freed, worktree removed)")
        elif rc == 10:
            free_slot_for_job(job)
            _log("",
                 f"freed slot (dead, not cleanly complete — crash/user-kill); "
                 f"worktree+claim left for shift: {job}")
        else:
            free_slot_for_job(job)
            _log("",
                 f"finalize_completed rc={rc} for {job} — slot freed, "
                 f"worktree+claim left for shift")


def _progress_has_done(progress: Path) -> bool:
    try:
        with open(progress, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return False
    return bool(re.search(r'"phase":\s*"done"|results written|"msg":\s*"done"', tail))


# ----------------------------------------------------------------------------
# Phase B: deterministic schedule
# ----------------------------------------------------------------------------


def free_gpu_ids() -> list[str]:
    slots = _read_slots()
    return sorted(
        (k for k, v in slots.items() if v is None),
        key=lambda x: int(x) if x.isdigit() else x,
    )


_SCALAR_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")


def spec_scalar(key: str, spec_path: Path) -> Optional[str]:
    """Read a YAML scalar from a run.spec (no YAML dependency)."""
    try:
        with open(spec_path, "r") as f:
            for line in f:
                m = _SCALAR_KEY.match(line.rstrip("\n"))
                if m and m.group(1) == key:
                    return m.group(2).strip()
    except OSError:
        pass
    return None


class FoldedScalar(Exception):
    """Raised when run_flags is a YAML folded/block scalar — defer to shift."""


def spec_runflags(spec_path: Path) -> str:
    """Return the flat run_flags / args scalar from run.spec, or '' if absent.

    Raises :class:`FoldedScalar` when the value is folded/block-scalar
    (``>`` / ``|`` or empty with following indented lines) so the caller
    defers the spec to the shift rather than risk launching with the
    wrong flags.
    """
    try:
        with open(spec_path, "r") as f:
            for line in f:
                stripped = line.rstrip("\n")
                if stripped.startswith("run_flags:") or stripped.startswith("args:"):
                    val = stripped.split(":", 1)[1].strip()
                    if not val or val.startswith(">") or val.startswith("|"):
                        raise FoldedScalar()
                    # Strip surrounding quotes.
                    return val.strip("'\"")
    except OSError:
        return ""
    return ""


def defer(id_: str, job: str) -> None:
    """Return a claimed row to ready, drop its worktree, delete the snapshot."""
    cbq("unclaim", id_, quiet=True)
    subprocess.run(
        ["git", "-C", str(BASE), "worktree", "remove", "--force",
         str(JOBS_DIR / job)],
        text=True, capture_output=True,
    )
    snap = INFLIGHT_DIR / f"{job}.task"
    snap.unlink(missing_ok=True) if hasattr(snap, "unlink") else None
    try:
        snap.unlink()
    except OSError:
        pass


def _pick_spec(ready_rows: list[dict], free_n: int,
               excluded: set[str]) -> Optional[dict]:
    """Mirror the bash placement policy: lowest-id eligible if it fits,
    else hold for it and only backfill a shorter (<=30 min) one."""
    eligible = [r for r in ready_rows if r.get("id") not in excluded]
    if not eligible:
        return None
    lo = eligible[0]
    if int(lo.get("gpus", 1) or 1) <= free_n:
        return lo
    for r in eligible:
        if (int(r.get("gpus", 1) or 1) <= free_n
                and int(r.get("timeout_min", 999) or 999) <= 30):
            return r
    return None


def schedule(*, dry: bool) -> None:
    if not DET_SCHED and not dry:
        _log("",
             "Phase B disabled (WORKER_DET_SCHED!=1) — schedule deferred to shift")
        return
    label = "/dry" if dry else ""
    if CB_LEASE_RESOURCE:
        lease = cbq("lease-active", CB_LEASE_RESOURCE, quiet=True)
        holder = (lease.stdout or "").splitlines()[:1]
        if holder and holder[0].strip():
            _log(label,
                 f"draining (node leased by {holder[0].strip()}) — not "
                 f"scheduling")
            return

    free = free_gpu_ids()
    if not free:
        return

    slots = _read_slots()
    excluded: set[str] = set()
    for v in slots.values():
        if isinstance(v, dict):
            j = v.get("job") or ""
            if isinstance(j, str) and "-" in j:
                excluded.add(j.split("-", 1)[0])

    while free:
        ready_res = cbq("list", "--status", "ready", *_kind_args(),
                        "--json", quiet=True)
        if ready_res.returncode != 0:
            break
        try:
            ready_rows = json.loads(ready_res.stdout or "[]")
        except json.JSONDecodeError:
            break
        if not isinstance(ready_rows, list):
            break

        nfree = len(free)
        pick = _pick_spec(ready_rows, nfree, excluded)
        if pick is None:
            _log(label,
                 f"nothing schedulable: no eligible ready spec fits {nfree} "
                 f"free GPU(s) (excludes in-slot ids)")
            break

        id_ = str(pick.get("id") or "")
        slug = str(pick.get("slug") or "")
        g = int(pick.get("gpus", 1) or 1)
        job = f"{id_}-{slug}"
        gpu_csv = ",".join(free[:g])

        if dry:
            _log(label,
                 f"WOULD claim+launch {job} (gpus={g}) onto GPU(s) [{gpu_csv}]")
            excluded.add(id_)
            free = free[g:]
            continue

        claim = cbq("claim", "execute", "--id", id_, *_kind_args(), quiet=True)
        if claim.returncode != 0:
            _log("", f"claim race for {id_} — re-listing")
            continue
        excluded.add(id_)
        INFLIGHT_DIR.mkdir(parents=True, exist_ok=True)
        snap = INFLIGHT_DIR / f"{job}.task"
        snap_res = cbq("show", id_, "--markdown", quiet=True)
        if snap_res.returncode == 0:
            snap.write_text(snap_res.stdout or "")

        wt = JOBS_DIR / job
        br = f"exp/{job}"
        if not wt.exists():
            subprocess.run(
                ["git", "-C", str(BASE), "fetch", "origin",
                 f"{br}:{br}", "--quiet"],
                text=True, capture_output=True,
            )
            add = subprocess.run(
                ["git", "-C", str(BASE), "worktree", "add",
                 str(wt), br, "--quiet"],
                text=True, capture_output=True,
            )
            if add.returncode != 0:
                _log("", f"worktree add failed for {job} — defer to shift")
                defer(id_, job)
                break
        res_dir = wt / "ml" / "eval" / "experiments" / "results" / job
        spec_path = res_dir / "run.spec"
        if not spec_path.exists():
            _log("", f"run.spec missing for {job} — defer to shift")
            defer(id_, job)
            break

        venv = spec_scalar("venv", spec_path) or ""
        tmin = spec_scalar("timeout_min", spec_path) or "90"
        smin = spec_scalar("smoke_timeout_min", spec_path) or "10"
        smoke = (spec_scalar("smoke", spec_path) or "").strip("'\"")

        if not venv or not (QVENVS / venv / "bin" / "python").is_file():
            _log("",
                 f"venv '{venv or '<none>'}' absent for {job} — defer to shift "
                 f"(venv build)")
            defer(id_, job)
            break
        if not smoke:
            _log("", f"no smoke flag in run.spec for {job} — defer to shift")
            defer(id_, job)
            break
        try:
            rflags = spec_runflags(spec_path)
        except FoldedScalar:
            _log("",
                 f"run flags are a folded/multi-line scalar in run.spec for "
                 f"{job} — defer to shift")
            defer(id_, job)
            break

        _log("",
             f"launch {job} gpus={gpu_csv} venv={venv} timeout={tmin}m "
             f"smoke='{smoke}' smoke_min={smin}m run_flags='{rflags}'")
        launch_log = Path(f"/tmp/launch-{id_}.log")
        if not SMOKE_AND_LAUNCH.exists():
            _log("", f"smoke_and_launch.sh missing — defer to shift: {job}")
            defer(id_, job)
            break
        with open(launch_log, "ab") as lf:
            subprocess.Popen(
                [str(SMOKE_AND_LAUNCH), id_, slug, gpu_csv, venv,
                 tmin, smin, smoke, str(res_dir), rflags],
                stdout=lf, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        free = free[g:]


# ----------------------------------------------------------------------------
# Entrypoint
# ----------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    dry = "--dry-run" in argv

    if not SLOTS_PATH.exists() or SLOTS_PATH.stat().st_size == 0:
        _log("/dry" if dry else "", "no slots.json yet — nothing to do")
        return 0

    honor_stop_intent(dry=dry)
    finalize_dead_clean(dry=dry)
    schedule(dry=dry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
