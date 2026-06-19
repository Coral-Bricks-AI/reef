# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``alphacumen`` -- finance implementation on top of :mod:`harness`.

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
