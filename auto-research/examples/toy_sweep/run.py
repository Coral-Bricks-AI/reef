"""Toy worker: "trains" a stub model, "evaluates" it, writes results.json.

Stand-in for what a real GPU worker would do — load a model, run training,
score it on a held-out eval. Here we just sleep and emit a number that's a
deterministic function of the LR + seed so the loop has signal to optimize.

Usage (mirrors the real harness):
    python run.py path/to/spec.yaml path/to/results_dir/
"""

from __future__ import annotations

import json
import math
import random
import sys
import time
from pathlib import Path

import yaml


def fake_score(lr: float, seed: int) -> float:
    """A unimodal accuracy curve peaked near lr=3e-4 — gives the
    auto-suggester something real to climb."""
    rng = random.Random(seed)
    # log10 lr offset from optimum
    offset = math.log10(lr) - math.log10(3e-4)
    base = max(0.0, 0.85 - 0.6 * offset * offset)
    noise = rng.gauss(0, 0.02)
    return max(0.0, min(1.0, base + noise))


def main(spec_path: str, results_dir: str) -> int:
    spec = yaml.safe_load(Path(spec_path).read_text())
    out = Path(results_dir)
    out.mkdir(parents=True, exist_ok=True)

    lr = float(spec["train"]["lr"])
    seed = int(spec["train"].get("seed", 42))
    steps = int(spec["train"]["steps"])

    # Pretend to train.
    print(f"[toy] training: lr={lr} steps={steps} seed={seed}")
    time.sleep(min(steps * 0.01, 3.0))  # cap to 3s so loop tests stay snappy

    accuracy = fake_score(lr, seed)
    n = int(spec["eval"]["n_samples"])

    results = {
        "spec_path": spec_path,
        "train": {"lr": lr, "steps": steps, "seed": seed},
        "eval": {"n_samples": n, "accuracy_overall": accuracy},
        "headline": f"accuracy={accuracy:.3f} @ lr={lr}",
    }
    (out / "results.json").write_text(json.dumps(results, indent=2))
    print(f"[toy] wrote {out/'results.json'} — accuracy={accuracy:.3f}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: run.py <spec.yaml> <results_dir>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
