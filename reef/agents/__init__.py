# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``reef.agents`` -- pluggable agent drivers for the harness.

The in-process ReAct loop in :mod:`reef.react` is the canonical driver
for skill-based harnesses (AlphaCumen et al). For orchestration use
cases where the agent is driven via the ``claude`` CLI subprocess
(Polyp's Architect / Analyzer / Auto-suggester), :mod:`reef.agents.headless_claude`
wraps the CLI so callers get the same :class:`reef.react.Trajectory`
shape and Langfuse tracing they'd get from ``run_react``.
"""

from reef.agents.headless_claude import (
    ClaudeResult,
    HeadlessClaude,
    parse_stream_json_log,
)

__all__ = [
    "ClaudeResult",
    "HeadlessClaude",
    "parse_stream_json_log",
]
