# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``alphacumen.capabilities`` -- model-facing rendering of index schemas.

alphacumen tools are bound to specific platform indices (``GDELT_EVENTS_INDEX``
for ``bm25_gdelt``, ``GRAPH_INDEX`` for ``query_graph`` / ``multihop_graph``,
...). The platform now ships a typed schema for every per-verb config a
registration declares (slice 5d), and ``coralbricks.sandbox.tools.list_tools``
surfaces it back to the runner. This module is the alphacumen-side glue:

1. :func:`fetch_index_capabilities` calls ``list_tools`` and indexes
   the response by slug so the rest of alphacumen can look up the schema
   for a given index without re-issuing RPCs.
2. :func:`render_*_section` functions render the per-verb schema into
   plain-text fragments suitable for appending to a tool's
   ``description``. The model reads these to know what BM25 field
   names exist, what SQL tables / columns are available, what edge
   predicates the multihop verb accepts, etc. The gateway also
   call-time-validates these args, so the worst case for a model that
   ignores the rendered schema is a clean ``ToolPolicyError`` listing
   the valid set -- the model self-corrects on the next turn.

Why render rather than enum?
----------------------------

We could (and for some keys, do) inject ``"enum": [...]`` constraints
into the JSON schema we hand the model. But the model also needs the
*description* of each option (a plain ``enum`` of ``["title", "body",
"mentions.entity"]`` doesn't tell it which field is best for entity
queries). Rendering the schema into the description gives the model
both the names and the rationale; the JSON-schema enum is a belt-and
braces backup we add for fields where the registration ships
descriptions short enough not to bloat the parameters block.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from harness.stubs import tools as cb_tools

logger = logging.getLogger(__name__)


IndexCapabilitiesMap = dict[str, dict[str, dict[str, Any]]]
"""``{slug: {verb: <typed-cfg>}}``.

Convenience alias used across the alphacumen capability-rendering and
specialist-binding code paths. The inner ``<typed-cfg>`` shape is
defined by :mod:`gateway.store.indices.capabilities` and validated
at registration time, so consumers can rely on the keys being
present + correctly typed.
"""


def fetch_index_capabilities() -> IndexCapabilitiesMap:
    """Call ``tools.list`` and index the response by slug.

    Returns an empty mapping when the gateway is unreachable or
    returns a malformed payload -- callers fall back to the static
    tool descriptions in that case so the swarm still launches
    rather than failing the whole run on a discovery hiccup.
    """
    try:
        env = cb_tools.list_tools()
    except Exception as exc:  # noqa: BLE001 - defensive, see docstring
        logger.warning(
            "list_tools failed; falling back to static tool descriptions: %s",
            exc,
        )
        return {}
    indices = env.get("indices") or []
    out: IndexCapabilitiesMap = {}
    for entry in indices:
        if not isinstance(entry, Mapping):
            continue
        slug = entry.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        caps = entry.get("capabilities") or {}
        if not isinstance(caps, Mapping):
            continue
        out[slug] = {
            verb: dict(cfg) for verb, cfg in caps.items()
            if isinstance(cfg, Mapping)
        }
    return out


# ---------------------------------------------------------------------------
# Per-verb rendering -- each takes the typed ``cap_cfg`` for one verb on
# one index and returns a multi-line string ready to append to a tool
# description (or "" when there's nothing to say).
# ---------------------------------------------------------------------------


def render_bm25_section(slug: str, cfg: Optional[Mapping[str, Any]]) -> str:
    if not cfg:
        return ""
    parts: list[str] = []
    fields = cfg.get("fields") or []
    if fields:
        lines = [f"\n\nAvailable BM25 fields on `{slug}`:"]
        for f in fields:
            if not isinstance(f, Mapping):
                continue
            name = f.get("name")
            if not isinstance(name, str):
                continue
            type_ = f.get("type") or "text"
            boost = f.get("boost")
            boost_part = f", default boost {boost:g}" if isinstance(boost, (int, float)) else ""
            desc = f.get("description")
            desc_part = f": {desc}" if isinstance(desc, str) and desc else ""
            lines.append(f"- `{name}` ({type_}{boost_part}){desc_part}")
        lines.append(
            "\nPass `fields` as a list of these names (optionally with "
            "`^boost`, e.g. `\"title^4\"`); unknown names are rejected by "
            "the gateway with a ToolPolicyError."
        )
        parts.append("\n".join(lines))
    filterable = cfg.get("filterable_fields") or []
    if filterable:
        flines = [f"\n\nFilterable fields on `{slug}` (pass inside `filters`):"]
        for f in filterable:
            if not isinstance(f, Mapping):
                continue
            name = f.get("name")
            if not isinstance(name, str):
                continue
            type_ = f.get("type") or "keyword"
            desc = f.get("description")
            desc_part = f": {desc}" if isinstance(desc, str) and desc else ""
            flines.append(f"- `{name}` ({type_}){desc_part}")
        parts.append("\n".join(flines))
    return "".join(parts)


def render_ann_section(slug: str, cfg: Optional[Mapping[str, Any]]) -> str:
    if not cfg:
        return ""
    parts: list[str] = []
    embedder = cfg.get("embedder")
    dim = cfg.get("vector_dim")
    metric = cfg.get("metric")
    if isinstance(embedder, str):
        meta = [f"embedder=`{embedder}`"]
        if isinstance(dim, int):
            meta.append(f"dim={dim}")
        if isinstance(metric, str):
            meta.append(f"metric={metric}")
        parts.append(
            f"\n\nVector schema on `{slug}`: {', '.join(meta)}. "
            f"You pass `text` and the gateway runs the embedder "
            f"server-side."
        )
    filterable = cfg.get("filterable_fields") or []
    if filterable:
        parts.append(f"\n\nFilterable fields on `{slug}`:")
        for f in filterable:
            if not isinstance(f, Mapping):
                continue
            name = f.get("name")
            if not isinstance(name, str):
                continue
            type_ = f.get("type") or "keyword"
            desc = f.get("description")
            desc_part = f": {desc}" if isinstance(desc, str) and desc else ""
            parts.append(f"- `{name}` ({type_}){desc_part}")
    return "".join(parts)


def render_sql_section(slug: str, cfg: Optional[Mapping[str, Any]]) -> str:
    if not cfg:
        return ""
    tables = cfg.get("tables") or []
    if not tables:
        return ""
    lines = [f"\n\nTables available via SQL on `{slug}`:"]
    for t in tables:
        if not isinstance(t, Mapping):
            continue
        name = t.get("name")
        if not isinstance(name, str):
            continue
        desc = t.get("description")
        head = f"- `{name}`"
        if isinstance(desc, str) and desc:
            head += f" -- {desc}"
        lines.append(head)
        for c in t.get("columns") or []:
            if not isinstance(c, Mapping):
                continue
            cname = c.get("name")
            if not isinstance(cname, str):
                continue
            ctype = c.get("type") or ""
            cdesc = c.get("description")
            cdesc_part = f" -- {cdesc}" if isinstance(cdesc, str) and cdesc else ""
            lines.append(f"    - `{cname}` ({ctype}){cdesc_part}")
    return "\n".join(lines)


def render_multihop_section(slug: str, cfg: Optional[Mapping[str, Any]]) -> str:
    if not cfg:
        return ""
    parts: list[str] = []
    node_types = cfg.get("node_types") or []
    if node_types:
        lines = [f"\n\nNode types on `{slug}`:"]
        for n in node_types:
            if not isinstance(n, Mapping):
                continue
            name = n.get("name")
            if not isinstance(name, str):
                continue
            desc = n.get("description")
            desc_part = f": {desc}" if isinstance(desc, str) and desc else ""
            lines.append(f"- `{name}`{desc_part}")
        parts.append("\n".join(lines))
    predicates = cfg.get("predicates") or []
    if predicates:
        lines = [f"\n\nEdge predicates on `{slug}` (use these in `predicate_filter`):"]
        for p in predicates:
            if not isinstance(p, Mapping):
                continue
            name = p.get("name")
            if not isinstance(name, str):
                continue
            desc = p.get("description")
            desc_part = f": {desc}" if isinstance(desc, str) and desc else ""
            lines.append(f"- `{name}`{desc_part}")
        parts.append("\n".join(lines))
    return "".join(parts)


def render_get_section(slug: str, cfg: Optional[Mapping[str, Any]]) -> str:
    if not cfg:
        return ""
    schema = cfg.get("doc_schema") or []
    if not schema:
        return ""
    lines = [f"\n\nFields returned by `tools.get` on `{slug}`:"]
    for f in schema:
        if not isinstance(f, Mapping):
            continue
        name = f.get("name")
        if not isinstance(name, str):
            continue
        type_ = f.get("type") or ""
        desc = f.get("description")
        desc_part = f": {desc}" if isinstance(desc, str) and desc else ""
        lines.append(f"- `{name}` ({type_}){desc_part}")
    return "\n".join(lines)


_VERB_RENDERERS = {
    "bm25": render_bm25_section,
    "ann": render_ann_section,
    "sql": render_sql_section,
    "multihop": render_multihop_section,
    "get": render_get_section,
}


def render_index_section(
    slug: str,
    verbs: tuple[str, ...],
    capabilities: IndexCapabilitiesMap,
) -> str:
    """Render the requested ``verbs`` of a single index into one fragment.

    Returns an empty string when the slug is not known to the
    capability map (e.g. the registry didn't include it because the
    pipeline's manifest didn't declare it). Caller should ignore the
    fragment in that case -- the static tool description is still
    valid, just less informative.
    """
    cfg_for_index = capabilities.get(slug)
    if not cfg_for_index:
        return ""
    chunks: list[str] = []
    for verb in verbs:
        renderer = _VERB_RENDERERS.get(verb)
        if renderer is None:
            continue
        cfg = cfg_for_index.get(verb)
        chunks.append(renderer(slug, cfg))
    return "".join(chunks)


__all__ = [
    "IndexCapabilitiesMap",
    "fetch_index_capabilities",
    "render_ann_section",
    "render_bm25_section",
    "render_get_section",
    "render_index_section",
    "render_multihop_section",
    "render_sql_section",
]
