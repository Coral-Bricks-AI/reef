-- cb_queue: Postgres-backed experiment queue (replaces git-as-message-bus).
-- Applied by `cbq init-db`. Idempotent. All state lives in the cb_queue
-- schema so this can share a database with anything else safely.
--
-- Status machine (one owner per failure mode — see ml/eval/experiments/README.md):
--   enqueued -> coding -> ready -> executing -> executed -> analyzing
--   analyzing -> done | falsified | blocked            (scientific verdicts)
--   coding    -> code_failed                           (parked; human respins)
--   analyzing -> executed (retry) | analyze_stuck      (parked after retry cap)
--   enqueued  -> cancelled                             (veto window)
-- Archive files (done/, falsified/) are derived exports; THESE ROWS are
-- authoritative.

CREATE SCHEMA IF NOT EXISTS cb_queue;

CREATE TABLE IF NOT EXISTS cb_queue.experiments (
  id                  text PRIMARY KEY,          -- '0054' or '0050.2' (dotted sub-ids)
  ord                 integer[] NOT NULL,        -- id split on '.', for natural ordering
  slug                text NOT NULL,
  status              text NOT NULL DEFAULT 'enqueued' CHECK (status IN (
                        'enqueued','coding','ready','executing','executed','analyzing',
                        'done','falsified','blocked',
                        'code_failed','analyze_stuck','cancelled')),
  origin              text NOT NULL DEFAULT 'user'
                        CHECK (origin IN ('user','auto-suggest','respin','backfill')),
  -- Worker kind — THE routing dimension. Each kind maps to exactly one GPU
  -- worker, so multiple worker types share one queue without stepping on
  -- each other:
  --   research  -> worker-benchmark (in-process model evals; the original kind)
  --   loadtest  -> worker-loadtest (drives a running inference server; throughput sweeps)
  --   finetune  -> worker-finetune  (training runs)
  -- Workers claim only their own kind (`cbq claim execute --kind <k>`).
  kind                text NOT NULL DEFAULT 'research'
                        CHECK (kind IN ('research','loadtest','finetune')),
  -- Hardware class the spec is sized for (a10 1x A10G 23GB | h100 4x H100
  -- 80GB). An attribute, NOT a routing dimension: the architect stamps it
  -- from the kind's worker heartbeat so the design fits the box that will
  -- run it; claims filter by kind alone.
  machine             text NOT NULL DEFAULT 'a10'
                        CHECK (machine IN ('a10','h100')),
  parent_id           text,

  task_md             text NOT NULL,             -- original task as submitted
  handoff_md          text,                      -- '## Handoff' section (code phase output)
  execution_report_md text,                      -- '## Execution report (worker)' section
  review_md           text,                      -- '## Architect review' (+ '## Line closed')
  archive_md          text,                      -- rendered archive file (done/falsified only)

  branch              text,
  sha                 text,
  gpus                integer,
  timeout_min         integer,
  code_attempts       integer,
  code_log_url        text,
  -- Smoke memo: the git SHA whose code last passed --smoke on this row's
  -- worker box. The worker skips the (expensive, model-loading) smoke when the
  -- worktree HEAD equals this — so a crash-relaunch of unchanged code goes
  -- straight to launch, and a code fix (new SHA) still re-smokes. Set by
  -- `cbq mark-smoked`; never cleared (a stale SHA simply won't match).
  smoked_sha          text,
  execution_status    text CHECK (execution_status IN ('completed','escalated') OR execution_status IS NULL),
  analyze_attempts    integer NOT NULL DEFAULT 0,

  verdict             text CHECK (verdict IN ('confirmed','falsified') OR verdict IS NULL),
  blocked_on          text,                      -- C-NNN id in constraints.md (status=blocked)
  failure_md          text,                      -- code-phase failure record (status=code_failed)
  last_error          text,

  claimed_by          text,
  claimed_at          timestamptz,

  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS experiments_status_ord ON cb_queue.experiments (status, ord);
CREATE INDEX IF NOT EXISTS experiments_parent     ON cb_queue.experiments (parent_id);
CREATE INDEX IF NOT EXISTS experiments_blocked_on ON cb_queue.experiments (blocked_on)
  WHERE blocked_on IS NOT NULL;

CREATE TABLE IF NOT EXISTS cb_queue.transitions (
  seq           bigserial PRIMARY KEY,
  experiment_id text NOT NULL REFERENCES cb_queue.experiments(id),
  from_status   text,
  to_status     text NOT NULL,
  actor         text NOT NULL,
  note          text,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS transitions_exp  ON cb_queue.transitions (experiment_id, seq);
CREATE INDEX IF NOT EXISTS transitions_time ON cb_queue.transitions (created_at);

-- Top-level ID counter. Seeded by backfill to max(existing); submit bumps it.
-- A table (not a sequence) so backfill can CAS it upward idempotently.
CREATE TABLE IF NOT EXISTS cb_queue.id_counter (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  top       integer NOT NULL DEFAULT 0
);
INSERT INTO cb_queue.id_counter (singleton, top) VALUES (true, 0)
  ON CONFLICT (singleton) DO NOTHING;

-- ── Migrations for already-initialized DBs ────────────────────────────────
-- CREATE TABLE IF NOT EXISTS above only builds the table on a fresh DB; these
-- idempotent ALTERs bring an existing cb_queue up to the current shape so
-- `cbq init-db` is safe to re-run on a live database.
ALTER TABLE cb_queue.experiments
  ADD COLUMN IF NOT EXISTS smoked_sha text;
ALTER TABLE cb_queue.experiments
  ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'research';
DO $$ BEGIN
  ALTER TABLE cb_queue.experiments
    ADD CONSTRAINT experiments_kind_chk CHECK (kind IN ('research','loadtest','finetune'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
CREATE INDEX IF NOT EXISTS experiments_kind_status_ord
  ON cb_queue.experiments (kind, status, ord);
ALTER TABLE cb_queue.experiments
  ADD COLUMN IF NOT EXISTS machine text NOT NULL DEFAULT 'a10';
DO $$ BEGIN
  ALTER TABLE cb_queue.experiments
    ADD CONSTRAINT experiments_machine_chk CHECK (machine IN ('a10','h100'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
CREATE INDEX IF NOT EXISTS experiments_machine_status_ord
  ON cb_queue.experiments (machine, status, ord);

-- ── Stop-intent on executing rows ─────────────────────────────────────────────
-- `cancel` is the pre-launch veto (enqueued|ready -> cancelled). For an already-
-- running experiment that needs to be killed, the user sets stop_requested_at
-- via `cbq stop`. The worker reconcile loop polls executing rows it owns whose
-- stop_requested_at IS NOT NULL, SIGTERMs the pid, then the dead-not-clean heal
-- frees the slot. We DELIBERATELY do not transition status here: the row stays
-- 'executing' until the shift finalizes it via the normal escalated path, so
-- the attempt history (cbq exec-summary) and exec_events tail stay consistent
-- with how every other crash is recorded.
ALTER TABLE cb_queue.experiments
  ADD COLUMN IF NOT EXISTS stop_requested_at timestamptz;
ALTER TABLE cb_queue.experiments
  ADD COLUMN IF NOT EXISTS stop_requested_by text;
ALTER TABLE cb_queue.experiments
  ADD COLUMN IF NOT EXISTS stop_reason       text;
CREATE INDEX IF NOT EXISTS experiments_stop_pending
  ON cb_queue.experiments (claimed_by)
  WHERE stop_requested_at IS NOT NULL AND status = 'executing';

-- ── Execution attempt events: per-retry state for the GPU worker ───────────
-- Written ONLY by worker_watcher.sh (deterministic bash), never by Claude
-- sessions. The watcher records a 'launch' when a new pid appears in
-- slots.json and a 'death' (with wall_sec + run.out tail) when it disappears.
-- Read by the shift snapshot (cbq exec-summary) so a fresh session knows it
-- is fix-round N, not fix-round 1, and by the watcher's crash-loop debounce.
-- Append-only: attempt count, cumulative burn, and the failure-signature
-- timeline are all derivable; no read-modify-write races.
CREATE TABLE IF NOT EXISTS cb_queue.exec_events (
  id            bigserial PRIMARY KEY,
  experiment_id text NOT NULL,           -- no FK: events may outlive row edits
  at            timestamptz NOT NULL DEFAULT now(),
  kind          text NOT NULL CHECK (kind IN
                  ('launch','death','stall_kill','timeout_kill','finalize')),
  pid           integer,
  wall_sec      integer,                 -- death/kill: lifetime of that attempt
  actor         text,
  note          text                     -- e.g. tail of /tmp/run-<job>.out
);
CREATE INDEX IF NOT EXISTS exec_events_exp  ON cb_queue.exec_events (experiment_id, id);
CREATE INDEX IF NOT EXISTS exec_events_time ON cb_queue.exec_events (at);

-- ── Worker heartbeats: which kinds have a live worker, and on what box ─────
-- worker_watcher.sh upserts its row from a background subloop (~60s; not the
-- poll loop, which blocks for whole synchronous shifts). Each kind maps to
-- one GPU worker; the architect's generator authors new experiments ONLY for
-- kinds a worker is actually serving — a kind whose box is stopped gets no
-- auto-suggests, so the queue never fills with specs nothing can execute.
-- `machine` is the hardware class the worker currently runs on; the architect
-- sizes new specs for it. A row is "active" when last_seen is recent
-- (cbq workers --active-min); stale rows are just ignored, never deleted, so
-- they double as an inventory of known workers.
CREATE TABLE IF NOT EXISTS cb_queue.workers (
  actor      text PRIMARY KEY,            -- CBQ_ACTOR, e.g. 'worker:worker-benchmark'
  kind       text                          -- NULL = any-kind (legacy worker)
               CHECK (kind IS NULL OR kind IN ('research','loadtest','finetune')),
  machine    text NOT NULL
               CHECK (machine IN ('a10','h100')),
  gpus       text,                         -- WORKER_GPUS csv, informational
  last_seen  timestamptz NOT NULL DEFAULT now()
);

-- ── Node leases: clean-room coordination on a shared GPU box ───────────────
-- GPU/control/file isolation stops *forbidden* interference; it cannot stop
-- *physics* interference (shared NVLink/NVSwitch fabric + host CPU/RAM/NIC).
-- For trustworthy headline numbers a benchmark needs the node quiet. A lease
-- is a cooperative signal: a holder of an 'exclusive' lease asks peers to
-- DRAIN (stop launching new jobs) until it releases. Workers check
-- `cbq lease-active` before launching and honor an exclusive hold.
CREATE TABLE IF NOT EXISTS cb_queue.leases (
  resource    text PRIMARY KEY,          -- e.g. 'node' (the shared 8xH100 box)
  mode        text NOT NULL CHECK (mode IN ('exclusive','shared')),
  holder      text NOT NULL,             -- actor holding it
  reason      text,
  acquired_at timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz                -- safety TTL; NULL = until released
);
