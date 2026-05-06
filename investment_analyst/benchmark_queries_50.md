# Investment Analyst Benchmark Queries (50)

**Date:** May 1, 2026
**Purpose:** Test and benchmark the cb_ia investment analyst pipeline across all data sources (GDELT, SEC, Stock/Options, Macro, Reddit, Scraped Articles).

Queries are organized by category and tagged with the primary data sources they should exercise.

---

## Earnings & Financials (SEC + Stock)

1. "Tell me about Amazon's Q1 2026 earnings announced yesterday."
2. "What did Microsoft report in Q1 2026? How did Azure growth compare to expectations?"
3. "Compare Alphabet and Microsoft cloud revenue growth in their latest quarter."
4. "Did NVIDIA beat earnings estimates in its most recent filing? What was the data center revenue?"
5. "Analyze Eli Lilly's Q1 2026 earnings — revenue, EPS, and guidance for the GLP-1 segment."
6. "What is Chevron's latest financial performance? How do margins compare to Exxon?"
7. "Has Snowflake filed any earnings since January 2026? What was net revenue retention?"
8. "Show me Palantir's most recent 8-K filing and explain the key takeaways."
9. "What did Meta report for Q1 2026? Focus on ad revenue and Reality Labs losses."
10. "Compare Apple and Samsung — who had stronger Q1 2026 results?"

## Geopolitical & Macro Risk (GDELT + Macro + Scraped Articles)

11. "What is the impact of the Iran war on global oil markets?"
12. "How has the Strait of Hormuz closure affected energy stocks like Chevron and Exxon?"
13. "What are the latest regulatory risks around TikTok, Meta, and Alphabet?"
14. "How have Trump's tariffs affected semiconductor supply chains in 2026?"
15. "What macro risks should I be aware of heading into Q2 2026?"
16. "Has there been a ceasefire in the Iran conflict? What's the market impact?"
17. "How has the US-China trade relationship evolved since the new Section 301 investigations?"
18. "What are the tail risks for the S&P 500 right now?"
19. "How is the Federal Reserve's rate path affecting growth stocks in 2026?"
20. "What geopolitical events have moved oil prices more than 5% this year?"

## Stock Analysis & Technicals (Stock + Options + Macro)

21. "Is the SNAP earnings move priced in?"
22. "What's NVIDIA's technical setup ahead of its next earnings?"
23. "Compare the put/call ratio and implied volatility for AMD vs NVDA."
24. "Is Tesla overbought or oversold based on the last 90 days of price action?"
25. "What does the options flow say about Amazon ahead of earnings?"
26. "Analyze Chevron's stock performance since the Iran war started in February."
27. "Compare NVO and LLY stock performance year-to-date. Who's winning the GLP-1 trade?"
28. "What's the max pain for AAPL options expiring this Friday?"
29. "Show me Broadcom's price action and RSI over the past 6 months."
30. "Is the energy sector (XOM, CVX, SLB) still a buy at current crude prices?"

## Competitive Landscape & VC (Scraped Articles + GDELT)

31. "Show me the ecosystem around NVIDIA for AI data center startups."
32. "Compare Snowflake and Databricks on product launches and strategic alliances this year."
33. "What startups are competing with NVIDIA in custom AI silicon?"
34. "How is the cloud market share shifting between AWS, Azure, and Google Cloud in 2026?"
35. "What are the competitive threats to Novo Nordisk in the GLP-1 market?"
36. "Which AI infrastructure companies received the largest funding rounds in Q1 2026?"
37. "How is the TikTok divestiture affecting Meta's and Alphabet's competitive position?"
38. "What's Cerebras doing differently from NVIDIA in the AI training market?"
39. "Compare Boeing and Airbus on order book and delivery trends since 2025."
40. "Which defense stocks benefit most from increased Middle East military spending?"

## Foreign Private Issuers (20-F / 6-K)

41. "What's changed for Novo Nordisk since last quarter? Show me their latest 6-K filing."
42. "Compare ASML's latest 20-F with NVIDIA's 10-K on R&D spending."
43. "Has Taiwan Semiconductor (TSM) filed any recent 6-K disclosures about geopolitical risk?"
44. "What does Novo Nordisk's annual report say about CagriSema clinical trial results?"

## Multi-Source / Complex Queries

45. "Analyze NVIDIA's 8-K filing in March 2026. Provide key takeaways, AI supply chain implications, hidden risks, and a trade recommendation."
46. "What is the impact of recent geopolitical events on Chevron Corp stock? Include price action, macro context, and regulatory risks."
47. "Give me a full investment thesis on Eli Lilly — financials, competitive moat, GLP-1 pipeline, and macro risks."
48. "Compare the Magnificent 7 stocks on AI capex spending and which ones are seeing the best return on investment."
49. "Is there a correlation between rising oil prices and semiconductor stock performance this year?"
50. "Build me a risk scorecard for a portfolio holding NVDA, AMZN, LLY, CVX, and SNAP — what could go wrong for each?"

---

## Data Source Coverage Matrix

| Category | Queries | SEC | GDELT | Stock | Options | Macro | Scraped | Reddit |
|---|---|---|---|---|---|---|---|---|
| Earnings & Financials | 1-10 | ✓ | | ✓ | | | | |
| Geopolitical & Macro | 11-20 | | ✓ | | | ✓ | ✓ | |
| Stock & Technicals | 21-30 | | | ✓ | ✓ | ✓ | | ✓ |
| Competitive & VC | 31-40 | | ✓ | | | | ✓ | |
| Foreign Issuers | 41-44 | ✓ | | ✓ | | | | |
| Multi-Source | 45-50 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
