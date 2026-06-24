# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Small shared helpers: row selectors, record shaping, JSONL output."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Union


def parse_rows_spec(spec: str, total: int) -> list[int]:
    """Parse ``"0-9,12,17"`` style row selectors into a sorted unique list.

    Out-of-range indices are dropped; reversed ranges (``"9-0"``) are
    normalized. Only indices in ``[0, total)`` survive.
    """
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a_str, b_str = part.split("-", 1)
            a, b = int(a_str), int(b_str)
            if a > b:
                a, b = b, a
            for i in range(a, b + 1):
                if 0 <= i < total:
                    out.add(i)
        else:
            i = int(part)
            if 0 <= i < total:
                out.add(i)
    return sorted(out)


def pick_field(rec: dict[str, Any], *names: str) -> Any:
    """Return the first non-None field from ``rec`` matching ``names``."""
    for n in names:
        if n in rec and rec[n] is not None:
            return rec[n]
    return None


def iso(value: Any) -> Union[str, None]:
    """Render ``datetime`` as ISO-8601; pass through ``None``; else ``str()``."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def append_jsonl(path: Union[str, Path], record: dict[str, Any]) -> None:
    """Append one ``record`` as a JSON line to ``path`` (creates parent dirs)."""
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")
