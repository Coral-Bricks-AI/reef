# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``alphacumen`` -- finance implementation on top of :mod:`reef`.

Composes the generic harness with:

- Finance specialists (sector / news_quant / stock / vc / grok / risk) and
  their persona prompts.
- The SEC/XBRL/market/news tool roster (~9k LOC of finance verbs).
- 29 planner-side flat routing playbooks (``planner_skills/``) and 44
  ``SKILL.md`` + ``impl.py`` folder skills (``skills/``).
- The pre-built ``HarnessConstraints`` defaults that AlphaCumen runs use
  in production (asof from filing date, tool budgets sized for v2-grade
  questions, allowed-index list).

The harness primitives are the reusable part. This package is what you'd
fork if you're building a finance agent; what you'd swap for your own
package if you're building a non-finance one on top of the same harness.
"""

__version__ = "0.1.0"


# Tell the harness's langfuse primitive to attribute traces to alphacumen.
# Must run before any RunTrace is constructed.
from reef import _langfuse as _harness_langfuse
_harness_langfuse.configure(source="alphacumen", pipeline_default="investment_analyst")
del _harness_langfuse


# --------------------------------------------------------------------------
# Hosted-runtime integration.
#
# When the Coral Bricks platform's sandbox is on the path (gateway-installed
# per-pipeline venv), pipe LLM calls + kernel-verb tools + the memory store
# through the gateway. The sandbox exposes:
#
#   - coralbricks.sandbox.llm.chat — OpenAI-shape proxy that uses the
#     gateway's OSS-LLM keys (no API key plumbing required in the wheel).
#   - coralbricks.sandbox.tools — bm25/ann/sql/multihop/get/py/grok over
#     the prefab finance corpus (~4.5TB of SEC + market + news data).
#   - coralbricks.sandbox.memory — Memory API persistence keyed on
#     CORAL_REQUEST_ID.
#
# Override the harness's standalone defaults (direct provider, redirect-to-
# team stubs, in-memory no-op memo) so the wheel routes everything through
# the gateway. When the sandbox isn't present (OSS local clone), keep the
# harness defaults — the framework still imports cleanly; AlphaCumen calls
# just hit the redirect stubs.
# --------------------------------------------------------------------------

try:
    from coralbricks.sandbox import llm as _sandbox_llm
    from coralbricks.sandbox import tools as _sandbox_tools
    from coralbricks.sandbox import memory as _sandbox_memory

    # Reroute the harness chat client through the sandbox proxy. Same envelope
    # shape, gateway-managed auth.
    import reef.llm as _harness_llm
    _harness_llm.chat = _sandbox_llm.chat

    # Replace the kernel-verb stubs with real sandbox tool dispatches.
    import reef.stubs.tools as _stub_tools
    for _name in getattr(_sandbox_tools, "__all__", ()):
        _val = getattr(_sandbox_tools, _name, None)
        if _val is not None:
            setattr(_stub_tools, _name, _val)

    # Replace the memo stub with the real Memory API persistence.
    from . import memo as _memo
    if hasattr(_sandbox_memory, "persist_memo"):
        _memo.persist_memo = _sandbox_memory.persist_memo

    _SANDBOX_ACTIVE = True
    del _harness_llm, _stub_tools, _memo, _name, _val
except ImportError:
    # No sandbox on the path — keep the harness standalone defaults.
    _SANDBOX_ACTIVE = False
