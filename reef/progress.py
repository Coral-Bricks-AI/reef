# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``reef.progress`` -- line-buffered JSONL progress for long-running runs.

Convention: any reef harness whose tool dispatches or eval loops run for
more than ~2 min should write line-buffered progress events to a known
file path so the orchestrator (a parent watcher, a sibling Claude
session, a human running ``tail -f``) can read incremental progress
without blocking on the subprocess's exit.

Without this, an orchestrator launches ``python run.py`` synchronously,
waits N minutes for the subprocess to finish, and cannot tell whether
the run is making progress, stalled on a CUDA hang, or quietly
degrading. With it, the pattern becomes::

    nohup python -u run.py > /tmp/run.stdout 2>&1 &
    EVAL_PID=$!
    while kill -0 "$EVAL_PID" 2>/dev/null; do
      sleep 60
      tail -3 results/<id>/progress.log
    done
    wait "$EVAL_PID"; RC=$?

If three consecutive 60s polls show no new lines in ``progress.log`` the
run has stalled -- kill ``EVAL_PID``, read ``/tmp/run.stdout``, diagnose,
retry. Do NOT just wait for it to magically resume.

Usage::

    from reef.progress import ProgressLog

    prog = ProgressLog("results/0042-foo/progress.log")
    prog.event("setup", "model loading")
    model = load_model(...)
    prog.event("setup", "model loaded", vram_gb=4.2)

    with prog.phase("dense", total=30) as p:
        for i, sample in enumerate(samples):
            result = run_sample(model, sample)
            p.tick(sample_idx=i, correct=result.correct, ttft_ms=result.ttft_ms)

The output is one JSON event per line, line-buffered::

    {"t":"2026-06-09T22:15:33Z","phase":"setup","msg":"model loaded","vram_gb":4.2}
    {"t":"2026-06-09T22:15:45Z","phase":"dense","step":1,"total":30,"sample_idx":0,...}
    ...
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional


class ProgressLog:
    """Line-buffered progress event writer. Safe to instantiate at the top
    of a ``run.py``; the process exit flushes everything."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # buffering=1 -> line-buffered; orchestrator's tail -f sees each
        # event as it lands, not on flush
        self._f = open(self.path, "a", buffering=1)
        self.event("init", "ProgressLog opened", pid=os.getpid(), path=str(self.path))

    def _emit(self, **fields: Any) -> None:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        rec = {"t": ts, **fields}
        self._f.write(json.dumps(rec, default=str) + "\n")

    def log(self, fields: dict) -> None:
        """Accept a raw dict and emit it directly."""
        self._emit(**fields)

    def event(self, phase: str, msg: str, **kv: Any) -> None:
        """Discrete event, not tied to a step counter. Use for setup,
        teardown, milestones, errors."""
        self._emit(phase=phase, msg=msg, **kv)

    @contextmanager
    def phase(self, name: str, total: Optional[int] = None) -> Iterator["PhaseTicker"]:
        """Wrap a phase (eval loop, batch processing). Emits ``phase_start``
        + ``phase_end`` and gives you a ticker for per-step events."""
        self._emit(phase=name, msg="phase_start", total=total)
        t0 = time.time()
        ticker = PhaseTicker(self, name, total)
        try:
            yield ticker
        except Exception as e:
            self._emit(phase=name, msg="phase_error",
                       error_type=type(e).__name__, error=str(e)[:500],
                       elapsed_s=round(time.time() - t0, 2),
                       step=ticker.step, total=total)
            raise
        else:
            self._emit(phase=name, msg="phase_end",
                       elapsed_s=round(time.time() - t0, 2),
                       step=ticker.step, total=total)


class PhaseTicker:
    """Per-step progress emitter handed back from :meth:`ProgressLog.phase`."""

    def __init__(self, log: ProgressLog, name: str, total: Optional[int]):
        self._log = log
        self.name = name
        self.total = total
        self.step = 0

    def tick(self, **fields: Any) -> None:
        """Emit one progress event. ``step`` auto-increments. Pass any
        per-step fields as kwargs (``sample_idx``, ``correct``,
        ``latency_ms``, etc.)."""
        self.step += 1
        fields.pop("step", None)   # caller must not override the auto-increment counter
        fields.pop("total", None)
        self._log._emit(phase=self.name, step=self.step, total=self.total, **fields)


__all__ = ["ProgressLog", "PhaseTicker"]
