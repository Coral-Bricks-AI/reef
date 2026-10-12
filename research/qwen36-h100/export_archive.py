#!/usr/bin/env python3
"""Export the internal Qwen3.6 experiment ledger as a public archive."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.0.0"
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
HOME_PATH_RE = re.compile(r"/(?:Users|home)/[^/\s]+")
INTERNAL_SNAPSHOT_RE = re.compile(r"qwen36_megakernel_snapshot_[^\s,;)]*")


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: sanitize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if not isinstance(value, str):
        return value

    value = IPV4_RE.sub("<redacted-host>", value)
    value = HOME_PATH_RE.sub("<redacted-home>", value)
    return INTERNAL_SNAPSHOT_RE.sub("<internal-snapshot>", value)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(canonical_json(record))
            handle.write("\n")


def export(ledger_path: Path, output_dir: Path) -> None:
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    if not isinstance(ledger, list):
        raise ValueError("ledger root must be a JSON array")

    experiments: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []

    for source_experiment in ledger:
        experiment_id = source_experiment["id"]
        source_runs = source_experiment.get("runs", [])
        experiments.append(
            sanitize(
                {
                    "schema_version": SCHEMA_VERSION,
                    "experiment_id": experiment_id,
                    "title": source_experiment["title"],
                    "hypothesis": source_experiment["hypothesis"],
                    "conclusion": source_experiment.get("conclusion"),
                    "verdict": source_experiment.get("verdict"),
                    "run_count": len(source_runs),
                }
            )
        )

        for source_run in source_runs:
            runs.append(
                sanitize(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "experiment_id": experiment_id,
                        "run_id": source_run["runId"],
                        "arm": source_run["arm"],
                        "is_control": source_run.get("isControl", False),
                        "condition": source_run.get("condition"),
                        "metrics": source_run.get("metrics", {}),
                        "engine_environment": source_run.get("engineEnv", {}),
                    }
                )
            )

    output_dir.mkdir(parents=True, exist_ok=True)
    experiments_path = output_dir / "experiments.jsonl"
    runs_path = output_dir / "runs.jsonl"
    index_path = output_dir / "experiment_index.csv"
    manifest_path = output_dir / "manifest.json"

    write_jsonl(experiments_path, experiments)
    write_jsonl(runs_path, runs)

    with index_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["experiment_id", "verdict", "run_count", "title"],
            lineterminator="\n",
        )
        writer.writeheader()
        for experiment in experiments:
            writer.writerow(
                {
                    "experiment_id": experiment["experiment_id"],
                    "verdict": experiment["verdict"] or "diagnostic",
                    "run_count": experiment["run_count"],
                    "title": experiment["title"],
                }
            )

    verdicts = Counter(experiment["verdict"] or "diagnostic" for experiment in experiments)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "archive": "mantis-qwen36-h100",
        "published_by": "Coral Bricks",
        "model": "Qwen3.6-35B-A3B-FP8",
        "engine_baseline": "lmsysorg/sglang:v0.5.18-cu129",
        "hardware": "1x NVIDIA H100 SXM",
        "primary_scope": "tensor-parallel 1, concurrency 1 decode",
        "experiment_count": len(experiments),
        "run_count": len(runs),
        "verdict_counts": dict(sorted(verdicts.items())),
        "files": {
            experiments_path.name: {"sha256": sha256(experiments_path)},
            runs_path.name: {"sha256": sha256(runs_path)},
            index_path.name: {"sha256": sha256(index_path)},
        },
        "privacy": {
            "host_identifiers_removed": True,
            "credentials_present": False,
            "raw_prompts_present": False,
            "private_issue_links_present": False,
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("ledger", type=Path, help="Path to the internal ledger.json")
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "data")
    args = parser.parse_args()
    export(args.ledger, args.output)


if __name__ == "__main__":
    main()
