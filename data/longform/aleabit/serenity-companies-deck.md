# Serenity — every company in the longform, tied to ticker + thesis

Pulled from `data/longform/aleabit/reports/report.json` on 2026-05-11.
70 entities total; ranked by Serenity's conviction tier (S → A → B → BEAR).
Mentions counter in source data is currently zero across the board (known
artifact of the assemble step); ranking uses **score** (composite of
chapter-coverage + per-tweet sentiment + position size + dossier weight).

---

## TL;DR — the 3 stocks Serenity is most bullish on

| Rank | Ticker | Name | Why this is the highest-conviction call |
|---|---|---|---|
| **1** | **SIVE** (SIVE.ST) | Sivers Semiconductors | Score **43** — the single highest in the report. Described as "the next chokepoint" in continuous-wave DFB lasers for co-packaged optics. Independent western source of the laser dies that go into AI-data-centre transceivers; Serenity returns to this name across multiple chapters as the structural keystone of the photonics thesis. |
| **2** | **AXTI** | AXT Inc. | Score **24**, S-tier. The call that *established* Serenity's track record — $12 → $80 (5x) on the indium-phosphide substrate thesis. Aleabit was banned from r/WallStreetBets over how aggressively the call worked. Their explicit framing: "stop trying to model bottlenecks with traditional metrics like P/S or P/E." |
| **3** | **MRVL** | Marvell Technology | Score **22**, A-tier — but the report calls it out explicitly as "Serenity's highest-conviction call in [the AI silicon] cluster." Alpha vs SPY measured at **1.07**, vs NVDA's modest **0.12**. Custom-silicon play; AI revenue ramping 106% YoY. |

Honorable mention: **NBIS** (Nebius) — explicit $400 price target, framed as *"the next Microsoft in the making."*

---

## S-tier (4 names — the keystones)

| Ticker | Name | Score | Thesis (one-line) |
|---|---|---|---|
| **SIVE** | Sivers Semiconductors | 43 | "The next chokepoint" in CW DFB lasers for co-packaged optics. Independent western laser-die source alongside Coherent/Lumentum. |
| **AXTI** | AXT Inc. | 24 | InP substrate monopoly. Foundational $12→$80 call. Conventional valuation models break on bottleneck names. |
| **IQE** | IQE plc | 11 | World's largest independent epitaxial wafer foundry. Grows the active semiconductor stack on InP substrates; "nothing inside a data-centre laser works without what IQE makes." |
| **SOI** | Soitec SA | 10 | Silicon-photonics substrate layer. Management says product is "deployed in 100% of next-generation AI data centers." |

---

## A-tier (15 names — high conviction)

| Ticker | Name | Score | Thesis |
|---|---|---|---|
| **MRVL** | Marvell Technology | 22 | Hidden hyperscaler ASIC play. Alpha 1.07 (vs NVDA 0.12). Custom silicon for hyperscalers + datacenter networking. |
| **AEHR** | Aehr Test Systems | 16 | CoWoS-era wafer-level burn-in. A latent defect in a $30 photonics die assembled into a $3,000 CoWoS package = catastrophic yield event. |
| **LITE** | Lumentum Holdings | 13 | Direct beneficiary of NVDA's $2B strategic investment + multi-year purchase commitment (announced Mar 2 2026). Transceiver supplier to AI datacenters. |
| **NVDA** | NVIDIA | 13 | The demand center, not the trade. Position is held but treated as a "tax on AI buildout" rather than the alpha source. |
| **NBIS** | Nebius Group | 11 | "Next Microsoft in the making." $400 12-month PT. AWS-style full-stack neocloud, separated from miner-to-HPC pivots. |
| **AAOI** | Applied Optoelectronics | 9 | Texas-based transceiver maker. "Most straightforward AI-demand story" in the assembler cluster. Laser-fab to assembly ramp to $379m/mo. |
| **POET** | POET Technologies | 8 | Optical interposer + co-packaged optics platform. *Single-customer-concentration risk* surfaced when MRVL/Celestial AI cancelled all POs in April. |
| **TSEM** | Tower Semiconductor | 8 | Silicon-photonics foundry. One of the small set of fabs that can pattern the 220nm SOI wafer for photonic chips. |
| **AVGO** | Broadcom | 8 | TPU builder for GOOGL (supply agreement to 2031); building META training chips too. Q1 AI revenue $8.4B, +106% YoY. |
| **SNDK** | SanDisk | 7 | Pure-play NAND post-WDC spinoff. "Inference detour" thesis — NAND demand ramps as inference workloads explode. |
| **META** | Meta Platforms | 6 | Hyperscaler capex line item. Building proprietary training chips via AVGO. |
| **TSM** | Taiwan Semiconductor | 6 | CoWoS is "the most constrained manufacturing resource in the global semiconductor industry." |
| **ARM** | ARM Holdings | 6 | The CPU bottleneck. Position taken late March 2026 on the new AI CPU roadmap announcement. |
| **JBL** | Jabil Inc | 3 | Optical assembly contract manufacturer. Scaling optical orders for the photonics cluster. |
| **INTC** | Intel | 3 | Foundry option; not core to the thesis but tracked. |

---

## B-tier (5 names — held but not the alpha source)

| Ticker | Name | Score | Thesis |
|---|---|---|---|
| **GOOGL** | Alphabet | 3 | TPU buyer from AVGO. Demand signal, not alpha trade. |
| **MU** | Micron Technology | 3 | HBM supplier. Beneficiary of HBM tightness but Serenity prefers Korean memory exposure (EWY / SK Hynix EUR) for cleaner play. |
| **MSFT** | Microsoft | 2 | Hyperscaler capex datapoint. |
| **AMZN** | Amazon | 2 | Same. |
| **CRWV** | CoreWeave | 2 | Neocloud category; held but separated from full-stack NBIS in the discriminating framework. |

---

## BEAR / SKIP

| Ticker | Name | Tier | Why bearish |
|---|---|---|---|
| **COHR** | Coherent | BEAR | Got the $2B NVDA investment but the company's mixed end-markets dilute the photonics signal; Serenity prefers pure-plays. |
| **IREN** | IREN Limited | BEAR | Miner-to-HPC pivot category that didn't make the discrimination cut vs NBIS. |
| **HIMX** | Himax Technologies | SKIP | Display-driver legacy; not a clean photonics-supercycle name. |

---

## Untiered names with chapter coverage (the supporting cast)

These show up in the prose but don't carry an investable dossier weight.

**Memory supercycle:** Samsung Electronics (005930.KS), SK Hynix (000660.KS), EWY (Korea ETF proxy)

**HBM / advanced packaging suppliers:** TOWA (6315.T), MSSCorp (6830.T), KLA (KLAC), Onto Innovation (ONTO), Applied Materials (AMAT), Lam Research (LRCX), Besi, ASML (ASML)

**Optical assemblers / vendors:** Fabrinet (FN), Credo (CRDO), Astera Labs (ALAB), LPKF Laser & Electronics (LPK.DE), Riber (ALRIB.PA), GlobalFoundries, SiFive, RISC-V International, ISE Labs, SemiAnalysis

**Neoclouds & power:** Applied Digital (APLD), Cipher (CIFR), Core Scientific (CORZ), Vertiv (VRT), Vistra (VST), Constellation Energy (CEG), Talen (TLN), Digital Realty (DLR), Equinix (EQIX), Churchill Capital X

**Quantum (mostly post-mortem):** Infleqtion (INFQ), IonQ (IONQ), Rigetti (RGTI)

**Other:** Oracle (ORCL), Upwork (UPWK), Dragonfly Energy (DFLI), XLU (utilities ETF)

---

# Macro trend — what Serenity is actually betting on

## Core thesis: "functional monopolies" at compute chokepoints

Serenity's organising principle is **not** a bet on AI growth or hyperscaler earnings — it's a bet on **supply-chain geography**. The conviction: the AI compute buildout concentrates economic value at obscure, hard-to-replicate chokepoints that institutional analysts have not yet mapped.

The frame: at terabit speeds, moving data between chips requires **light, not electrons**. That single physics fact turns a handful of obscure substrate and laser companies into the most consequential names in the AI hardware stack — and most of them sit on European and Japanese exchanges where US institutional research is thin.

## The three converging waves

1. **Photonics supercycle (co-packaged optics, "CPO")**
   - GB200-class GPUs need optical interconnects to scale beyond rack-level.
   - Stack: InP substrate (AXTI) → epitaxial layer (IQE) → laser die (SIVE, COHR, LITE) → SOI wafer (SOI) → photonic IC (TSEM/TSM/GF) → transceiver assembly (AAOI, FN, JBL).
   - Each layer has 1–3 vendors. Most haven't been priced as essential yet.

2. **HBM + advanced packaging (CoWoS)**
   - Bottleneck = CoWoS capacity, not silicon. "The most constrained manufacturing resource in the global semiconductor industry."
   - 10-name cluster maps every step: TOWA (compression molding), AEHR (wafer-level burn-in), KLAC/ONTO (inspection), MSSCorp (carrier), etc.

3. **Korean memory + 2028 vega expansion (the macro play)**
   - SK Hynix/Samsung at 4-5x forward P/E despite memory supercycle.
   - The OPTIONS trade: long-dated EWY 2028 OTM LEAPs priced at IV ≈ 33% when shorter-dated IV was 48%+ — bet on vega expansion.
   - *This is the one trade where market timing dominates; the chart is now in the Timeline tab*.

## What to watch for (going forward)

**Bullish catalysts that would confirm the framework:**
- Continued NVDA strategic investments in supply-chain names (COHR/LITE was the template; SIVE or AXTI would be confirmation)
- CoWoS capacity expansion announcements from TSMC (signal of how tight)
- Hyperscaler capex revisions upward into H2 2026
- Sivers spinning off the photonics unit with a dual NYSE listing (rumored, not confirmed)
- IV convergence in EWY 2028 chain toward shorter-dated levels (this is *already happening* — see EWY option track in the Timeline tab)

**Bearish flags that would crack it:**
- DeepSeek-style efficiency breakthroughs that reduce hyperscaler compute demand (already shook VST/XLU position once)
- Single-customer cancellation risk crystallising again (POET/Marvell episode in April was the dry run)
- Korean memory derating if HBM oversupply lands faster than expected
- Failure of any photonics chokepoint to translate volume → margin (the unproven leg)

**Things explicitly NOT working (per Serenity's own post-mortem):**
- *Momentum-chasing without a supply-chain anchor* → DFLI down 84%
- *Macro conviction substituting for company-level research* → VST/XLU options down 9%
- *Social-consensus override* → INFQ down 30%; influencers convinced them rather than their own DD

The framework degrades predictably when Serenity departs from it. The post-mortem candor on those three losses is itself a signal: when the next miss comes, it'll probably be flagged early.

## The one number to remember

**Hyperscaler 2026 capex: ~$690B combined** (GOOGL + MSFT + META + AMZN). Serenity's read: that's not a reason to buy hyperscalers — it's a demand signal for the upstream supply chains they've been accumulating since mid-2025.
