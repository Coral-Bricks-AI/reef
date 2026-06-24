# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Shared argparse fragments."""

from __future__ import annotations

import argparse


def add_json_out_arg(p: argparse.ArgumentParser) -> None:
    """Add ``--json-out`` for an optional JSONL mirror of the eval log.

    When set, each evaluated row appends its full result envelope as one
    JSON line to this file. The OSS evals don't ship a Memory API
    persistence layer, so ``--json-out`` is the canonical way to keep a
    durable record of a sweep.
    """
    p.add_argument(
        "--json-out", type=str, default=None,
        help="Optional path to a JSONL file. Each evaluated row appends its "
             "full result envelope as one line.",
    )
