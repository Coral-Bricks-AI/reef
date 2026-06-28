"""``polyp.lib`` -- helpers for worker-side experiment scripts.

The Architect drafts ``run.py`` per task; these modules are the shared
primitives those drafts import so each new experiment doesn't re-derive
the same fixes.

- :mod:`polyp.lib.gpu`      -- nvidia-smi sampler / monitor
- :mod:`polyp.lib.hf_hub`   -- HuggingFace cache helpers
- :mod:`polyp.lib.progress` -- compat re-export of :mod:`reef.progress`
"""

from . import gpu, hf_hub, progress

__all__ = ["gpu", "hf_hub", "progress"]
