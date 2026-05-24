# Unitika dossier — v1 COMPLETE ✅

**Built:** 2026-05-22 → 2026-05-23 (4 iterations of /loop 5m heartbeat)
**Mode:** autonomous orchestration, cron 62672e9a (now stopped)

## Final report

- **Title:** *Unitika — a polymer chokepoint at a crossroads*
- **Subtitle:** *How a 137-year-old Osaka synthetic-fiber maker is restructuring around technical films, glass fibers, and an unsolved succession question*
- **Chapters:** 13/13
- **Word count:** 13,130
- **Sources in bibliography:** 32
- **Snapshot date:** 2026-05-22
- **Served at:** http://localhost:3515 → Longform tab → "🏢 Unitika — a polymer chokepoint at a crossroads"
- **API:** `GET /api/longform/companies/unitika`

## Chapter manifest

| # | Chapter | Words |
|---|---|---|
| 1 | Snapshot | 814 |
| 2 | From Amagasaki to Unitika — a 137-year arc | 1089 |
| 3 | What Unitika actually sells | 1329 |
| 4 | The numbers behind the turnaround | 1444 |
| 5 | The last four quarters in narrative form | 1041 |
| 6 | The board after REVIC | 861 |
| 7 | Buyers, sellers, and the people forgiving the debt | 1124 |
| 8 | Where Unitika sits among Japan's polymer giants | 1145 |
| 9 | Twelve months of headlines | new |
| 10 | Patents, products, and the case for specialty pricing | new |
| 11 | What could break this | 908 |
| 12 | What to watch for next | 799 |
| 13 | Appendices | 217 |

## The story v1 tells

> 137-year-old Osaka polymer/fiber maker mid-restructuring under REVIC supervision (¥35B total commitment: ¥20B equity + ¥15B financing). Lenders forgave up to ¥43B. New career-engineer CEO (Fujii Minoru, joined 1987, ran the glass-fiber division before promotion). FY3/25 saw a ¥37.9B impairment driving a ¥24.3B net loss, but OP flipped positive. FY3/26 9M: OP +110% to ¥9.0B, NI ¥10.6B (from -¥24.4B), cash ¥13.5B → ¥58.0B, equity ratio 10.4% → 25.2%. Strategic pivot: shrink from ¥126B → ¥70B revenue by FY3/29, exit textiles entirely (5 buyers: Seiren, Shikibo, Kawabo, Zuiko, Boken Quality Eval Org), concentrate on chip-substrate glass fibers, retort-barrier film (EMBLEM HG), semiconductor release film (UniPeel), mmWave PCB film (DIC partnership at CES 2026). Whether 9.3% OP margin at ¥70B beats Toray's 5.6% — that's the question Fujii has 5 years to answer.

## Architecture delivered (reusable for next company)

New code:
- `longform/research/runner_company.py` — per-chapter Sonnet sub-agent runner (claude -p CLI)
- `longform/compose/compose_company.py` — company-shape report.json assembler
- `x_bookmark_ui.py` route additions — `/api/longform/companies/<id>` GET + list

Frontend tweaks:
- `web/app.js` — added `state.longformReportsList` + ToC report-picker (🏢 company / 👤 profile icons)
- `web/styles.css` — `.lf-toc-report-picker` styling

Bug fix:
- URL-decode in route dispatcher so `companies%2Funitika` resolves correctly

Data shape:
- `data/longform/companies/<slug>/` parallel to existing `data/longform/<slug>/`
- prep/ contains: snapshot, financials, leadership, counterparties, peers, press_release_index, timeline, history.md
- inputs/ contains: stax candles, ir_pages (PDFs + pdftotext), news (.md summaries)

## What v1 deliberately deferred

| Item | Why | When to revisit |
|---|---|---|
| EDINET v2 API filings | Needs free Subscription-Key registration | When user registers; gives structured XBRL |
| JP earnings-call audio transcription | Per original constraint — separate lane | Later, with Whisper/MLX pipeline |
| Kawabo buyer name verification | Search returned ambiguous result (could be Kawabo Co. or Kawashima Selkon) | When pulling individual M&A filings |
| Asahi Kasei / Mitsubishi Chemical / Kuraray FY2025 detail | peers.json has placeholder entries | When competitor benchmarking gets deeper |
| News pull beyond initial 6-month window | We covered 24mo from search but didn't deep-pull every Reuters/Nikkei article | Future iteration if Unitika moves materially |

## Sanity check

All UI verified live:
- ✅ Report appears in `/api/longform` list with `type: "company"`
- ✅ `/api/longform/companies/unitika` returns full assembled report
- ✅ Bookmark UI renders all 13 chapters
- ✅ ToC shows 13 chapters + Companies + Sources appendices
- ✅ Bibliography lists all 32 sources
- ✅ Report-picker switches cleanly between Serenity (profile) and Unitika (company)
- ✅ Mobile collapsed ToC topbar works (built earlier in conversation)
- ✅ Subtitle correctly reads "137-year-old" (was 130 in draft; fixed)

## Cron status

`62672e9a` deleted. Loop ended. v1 delivered.
