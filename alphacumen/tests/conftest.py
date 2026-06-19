# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``alphacumen`` test bootstrap.

Puts the coral-ai repo root on ``sys.path`` so ``import harness`` and
``import alphacumen`` resolve from a clean checkout without ``pip install
-e .``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent  # coral-ai/
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


import pytest


@pytest.fixture(autouse=True)
def _reset_alphacumen_run_caches():
    """Reset module-level caches in alphacumen.tools between tests.

    alphacumen.tools caches equity bars + macro series at module level
    (one cache lifetime = one swarm subprocess). Pytest loads the module
    once, so two tests sharing the same (symbol, start, end) cache key
    would see each other's data without this reset.
    """
    try:
        from alphacumen import tools as _tools
    except ImportError:
        yield
        return
    _tools._clear_run_caches()
    yield
    _tools._clear_run_caches()
