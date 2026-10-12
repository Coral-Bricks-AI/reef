#!/usr/bin/env python3
"""Validate counts, structure, checksums, and public-data hygiene."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).parent
DATA = ROOT / "data"
SENSITIVE_PATTERNS = {
    "IPv4 address": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    "home path": re.compile(r"/(?:Users|home)/[^/\s]+"),
    "private issue": re.compile(r"Coral-Bricks-AI/www/(?:issues|pull)/", re.IGNORECASE),
    "internal snapshot path": re.compile(r"qwen36_megakernel_snapshot_", re.IGNORECASE),
    "credential marker": re.compile(
        r"(?:api[_-]?key|authorization|bearer|password|passwd|aws_secret)",
        re.IGNORECASE,
    ),
}


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    experiments = read_jsonl(DATA / "experiments.jsonl")
    runs = read_jsonl(DATA / "runs.jsonl")

    assert len(experiments) == manifest["experiment_count"] == 53
    assert len(runs) == manifest["run_count"] == 211
    assert sum(experiment["run_count"] for experiment in experiments) == len(runs)
    assert manifest["verdict_counts"] == {
        "confirmed": 26,
        "diagnostic": 2,
        "falsified": 25,
    }

    experiment_ids = {experiment["experiment_id"] for experiment in experiments}
    assert len(experiment_ids) == len(experiments)
    assert all(run["experiment_id"] in experiment_ids for run in runs)
    assert len({(run["experiment_id"], run["run_id"]) for run in runs}) == len(runs)

    for filename, metadata in manifest["files"].items():
        assert sha256(DATA / filename) == metadata["sha256"]

    public_text = "\n".join(path.read_text(encoding="utf-8") for path in DATA.iterdir())
    for label, pattern in SENSITIVE_PATTERNS.items():
        assert not pattern.search(public_text), f"found {label}"

    print("archive valid: 53 experiments, 211 runs")


if __name__ == "__main__":
    main()
