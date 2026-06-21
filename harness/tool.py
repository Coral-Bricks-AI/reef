# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``harness.tool`` -- the ``Tool`` dataclass + dispatch primitives.

A :class:`Tool` is the framework's atomic unit of capability: ``name`` +
``description`` + JSON Schema for ``parameters`` + the Python callable
the runtime invokes when the model emits a ``tool_call``. The shape
matches OpenAI's tool-calling contract (see :meth:`Tool.to_openai_schema`),
which is the lowest common denominator across DeepInfra, Cerebras,
OpenAI, Bedrock, and self-hosted SGLang -- so a model swap is one line.

Tools are immutable and process-singleton-safe: the same instance can
appear in multiple specialist rosters. Per-run capability schemas are
stamped on via :meth:`Tool.with_capabilities`, which returns a new
instance rather than mutating in place -- concurrent runs against
different gateway snapshots can hold different stamped variants of the
same source tool without racing.

This module is domain-agnostic. Concrete tool surfaces (retrieval
verbs, SQL, executors, ...) live in the consumer package. The
generic skill-dispatch tool (``INVOKE_SKILL_FN``) and the
``make_load_skill_tool`` factory live next door in
:mod:`harness.skill_tools`.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from harness.stubs import tools as cb_tools
from harness.stubs.py_executor import PyValidationError


_TOOL_RESULT_MAX_CHARS = 32_000
"""Per-call clip on top-level string tool results.

Structured payloads (the common case for ``tools.bm25`` / ``tools.sql``)
flow through untruncated because the gateway already enforces row caps.
The clip lives at the framework layer because the OpenAI tool-call wire
shape has the same byte-budget pressure regardless of domain.
"""


# ---------------------------------------------------------------------------
# Tool dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Tool:
    """One model-facing tool, callable via the OpenAI tool-calling shape.

    The model only ever sees ``name`` + ``description`` + the JSON
    ``parameters`` schema. ``fn`` is the Python callable the runtime
    invokes when a ``tool_call`` for ``name`` arrives; it must return
    a JSON-serializable value (the runtime ``json.dumps`` the result
    before handing it back to the model).

    Tools are immutable -- the runtime composes them into specialist
    rosters but never mutates any field. The same instance can appear
    in multiple rosters.

    ``bound_indices`` records the platform index slugs this tool
    queries, paired with the verbs (``"bm25"``, ``"sql"``,
    ``"multihop"``, ...) it touches on each. The capability schema
    rendered into ``description`` at run start (via
    :meth:`with_capabilities`) lets the model see the actual queryable
    field / table / predicate names rather than guessing. ``()`` (empty)
    means the tool is index-agnostic; we leave its description alone.
    """

    name: str
    description: str
    parameters: Mapping[str, Any]
    fn: Callable[..., Any]
    bound_indices: tuple[tuple[str, tuple[str, ...]], ...] = field(default=())

    def to_openai_schema(self) -> dict[str, Any]:
        """Render this tool into the OpenAI ``tools=[...]`` shape.

        The shape mirrors what ``llm.chat`` forwards to the upstream
        provider: ``{type: "function", function: {name, description,
        parameters}}``. Pure passthrough -- no transformation other
        than copying ``parameters`` so the caller can't mutate our
        frozen dataclass via reference.
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": copy.deepcopy(dict(self.parameters)),
            },
        }

    def with_capabilities(
        self,
        capabilities: Mapping[str, Any],
        renderer: Optional[Callable[[str, tuple[str, ...], Mapping[str, Any]], str]] = None,
    ) -> "Tool":
        """Return a copy of this tool with the per-index schema baked into the description.

        For each ``(slug, verbs)`` in :attr:`bound_indices`, the caller's
        ``renderer`` is invoked with ``(slug, verbs, capabilities)`` and
        the returned fragment (field list / table list / predicate list,
        ...) is appended to the existing description.

        ``renderer`` is supplied by the consumer package: the harness
        owns the shape of the call, but the rendering logic lives with
        the index backend. When ``renderer`` is ``None`` (or the tool
        has no :attr:`bound_indices`, or ``capabilities`` is empty) this
        is a no-op and returns ``self``.

        Returning a new dataclass instance (rather than mutating in
        place) keeps the original module-level ``Tool`` constants
        immutable -- multiple runs can stamp different schemas
        onto the same source tool without cross-contamination.
        """
        if not self.bound_indices or not capabilities or renderer is None:
            return self
        chunks: list[str] = []
        for slug, verbs in self.bound_indices:
            section = renderer(slug, verbs, capabilities)
            if section:
                chunks.append(section)
        if not chunks:
            return self
        return Tool(
            name=self.name,
            description=self.description + "".join(chunks),
            parameters=self.parameters,
            fn=self.fn,
            bound_indices=self.bound_indices,
        )


# ---------------------------------------------------------------------------
# Tool-result helpers
# ---------------------------------------------------------------------------

def truncate_for_model(payload: Any) -> Any:
    """Clip any string-shaped tool result to :data:`_TOOL_RESULT_MAX_CHARS`.

    Only top-level strings are clipped; structured payloads flow
    through as-is because the gateway already enforces row caps.
    Returning ``payload`` untouched when no clipping is needed keeps
    the serialized JSON identical to the kernel envelope so the model
    sees a stable shape.
    """
    if isinstance(payload, str) and len(payload) > _TOOL_RESULT_MAX_CHARS:
        return (
            payload[:_TOOL_RESULT_MAX_CHARS]
            + f"\n... [truncated at {_TOOL_RESULT_MAX_CHARS} chars]"
        )
    return payload


# Legacy alias for code that imports the underscored form. New code
# should use the public name.
_truncate_for_model = truncate_for_model


# ---------------------------------------------------------------------------
# bind_as -- shared JSON-schema fragment + the "also push into runner
# globals" affordance every retrieval/SQL tool exposes
# ---------------------------------------------------------------------------

BIND_AS_PARAM_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": (
        "Optional. If set, the (embedding-stripped) tool result is "
        "ALSO bound to this name as a top-level variable in the "
        "in-runner Python interpreter (the same one `run_python` "
        "executes against). The tool still returns the same full "
        "envelope to you; the binding is an extra side effect that "
        "lets a later `run_python(code=...)` call reference the "
        "value by name -- no need to re-emit the bytes via "
        "`inputs=`. Use this whenever you intend to post-process "
        "the result in `run_python` (RRF fusion across two hit "
        "lists, dedup, regrouping, scoring filing chunks). The "
        "name must be a valid Python identifier and must not start "
        "with double underscores. Bindings are PER-SPECIALIST "
        "(each specialist runs on its own thread) and persist "
        "across `run_python` calls within that specialist's loop."
    ),
}
"""Schema fragment for the ``bind_as`` parameter on every retrieval tool.

Why one definition: a docstring tweak shouldn't require touching every
finance tool. Tools splice this into their own ``parameters.properties``
under the key ``"bind_as"``.
"""

# Legacy alias.
_BIND_AS_PARAM_SCHEMA = BIND_AS_PARAM_SCHEMA


def apply_binding(bind_as: Optional[str], full_result: Any) -> Any:
    """If ``bind_as`` is set, also push ``full_result`` into the runner's globals.

    Returns ``full_result`` either way -- with a ``bound_as`` marker if
    the bind succeeded, or a ``bind_error`` marker if the name was
    invalid. The full payload reaches the in-runner Python interpreter
    via :func:`coralbricks.sandbox.tools.bind_py_global`, which writes
    into the calling thread's per-thread globals dict on the
    process-singleton :class:`PyExecutor`. A later
    ``run_python(code='...')`` call from the same specialist thread
    sees the value as a top-level Python global.

    Per-specialist isolation is structural: each specialist runs on its
    own thread, so binding ``bm25_hits`` in ``vc_analyst`` cannot
    clobber ``risk_analyst``'s ``bm25_hits``.

    A failed bind (bad identifier, dunder name) does NOT swallow the
    result -- the fetch already paid its cost, the data is still
    useful. The ``bind_error`` marker tells the model the variable
    isn't available so it can self-correct on retry.
    """
    if not bind_as:
        return full_result
    try:
        cb_tools.bind_py_global(bind_as, full_result)
    except PyValidationError as e:
        if isinstance(full_result, Mapping):
            out = dict(full_result)
            out["bind_error"] = str(e)
            return out
        return {"value": full_result, "bind_error": str(e)}
    if isinstance(full_result, Mapping):
        out = dict(full_result)
        out["bound_as"] = bind_as
        return out
    return full_result


# Legacy alias.
_apply_binding = apply_binding


# ---------------------------------------------------------------------------
# Roster helpers
# ---------------------------------------------------------------------------

def lookup_tool(name: str, tools: Sequence[Tool]) -> Tool:
    """Find a :class:`Tool` by name within a roster.

    Raises :class:`KeyError` for an unknown name. The runtime catches
    that and turns it into a ``role: tool`` error message the model
    can self-correct from -- much better UX than crashing the swarm.

    Note: framework-level signature has no default roster. The
    finance package re-exports a wrapper that adds the
    ``ALL_TOOLS`` default for convenience callers.
    """
    for t in tools:
        if t.name == name:
            return t
    raise KeyError(name)


def bind_tools(
    tools: Sequence[Tool],
    capabilities: Mapping[str, Mapping[str, Any]],
    renderer: Optional[Callable[[str, tuple[str, ...], Mapping[str, Any]], str]] = None,
) -> tuple[Tool, ...]:
    """Stamp each tool's bound-index schema into its description.

    Walks ``tools`` and calls :meth:`Tool.with_capabilities` with
    ``capabilities`` (the ``{slug: {verb: cfg}}`` map produced by the
    consumer's capability fetcher) and the consumer-supplied
    ``renderer``. The returned tuple is what the runner hands to
    :func:`harness.react.run_react` -- the module-level ``Tool``
    constants stay untouched, so concurrent runs against different
    gateway snapshots don't race.

    When ``renderer`` is ``None`` this returns the input tools
    unchanged -- handy for harnesses without dynamic capability
    schemas (cocktails, hello-world).
    """
    return tuple(t.with_capabilities(dict(capabilities), renderer) for t in tools)


__all__ = [
    "BIND_AS_PARAM_SCHEMA",
    "Tool",
    "apply_binding",
    "bind_tools",
    "lookup_tool",
    "truncate_for_model",
    # Legacy underscored aliases retained for back-compat with code
    # that imported the private names directly.
    "_BIND_AS_PARAM_SCHEMA",
    "_apply_binding",
    "_truncate_for_model",
]
