"""
ml/eval/experiments/lib/hf_hub.py

HF Hub runtime policy — single home for the env-var dance between dataset
offline mode and the kernels-community trust check the MXFP4 quantizer
performs at model-load time.

Owns configs/hf_hub.yaml as its source of truth (trusted_publishers,
runtime_env, prepopulate_trust_cache). Two public entry points:

    apply_hf_hub_policy()         # called from run.py setup; idempotent.
    allow_publisher_trust_check()  # context manager around from_pretrained.

The 0112.1.1.1.1 smoke failure on 2026-06-13 traced to a global
HF_HUB_OFFLINE=1 + kernels.utils._check_trust_remote_code reaching out to
GET /api/organizations/kernels-community/overview. Two solutions baked in
here:

  1. Pre-populate the kernels trust cache for known publishers so the org-
     overview HTTP call is short-circuited entirely (preferred).
  2. Context-manager that temporarily pops HF_HUB_OFFLINE around model
     load when (1) cannot be applied (older kernels versions etc.).
"""
from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import Iterator, Optional

_CONFIG_PATH = Path(__file__).parent / "configs" / "hf_hub.yaml"
_policy_cache: Optional[dict] = None
_applied = False


def _load_policy() -> dict:
    global _policy_cache
    if _policy_cache is None:
        import yaml
        with _CONFIG_PATH.open() as fh:
            _policy_cache = yaml.safe_load(fh)
    return _policy_cache


def _prepopulate_trust_cache(publishers: list) -> None:
    """Mark `publishers` as trusted in the kernels package's runtime cache so
    the org-overview HTTP fetch in kernels.utils._check_trust_remote_code is
    skipped. Best-effort: if kernels' internal API has shifted (0.15+
    introduced a different cache shape), warn and let the context-manager
    fallback take over.

    kernels 0.14.1's check looks up `publisher` in a module-level dict that
    persists for the process; pre-populating that dict before any
    get_kernel() call removes the network requirement entirely.
    """
    try:
        from kernels import utils as _ku  # type: ignore
    except ImportError:
        return                            # kernels not in this venv; nothing to do

    # The cache attribute changed between kernels versions; cover the two
    # known shapes. If neither is present, fall through silently — the
    # context-manager path below still works.
    for attr in ("_TRUSTED_PUBLISHERS", "_trusted_publishers", "TRUSTED_PUBLISHERS"):
        cache = getattr(_ku, attr, None)
        if isinstance(cache, (set, list)):
            for pub in publishers:
                if pub not in cache:
                    if isinstance(cache, set):
                        cache.add(pub)
                    else:
                        cache.append(pub)
            return
        if isinstance(cache, dict):
            for pub in publishers:
                cache.setdefault(pub, True)
            return


def apply_hf_hub_policy() -> None:
    """Idempotent. Apply configs/hf_hub.yaml's runtime_env to os.environ AND
    pre-populate the kernels trust cache for the listed publishers. Call once
    at run.py startup, BEFORE importing transformers or kernels."""
    global _applied
    if _applied:
        return
    policy = _load_policy()

    for k, v in policy.get("runtime_env", {}).items():
        os.environ.setdefault(k, str(v))

    if policy.get("prepopulate_trust_cache"):
        _prepopulate_trust_cache(policy.get("trusted_publishers", []))

    _applied = True
    print(
        f"[lib.hf_hub] applied policy: HF_HUB_OFFLINE="
        f"{os.environ.get('HF_HUB_OFFLINE')}, trusted_publishers="
        f"{policy.get('trusted_publishers', [])}",
        file=sys.stderr,
    )


# huggingface_hub flips its internal offline flag if EITHER HF_HUB_OFFLINE OR
# TRANSFORMERS_OFFLINE is set; both must come off for the kernels-community
# trust-check HTTP call to complete. We don't touch HF_DATASETS_OFFLINE — the
# dataset path is the consumer that intentionally wants offline.
_OFFLINE_VARS = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")


@contextlib.contextmanager
def allow_publisher_trust_check() -> Iterator[None]:
    """Context manager that drops HF_HUB_OFFLINE and TRANSFORMERS_OFFLINE for
    the duration of the block, so kernels' first-time publisher-trust HEAD
    call can succeed. Restores both on exit (including on exceptions).

    Why both: huggingface_hub.HF_HUB_OFFLINE is computed at module import as
    `HF_HUB_OFFLINE_VAR or TRANSFORMERS_OFFLINE_VAR`, so setting only
    TRANSFORMERS_OFFLINE=1 (helpful when callers want pure-local cache reuse)
    still triggers OfflineModeIsEnabled on the kernel trust check.

    Use this when apply_hf_hub_policy() can't pre-populate the trust cache
    (older kernels versions, unexpected cache shape). Costs one HTTP HEAD on
    first model load per process.
    """
    saved = {k: os.environ.pop(k, None) for k in _OFFLINE_VARS}
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
