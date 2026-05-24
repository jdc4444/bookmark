# AAOI dossier — STATUS

## Snapshot date: 2026-05-23

## Completed
- [x] Directory structure
- [x] meta.json (with corrected FY25 revenue $455.7M, not $186.7M)
- [x] inputs/stax/AAOI-5y-1d.json (5y daily candles)
- [x] inputs/news/_initial_findings.md
- [x] prep/leadership.json — all 7 directors + founder/CEO + 6 executives
- [x] prep/financials.json — FY25 10-K data, quarterly progression, segment mix, customer concentration
- [x] prep/counterparties.json — Digicomm + Microsoft + Amazon warrant + supply chain
- [x] prep/history.md — 1997-2026 corporate arc
- [x] prep/snapshot.json — headline summary for chapter writer
- [x] discover/briefs.json — 13-chapter briefs

## v1 COMPLETE 2026-05-23 19:55 local

- [x] All 13 chapters written via claude -p Sonnet (parallel)
- [x] Assembled report.json (22,448 words, 16 unique sources)
- [x] Validated UI render at http://127.0.0.1:3515/api/longform/companies/aaoi (HTTP 200)
- [x] Leadership chapter passes 11/11 diagnostic checks (every director profiled, no punt phrases)
- [x] meta.abstract used verbatim by assembler (1995 chars, no template leakage)

## Chapter word counts
| # | Chapter | Words |
|---|---|---|
| 1 | snapshot | 769 |
| 2 | corporate_history | 1,713 |
| 3 | business_segments | 1,615 |
| 4 | financial_deep_dive | 1,710 |
| 5 | earnings_story | 2,047 |
| 6 | leadership | 1,919 |
| 7 | customers_supply_chain | 1,655 |
| 8 | competitive_position | 1,785 |
| 9 | news_catalysts | 1,611 |
| 10 | ip_rd | 1,139 |
| 11 | risks | 1,736 |
| 12 | outlook | 1,692 |
| 13 | appendices | 1,059 |
| **Total** | | **22,448** |

## Key data points (for downstream verification)
- FY2025 revenue: $455.7M (+82.8% YoY from $249.4M)
- FY2025 GAAP gross margin: 30.0% (up from 24.8%)
- FY2025 net loss: $38.2M (narrowed from $186.7M)
- Accumulated deficit: $493.1M
- Segments FY25: CATV 53.8%, DC 42.9%, Telecom 3.0%, FTTH 0.3%
- Customer concentration: Digicomm 53.1% + Microsoft 28.8% = 81.9%
- Amazon warrant March 2025: up to 7.945M shares
- FY26 order book Mar-Apr 2026 from one hyperscaler: >$324M ($53M + >$200M 1.6T + $71M)
- Headcount: 4,691 (548 US, 1,262 Taiwan, 2,881 China)
- Patents: 199 US, 140 China/Taiwan, 10 European
- Stock: 5y range $1.50-$223.10, snapshot ~$181

## Critical voice rules
- Primary sources only (SEC EDGAR, IR pages, Lightreading/ConvergeDigest as primary news)
- NO Aleabit / Substack / X as primary source — bounded to ≤200 word subsection in news_catalysts only
- Past-tense narrative voice
- Quote management verbatim from earnings calls / press releases when paraphrasing claims
- Do not hedge with 'appears to' or 'seems to' — declarative statements only
