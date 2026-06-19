# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``harness.stubs.tools`` -- stubs for the kernel retrieval verbs.

Drop-in replacement for the ``coralbricks.sandbox.tools`` interface used by
``alphacumen.tools`` and the ``compute_*`` / ``extract_*`` skill ``impl.py``
modules. Every call raises :class:`NotImplementedError` with a message that
redirects to the Coral Bricks team for the hosted experience, or to a BYO
implementation against your own data backend.

The framework primitives (skills, planner, specialists, constraints) work
standalone without these verbs; only the retrieval / compute pipeline needs
a backend.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

_MSG = (
    "\n\n"
    "AlphaCumen's kernel retrieval verb `{verb}` requires a data backend "
    "(BM25 index, vector store, SQL DB, graph DB, HTTP client, or Python "
    "executor).\n\n"
    "👉 For the hosted experience over the prefab finance corpus (~4.5TB of "
    "SEC filings + market data + news), talk to the Coral Bricks team:\n"
    "   https://coralbricks.ai/alphacumen\n\n"
    "👉 To wire your own retrieval against your data (OpenSearch, Pinecone, "
    "DuckDB, etc.), implement this function with your backend and replace "
    "the stub. The framework primitives -- skills, planner, specialists, "
    "constraints -- work standalone.\n"
)


def _raise(verb: str) -> "NoReturn":  # type: ignore[name-defined]
    raise NotImplementedError(_MSG.format(verb=verb))


def bm25(
    *,
    index: str,
    query: str,
    k: int = 10,
    fields: Optional[Sequence[str]] = None,
    filters: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Stub for keyword (BM25) search over an indexed corpus."""
    _raise("bm25")


def ann(
    *,
    index: str,
    query: str,
    k: int = 10,
    filters: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Stub for approximate-nearest-neighbour vector search."""
    _raise("ann")


def sql(
    *,
    index: str,
    query: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Stub for SQL queries against an indexed table-shaped corpus."""
    _raise("sql")


def multihop(
    *,
    index: str,
    seed: Any = None,
    query: Optional[str] = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Stub for multi-hop graph traversal."""
    _raise("multihop")


def get(
    *,
    index: str,
    id: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Stub for fetching a single record by id from an indexed corpus."""
    _raise("get")


def py(
    code: str,
    inputs: Optional[Mapping[str, Any]] = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Stub for the Python execution environment used by compute skills."""
    _raise("py")


def grok(
    *,
    query: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Stub for the prose-comprehension verb used by some sector flows."""
    _raise("grok")


def bind_py_global(name: str, value: Any) -> None:
    """Stub for binding a global into the Python executor's namespace.

    No-op for valid identifiers (there's no executor namespace to bind
    into in the open-source build -- ``py()`` raises before any bound
    global would be reachable). For *invalid* identifiers we still raise
    :class:`harness.stubs.py_executor.PyValidationError`, mirroring the
    hosted runtime's contract -- ``harness.tool.apply_binding`` relies on
    the exception to attach a ``bind_error`` marker so the model can
    self-correct rather than silently believing the bind succeeded.
    """
    from harness.stubs.py_executor import PyValidationError

    if not isinstance(name, str) or not name.isidentifier() or name.startswith("__"):
        raise PyValidationError(
            f"bind_as={name!r} is not a valid Python identifier"
        )
    return None


def list_tools(**kwargs: Any) -> dict[str, Any]:
    """Stub for listing kernel-side tool registrations.

    Returns an empty registration envelope so ``alphacumen.capabilities``
    can construct an empty :class:`IndexCapabilitiesMap` without raising.
    Real introspection only matters when the runtime is wired up.
    """
    return {"ok": True, "registrations": []}


__all__ = [
    "ann",
    "bind_py_global",
    "bm25",
    "get",
    "grok",
    "list_tools",
    "multihop",
    "py",
    "sql",
]
