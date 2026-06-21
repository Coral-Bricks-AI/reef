# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``reef`` -- generic agent-harness primitives.

Domain-agnostic core: ReAct loop, skill loaders, ``@skill_fn`` decorator,
constraint declaration + enforcement, multi-provider LLM client. The
framework owns the loop, the dispatch contract, and the trajectory; the
consumer owns the corpus, the tools, and the personas.

See ``examples/cocktails`` for the minimal end-to-end binding (one
specialist, two skills, ~50 lines of glue).

Public surface ergonomics::

    from reef import (
        HarnessConstraints,
        LocalEnforcer,
        time_bounded,
        harness_context,
    )

    @time_bounded(asof_arg="as_of_iso", filter_field="filing_date")
    def my_tool(as_of_iso: str | None = None): ...

    constraints = HarnessConstraints(asof="2024-09-30", tool_budget=20)
    with harness_context(constraints, LocalEnforcer()):
        ...  # tool dispatches honor the contract
"""

__version__ = "0.1.0"

from reef.constraints import (
    AsofLike,
    HarnessConstraints,
)
from reef.context import (
    current_constraints,
    current_enforcer,
    harness_context,
)
from reef.decorators import time_bounded
from reef.enforcement import (
    AsofViolation,
    AuditingEnforcer,
    BudgetExceeded,
    ConstraintEnforcer,
    ConstraintViolation,
    EnforcementEvent,
    IndexNotAllowed,
    LocalEnforcer,
    NullEnforcer,
    TimeBound,
    enforced_run,
    get_time_bound,
)

__all__ = [
    "AsofLike",
    "AsofViolation",
    "AuditingEnforcer",
    "BudgetExceeded",
    "ConstraintEnforcer",
    "ConstraintViolation",
    "EnforcementEvent",
    "HarnessConstraints",
    "IndexNotAllowed",
    "LocalEnforcer",
    "NullEnforcer",
    "TimeBound",
    "current_constraints",
    "current_enforcer",
    "enforced_run",
    "get_time_bound",
    "harness_context",
    "time_bounded",
]
