# Mantis Qwen3.6 H100 experiment archive

This archive contains the complete structured experiment record behind Coral Bricks' optimization of `Qwen3.6-35B-A3B-FP8` for concurrency-1 inference on one NVIDIA H100 SXM.

The work was run with **Mantis, Coral Bricks' AI CUDA engineer harness**. Mantis was built by the Coral Bricks team and was actively steered by Hitesh Jain during this campaign.

## Headline result

Against stock `lmsysorg/sglang:v0.5.18-cu129`, tensor parallelism 1:

| Metric | Stock SGLang | Final stack | Change |
|---|---:|---:|---:|
| MTP decode, MT-Bench batch 1 | 506.9 tok/s | **760.5 tok/s** | **+50.0%** |
| No-MTP decode, fixed 1,024-in/256-out | 3.73 ms/token | **2.41 ms/token** | **-35.4%** |
| GSM8K, 1,319 questions | 96.2% to 96.4% | 96.2% to 96.7% | unchanged within run variation |

These results apply to the measured configuration. They are not claims about higher concurrency, other accelerators, or fleet-level economics.

## What is in the archive

| File | Records | Description |
|---|---:|---|
| [`data/experiments.jsonl`](data/experiments.jsonl) | 53 | Hypothesis, conclusion, verdict, and run count for every experiment |
| [`data/runs.jsonl`](data/runs.jsonl) | 211 | Every recorded arm, condition, metric object, and engine configuration |
| [`data/experiment_index.csv`](data/experiment_index.csv) | 53 | Human-readable experiment index |
| [`data/manifest.json`](data/manifest.json) | 1 | Scope, counts, privacy declaration, and SHA-256 checksums |

Verdicts:

- 26 confirmed
- 25 falsified
- 2 diagnostic/no final verdict

Run IDs are scoped to an experiment. One label was reused in two experiments, so the 211 records contain 210 distinct `run_id` strings but 211 unique `(experiment_id, run_id)` pairs.

## Why publish the failures

The failed arms define the result as much as the final stack. They include:

- a persistent MoE kernel that was slower than CUDA-graph launches;
- three expert-grouped MoE designs that lost to the simpler path;
- a false megakernel win caused by unintended overlap between independent layers;
- an `mbarrier` parity-aliasing race;
- L2 prefetch arms that made the next layer slower;
- tree drafting and longer draft chains that did not repay their verification cost;
- draft-head slicing that saved compute but lost more through lower acceptance.

Publishing negative results makes the search auditable and helps other kernel engineers avoid paying to rediscover the same dead ends.

## Measurement protocol

- Model: Qwen's official `Qwen3.6-35B-A3B-FP8` checkpoint, without further weight quantization
- Engine baseline: `lmsysorg/sglang:v0.5.18-cu129`
- Hardware: one NVIDIA H100 SXM, tensor parallelism 1
- Primary target: concurrency-1 decode
- No-MTP benchmark: fixed 1,024-token input and 256-token output
- MTP benchmark: MT-Bench, 80 questions, batch 1
- Quality gate: GSM8K, all 1,319 test questions, thinking disabled

Important MTP comparisons were run sequentially, one server at a time. Early concurrent-server measurements were discarded after CPU contention was found to depress throughput.

## Record schema

Each experiment record contains:

```json
{
  "schema_version": "1.0.0",
  "experiment_id": "exp-...",
  "title": "...",
  "hypothesis": "...",
  "conclusion": "...",
  "verdict": "confirmed | falsified | null",
  "run_count": 4
}
```

Each run record contains:

```json
{
  "schema_version": "1.0.0",
  "experiment_id": "exp-...",
  "run_id": "q36-...",
  "arm": "...",
  "is_control": false,
  "condition": "...",
  "metrics": {},
  "engine_environment": {}
}
```

Metric objects intentionally remain heterogeneous. A kernel microbenchmark, an end-to-end MTP run, and an accuracy evaluation do not have the same natural measurements. Preserving the original metric names avoids manufacturing a false common schema.

## Privacy and provenance

The archive is exported from the immutable final campaign ledger. The exporter removes ephemeral host IPs and user home paths. The public files contain no credentials, raw prompts, private transcripts, or private issue links.

To validate the checked-in archive:

```bash
python research/qwen36-h100/validate_archive.py
```

To regenerate it from an authorized copy of the internal ledger:

```bash
python research/qwen36-h100/export_archive.py /path/to/ledger.json
```

The exporter and validator are included so that record counts, composite IDs, checksums, and redaction constraints are reviewable rather than matters of trust.

CI runs the same validation on every pull request that changes this archive.

## License

This archive is released under the repository's Apache 2.0 license.
