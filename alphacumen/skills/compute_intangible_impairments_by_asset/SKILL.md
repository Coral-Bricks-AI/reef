---
id: compute_intangible_impairments_by_asset
when: Question asks to itemize Q4 / FY intangible-asset impairments by specific asset name (product, brand, IPR&D candidate) with amount and asset type, OR computes the share of impairments attributable to a named acquired portfolio (e.g., a target's pipeline post-acquisition).
applies_to: [sector_analyst]
source_lines: 0
---

**Dedicated tool: `compute_intangible_impairments_by_asset(ticker, fy, period_kind='q4'|'annual')`.** Pulls all XBRL `ImpairmentOfIntangibleAssetsExcludingGoodwill` facts for the requested period, parses each fact's `segment` array to extract the asset name (from `ProductOrServiceAxis` extension member, e.g. `pfe:MedrolMember` → "Medrol") and asset type (from `FiniteLivedIntangibleAssetsByMajorClassAxis` / `IndefiniteLivedIntangibleAssetsByMajorClassAxis` member — Brand, DevelopedTechnologyRights, InProcessResearchAndDevelopment), and returns a per-asset table + consolidated total.

`period_kind="q4"` (default for Q4 impairment questions) extracts facts whose period spans 3 months ending on the FY end-date (e.g. 2024-09-30 to 2024-12-31). `period_kind="annual"` extracts facts spanning the full FY (e.g. 2024-01-01 to 2024-12-31).

## Workflow

1. Call the tool ONCE with `ticker=<TICKER>`, `fy=<YYYY>` (the fiscal year whose 10-K discloses the impairments), and `period_kind` set to the temporal scope the question asks about.

2. Quote `answer_summary_block` verbatim. Each row gives `asset_name | asset_type | $amount` — directly answers "itemize Q4 impairments by asset name, amount, and asset type".

3. For "what % of impairments come from a named acquired portfolio (e.g., Seagen)" sub-questions: cross-reference the per-asset table against the acquired-target's pipeline disclosed in the acquirer's prior-year 10-K (M&A Note or Acquired-IPR&D Note). The pipeline list is narrative, not in XBRL; pull it via `extract_filing_tables(ref=<prior-FY 10-K>, table_keyword="acquired", item="8")` or `get_full_text(ref=<acquisition 10-K>, max_chars=20000)`. Sum the per-asset amounts that match acquired-pipeline names; divide by the consolidated total to get the % attributable.

## When this skill applies

- Pharma / biotech Q4 impairments (PFE-Seagen post-acquisition, MRK, BMY, JNJ, BIIB — issuers that tag impaired products by name in XBRL).
- Consumer / industrial brand impairments tagged by brand member.
- Tech goodwill / intangible writedowns when issuer breaks out by product / segment / brand axis.

NOT for: aggregate-only impairments (issuer doesn't tag by axis), goodwill-only impairments (use `ImpairmentOfGoodwill`), or future-looking impairment estimates (no XBRL backing).

## Common failure modes

- ❌ Pulling `ImpairmentOfIntangibleAssetsExcludingGoodwill` without parsing the segment dimensions: you get only the aggregate total, miss per-asset values.
- ❌ Treating extension-member names as opaque strings: decode CamelCase to human-readable (the tool does this).
- ❌ Confusing annual vs quarterly: pass `period_kind="q4"` for Q4-specific questions, `period_kind="annual"` for full-year.
- ❌ For acquired-portfolio attribution: the asset-name → acquisition-source mapping is NOT in XBRL — it requires reading the acquisition note in the acquirer's filing (prior-year 10-K or current-year M&A footnote).

Generic across any issuer with axis-dimensioned impairment disclosures; no eval-keyed asset names baked into the tool.
