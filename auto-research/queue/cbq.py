#!/usr/bin/env python3
"""cbq — Postgres-backed experiment queue for the cb experiments pipeline.

Replaces the git-as-message-bus queue (enqueued/ready/executed dirs + lifecycle
commits on main). The experiments table is AUTHORITATIVE for all live state;
git keeps only the archive exports (done/, falsified/), constraints.md, and the
exp/NNNN-* code/artifact branches.

Status machine:

    enqueued -> coding -> ready -> executing -> executed -> analyzing
    analyzing -> done | falsified | blocked     (scientific verdicts; done/falsified
                                                 render an archive file in git)
    coding    -> code_failed                    (parked: in-phase retries exhausted;
                                                 Slack escalation, human respins)
    analyzing -> executed | analyze_stuck       (one auto-retry, then parked; GPU data
                                                 intact on the exp/ branch)
    enqueued  -> cancelled                      (veto window for auto-suggests)

A `blocked` verdict is a PAIRED write: the row gets blocked_on=C-NNN and
constraints.md must already contain that C-NNN bullet (validated here).

DSN resolution order:
    1. $CB_QUEUE_DB_URL
    2. ~/.cb_queue_db_url
    3. <repo>/ml/cb_queue_db_url        (gitignored, like ml/coral_api_key)
    4. AWS Secrets Manager: $CB_QUEUE_DB_SECRET (default 'cb-queue/db-url'),
       SecretString = raw DSN or JSON with DB_URL / DATABASE_URL key.

Everything lives in the `cb_queue` Postgres schema, so the queue can share a
database server with other services safely.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "cbq: psycopg (v3) is required: pip3 install 'psycopg[binary]'\n"
    )
    sys.exit(2)

HERE = Path(__file__).resolve().parent
SCHEMA_FILE = HERE / "schema.sql"
EXPERIMENTS_DIR_REL = Path("ml/eval/experiments")

LIVE_STATUSES = ("enqueued", "coding", "ready", "executing", "executed", "analyzing")
PARKED_STATUSES = ("code_failed", "analyze_stuck", "blocked")
TERMINAL_STATUSES = ("done", "falsified", "cancelled")
ALL_STATUSES = LIVE_STATUSES + PARKED_STATUSES + TERMINAL_STATUSES

# analyze failures before parking: 1st failure -> back to executed for one more
# wrapper invocation; 2nd -> analyze_stuck (mirrors the old <!-- analyze-retry -->
# marker, but visible and queryable).
ANALYZE_RETRY_CAP = 2

CLAIM_PHASES = {
    "code": ("enqueued", "coding"),
    "execute": ("ready", "executing"),
    "analyze": ("executed", "analyzing"),
}

ID_RE = re.compile(r"^\d{1,4}(\.\d+)*$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Machine = hardware class the spec is sized for (informational attribute on
# the row; ROUTING is by kind — each kind maps to one GPU worker). 'a10' is
# the default for unmarked submissions. A task file can pin it with a
# top-level `machine: h100` line in its yaml block.
MACHINES = ["a10", "h100"]
KINDS    = ["research", "loadtest", "finetune", "swe-bench", "swe-bench-sweep"]
MACHINE_RE = re.compile(r"^\s*machine:\s*([a-z0-9-]+)\s*(?:#.*)?$", re.MULTILINE)
CONSTRAINT_ID_RE = re.compile(r"^C-\d+$")


def die(msg: str, rc: int = 1):
    sys.stderr.write(f"cbq: {msg}\n")
    sys.exit(rc)


# --------------------------------------------------------------------------- dsn


def _repo_root() -> Path | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            return Path(out.stdout.strip())
    except Exception:
        pass
    return None


def _sanitize_dsn(dsn: str) -> str:
    """Translate Prisma-style URI params into libpq-valid ones: drop schema=...
    (tables are schema-qualified in every query) and map sslmode=no-verify ->
    require (libpq has no no-verify; require encrypts without CA verification)."""
    if "?" not in dsn:
        return dsn
    base, query = dsn.split("?", 1)
    kept = []
    for p in query.split("&"):
        if not p or p.startswith("schema="):
            continue
        if p == "sslmode=no-verify":
            p = "sslmode=require"
        kept.append(p)
    return base + ("?" + "&".join(kept) if kept else "")


def resolve_dsn() -> str:
    env = os.environ.get("CB_QUEUE_DB_URL")
    if env:
        return _sanitize_dsn(env.strip())
    for cand in [Path.home() / ".cb_queue_db_url"]:
        if cand.is_file():
            return _sanitize_dsn(cand.read_text().strip())
    root = _repo_root()
    if root and (root / "ml/cb_queue_db_url").is_file():
        return _sanitize_dsn((root / "ml/cb_queue_db_url").read_text().strip())
    secret_id = os.environ.get("CB_QUEUE_DB_SECRET", "cb-queue/db-url")
    try:
        out = subprocess.run(
            ["aws", "secretsmanager", "get-secret-value", "--secret-id", secret_id,
             "--query", "SecretString", "--output", "text"],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode == 0 and out.stdout.strip():
            raw = out.stdout.strip()
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return _sanitize_dsn((parsed.get("DB_URL") or parsed.get("DATABASE_URL") or "").strip())
            except json.JSONDecodeError:
                return _sanitize_dsn(raw)
    except Exception:
        pass
    die(
        "no database DSN found. Set CB_QUEUE_DB_URL, write ~/.cb_queue_db_url or "
        "<repo>/ml/cb_queue_db_url, or create the 'cb-queue/db-url' secret."
    )


def connect():
    # prepare_threshold=None: no server-side prepared statements, so the queue
    # works through transaction-mode poolers (pgbouncer/supavisor) too
    return psycopg.connect(resolve_dsn(), row_factory=dict_row, prepare_threshold=None)


# ----------------------------------------------------------------------- helpers


def parse_ord(exp_id: str) -> list[int]:
    return [int(p) for p in exp_id.split(".")]


def actor_default() -> str:
    return os.environ.get("CBQ_ACTOR") or f"{os.environ.get('USER', 'unknown')}@{os.uname().nodename}"


def record_transition(cur, exp_id: str, from_status: str | None, to_status: str,
                      actor: str, note: str | None = None):
    cur.execute(
        "INSERT INTO cb_queue.transitions (experiment_id, from_status, to_status, actor, note)"
        " VALUES (%s, %s, %s, %s, %s)",
        (exp_id, from_status, to_status, actor, note),
    )


def move_status(cur, exp_id: str, expect: tuple[str, ...], to_status: str, actor: str,
                note: str | None = None, extra_sets: str = "", extra_args: tuple = ()):
    """Transition exp_id from one of `expect` to to_status; dies on state mismatch."""
    cur.execute(
        "SELECT status FROM cb_queue.experiments WHERE id = %s FOR UPDATE", (exp_id,)
    )
    row = cur.fetchone()
    if row is None:
        die(f"no experiment {exp_id!r}", 4)
    if row["status"] not in expect:
        die(f"{exp_id} is {row['status']!r}, expected one of {list(expect)}", 5)
    cur.execute(
        f"UPDATE cb_queue.experiments SET status = %s, updated_at = now() {extra_sets}"
        " WHERE id = %s",
        (to_status, *extra_args, exp_id),
    )
    record_transition(cur, exp_id, row["status"], to_status, actor, note)
    return row["status"]


def fetch_exp(cur, exp_id: str) -> dict:
    cur.execute("SELECT * FROM cb_queue.experiments WHERE id = %s", (exp_id,))
    row = cur.fetchone()
    if row is None:
        die(f"no experiment {exp_id!r}", 4)
    return row


def render_markdown(row: dict) -> str:
    """Assemble the full task document the way the git files used to look."""
    parts = [row["task_md"].rstrip()]
    for key in ("handoff_md", "execution_report_md", "review_md"):
        if row.get(key):
            parts.append("---\n" + row[key].rstrip())
    return "\n\n".join(parts) + "\n"


def archive_relpath(row: dict) -> Path:
    sub = "done" if row["status"] == "done" else "falsified"
    return EXPERIMENTS_DIR_REL / sub / f"{row['id']}-{row['slug']}.task"


def write_archive_file(row: dict, root: Path) -> Path:
    path = root / archive_relpath(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(row["archive_md"])
    return path


def git_commit_push(root: Path, paths: list[Path], message: str, retries: int = 5) -> bool:
    rel = [str(p.relative_to(root)) for p in paths]
    run = lambda *a: subprocess.run(["git", "-C", str(root), *a],
                                    capture_output=True, text=True)
    run("add", *rel)
    c = run("commit", "-m", message)
    if c.returncode != 0 and "nothing to commit" in (c.stdout + c.stderr):
        return True
    for attempt in range(retries):
        if run("push", "origin", "main").returncode == 0:
            return True
        run("pull", "--rebase", "origin", "main")
        time.sleep(2 * (attempt + 1))
    return False


def constraints_path(root: Path) -> Path:
    return root / EXPERIMENTS_DIR_REL / "constraints.md"


def constraint_exists(root: Path, cid: str) -> bool:
    p = constraints_path(root)
    if not p.is_file():
        return False
    return re.search(rf"(^|[\s\-]){re.escape(cid)}:", p.read_text()) is not None


def read_body(src: str) -> str:
    if src == "-":
        return sys.stdin.read()
    p = Path(src)
    if not p.is_file():
        die(f"no such file: {src}")
    return p.read_text()


def slug_from_filename(src: str) -> str:
    base = Path(src).name
    if base.endswith(".task"):
        base = base[: -len(".task")]
    base = re.sub(r"^\d+(\.\d+)*-", "", base)
    return base


# ---------------------------------------------------------------------- commands


def cmd_init_db(args):
    sql = SCHEMA_FILE.read_text()
    with connect() as conn:
        conn.execute(sql)
    print("cb_queue schema applied")


def cmd_submit(args):
    body = read_body(args.file)
    if not body.strip():
        die("empty task body")
    slug = args.slug or slug_from_filename(args.file if args.file != "-" else "")
    if args.file == "-" and not args.slug:
        die("--slug is required when submitting from stdin")
    slug = slug.lower()
    if not SLUG_RE.match(slug):
        die(f"bad slug {slug!r} (lowercase, [a-z0-9-])")
    actor = args.actor or actor_default()

    # Machine (hardware-class sizing): explicit flag wins; else a `machine:`
    # key in the task body; else the a10 default. Reject unknown values either
    # way so a typo can't silently mislabel a spec.
    body_machine = (MACHINE_RE.search(body) or [None, None])[1]
    machine = args.machine or body_machine or "a10"
    if machine not in MACHINES:
        die(f"unknown machine {machine!r} (expected one of {', '.join(MACHINES)})")

    with connect() as conn, conn.cursor() as cur:
        # serialize ID allocation across producers
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('cb_queue_id_alloc'))")
        if args.id:
            if not ID_RE.match(args.id):
                die(f"bad id {args.id!r}")
            exp_id = args.id
        elif args.parent:
            cur.execute("SELECT 1 FROM cb_queue.experiments WHERE id = %s", (args.parent,))
            if cur.fetchone() is None:
                die(f"parent {args.parent!r} not found", 4)
            pord = parse_ord(args.parent)
            cur.execute(
                "SELECT coalesce(max(ord[%s]), 0) AS m FROM cb_queue.experiments"
                " WHERE ord[1:%s] = %s::int[] AND array_length(ord, 1) = %s",
                (len(pord) + 1, len(pord), pord, len(pord) + 1),
            )
            exp_id = f"{args.parent}.{cur.fetchone()['m'] + 1}"
        else:
            cur.execute("SELECT coalesce(max(ord[1]), 0) AS m FROM cb_queue.experiments")
            row_max = cur.fetchone()["m"]
            cur.execute(
                "UPDATE cb_queue.id_counter SET top = GREATEST(top, %s) + 1 RETURNING top",
                (row_max,),
            )
            exp_id = f"{cur.fetchone()['top']:04d}"

        parent_id = args.parent or (".".join(exp_id.split(".")[:-1]) if "." in exp_id else None)

        # Kind: explicit flag wins; else inherit the parent's kind (a respin of a
        # finetune/loadtest line must stay in that line's worker lane); else the
        # research default for brand-new top-level lines.
        kind = args.kind
        if kind is None:
            if parent_id:
                cur.execute("SELECT kind FROM cb_queue.experiments WHERE id = %s", (parent_id,))
                prow = cur.fetchone()
                kind = (prow["kind"] if prow and prow.get("kind") else None) or "research"
            else:
                kind = "research"

        # loadtest is a human-curated sweep plan (loadtest/tasks/), NOT an
        # open-ended research line: block AUTO-generated loadtest rows (respin
        # iteration + auto-suggest) so they can't queue ahead of curated work or
        # poison the lane (see the 0056.1 respin-loop incident). User-submitted
        # loadtest tasks (origin=user) are unaffected. The analyze session's
        # `cbq submit --origin respin` becomes a clean no-op (exit 0) so it just
        # closes the line. Override with CBQ_ALLOW_LOADTEST_AUTO=1.
        if (kind == "loadtest" and args.origin in ("respin", "auto-suggest")
                and os.environ.get("CBQ_ALLOW_LOADTEST_AUTO") != "1"):
            print(f"skipped: loadtest {args.origin} disabled (curated lane only); line closed")
            return

        try:
            cur.execute(
                "INSERT INTO cb_queue.experiments (id, ord, slug, status, origin, kind, machine, parent_id, task_md)"
                " VALUES (%s, %s, %s, 'enqueued', %s, %s, %s, %s, %s)",
                (exp_id, parse_ord(exp_id), slug, args.origin, kind, machine, parent_id, body),
            )
        except psycopg.errors.UniqueViolation:
            die(f"id {exp_id} already exists", 6)
        record_transition(cur, exp_id, None, "enqueued", actor,
                          f"origin={args.origin} kind={kind} machine={machine}")
    print(f"submitted: {exp_id}-{slug} (kind={kind} machine={machine})")


def cmd_claim(args):
    from_status, to_status = CLAIM_PHASES[args.phase]
    actor = args.actor or actor_default()
    cond, params = "", []
    if args.id:
        cond, params = " AND id = %s", [args.id]
    else:
        # Kind filter: a worker claims only its own kind so loadtest /
        # finetune / research boxes share one queue without stealing each
        # other's rows. Omitting --kind keeps the legacy any-kind behavior.
        if args.kind:
            cond += " AND kind = %s"
            params.append(args.kind)
        # Exclude listed kinds (architect uses this to skip swe-bench rows,
        # which its controller transitions out of executed directly).
        if getattr(args, "kind_not_in", None):
            for k in args.kind_not_in.split(","):
                k = k.strip()
                if k:
                    cond += " AND kind <> %s"
                    params.append(k)
        # Optional hardware-class filter; routing is normally by kind alone.
        if args.machine:
            cond += " AND machine = %s"
            params.append(args.machine)
        if args.phase == "execute" and args.max_gpus:
            cond += " AND coalesce(gpus, 1) <= %s"
            params.append(args.max_gpus)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            WITH pick AS (
              SELECT id FROM cb_queue.experiments
              WHERE status = %s{cond}
              ORDER BY ord
              LIMIT 1
              FOR UPDATE SKIP LOCKED
            )
            UPDATE cb_queue.experiments e
            SET status = %s, claimed_by = %s, claimed_at = now(), updated_at = now()
            FROM pick WHERE e.id = pick.id
            RETURNING e.id, e.slug
            """,
            (from_status, *params, to_status, actor),
        )
        row = cur.fetchone()
        if row is None:
            sys.exit(3)  # nothing claimable: empty output, rc 3 (wrapper-friendly)
        record_transition(cur, row["id"], from_status, to_status, actor, "claim")
    print(row["id"])


def cmd_unclaim(args):
    back = {"coding": "enqueued", "executing": "ready", "analyzing": "executed"}
    actor = args.actor or actor_default()
    with connect() as conn, conn.cursor() as cur:
        row = fetch_exp(cur, args.id)
        if row["status"] not in back:
            die(f"{args.id} is {row['status']!r}; only coding/executing/analyzing can be unclaimed", 5)
        move_status(cur, args.id, (row["status"],), back[row["status"]], actor,
                    note=args.note or "unclaim",
                    extra_sets=", claimed_by = NULL, claimed_at = NULL")
    print(f"{args.id}: {row['status']} -> {back[row['status']]}")


def cmd_touch(args):
    """Refresh a held claim's clock (per-attempt heartbeat).

    The phase wrappers call this at the START of each in-phase attempt so a live
    multi-attempt session is never mistaken for an orphan: `cbq reap` keys off
    claimed_at, and without a touch a legitimate 3-attempt coding phase (≤30 min
    each) would age past the reap window. With touch, the reap window only has to
    cover ONE attempt + margin, independent of MAX_ATTEMPTS. No transition row is
    written (this is a heartbeat, not a state change). Only updates a claim still
    held by this actor in an in-phase status — a no-op (rc 0) otherwise, so a
    losing race or an already-reaped row never errors the wrapper.
    """
    actor = args.actor or actor_default()
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE cb_queue.experiments SET claimed_at = now(), updated_at = now()"
            " WHERE id = %s AND claimed_by = %s"
            " AND status IN ('coding','analyzing','executing')",
            (args.id, actor),
        )
        touched = cur.rowcount
    if not args.quiet:
        print(f"{args.id}: touched" if touched else f"{args.id}: no held claim to touch")


# ── Node leases: clean-room coordination on a shared GPU box ────────────────
# A benchmark needs the node quiet for trustworthy headline numbers, but GPU
# isolation can't quiet shared NVLink/host. `cbq lease` is the cooperative
# signal: acquire an exclusive hold, peers `cbq lease-active <resource>` before
# launching and DRAIN while it's held; `cbq release` lifts it.

def cmd_lease(args):
    """Acquire (or refresh) a lease on a shared resource (e.g. the node)."""
    actor = args.actor or actor_default()
    ttl = f"now() + make_interval(mins => {int(args.ttl_min)})" if args.ttl_min else "NULL"
    with connect() as conn, conn.cursor() as cur:
        # An exclusive request fails if anyone else already holds the resource.
        cur.execute(
            "SELECT holder, mode, expires_at FROM cb_queue.leases"
            " WHERE resource = %s AND (expires_at IS NULL OR expires_at > now())"
            " FOR UPDATE",
            (args.resource,),
        )
        cur_row = cur.fetchone()
        if cur_row and cur_row["holder"] != actor:
            die(f"{args.resource} already leased ({cur_row['mode']}) by "
                f"{cur_row['holder']}", 5)
        cur.execute(
            f"""
            INSERT INTO cb_queue.leases (resource, mode, holder, reason, acquired_at, expires_at)
            VALUES (%s, %s, %s, %s, now(), {ttl})
            ON CONFLICT (resource) DO UPDATE
              SET mode = EXCLUDED.mode, holder = EXCLUDED.holder,
                  reason = EXCLUDED.reason, acquired_at = now(), expires_at = EXCLUDED.expires_at
            """,
            (args.resource, args.mode, actor, args.reason),
        )
    print(f"leased {args.resource} ({args.mode}) by {actor}"
          + (f" for {args.ttl_min}min" if args.ttl_min else ""))


def cmd_release(args):
    actor = args.actor or actor_default()
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT holder FROM cb_queue.leases WHERE resource = %s", (args.resource,))
        row = cur.fetchone()
        if row is None:
            print(f"{args.resource}: not leased")
            return
        if row["holder"] != actor and not args.force:
            die(f"{args.resource} held by {row['holder']}, not {actor} (use --force)", 5)
        cur.execute("DELETE FROM cb_queue.leases WHERE resource = %s", (args.resource,))
    print(f"released {args.resource}")


def cmd_lease_active(args):
    """Print the active holder of an EXCLUSIVE lease, or nothing. rc 0 always.

    Workers call this before launching: if it prints a holder other than self,
    drain (don't launch). Expired leases are treated as absent.
    """
    actor = args.actor or actor_default()
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT holder, mode, reason FROM cb_queue.leases"
            " WHERE resource = %s AND mode = 'exclusive'"
            "   AND (expires_at IS NULL OR expires_at > now())",
            (args.resource,),
        )
        row = cur.fetchone()
    if row and row["holder"] != actor:
        print(f"{row['holder']}\t{row['reason'] or ''}")


def cmd_leases(args):
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT resource, mode, holder, reason, acquired_at, expires_at"
            " FROM cb_queue.leases WHERE expires_at IS NULL OR expires_at > now()"
            " ORDER BY resource"
        )
        rows = cur.fetchall()
    if not rows:
        print("(no active leases)")
        return
    for r in rows:
        ttl = f" expires {r['expires_at']:%H:%M}" if r["expires_at"] else ""
        print(f"{r['resource']}\t{r['mode']}\t{r['holder']}\t{r['reason'] or ''}{ttl}")


def cmd_heartbeat(args):
    """Upsert this worker's row (worker_watcher.sh, background subloop, ~60s).

    Each kind maps to one GPU worker. The architect's generator only authors
    experiments for kinds with a fresh heartbeat, so a stopped box silently
    drops out of auto-suggest rotation; `machine` reports the hardware class
    the worker currently runs on so new specs get sized for it.
    """
    actor = args.actor or actor_default()
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cb_queue.workers (actor, kind, machine, gpus, last_seen)"
            " VALUES (%s, %s, %s, %s, now())"
            " ON CONFLICT (actor) DO UPDATE"
            "   SET kind = EXCLUDED.kind, machine = EXCLUDED.machine,"
            "       gpus = EXCLUDED.gpus, last_seen = now()",
            (actor, args.kind, args.machine, args.gpus),
        )


def cmd_workers(args):
    """List workers; --active-min N keeps only fresh heartbeats (live kinds)."""
    cond, params = "", []
    if args.active_min:
        cond = " WHERE last_seen > now() - make_interval(mins => %s)"
        params.append(args.active_min)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT actor, kind, machine, gpus, last_seen,"
            "       extract(epoch FROM now() - last_seen)::int AS age_sec"
            f" FROM cb_queue.workers{cond} ORDER BY machine, kind NULLS FIRST, actor",
            params,
        )
        rows = cur.fetchall()
    if args.json:
        print(json.dumps(rows, default=str))
        return
    if not rows:
        print("(no workers)")
        return
    for r in rows:
        print(f"{r['actor']:<30} kind={r['kind'] or 'any':<10} machine={r['machine']:<5}"
              f" gpus={r['gpus'] or '-':<8} last_seen={r['age_sec']}s ago")


def cmd_reap(args):
    """Return stale architect claims to their queues (crashed phase sessions)."""
    actor = args.actor or actor_default()
    reaped = []
    plan = [("coding", "enqueued", args.coding_min), ("analyzing", "executed", args.analyzing_min)]
    if args.executing_min:
        plan.append(("executing", "ready", args.executing_min))
    with connect() as conn, conn.cursor() as cur:
        for status, back, minutes in plan:
            cur.execute(
                """
                UPDATE cb_queue.experiments
                SET status = %s, claimed_by = NULL, claimed_at = NULL, updated_at = now()
                WHERE status = %s AND claimed_at < now() - make_interval(mins => %s)
                RETURNING id
                """,
                (back, status, minutes),
            )
            for row in cur.fetchall():
                record_transition(cur, row["id"], status, back, actor,
                                  f"reaped stale {status} claim (>{minutes}min)")
                reaped.append((row["id"], status, back))
    for exp_id, status, back in reaped:
        print(f"reaped {exp_id}: {status} -> {back}")
    if not reaped and not args.quiet:
        print("nothing stale")


def cmd_ready(args):
    actor = args.actor or actor_default()
    handoff = "\n".join([
        "## Handoff (auto-appended by architect runner)",
        "",
        f"- Branch: [`{args.branch}`](https://github.com/Coral-Bricks-AI/www/tree/{args.branch}) @ `{args.sha}`",
        f"- Spec: `ml/eval/experiments/results/{args.id}-{{slug}}/run.spec` (on the branch)",
        f"- GPUs required: {args.gpus} · timeout: {args.timeout_min} min",
        f"- Code-phase log (S3): `{args.log_url}`",
        f"- Code-phase attempts: {args.attempts}",
    ])
    with connect() as conn, conn.cursor() as cur:
        row = fetch_exp(cur, args.id)
        handoff = handoff.replace("{slug}", row["slug"])
        move_status(
            cur, args.id, ("coding",), "ready", actor, note="code phase ok",
            extra_sets=(", handoff_md = %s, branch = %s, sha = %s, gpus = %s,"
                        " timeout_min = %s, code_log_url = %s, code_attempts = %s,"
                        " claimed_by = NULL, claimed_at = NULL"),
            extra_args=(handoff, args.branch, args.sha, args.gpus,
                        args.timeout_min, args.log_url, args.attempts),
        )
    print(f"{args.id}: coding -> ready")


def cmd_code_failed(args):
    actor = args.actor or actor_default()
    note_md = read_body(args.note_file) if args.note_file else ""
    with connect() as conn, conn.cursor() as cur:
        move_status(
            cur, args.id, ("coding",), "code_failed", actor,
            note="code phase exhausted in-phase retries; parked",
            extra_sets=", failure_md = %s, code_log_url = %s, code_attempts = %s,"
                       " last_error = %s, claimed_by = NULL, claimed_at = NULL",
            extra_args=(note_md, args.log_url, args.attempts,
                        args.error or "code phase failed"),
        )
    print(f"{args.id}: coding -> code_failed (parked; respin with a changed task)")


def cmd_executed(args):
    actor = args.actor or actor_default()
    report = read_body(args.report_file)
    with connect() as conn, conn.cursor() as cur:
        move_status(
            cur, args.id, ("executing",), "executed", actor,
            note=f"execution {args.exec_status}",
            extra_sets=", execution_report_md = %s, execution_status = %s,"
                       " claimed_by = NULL, claimed_at = NULL",
            extra_args=(report, args.exec_status),
        )
    print(f"{args.id}: executing -> executed ({args.exec_status})")


def cmd_analyze_failed(args):
    actor = args.actor or actor_default()
    with connect() as conn, conn.cursor() as cur:
        row = fetch_exp(cur, args.id)
        if row["status"] != "analyzing":
            die(f"{args.id} is {row['status']!r}, expected analyzing", 5)
        attempts = row["analyze_attempts"] + 1
        to_status = "analyze_stuck" if attempts >= ANALYZE_RETRY_CAP else "executed"
        note = ("analyze failed; parked after retry cap — GPU data intact on branch,"
                " requeue with `cbq requeue`" if to_status == "analyze_stuck"
                else "analyze failed; back to executed for one retry")
        move_status(
            cur, args.id, ("analyzing",), to_status, actor, note=note,
            extra_sets=", analyze_attempts = %s, last_error = %s,"
                       " claimed_by = NULL, claimed_at = NULL",
            extra_args=(attempts, args.error or f"analyze attempt {attempts} failed"),
        )
    print(f"{args.id}: analyzing -> {to_status} (analyze_attempts={attempts})")


def cmd_verdict(args):
    actor = args.actor or actor_default()
    review = read_body(args.review_file) if args.review_file else None
    root = _repo_root()

    if args.verdict == "blocked":
        if not args.constraint or not CONSTRAINT_ID_RE.match(args.constraint):
            die("blocked verdict requires --constraint C-NNN")
        if root is None:
            die("blocked verdict must run inside the www checkout (constraints.md is validated)")
        if not constraint_exists(root, args.constraint):
            die(f"{args.constraint} not found in {constraints_path(root)} — "
                "append the constraint bullet FIRST (paired write), then record the verdict")
    elif not args.review_file:
        die(f"{args.verdict} verdict requires --review-file")

    with connect() as conn, conn.cursor() as cur:
        row = fetch_exp(cur, args.id)
        if args.verdict == "blocked":
            move_status(
                cur, args.id, ("analyzing",), "blocked", actor,
                note=f"blocked on {args.constraint}",
                extra_sets=", blocked_on = %s, review_md = %s,"
                           " claimed_by = NULL, claimed_at = NULL",
                extra_args=(args.constraint, review),
            )
            print(f"{args.id}: analyzing -> blocked (blocked_on={args.constraint}; no archive record)")
            return

        verdict_val = "confirmed" if args.verdict == "done" else "falsified"
        cur.execute(
            "UPDATE cb_queue.experiments SET review_md = %s WHERE id = %s", (review, args.id)
        )
        move_status(
            cur, args.id, ("analyzing",), args.verdict, actor,
            note=f"verdict {verdict_val}",
            extra_sets=", verdict = %s, claimed_by = NULL, claimed_at = NULL",
            extra_args=(verdict_val,),
        )
        row = fetch_exp(cur, args.id)
        archive = render_markdown(row)
        cur.execute(
            "UPDATE cb_queue.experiments SET archive_md = %s WHERE id = %s",
            (archive, args.id),
        )
        row["archive_md"] = archive

    print(f"{args.id}: analyzing -> {args.verdict}")
    if args.commit:
        _export_row(row, commit=True)


def _export_row(row: dict, commit: bool):
    """Derived, idempotent archive export. The DB row is authoritative; this can
    be re-run any time (cbq export) if the git half of the dual write failed."""
    root = _repo_root()
    if root is None:
        print("warning: not in a git checkout — archive file not written "
              f"(run `cbq export {row['id']} --commit` from the www repo)", file=sys.stderr)
        return
    path = write_archive_file(row, root)
    print(f"archive written: {path.relative_to(root)}")
    if commit:
        to_add = [path]
        cpath = constraints_path(root)
        dirty = subprocess.run(
            ["git", "-C", str(root), "diff", "--name-only", "--", str(cpath)],
            capture_output=True, text=True,
        ).stdout.strip()
        if dirty:
            to_add.append(cpath)
        msg = f"analyze {row['id']}-{row['slug']}: {row['status']} [architect]"
        if git_commit_push(root, to_add, msg):
            print(f"committed + pushed: {msg}")
        else:
            print(f"warning: push failed — row is authoritative; retry with "
                  f"`cbq export {row['id']} --commit`", file=sys.stderr)


def cmd_export(args):
    with connect() as conn, conn.cursor() as cur:
        row = fetch_exp(cur, args.id)
    if row["status"] not in ("done", "falsified"):
        die(f"{args.id} is {row['status']!r}; only done/falsified rows export archive files", 5)
    if not row["archive_md"]:
        row["archive_md"] = render_markdown(row)
    _export_row(row, commit=args.commit)


def cmd_requeue(args):
    actor = args.actor or actor_default()
    default_to = {"code_failed": "enqueued", "analyze_stuck": "executed",
                  "blocked": "enqueued", "cancelled": "enqueued"}
    with connect() as conn, conn.cursor() as cur:
        row = fetch_exp(cur, args.id)
        if row["status"] not in default_to:
            die(f"{args.id} is {row['status']!r}; requeue applies to {list(default_to)}", 5)
        to_status = args.to or default_to[row["status"]]
        if to_status not in ("enqueued", "executed"):
            die("--to must be enqueued or executed")
        extra_sets = ", claimed_by = NULL, claimed_at = NULL, blocked_on = NULL"
        if to_status == "executed":
            extra_sets += ", analyze_attempts = 0"
        move_status(cur, args.id, (row["status"],), to_status, actor,
                    note=args.note or f"requeued from {row['status']}",
                    extra_sets=extra_sets)
    print(f"{args.id}: {row['status']} -> {to_status}")


def cmd_cancel(args):
    actor = args.actor or actor_default()
    with connect() as conn, conn.cursor() as cur:
        row = fetch_exp(cur, args.id)
        if row["status"] == "executing":
            die(f"{args.id} is 'executing' — use `cbq stop` to kill a running job; "
                f"`cancel` is the pre-launch veto for enqueued/ready rows", 5)
        move_status(cur, args.id, ("enqueued", "ready"), "cancelled", actor,
                    note=args.note or "cancelled")
    print(f"{args.id}: {row['status']} -> cancelled")


def cmd_stop(args):
    """Request that an executing experiment be killed. Sets stop_requested_at on
    the row (status stays 'executing'); the worker reconcile loop polls this and
    SIGTERMs the pid, then the dead-not-clean heal frees the slot in slots.json.
    The shift then finalizes via the normal escalated path. Idempotent — calling
    stop on a row that already has stop_requested_at refreshes the by/reason but
    keeps the original at-timestamp so retries don't reset the kill clock."""
    actor = args.actor or actor_default()
    reason = args.reason or "user-stop"
    with connect() as conn, conn.cursor() as cur:
        row = fetch_exp(cur, args.id)
        if row["status"] != "executing":
            die(f"{args.id} is {row['status']!r}; `stop` only works on executing rows "
                f"(use `cbq cancel` for enqueued/ready)", 5)
        cur.execute(
            "UPDATE cb_queue.experiments"
            "   SET stop_requested_at = COALESCE(stop_requested_at, now()),"
            "       stop_requested_by = %s,"
            "       stop_reason       = %s,"
            "       updated_at        = now()"
            " WHERE id = %s",
            (actor, reason, args.id),
        )
        record_transition(cur, args.id, row["status"], row["status"], actor,
                          note=f"stop requested: {reason}")
    print(f"{args.id}: stop requested by {actor} (reason={reason!r}) — "
          f"worker will SIGTERM within ~60s")


def cmd_stop_pending(args):
    """List IDs of executing rows with a pending stop request. The worker
    reconcile loop calls this every poll, scoped to its actor."""
    sql = ("SELECT id FROM cb_queue.experiments"
           " WHERE status = 'executing' AND stop_requested_at IS NOT NULL")
    params: list = []
    if args.claimed_by:
        sql += " AND claimed_by = %s"
        params.append(args.claimed_by)
    sql += " ORDER BY ord"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    if args.json:
        print(json.dumps([r["id"] for r in rows]))
    else:
        for r in rows:
            print(r["id"])


def cmd_show(args):
    with connect() as conn, conn.cursor() as cur:
        row = fetch_exp(cur, args.id)
    if args.field:
        val = row.get(args.field)
        if val is None:
            sys.exit(3)
        print(val if isinstance(val, str) else json.dumps(val, default=str))
        return
    if args.json:
        print(json.dumps(row, default=str, indent=2))
        return
    if args.markdown:
        sys.stdout.write(render_markdown(row))
        return
    print(f"{row['id']}-{row['slug']}  status={row['status']}"
          + (f" verdict={row['verdict']}" if row["verdict"] else "")
          + (f" blocked_on={row['blocked_on']}" if row["blocked_on"] else "")
          + (f" claimed_by={row['claimed_by']}" if row["claimed_by"] else ""))
    print(f"origin={row['origin']} parent={row['parent_id'] or '-'} "
          f"branch={row['branch'] or '-'} gpus={row['gpus'] or '-'} "
          f"timeout={row['timeout_min'] or '-'}m analyze_attempts={row['analyze_attempts']}")
    print(f"created={row['created_at']} updated={row['updated_at']}")


def cmd_list(args):
    cond, params = [], []
    if args.status:
        statuses = [s.strip() for s in args.status.split(",")]
        for s in statuses:
            if s not in ALL_STATUSES:
                die(f"unknown status {s!r}")
        cond.append("status = ANY(%s)")
        params.append(statuses)
    else:
        cond.append("status = ANY(%s)")
        params.append(list(LIVE_STATUSES + PARKED_STATUSES))
    if args.blocked_on:
        cond.append("blocked_on = %s")
        params.append(args.blocked_on)
    if args.claimed_by:
        cond.append("claimed_by = %s")
        params.append(args.claimed_by)
    if getattr(args, "kind", None):
        cond.append("kind = %s")
        params.append(args.kind)
    if getattr(args, "machine", None):
        cond.append("machine = %s")
        params.append(args.machine)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, slug, status, origin, kind, machine, gpus, timeout_min, blocked_on,"
            "       claimed_by, claimed_at, updated_at"
            " FROM cb_queue.experiments"
            f" WHERE {' AND '.join(cond)} ORDER BY ord",
            params,
        )
        rows = cur.fetchall()
    if args.json:
        print(json.dumps(rows, default=str, indent=2))
        return
    if not rows:
        print("(none)")
        return
    for r in rows:
        extras = []
        if r["machine"] != "a10":
            extras.append(f"machine={r['machine']}")
        if r["gpus"]:
            extras.append(f"gpus={r['gpus']}")
        if r["blocked_on"]:
            extras.append(f"blocked_on={r['blocked_on']}")
        if r["claimed_by"]:
            extras.append(f"claimed_by={r['claimed_by']}")
        print(f"{r['id']:>10}  {r['status']:<13} {r['slug']}"
              + ("  [" + " ".join(extras) + "]" if extras else ""))


def cmd_counts(args):
    cond, params = [], []
    if getattr(args, "kind", None):
        cond.append("kind = %s")
        params.append(args.kind)
    if getattr(args, "machine", None):
        cond.append("machine = %s")
        params.append(args.machine)
    where = f" WHERE {' AND '.join(cond)}" if cond else ""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT status, count(*) AS n FROM cb_queue.experiments{where} GROUP BY status",
            params,
        )
        counts = {r["status"]: r["n"] for r in cur.fetchall()}
    full = {s: counts.get(s, 0) for s in ALL_STATUSES}
    if args.json:
        print(json.dumps(full))
    else:
        for s in ALL_STATUSES:
            if full[s]:
                print(f"{s:>14}: {full[s]}")


def cmd_history(args):
    statuses = [s.strip() for s in (args.status or "done,falsified").split(",")]
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, slug, status, verdict, blocked_on FROM cb_queue.experiments"
            " WHERE status = ANY(%s) AND (task_md ~* %s OR coalesce(review_md,'') ~* %s"
            "   OR coalesce(execution_report_md,'') ~* %s OR coalesce(failure_md,'') ~* %s)"
            " ORDER BY ord",
            (statuses, args.grep, args.grep, args.grep, args.grep),
        )
        rows = cur.fetchall()
    for r in rows:
        tag = r["verdict"] or r["blocked_on"] or r["status"]
        print(f"{r['id']:>10}  {tag:<10} {r['slug']}")
    if not rows:
        sys.exit(3)


def cmd_log(args):
    with connect() as conn, conn.cursor() as cur:
        if args.since_min:
            cur.execute(
                "SELECT * FROM cb_queue.transitions"
                " WHERE created_at > now() - make_interval(mins => %s) ORDER BY seq",
                (args.since_min,),
            )
        else:
            cur.execute(
                "SELECT * FROM (SELECT * FROM cb_queue.transitions ORDER BY seq DESC LIMIT %s) t"
                " ORDER BY seq",
                (args.last,),
            )
        rows = cur.fetchall()
    if args.json:
        print(json.dumps(rows, default=str, indent=2))
        return
    for r in rows:
        print(f"{r['created_at']:%Y-%m-%d %H:%M:%S}  {r['experiment_id']:>10}  "
              f"{r['from_status'] or '·'} -> {r['to_status']}  [{r['actor']}]"
              + (f"  {r['note']}" if r["note"] else ""))


def cmd_exec_event(args):
    """Record one execution attempt event (launch/death/kill/finalize).

    Written by worker_watcher.sh ONLY — it diffs slots.json against reality
    every poll, so events need no cooperation from the Claude session. This is
    the pipeline's per-retry memory: each shift session is fresh, and without
    this table fix-round 10 looks identical to fix-round 1.
    """
    actor = args.actor or actor_default()
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cb_queue.exec_events"
            " (experiment_id, kind, pid, wall_sec, actor, note)"
            " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (args.id, args.kind, args.pid, args.wall_sec, actor, args.note),
        )
        seq = cur.fetchone()["id"]
        conn.commit()
    print(seq)


def cmd_exec_summary(args):
    """Attempt history for one experiment: counts, burn, last failure tail."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FILTER (WHERE kind = 'launch')                      AS launches,"
            "       count(*) FILTER (WHERE kind IN ('death','stall_kill','timeout_kill')) AS deaths,"
            "       count(*) FILTER (WHERE kind IN ('death','stall_kill','timeout_kill')"
            "                          AND at > now() - interval '1 hour')       AS deaths_1h,"
            "       coalesce(sum(wall_sec), 0)                                   AS wall_sec_total,"
            "       max(at)                                                      AS last_event_at"
            " FROM cb_queue.exec_events WHERE experiment_id = %s",
            (args.id,),
        )
        s = cur.fetchone()
        cur.execute(
            "SELECT kind, note FROM cb_queue.exec_events"
            " WHERE experiment_id = %s AND kind <> 'launch' AND note IS NOT NULL"
            " ORDER BY id DESC LIMIT 1",
            (args.id,),
        )
        last = cur.fetchone()
    s["last_failure_kind"] = last["kind"] if last else None
    s["last_failure_note"] = last["note"] if last else None
    if args.json:
        print(json.dumps(s, default=str))
        return
    print(
        f"{args.id}: launches={s['launches']} deaths={s['deaths']}"
        f" deaths_1h={s['deaths_1h']} wall_min_total={s['wall_sec_total'] // 60}"
        + (f"\n  last failure [{s['last_failure_kind']}]: {s['last_failure_note'][:300]}"
           if s["last_failure_note"] else "")
    )


def cmd_mark_smoked(args):
    """Record the git SHA whose code passed --smoke for this row, so the worker
    can skip re-smoking unchanged code on relaunch. Idempotent; last write wins."""
    actor = args.actor or actor_default()
    with connect() as conn, conn.cursor() as cur:
        row = fetch_exp(cur, args.id)
        cur.execute(
            "UPDATE cb_queue.experiments SET smoked_sha = %s, updated_at = now() WHERE id = %s",
            (args.sha, args.id),
        )
        record_transition(cur, args.id, row["status"], row["status"], actor,
                          f"smoked sha={args.sha[:12]}")
    print(f"{args.id}: smoked_sha = {args.sha}")


def cmd_status(args):
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT status, count(*) AS n FROM cb_queue.experiments GROUP BY status")
        counts = {r["status"]: r["n"] for r in cur.fetchall()}
        cur.execute(
            "SELECT id, slug, status, claimed_by, claimed_at FROM cb_queue.experiments"
            " WHERE status = ANY(%s) ORDER BY ord",
            (list(LIVE_STATUSES + PARKED_STATUSES),),
        )
        live = cur.fetchall()
    print("=== counts ===")
    for s in ALL_STATUSES:
        if counts.get(s):
            print(f"  {s:>14}: {counts[s]}")
    print("=== live + parked ===")
    if not live:
        print("  (queue empty)")
    for r in live:
        claim = f"  claimed_by={r['claimed_by']} at {r['claimed_at']:%H:%M:%S}" if r["claimed_by"] else ""
        print(f"  {r['id']:>10}  {r['status']:<13} {r['slug']}{claim}")


# ---------------------------------------------------------------------- backfill


def _classify_failed(body: str) -> tuple[str, str]:
    """-> (status, note) for a legacy failed/ file, per its auto-appended footer."""
    if "## Analyze-phase failure" in body:
        return "executed", "backfilled from failed/ (analyze-stuck; GPU data intact on branch)"
    if "## Code-phase failure summary" in body:
        return "code_failed", "backfilled from failed/ (code-phase failure)"
    if "## Failure summary (auto-appended by worker)" in body:
        return "code_failed", "backfilled from failed/ (legacy single-box runner failure)"
    return "code_failed", "backfilled from failed/ (unclassified footer)"


def _parse_handoff(body: str) -> dict:
    out = {}
    m = re.search(r"- Branch: \[`([^`]+)`\][^@]*@ `([0-9a-f]+)`", body)
    if m:
        out["branch"], out["sha"] = m.group(1), m.group(2)
    m = re.search(r"- GPUs required: (\d+) · timeout: (\d+)", body)
    if m:
        out["gpus"], out["timeout_min"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"- Code-phase log \(S3\): `([^`]+)`", body)
    if m:
        out["code_log_url"] = m.group(1)
    return out


def cmd_backfill(args):
    root = Path(args.experiments_dir).resolve() if args.experiments_dir else None
    if root is None:
        repo = _repo_root()
        if repo is None:
            die("--experiments-dir required outside a checkout")
        root = repo / EXPERIMENTS_DIR_REL
    if not root.is_dir():
        die(f"no such dir: {root}")
    actor = args.actor or actor_default()

    plan = []  # (id, slug, status, origin, fields-dict, note)
    dir_status = {"enqueued": "enqueued", "ready": "ready", "executed": "executed",
                  "done": "done"}
    for sub, status in dir_status.items():
        for f in sorted((root / sub).glob("*.task")):
            m = re.match(r"^(\d+(?:\.\d+)*)-(.+)\.task$", f.name)
            if not m:
                print(f"skip (no id prefix): {sub}/{f.name}")
                continue
            exp_id, slug = m.group(1), m.group(2)
            body = f.read_text()
            fields = {"task_md": body}
            if status in ("ready", "executed", "done"):
                fields.update(_parse_handoff(body))
                fields.setdefault("branch", f"exp/{exp_id}-{slug}")
            if status == "done":
                fields["verdict"] = "confirmed"
                fields["archive_md"] = body
            if status == "executed":
                fields["execution_status"] = (
                    "escalated" if re.search(r"- Status:\s*escalated", body) else "completed"
                )
            plan.append((exp_id, slug, status, fields,
                         f"backfilled from {sub}/"))
    for f in sorted((root / "failed").glob("*.task")):
        m = re.match(r"^(\d+(?:\.\d+)*)-(.+)\.task$", f.name)
        if not m:
            continue
        exp_id, slug = m.group(1), m.group(2)
        body = f.read_text()
        status, note = _classify_failed(body)
        fields = {"task_md": body, "failure_md": body if status == "code_failed" else None,
                  "branch": f"exp/{exp_id}-{slug}"}
        plan.append((exp_id, slug, status, fields, note))

    # seed counter from everything visible: rows-to-insert + exp/ branches
    max_top = max([parse_ord(p[0])[0] for p in plan], default=0)
    repo = _repo_root()
    if repo:
        ls = subprocess.run(["git", "-C", str(repo), "ls-remote", "--heads", "origin", "exp/*"],
                            capture_output=True, text=True)
        for mm in re.finditer(r"refs/heads/exp/(\d+)", ls.stdout):
            max_top = max(max_top, int(mm.group(1)))

    if args.dry_run:
        for exp_id, slug, status, _, note in plan:
            print(f"would insert {exp_id}-{slug} as {status}  ({note})")
        print(f"would seed id_counter.top = {max_top}")
        return

    inserted = skipped = 0
    with connect() as conn, conn.cursor() as cur:
        for exp_id, slug, status, fields, note in plan:
            cols = {k: v for k, v in fields.items() if v is not None}
            col_names = ", ".join(cols)
            placeholders = ", ".join(["%s"] * len(cols))
            cur.execute(
                f"INSERT INTO cb_queue.experiments (id, ord, slug, status, origin, parent_id, {col_names})"
                f" VALUES (%s, %s, %s, %s, 'backfill', %s, {placeholders})"
                " ON CONFLICT (id) DO NOTHING RETURNING id",
                (exp_id, parse_ord(exp_id), slug, status,
                 ".".join(exp_id.split(".")[:-1]) if "." in exp_id else None,
                 *cols.values()),
            )
            if cur.fetchone():
                record_transition(cur, exp_id, None, status, actor, note)
                inserted += 1
            else:
                print(f"warning: {exp_id} already present — skipped {exp_id}-{slug} "
                      "(id collision or re-run; the git archive file remains the record)",
                      file=sys.stderr)
                skipped += 1
        cur.execute("UPDATE cb_queue.id_counter SET top = GREATEST(top, %s)", (max_top,))
    print(f"backfill: {inserted} inserted, {skipped} already present, "
          f"id_counter.top >= {max_top}")


# -------------------------------------------------------- box ssh conveniences


def cmd_remote(args):
    remote = os.environ.get("CBQ_REMOTE", "worker-benchmark")
    if args.remote_cmd == "tail":
        pipe = ("tail -f $(ls -t ~/worker/logs/*.jsonl 2>/dev/null | head -1)")
        os.execvp("bash", ["bash", "-c",
                  f"ssh {remote} '{pipe}' | jq -rc 'select(.type==\"assistant\") "
                  f"| .message.content[]? | select(.type==\"text\") | .text'"])
    elif args.remote_cmd == "attach":
        os.execvp("ssh", ["ssh", "-t", remote, "tmux attach -t worker"])
    elif args.remote_cmd == "logs":
        os.execvp("ssh", ["ssh", remote, "ls -lht ~/worker/logs/ | head -30"])


# ------------------------------------------------------------------------- main


def main(argv=None):
    p = argparse.ArgumentParser(prog="cbq", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_actor(sp):
        sp.add_argument("--actor", help="who is acting (default: $CBQ_ACTOR or user@host)")

    sp = sub.add_parser("init-db", help="apply schema.sql")
    sp.set_defaults(fn=cmd_init_db)

    sp = sub.add_parser("submit", help="enqueue a task")
    sp.add_argument("file", help="task markdown file, or - for stdin")
    sp.add_argument("--parent", help="allocate a sub-id under this experiment")
    sp.add_argument("--id", help="force a specific id")
    sp.add_argument("--slug", help="override slug (required with stdin)")
    sp.add_argument("--origin", default="user", choices=["user", "auto-suggest", "respin"])
    sp.add_argument("--kind", default=None, choices=["research", "loadtest", "finetune"],
                    help="worker kind (each kind maps to one GPU worker: worker-benchmark/worker-loadtest/worker-finetune);"
                         " default: inherit the parent's kind when --parent is given, else research")
    sp.add_argument("--machine", choices=MACHINES,
                    help="GPU box class; default: the task body's `machine:` key, else a10")
    add_actor(sp)
    sp.set_defaults(fn=cmd_submit)

    sp = sub.add_parser("claim", help="atomically claim the next task for a phase")
    sp.add_argument("phase", choices=list(CLAIM_PHASES))
    sp.add_argument("--id", help="claim this specific id instead of the lowest")
    sp.add_argument("--kind", choices=KINDS,
                    help="only claim tasks of this kind (a worker claims its own kind)")
    sp.add_argument("--kind-not-in", default=None,
                    help="exclude these kinds (comma-separated, e.g. swe-bench,swe-bench-sweep)")
    sp.add_argument("--machine", choices=MACHINES,
                    help="only claim tasks for this box class (a worker claims its own machine)")
    sp.add_argument("--max-gpus", type=int, help="(execute) only claim specs needing <= N gpus")
    add_actor(sp)
    sp.set_defaults(fn=cmd_claim)

    sp = sub.add_parser("lease", help="acquire/refresh a lease on a shared resource (clean-room)")
    sp.add_argument("resource", help="resource name, e.g. 'node'")
    sp.add_argument("--mode", default="exclusive", choices=["exclusive", "shared"])
    sp.add_argument("--reason", help="why (shown to peers + in Slack)")
    sp.add_argument("--ttl-min", type=int, help="safety TTL in minutes (auto-expire); default none")
    add_actor(sp)
    sp.set_defaults(fn=cmd_lease)

    sp = sub.add_parser("release", help="release a lease you hold")
    sp.add_argument("resource")
    sp.add_argument("--force", action="store_true", help="release even if held by someone else")
    add_actor(sp)
    sp.set_defaults(fn=cmd_release)

    sp = sub.add_parser("lease-active",
                        help="print holder of an EXCLUSIVE lease (other than self), else nothing")
    sp.add_argument("resource")
    add_actor(sp)
    sp.set_defaults(fn=cmd_lease_active)

    sp = sub.add_parser("leases", help="list active leases")
    sp.set_defaults(fn=cmd_leases)

    sp = sub.add_parser("unclaim", help="return a claimed task to its queue")
    sp.add_argument("id")
    sp.add_argument("--note")
    add_actor(sp)
    sp.set_defaults(fn=cmd_unclaim)

    sp = sub.add_parser("touch", help="refresh a held claim's clock (per-attempt heartbeat)")
    sp.add_argument("id")
    sp.add_argument("--quiet", action="store_true")
    add_actor(sp)
    sp.set_defaults(fn=cmd_touch)

    sp = sub.add_parser("heartbeat", help="upsert this worker's liveness row (worker watchdog subloop)")
    sp.add_argument("--kind", choices=KINDS,
                    help="worker kind; omit for a legacy any-kind worker")
    sp.add_argument("--machine", default="a10", choices=MACHINES,
                    help="hardware class this worker runs on (sizes auto-suggested specs)")
    sp.add_argument("--gpus", help="WORKER_GPUS csv, informational")
    add_actor(sp)
    sp.set_defaults(fn=cmd_heartbeat)

    sp = sub.add_parser("workers", help="list worker heartbeats (live kinds)")
    sp.add_argument("--active-min", type=int,
                    help="only workers seen within the last N minutes")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_workers)

    sp = sub.add_parser("reap", help="return stale claims (crashed sessions) to their queues")
    # Policy: 30 min PER attempt, up to 3 coding attempts.
    # The wrappers enforce 30 min/attempt via `timeout` (ATTEMPT_TIMEOUT_SEC) AND
    # call `cbq touch` at the start of every attempt, refreshing claimed_at. So a
    # live multi-attempt wrapper keeps its clock fresh and these windows only need
    # to cover ONE attempt + margin (30 min cap + kill-grace + setup) — they are
    # pure ORPHAN cleanup (dead wrapper, no touch). Independent of MAX_ATTEMPTS.
    sp.add_argument("--coding-min", type=int, default=40)
    sp.add_argument("--analyzing-min", type=int, default=40)
    sp.add_argument("--executing-min", type=int, default=0,
                    help="also reap stale executing claims (default: off; slots.json owns these)")
    sp.add_argument("--quiet", action="store_true")
    add_actor(sp)
    sp.set_defaults(fn=cmd_reap)

    sp = sub.add_parser("ready", help="code phase done: record handoff, mark ready")
    sp.add_argument("id")
    sp.add_argument("--branch", required=True)
    sp.add_argument("--sha", required=True)
    sp.add_argument("--gpus", type=int, default=1)
    sp.add_argument("--timeout-min", type=int, required=True)
    sp.add_argument("--log-url", required=True)
    sp.add_argument("--attempts", type=int, required=True)
    add_actor(sp)
    sp.set_defaults(fn=cmd_ready)

    sp = sub.add_parser("code-failed", help="park a task whose code phase exhausted retries")
    sp.add_argument("id")
    sp.add_argument("--note-file", help="failure record markdown")
    sp.add_argument("--log-url")
    sp.add_argument("--attempts", type=int)
    sp.add_argument("--error")
    add_actor(sp)
    sp.set_defaults(fn=cmd_code_failed)

    sp = sub.add_parser("executed", help="worker finalize: store execution report")
    sp.add_argument("id")
    sp.add_argument("--exec-status", required=True, choices=["completed", "escalated"])
    sp.add_argument("--report-file", required=True)
    add_actor(sp)
    sp.set_defaults(fn=cmd_executed)

    sp = sub.add_parser("analyze-failed", help="analyze wrapper failed: retry once, then park")
    sp.add_argument("id")
    sp.add_argument("--error")
    add_actor(sp)
    sp.set_defaults(fn=cmd_analyze_failed)

    sp = sub.add_parser("verdict", help="record the scientific verdict")
    sp.add_argument("id")
    sp.add_argument("verdict", choices=["done", "falsified", "blocked"])
    sp.add_argument("--review-file", help="'## Architect review' markdown (required for done/falsified)")
    sp.add_argument("--constraint", help="C-NNN id in constraints.md (required for blocked)")
    sp.add_argument("--commit", action="store_true",
                    help="write the archive file and commit+push (with constraints.md if dirty)")
    add_actor(sp)
    sp.set_defaults(fn=cmd_verdict)

    sp = sub.add_parser("export", help="re-render the archive file for a done/falsified row")
    sp.add_argument("id")
    sp.add_argument("--commit", action="store_true")
    sp.set_defaults(fn=cmd_export)

    sp = sub.add_parser("requeue", help="resurrect a parked/blocked task")
    sp.add_argument("id")
    sp.add_argument("--to", choices=["enqueued", "executed"])
    sp.add_argument("--note")
    add_actor(sp)
    sp.set_defaults(fn=cmd_requeue)

    sp = sub.add_parser("cancel", help="cancel an enqueued or ready task (auto-suggest veto; a cancelled ready task can be requeued to re-enter the code phase). Use `stop` for executing rows.")
    sp.add_argument("id")
    sp.add_argument("--note")
    add_actor(sp)
    sp.set_defaults(fn=cmd_cancel)

    sp = sub.add_parser("stop", help="request kill of an executing job (worker SIGTERMs within ~60s; row stays 'executing' until shift finalizes via escalated)")
    sp.add_argument("id")
    sp.add_argument("--reason", help="free-form text recorded as stop_reason")
    add_actor(sp)
    sp.set_defaults(fn=cmd_stop)

    sp = sub.add_parser("stop-pending", help="list ids of executing rows with a pending stop request (worker reconcile loop)")
    sp.add_argument("--claimed-by", help="scope to a single worker actor")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_stop_pending)

    sp = sub.add_parser("show", help="show one experiment")
    sp.add_argument("id")
    sp.add_argument("--field", help="print a single column")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--markdown", action="store_true",
                    help="render task+handoff+report+review as one document")
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("list", help="list experiments (default: live + parked)")
    sp.add_argument("--status", help="csv of statuses")
    sp.add_argument("--blocked-on", help="filter by constraint id (C-NNN)")
    sp.add_argument("--claimed-by")
    sp.add_argument("--kind", choices=["research", "loadtest", "finetune"],
                    help="filter by worker kind")
    sp.add_argument("--machine", choices=MACHINES, help="filter by box class")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("counts", help="per-status counts")
    sp.add_argument("--kind", choices=["research", "loadtest", "finetune"],
                    help="count only this kind")
    sp.add_argument("--machine", choices=MACHINES, help="count only this box class")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_counts)

    sp = sub.add_parser("history", help="regex-search archived + parked records")
    sp.add_argument("--grep", required=True)
    sp.add_argument("--status", help="csv (default done,falsified)")
    sp.set_defaults(fn=cmd_history)

    sp = sub.add_parser("log", help="recent transitions")
    sp.add_argument("--last", type=int, default=20)
    sp.add_argument("--since-min", type=int)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_log)

    sp = sub.add_parser("exec-event", help="record an execution attempt event (watcher-only writer)")
    sp.add_argument("id")
    sp.add_argument("--kind", required=True,
                    choices=["launch", "death", "stall_kill", "timeout_kill", "finalize"])
    sp.add_argument("--pid", type=int)
    sp.add_argument("--wall-sec", type=int)
    sp.add_argument("--note")
    add_actor(sp)
    sp.set_defaults(fn=cmd_exec_event)

    sp = sub.add_parser("exec-summary", help="attempt history for one experiment")
    sp.add_argument("id")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_exec_summary)

    sp = sub.add_parser("mark-smoked", help="record the SHA whose code passed --smoke (skip-smoke memo)")
    sp.add_argument("id")
    sp.add_argument("--sha", required=True, help="git commit SHA that passed smoke")
    add_actor(sp)
    sp.set_defaults(fn=cmd_mark_smoked)

    sp = sub.add_parser("status", help="human overview")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("backfill", help="import legacy queue-dir files into the DB")
    sp.add_argument("--experiments-dir", help="default: <repo>/ml/eval/experiments")
    sp.add_argument("--dry-run", action="store_true")
    add_actor(sp)
    sp.set_defaults(fn=cmd_backfill)

    for rc in ("tail", "attach", "logs"):
        sp = sub.add_parser(rc, help=f"ssh convenience: {rc} on $CBQ_REMOTE")
        sp.set_defaults(fn=cmd_remote, remote_cmd=rc)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
