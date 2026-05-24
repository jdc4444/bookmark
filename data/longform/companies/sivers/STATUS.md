# Sivers Semiconductors dossier — v1 COMPLETE ✅

**Built:** 2026-05-23 (single session)
**Mode:** /loop 5m heartbeat (cron e559c81f — now stopped)

## Final report

- **Title:** *Sivers Semiconductors — the laser chokepoint at the start of the curve*
- **Subtitle:** *How a Stockholm-listed InP DFB laser maker became the upstream pure-play in the co-packaged optics supply chain — and what its planned US dual listing implies*
- **Chapters:** 13/13
- **Word count:** 15,352
- **Sources in bibliography:** 10 (lower than Unitika's 32 because Sivers has fewer English-language IR press-release pages; primary URLs collapse into fewer canonical sources)
- **Snapshot date:** 2026-05-23
- **Served at:** http://localhost:3515 → Longform → "🏢 Sivers Semiconductors..."
- **API:** `GET /api/longform/companies/sivers`

## Chapter manifest

| # | Chapter | Words |
|---|---|---|
| 1 | Snapshot | 897 |
| 2 | From Sivers Lab to Sivers Photonics — 75 years | 901 |
| 3 | Two subsidiaries, two supply chains | 1,583 |
| 4 | Two sets of books — and what the restatement actually means | 1,569 |
| 5 | The five quarters that built the rally | 1,231 |
| 6 | The team that is going to ring the Nasdaq bell | 1,683 |
| 7 | Where Sivers sits in the AI photonics stack | 1,476 |
| 8 | Why the comparison is COHR and LITE, but the analog is something smaller | 1,299 |
| 9 | Twelve months of headlines + how the equity story has traveled in specialist commentary | 1,032 |
| 10 | InP DFB lasers — patents, products, and the case for chokepoint pricing | 1,296 |
| 11 | What could break this | 1,168 |
| 12 | Three events that will decide whether the rally was right | 1,002 |
| 13 | Appendices | 215 |

## The story v1 tells

> 75-year-old Stockholm passive-microwave shop (Sivers Lab AB, founded 1951 by Carl von Sivers) became the leading independent western source of InP DFB lasers for AI data center co-packaged optics — through acquisitions of Scottish III-V photonics specialist CST Global in 2017 (now Sivers Photonics Ltd.) and US mmWave startup MixComm in 2022 (now Sivers Wireless AB). FY2025 revenue SEK 306.6M (+25% YoY, +33% constant FX), pipeline $453M (+64% YoY). PCAOB-restated EBIT loss SEK 177.8M — wider than the SEK 141.3M reported under Swedish K3, as the dual-listing accounting prep surfaces less-conservative items (revenue period reallocation, inventory revaluation, share-based comp fair value, impairment of capitalized development R&D). Stock SEK 1.80 → SEK 72.90 over five years = 40x move, currently at all-time high.
>
> The leadership team is built for Nasdaq New York. CEO Vickram Vathulya (PhD Lehigh, MBA Berkeley Haas, 25y semis at NXP/Maxim/Nuvotronics) appointed Aug 2024 with a personal commitment to buy up to 2M shares at up to $1M. Executive Chairman Bami Bastani (42y semis, 3× public-company turnaround CEO — ANADIGICS/Trident/Meru — ex-GlobalFoundries SVP) joined as Strategic Advisor Aug 2023 → Chairman Feb 2024. Co-founder Erik Fällström (controls 11.8% votes via DDM Debt + Family Office) departs the board June 15 2026 — clearing the room for a Nasdaq pitch DDM has publicly stated it doesn't fully support.
>
> The upstream chokepoint thesis runs through POET Technologies (Sept 2025 partnership, $1B+ ELS market target, prototypes H1 2026, production end-2026), Ayar Labs (SuperNova multi-wavelength light source — multi-year relationship since ECOC 2022), and O-Net/Enablence (ELS thermal solutions). NEMC funding $6.6M (May 2026, Year 2) anchors US DoD work. The June 15 AGM is the pivotal test: shareholders vote on a 15% dilution authority + board reshuffle. Sweden's Economic Crime Authority is investigating a 48h-early leak of the Nasdaq listing announcement via anonymous X account. MSCI Sweden Small Cap inclusion May 29 forces passive buying simultaneous with delayed Q1 results.

## Course-correction applied per user feedback

User flagged @aleabitoreddit was not a reliable primary source and should not be the analytical core. Course-correction:
1. **Killed in-flight writers** using contaminated briefs (leadership + customers_supply_chain)
2. **Audited 4 already-completed chapters** (snapshot, business_segments, financial_deep_dive, corporate_history) — verified all rely on primary sources: Sivers IR, PCAOB-restated annual report, joint POET/Sivers press release, independent substack analyst. Zero Aleabit-as-primary citations
3. **Rewrote 7 briefs** to remove all "Quote Aleabit" / "per Aleabit primary source" directives. Pattern-match arguments reframed as "cited generically, not attributed to any retail source"
4. **Bounded Aleabit's role** to one ~200-word subsection in news_catalysts titled "How the equity story has traveled in specialist commentary" — framed as one signal of retail propagation, not as a sourcing anchor
5. **Re-dispatched 9 chapters** with corrected briefs
6. **Post-completion audit confirmed**: 11 of 13 chapters have ZERO Aleabit references. The 2 chapters that do (news_catalysts + appendices) bound Aleabit to clearly-labeled retail-commentary / unverified-leads sections

## Architecture reused from Unitika

Zero new infrastructure required:
- `longform/research/runner_company.py` — chapter writer (claude -p)
- `longform/compose/compose_company.py` — assembler
- `/api/longform/companies/<id>` UI route
- Frontend report-picker with 🏢/👤 icons

Per-company data shape unchanged:
```
data/longform/companies/sivers/
├── meta.json
├── inputs/{ir_pages,news,stax,xalpha_excerpt}/
├── prep/{snapshot,financials,leadership,counterparties,history.md}/
│   └── chapters/   (13 chapter JSONs)
├── discover/briefs.json
└── reports/report.json
```

## What v1 deliberately defers (potential follow-ups)

| Item | Why deferred | When to revisit |
|---|---|---|
| Annual Report 2025 full PDF parse | Available; not fully scraped this session | When deeper financial line-item analysis needed |
| Q1 2026 results (delayed to May 29 2026) | Post-snapshot | When the actual results land |
| Joakim Nideborn + Helena Svancar full bios | Limited public profile at snapshot | After June 15 AGM proxy materials |
| Ekobrottsmyndigheten investigation outcome | Active investigation | When SE EM publishes findings |
| Direct hyperscaler validation tracker | Not yet announced | When NVIDIA/MSFT/META/AMZN names Sivers via integrator |
| Full patent breadth analysis | 3 issued + 16 pending stated; not USPTO/EPO line-item parsed | When IP density deserves a dedicated chapter |

## Sanity checks (all pass)

- ✅ Report appears in `/api/longform` list with `type: "company"`
- ✅ `/api/longform/companies/sivers` returns full assembled report
- ✅ Bookmark UI renders all 13 chapters
- ✅ ToC shows 13 chapters + 1 appendix item (14 ToC entries)
- ✅ Report-picker shows all 3 reports (Serenity profile, Sivers + Unitika companies)
- ✅ Aleabit references bounded to news_catalysts + appendices only
- ✅ All 10 other chapters use primary sources (IR + annual report + joint press releases + independent analysts)

## Cron status

`e559c81f` deleted. Loop ended. v1 delivered.
