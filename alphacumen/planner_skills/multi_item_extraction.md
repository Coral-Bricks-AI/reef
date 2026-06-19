---
id: multi_item_extraction
when: Query asks to "list all" / "summarize all terms" / "what KPIs does X track" -- multi-item extraction from a filing (raise max_steps).
applies_to: [sector_analyst]
source_lines: 699-706
---

- **Multi-item extraction → raise max_steps to 12.** When the
  question asks to "list all", "summarize all terms", "what KPIs
  does X track", or otherwise requires extracting many items from
  a filing, set `max_steps: 12` (not the default 6) in the
  `invoke_next` instruction for `sector_analyst`. The specialist
  needs extra tool calls to read multiple chunks from the same
  filing (e.g. a 28-chunk prospectus or a 10-chunk proxy statement).
  The default 8 steps is insufficient for 10+ item extraction.
