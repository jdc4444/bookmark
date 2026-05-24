# aleabit-report

Standalone longform on @aleabitoreddit (Serenity) — the AI/semi supply-chain
analyst whose @Reddit-WSB-trader-now-on-X feed has compounded into one of the
better public photonics calls of 2026. The report is a **thesis explainer**
written in an Economist register, not an audit: the 630% YTD self-report is
taken at face value; the work goes into making the thesis legible to a smart
reader who hasn't been following along.

## Deliverable

One longform document, 5,000–10,000 words, snapshot-style (no live refresh).
Roughly 10–15% profile, 85–90% the meat of the thesis. Rendered through a
forked Bookmark single-article reader with sticky chapter ToC.

## Pipeline

```
prep/        # pure-Python, no LLM. Freezes inputs, scans corpus, builds JSON
             # intermediates the chapter prompts feed on.
research/    # one Sonnet sub-agent call per chapter (claude_call harness from
             # Bookmark), 12–18 primary sources each.
compose/     # assembles chapters into report.json, dedupes bibliography,
             # generates ToC.
web/         # forked from Bookmark/web/, single-article reader.
server.py    # minimal http server, serves static + report.json.
```

## Inputs (frozen at snapshot time into `data/inputs/`)

- `~/Documents/Code/xalpha/data/aleabitoreddit/` — tweets, calls_llm, calls_spark,
  portfolio, portfolio_charts, dossiers, signals, network, following
- `~/Documents/Code/xalpha/portfolio_charts.py` + `portfolio.py` — for the 553%
  reconstruction (sidebar metric, not the headline)
- `~/Documents/Code/Bookmark/data/modules/` — pre-cached ticker + poster
  encyclopedia entries (Sonnet-subagent quality)
- `~/Documents/Code/fin_v1/kb/sources/` — 216 primary citations (sourced;
  reliable). NOT `kb/derived/*` (engine output) or `kb/tickers/*` (manual
  opinion) for direct citation.

## Sourcing rules

- `kb/sources/` URLs: quote freely
- `kb/tickers/`, `kb/claims/`, `kb/subcycles/`: frame as *fin_v1's view*, never as fact
- `kb/derived/*`: orientation only, never cite
- xalpha portfolio numbers: cite as reconstructions with explicit method
- Direct Serenity quotes via Bookmark's macro/post UI render path

## Chapter map

1. Cover & preamble (~400w)
2. Profile of Serenity (~700w) — bio, follow graph, who-talks-about-them, style
3. The "functional monopolies" worldview (~700w)
4. Photonics supercycle (~1,500w) — deepest chapter
5. HBM & advanced packaging (~1,000w)
6. AI infra & neoclouds (~800w)
7. Hyperscaler & sovereign AI book (~800w)
8. What hasn't worked / what they avoid (~500w)
9. The reading network (~500w) — neutral framing of dialogue cluster
10. Open questions (~400w)

Total: ~7,300 words baseline, room to expand the deepest chapters.

## Port

`.plrc` claims 3489. Strict — no auto-rebind.
