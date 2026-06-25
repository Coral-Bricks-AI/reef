"""
nvidia-smi sampler.

Polls util / mem / power at 1Hz in a background thread, returns aggregate
stats on `.summary()`. Use as a context manager around the eval region.

    with GpuMonitor() as mon:
        run_eval(...)
    print(mon.summary())   # {"util_pct_mean": 78, "vram_used_gb_max": 21.2, ...}

Or manually:
    mon = GpuMonitor().start()
    ...
    mon.stop()
    summary = mon.summary()
"""
from __future__ import annotations

import subprocess
import threading
from typing import List


class GpuMonitor:
    def __init__(self, gpu_idx: int = 0, sample_hz: float = 1.0):
        self.gpu_idx = gpu_idx
        self.interval_s = 1.0 / sample_hz
        self.records: List[dict] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    # context-manager sugar
    def __enter__(self):
        return self.start()
    def __exit__(self, *exc):
        self.stop()

    def start(self):
        if not self._thread.is_alive():
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=3)

    def _loop(self):
        while not self._stop.is_set():
            try:
                out = subprocess.check_output(
                    ["nvidia-smi",
                     f"--id={self.gpu_idx}",
                     "--query-gpu=utilization.gpu,memory.used,power.draw",
                     "--format=csv,noheader,nounits"],
                    timeout=2,
                ).decode().strip()
                util, mem_mib, power_w = [float(x.strip()) for x in out.split(",")]
                self.records.append({"util": util, "mem_mib": mem_mib, "power_w": power_w})
            except Exception:
                pass  # transient nvidia-smi failures don't kill the monitor
            self._stop.wait(self.interval_s)

    def summary(self) -> dict:
        if not self.records:
            return {"util_pct_mean": None, "util_pct_max": None,
                    "vram_used_gb_max": None, "power_w_mean": None, "n_samples": 0}
        utils = [r["util"]    for r in self.records]
        mems  = [r["mem_mib"] for r in self.records]
        pwrs  = [r["power_w"] for r in self.records]
        return {
            "util_pct_mean":    round(sum(utils) / len(utils), 1),
            "util_pct_max":     round(max(utils), 1),
            "vram_used_gb_max": round(max(mems) / 1024, 2),
            "vram_used_gb_mean": round((sum(mems) / len(mems)) / 1024, 2),
            "power_w_mean":     round(sum(pwrs) / len(pwrs), 1),
            "power_w_max":      round(max(pwrs), 1),
            "n_samples":        len(self.records),
        }
