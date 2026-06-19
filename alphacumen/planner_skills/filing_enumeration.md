---
id: filing_enumeration
when: Query asks whether a company has filed a specific type of disclosure ("has X filed any Y about Z").
applies_to: [sector_analyst]
source_lines: 787-797
---

- **Filing-enumeration queries ("has X filed any Y about Z"):** When
  the user asks whether a company has filed a specific type of
  disclosure, instruct `sector_analyst` to:
  1. Search with `k=10` to scan multiple recent filings of that type
  2. Characterize what each filing covers (earnings, governance,
     routine updates, etc.) — don't just read the first hit
  3. Explicitly state if NONE of the filings address the specific
     topic asked about — "no relevant filing found" is a valid answer
  4. Cross-reference the annual filing (10-K Item 1A for US companies,
     20-F Item 3 for foreign private issuers) since risk disclosures
     often live there, not in periodic 8-K/6-K filings
