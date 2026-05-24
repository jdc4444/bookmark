# Longform pipeline — handoff

State of the world as of 2026-05-10. Read this if you're picking up work on
the longform-report pipeline (lives in `Bookmark/longform/`, data lives in
`Bookmark/data/longform/<report_id>/`).

## TL;DR what got built

A generic pipeline that turns one X handle into a 5,000–10,000-word
longform report rendered inside Bookmark's frontend. Originally built
end-to-end for `@aleabitoreddit` (Serenity); now generic-with-discovery so
any handle in the xalpha corpus can be processed.

A snapshot for `aleabit` is shipped: 7 chapters, 9,812 words, 91 sources, 70
companies, 34 concepts, 44 dated predictions tracked against live prices.

## Pipeline shape

```
Bookmark/longform/
├── _paths.py                   # path resolver + report-id validation
├── build.py                    # CLI orchestrator: freeze → prep → discover → research → assemble
├── HANDOFF.md                  # ← you are here
├── prep/                       # pure-data, no LLM
│   ├── freeze.py               # snapshot xalpha + Bookmark modules + kb sources → inputs/
│   ├── profile.py              # bio + tweet volume + reply graph + dossiers → prep/profile.json
│   ├── corpus_scan.py          # scan all 85k xalpha handles for mentions of subject
│   ├── themes.py               # join discover/themes.json with calls + dossiers
│   └── reconstruct.py          # in-sample portfolio reconstruction (sidebar metric)
├── discover/                   # LLM-driven (Sonnet, no tools)
│   ├── themes.py               # propose 3–7 ticker clusters from the data
│   ├── briefs.py               # write chapter briefs (research directives, not theses)
│   ├── timeline.py             # extract dated predictions from tweets via codex GPT-5.5
│   ├── target_parser.py        # turn "30-40% growth" → structured target schema
│   ├── stax_prices.py          # read-only Stax sqlite reader + supplement fall-through
│   ├── timeline_progress.py    # join targets + prices + market caps → progress %
│   ├── seed_supplement.py      # populate supplement DB via yfinance (candles + quotes + FX)
│   ├── supplement_cache.py     # SQLite supplement: candles_cache + quote_cache + fx_cache
│   ├── symbol_aliases.json     # ticker → Yahoo/Stax key + canonical currency
│   └── tests/                  # pytest, currently 19 passing
├── research/runner.py          # one Sonnet sub-agent call per chapter (claude -p, --effort high)
└── compose/assemble.py         # merge chapter JSONs + entities + timeline → reports/report.json
```

Per-report data:

```
Bookmark/data/longform/<report_id>/
├── meta.json                   # handle, display_name, snapshot_date, title, subtitle, byline
├── inputs/                     # frozen snapshot; never written to after freeze
│   ├── xalpha/<handle>/        # tweets, calls_llm, portfolio, dossiers, etc.
│   ├── xalpha_engine/          # frozen portfolio_charts.py + portfolio.py for reconstruction
│   ├── bookmark_modules/       # poster + ticker modules from Bookmark
│   ├── kb_sources/             # fin_v1 primary citations
│   └── stax_supplement.sqlite3 # Yahoo-supplemented candles + quotes + FX
├── discover/
│   ├── themes.json             # editable: ticker clusters
│   └── briefs.json             # editable: chapter list + briefs (defines ORDER)
├── prep/
│   ├── profile.json
│   ├── corpus_mentions.json
│   ├── themes.json             # joined cluster data — used by research stage
│   ├── reconstruction.json
│   ├── timeline.json           # schema_version: 2 — predictions w/ targets + progress
│   └── chapters/<slug>.json    # one per chapter
└── reports/report.json         # final assembled artifact, served by /api/longform
```

## Frontend

Bookmark's existing UI gained a "Longform" mode (alongside Posts / Articles
/ Index). All longform code is namespaced `lf-` in CSS and `renderLongform*`
in JS.

- Endpoints in `x_bookmark_ui.py`: `GET /api/longform`, `GET /api/longform/<report_id>`
- Topbar tab `#longformToggle` flips body class `longform-mode`
- Reader: sticky ToC + chapter spans + appendices (Companies, Glossary, Timeline, Sources)

### Companies appendix
- Keyed by uppercased ticker (deduped — was breaking when same company appeared with/without ticker)
- Sorted by **Serenity rank**: dossier present first, then `score` desc, then `total_mentions` desc, then name
- Tier badge (S/A/B/C/BEAR) on each card
- Per-company quotes pulled by extracting `> ...` block-quotes adjacent to a ticker mention in the chapter bodies

### Per-company timeline
- Replaces an earlier flat chronological list
- One card per ticker, ordered by predictions count desc
- Each prediction row: post date · target line (resolved + raw) · progress bar · status pill · entry/current prices
- Status filter chip row at top (All / Met / In progress / Catalysts / etc.)
- Progress is FX-converted when target currency ≠ quote currency (badge: `≈ converted` with rate tooltip)

## Status taxonomy (codex's enum, used everywhere)

| status | meaning | UI label |
|---|---|---|
| `hit` | current price (or peak) crossed threshold | "met" (green) |
| `in_progress` | numeric target, still pending, target_date not passed | "in progress" (blue) |
| `miss` | target_date passed without crossing threshold | "missed" (red) |
| `qualitative` | event/comparative/revenue — no numeric target | "catalyst" (grey) |
| `currency_mismatch` | target currency ≠ quote currency AND no FX route | "currency mismatch" (yellow) |
| `missing_cache` | no price data anywhere | "no price data" (grey) |
| `ok` (price_status only) | price resolution succeeded |

After the FX work, `currency_mismatch` and `missing_cache` should be 0 for any
report whose tickers have been seeded.

## How to run from scratch

```bash
cd /Users/alphaone/Documents/Code/Bookmark

# 1. Freeze inputs (snapshot from live xalpha + Bookmark + fin_v1):
python3 longform/build.py <handle>                    # all stages, default flow

# Or stage-by-stage:
python3 longform/build.py <handle> --stages freeze
python3 longform/build.py <handle> --stages prep
python3 longform/build.py <handle> --stages discover  # writes themes.json + briefs.json — review/edit before continuing
python3 longform/build.py <handle> --stages research  # ~30–60min for a 7-chapter report (parallelizable)
python3 longform/build.py <handle> --stages assemble

# 2. Seed prices for tickers that aren't in user's stax cache:
python3 longform/discover/seed_supplement.py --report-id <id>
# fetches 5Y daily candles + market cap via yfinance + 7 USD-X FX rates

# 3. Extract dated predictions and compute progress:
# (timeline.py is invoked separately — see longform/discover/timeline.py for codex shell-out)
python3 longform/discover/timeline_progress.py --report-id <id>

# 4. Re-assemble after any data change:
python3 longform/compose/assemble.py --report-id <id>

# 5. Open: Bookmark UI → Longform tab.
```

## Known reports

- `aleabit` (handle: aleabitoreddit, display: Serenity) — full pipeline run, all 7 chapters present, 44 predictions enriched. **Ready to read.**

To list reports: `python3 longform/build.py --list`

## What still works manually but isn't yet automated

1. **Theme + brief editing**: `discover/themes.json` and `discover/briefs.json` are designed to be human-edited before `research` runs. The `--force-discover` flag re-runs the LLM versions, but typically you write themes/briefs by hand for tone control.

2. **Codex shell-out for `timeline.py`**: `discover/timeline.py` invokes codex CLI directly to extract dated predictions. There's no Python-only path for that step — it really needs GPT-5.5 to interpret natural-language timing claims at scale.

3. **Window expansion**: `discover/timeline.py --window-days 365` is supported; aleabit currently runs at the default 180. User asked for 365 expansion as a follow-up; not done yet.

## Codex collaboration pattern

This pipeline was built by Claude (me) doing UI + planning + orchestration,
and codex (GPT-5.5 via the codex CLI) doing the data-pipeline implementation.
Pattern that worked:

1. Claude writes a plan doc (e.g. `/tmp/timeline-redesign-plan-vN.md`)
2. Send to `codex exec --sandbox read-only` for critique. Codex flags real
   issues (multi-ticker fan-out, currency on foreign listings, schema gaps).
3. Iterate plan to v2/v3 incorporating codex's feedback
4. Send to `codex exec --sandbox workspace-write` to implement
5. Codex writes the data side; Claude does the UI; both verify with tests

Codex's pushbacks were consistently high-signal. Worth keeping as a check
even when you're sure the design is right.

### Codex CLI invocation (the working incantation)

```bash
nohup /Applications/Codex.app/Contents/Resources/codex exec \
  --model gpt-5.5 \
  --skip-git-repo-check \
  --sandbox workspace-write \
  --output-last-message /tmp/codex-LABEL.txt \
  --cd /Users/alphaone/Documents/Code/Bookmark \
  - < /tmp/plan-doc.md > /tmp/codex-LABEL.log 2>&1 &
disown
```

`nohup` matters — backgrounded `codex exec` invocations have died silently
multiple times when the parent bash shell got killed by harness restarts.

## Quirks and gotchas

### Yahoo rate-limit story
- Our raw-urllib + crumb-auth fetcher worked in tests but our IP got
  permanent-banned by Yahoo from the earlier no-crumb fast-fire attempts.
  Even after a 2h wait + crumb auth, every endpoint returned HTTP 429.
- Fix: swapped the YahooClient internals for **yfinance** in
  `seed_supplement.py:fetch_candles` and `fetch_quote`. yfinance handles
  cookies/crumb/backoff differently and gets through.
- The crumb code is still in seed_supplement.py for reference. Unused but
  harmless.

### Stax DB is read-only for us
- We never write to `~/Library/Application Support/stax/stax.db` — that's
  the user's live Stax app. The supplement DB is at
  `data/longform/<id>/inputs/stax_supplement.sqlite3` and `stax_prices.py`
  falls through to it on cache miss.

### `derive_report_id`
- Strips `oreddit` and `reddit` suffixes (so `aleabitoreddit` → `aleabit`).
  If you add a new handle that ends in `reddit` but you want to keep that,
  pass `--report-id <id>` explicitly.

### Schema migration history
- `timeline.json` has a `schema_version` field. v1 was a flat
  predictions list with no enrichment. v2 (current) is one row per (tweet,
  ticker) fan-out with target/price/progress fields populated.
- The frontend gracefully falls back to v1 rendering if `schema_version` is
  missing — don't be surprised if you see a flat list during pipeline
  partial state.

### Codex's status enum vs. mine in plan v3
- Plan v3 spec'd 7 statuses (met/missed/on_track/behind/pending/expired_near/ahead).
- Codex implemented 4 (hit/in_progress/missing_cache/qualitative) plus
  later added `miss` and `currency_mismatch`.
- The frontend handles both vocabularies. If you change either side, make
  sure `_statusPill`, `_formatRollup`, and the `filterStatuses` array in
  `app.js` are kept in sync, plus the matching CSS classes in `styles.css`.

### `validate_report_id`
- Every entrypoint calls `validate_report_id(args.report_id)` before doing
  anything destructive. Slugs must match `^[a-z0-9_-]+$`. Pass
  `--report-id` explicitly if you don't want the auto-derived form.

### Codex's tests
- `pytest longform/discover/tests/` — 19 tests passing covering target
  parsing edge cases, supplement cache round-trips, and stax_prices
  fall-through. Run after any change to discover/* code.

## Open work / follow-ups (in priority order)

1. **Window expansion to 365 days** — user asked for this; deferred
   while the missing-data + FX work landed. To do:
   ```bash
   python3 longform/discover/timeline.py --report-id aleabit --window-days 365 --force
   python3 longform/discover/timeline_progress.py --report-id aleabit
   python3 longform/compose/assemble.py --report-id aleabit
   ```
   Will roughly double the prediction count and pick up older calls
   (Sept–Dec 2025) currently outside the window.

2. **HBM-related Asian tickers** — `MSS` / `MSSCorp` correctly aliases to
   `6830.TW` (Taiwan) and is in stax cache. `TOWA` aliases to `6315.T`
   (Tokyo) and is in stax cache. Both work; nothing to do unless you
   start tracking different Asian names.

3. **Auto-rerun on freshness** — `seed_supplement.py` has a "skip if
   recently fetched" check (1h staleness window), but `timeline_progress`
   doesn't auto-detect when the supplement DB has changed. Manual
   re-runs needed after `seed_supplement`.

4. **FX rate freshness** — FX is cached in the supplement DB without
   automatic re-fetch. Re-run `seed_supplement.py` if rates drift
   significantly. Could add a TTL check.

5. **Per-prediction sparkline** — frontend has `_renderSparkline()` but
   isn't currently called. Plan v3 had it; codex's progress shape doesn't
   emit per-day prices. To enable: stash a downsampled price series in
   `timeline_progress` output (e.g. 30 monthly closes between entry and
   current), then call the sparkline helper from `_renderPredictionRow`.

6. **Curated option tracking** — option predictions are deliberately
   out-of-band from the main longform build. Add hand-picked specs under
   `longform/discover/option_specs/` and run `python3
   longform/discover/option_tracker.py --report-id <id> --spec <spec.json>`
   to write `data/longform/<id>/prep/options/<prediction_id>.json`. The
   frontend will render a Timeline option chart when that JSON exists, but
   `longform/build.py` must not auto-run the tracker because contract,
   strike, and expiry selection are editorial choices.

7. **Generalisation to other handles** — pipeline is fully generic but
   only run for one handle so far. Test on a handle with a different
   trade style (macro / FX / crypto) to validate the discover/themes
   prompt doesn't over-fit to semis.

## Files outside `longform/` that this work touched

- `Bookmark/x_bookmark_ui.py` — added `/api/longform` and `/api/longform/<id>` GET routes
- `Bookmark/web/index.html` — added `#longformToggle` button + `#longformView` shell
- `Bookmark/web/app.js` — Longform mode toggle, all `renderLongform*` functions, status filter wiring, option-track hydration
- `Bookmark/web/styles.css` — all `body.longform-mode`, `.lf-*`, `.lf-tl-*`, `.lf-option-*` rules

## Don't touch / careful with

- `data/longform/aleabit/inputs/` — frozen snapshot. Re-running `freeze`
  re-copies from live xalpha; harmless but defeats the point of "frozen".
- `data/longform/aleabit/discover/` — manually-curated. The
  `--force-discover` flag overwrites these via LLM. Avoid unless you want
  to re-discover.
- `~/Library/Application Support/stax/stax.db` — never write to this. The
  user's Stax app owns it.

## Quick sanity checks

```bash
# Pipeline tests pass?
pytest longform/discover/tests/

# Aleabit assemble still produces the canonical numbers?
python3 longform/compose/assemble.py --report-id aleabit
# expect: chapters: 7/7, word count: 9,812, sources: 91

# Frontend renders without errors?
# Open Bookmark UI → Longform tab. Should show 17 ticker cards with
# 4 hit / 26 in_progress / 3 miss / 11 qualitative status counts.
```

## Key sources of truth

- This handoff doc — high-level overview
- `longform/_paths.py` — canonical path layout
- `longform/discover/symbol_aliases.json` — symbol resolution truth (codex authoritative)
- `data/longform/aleabit/discover/{themes,briefs}.json` — chapter spec for aleabit
- The aleabit `report.json` itself — the canonical example output

## Last commit before handoff

If untested, run `pytest longform/discover/tests/` first to confirm green.
The pipeline is in a good state — all 44 predictions render with progress,
no missing_cache, no currency_mismatch.
