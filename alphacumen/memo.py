# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``alphacumen.memo`` -- memory persistence stub.

The hosted Coral Bricks runtime persists each IA run as a memo into a
Memory API store keyed on ``CORAL_REQUEST_ID``. The open-source build
exposes the same ``persist_memo`` signature so the orchestration loop
calls work, but the persistence path is stubbed: a memo id is still
minted (so caller-side wiring stays consistent), and the actual save
is a no-op that logs the redirect message.

For the hosted experience over the prefab finance corpus, talk to the
Coral Bricks team: https://coralbricks.ai/alphacumen

To run AlphaCumen for one-shot queries with no cross-call memory, this
no-op is exactly what you want -- a single ``swarm.run()`` call doesn't
read prior memos. To wire your own memory store (vector DB, document
store, postgres), replace :func:`persist_memo` with a backend client.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

MEMOS_STORE = "memos"

_STUB_MSG = (
    "alphacumen.memo.persist_memo is stubbed in the open-source build. "
    "For the hosted experience (cross-call memo persistence over the prefab "
    "finance corpus), talk to the Coral Bricks team at "
    "https://coralbricks.ai/alphacumen. For one-shot single-query runs, "
    "this no-op is the right behaviour -- no cross-call memory is needed."
)


def persist_memo(
    *,
    query: str,
    answer: Optional[Mapping[str, Any]],
    answer_summary: Optional[str],
    equity_chart: Optional[Mapping[str, Any]],
    pipeline: str,
    model: str,
    mode: str,
    asof: Optional[str],
    rounds: int,
    elapsed_ms: int,
) -> tuple[Optional[str], Optional[str]]:
    """Stub: mint a memo id, log the redirect, return ``(memo_id, None)``.

    Signature kept identical to the hosted implementation so
    ``alphacumen.swarm`` calls without modification.
    """
    del (
        query, answer, answer_summary, equity_chart, pipeline, model, mode,
        asof, rounds, elapsed_ms,
    )
    memo_id = f"memo_{uuid.uuid4().hex[:16]}"
    logger.info(_STUB_MSG)
    return memo_id, None


__all__ = ["MEMOS_STORE", "persist_memo"]
