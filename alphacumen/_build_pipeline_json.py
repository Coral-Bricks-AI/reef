# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Generate ``alphacumen/pipeline.json`` from ``pyproject.toml``.

The Coral Bricks platform gateway recovers a pipeline package's
manifest after a versioned ``pip install`` by reading
``<top_level>/pipeline.json`` from the installed wheel via
``importlib.resources``. PEP 517 wheels don't include the original
``pyproject.toml`` (it's a build input, not a runtime artefact), so the
JSON sidecar is the only path that survives publish + reinstall.

This script is the single source of truth for that conversion. It's
called from two places:

1. ``setup.py``'s ``build_py`` step (so every wheel build refreshes
   the sidecar before files are copied into the build tree).
2. The ``cb pipeline publish`` CLI (so a publisher who edits the
   manifest doesn't ship a stale JSON copy by accident).

The generated JSON has the same shape as a parsed ``pyproject.toml``::

    {
      "project": {"name": "cb-ia", "version": "0.0.2"},
      "tool":    {"coralbricks": {"pipeline": {... full manifest ...}}}
    }

so the gateway's existing :func:`parse_manifest_from_dict` validator
drives both the build-time and runtime parse paths -- one schema, one
validator, one set of error messages.

Idempotent: re-running with no changes leaves the file byte-identical
(stable key ordering + a trailing newline). Safe to call from clean
checkouts and from CI.

Run standalone for a manual rebuild::

    python _build_pipeline_json.py
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

_PYPROJECT = Path(__file__).resolve().parent / "pyproject.toml"
_OUT = Path(__file__).resolve().parent / "pipeline.json"


def build_pipeline_json(pyproject_path: Path = _PYPROJECT) -> dict:
    """Read ``pyproject.toml`` and return the pipeline.json payload."""
    with pyproject_path.open("rb") as fh:
        data = tomllib.load(fh)

    project = data.get("project")
    if not isinstance(project, dict):
        raise SystemExit(
            f"{pyproject_path}: missing [project] table; cannot build "
            "pipeline.json"
        )

    name = project.get("name")
    version = project.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        raise SystemExit(
            f"{pyproject_path}: [project].name and [project].version "
            "are required and must be strings"
        )

    cb_block = (
        data.get("tool", {})
            .get("coralbricks", {})
            .get("pipeline")
    )
    if not isinstance(cb_block, dict):
        raise SystemExit(
            f"{pyproject_path}: missing [tool.coralbricks.pipeline] table; "
            "cannot build pipeline.json"
        )

    return {
        "project": {"name": name, "version": version},
        "tool": {"coralbricks": {"pipeline": cb_block}},
    }


def write_pipeline_json(out_path: Path = _OUT) -> Path:
    """Generate the sidecar at ``out_path`` and return the path."""
    payload = build_pipeline_json()
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    out_path.write_text(text, encoding="utf-8")
    return out_path


if __name__ == "__main__":
    p = write_pipeline_json()
    print(f"wrote {p}", file=sys.stderr)
