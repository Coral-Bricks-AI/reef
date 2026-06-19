# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``harness`` -- generic agent-harness primitives.

Domain-agnostic core: ReAct loop, skill loaders, ``@skill_fn`` decorator,
constraint declaration + enforcement, planner/specialist/swarm scaffolding.

The finance-specific layer that composes on top of this lives in
:mod:`alphacumen`. The split lets a non-finance harness reuse the
machinery here without dragging in SEC/XBRL tools, finance prompts, or the
issuer-analyst roster.

Public surface ergonomics::

    from harness import (
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

from harness.constraints import (
    AsofLike,
    HarnessConstraints,
)
from harness.context import (
    current_constraints,
    current_enforcer,
    harness_context,
)
from harness.decorators import time_bounded
from harness.enforcement import (
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
