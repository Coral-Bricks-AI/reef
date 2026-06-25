"""
ml/eval/experiments/lib

Shared utilities for sparse-attention / block-attention benchmarking experiments
on the worker-benchmark task queue. Extracted from #0003 (sliding-window) and #0004
(vertical-slash block-sparse) so future experiments don't re-derive primitives.

Typical use:
    from ml.eval.experiments.lib import niah, attention, gpu, results

    # 1) generate eval samples
    samples = niah.make_single_key_samples(
        tokenizer, n_samples=10, positions=[0.1, 0.5, 0.9], target_tokens=8192,
    )

    # 2) wrap a region with GPU sampling
    with gpu.GpuMonitor() as mon:
        per_sample = run_eval(model, tokenizer, samples)
    gpu_summary = mon.summary()

    # 3) aggregate + write to schema
    results.write_results(
        path="results.json",
        experiment_id="#0042",
        variants={"dense": {"per_sample": per_sample, "gpu": gpu_summary, ...}},
    )

Each module is independently importable; nothing here depends on transformers
*specifically* — attention helpers take generic tensors / cache objects.
"""

from . import attention, exp_setup, flash_blockattn, gpu, niah, progress, results

__all__ = ["attention", "exp_setup", "flash_blockattn", "gpu", "niah", "progress", "results"]
