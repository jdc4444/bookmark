"""Generate web/tickers.md from data/modules/ticker_*.json.

Each module needs at minimum: a display ticker, a company name, an exchange,
a `blurb` paragraph, and a `recent` paragraph. The script:
  - reads every ticker module in data/modules/
  - assigns a regional section from the ticker suffix (or exchange fallback)
  - sorts each section alphabetically (uppercase) by ticker
  - emits markdown matching the existing tickers.md format

Run: `python3 build_tickers_md.py` — overwrites web/tickers.md.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODULES = ROOT / 'data' / 'modules'
OUTPUT = ROOT / 'web' / 'tickers.md'

SECTION_ORDER = [
    'US', 'Canada', 'Taiwan', 'Hong Kong', 'Japan',
    'Korea', 'China A', 'Oslo', 'Germany', 'Other / Unconfirmed',
]


def derive_section(ticker: str, exchange: str) -> str:
    """Place a ticker into a regional section. Hard suffix wins; exchange string is fallback.

    A US ADR / OTC listing is treated as US even when the underlying primary listing is abroad
    (e.g. ATEYY whose exchange field is "OTC (ADR); primary TSE: 6857.T" lives under US).
    """
    u = ticker.upper()
    suffix_map = [
        ('.V', 'Canada'), ('.TO', 'Canada'),
        ('.TWO', 'Taiwan'), ('.TW', 'Taiwan'),
        ('.HK', 'Hong Kong'),
        ('.KS', 'Korea'),
        ('.SS', 'China A'), ('.SZ', 'China A'),
        ('.T', 'Japan'),
        ('.OL', 'Oslo'),
        ('.DE', 'Germany'),
    ]
    for suffix, section in suffix_map:
        if u.endswith(suffix):
            return section

    e = (exchange or '').upper()
    if any(tok in e for tok in ('OTC', 'NASDAQ', 'NYSE', 'ADR', 'AMEX', 'ARCA')):
        return 'US'
    if 'TSX' in e:
        return 'Canada'
    if 'TWSE' in e or 'TPEX' in e or 'TAIWAN' in e:
        return 'Taiwan'
    if 'HKEX' in e or 'HONG KONG' in e:
        return 'Hong Kong'
    if e.startswith('TSE') or 'TOKYO' in e:
        return 'Japan'
    if 'KRX' in e or 'KOREA' in e:
        return 'Korea'
    if 'SZSE' in e or 'SSE' in e or 'SHANGHAI' in e or 'SHENZHEN' in e:
        return 'China A'
    if 'OSLO' in e:
        return 'Oslo'
    if 'XETRA' in e or 'FRANKFURT' in e:
        return 'Germany'
    if 'EURONEXT' in e or 'SIX SWISS' in e or e in {'', 'UNKNOWN / UNCONFIRMED'}:
        return 'Other / Unconfirmed'
    return 'US'


def display_ticker(module: dict, slug: str) -> str:
    """Prefer `ticker`, fall back to `key` (preserves original casing), finally slug."""
    return module.get('ticker') or module.get('key') or slug


def display_name(module: dict) -> str:
    """Schema accepts `name`, `company`, or `official_name`."""
    return module.get('name') or module.get('company') or module.get('official_name') or ''


def load_modules() -> list[dict]:
    out = []
    for path in sorted(MODULES.glob('ticker_*.json')):
        slug = path.stem.removeprefix('ticker_')
        d = json.loads(path.read_text())
        d['_slug'] = slug
        out.append(d)
    return out


def render_entry(m: dict) -> str:
    ticker = display_ticker(m, m['_slug'])
    name = display_name(m)
    exch = m.get('exchange', '')
    blurb = (m.get('blurb') or m.get('description') or '').strip()
    recent = (m.get('recent') or '').strip()

    header = f'### ${ticker} — {name}  *({exch})*' if name else f'### ${ticker}  *({exch})*'

    lines = [header, '']
    if blurb:
        lines.append(blurb)
        lines.append('')
    if recent:
        lines.append(f'**Recent.** {recent}')
        lines.append('')
    return '\n'.join(lines)


def render(modules: list[dict]) -> str:
    bucketed: dict[str, list[dict]] = {s: [] for s in SECTION_ORDER}
    for m in modules:
        ticker = display_ticker(m, m['_slug'])
        section = derive_section(ticker, m.get('exchange', ''))
        bucketed.setdefault(section, []).append(m)

    today = dt.date.today().isoformat()
    parts = [
        '# Companies & Tickers Index',
        '',
        f'_{len(modules)} entries · generated {today}_',
        '',
        "Compiled from editorial briefs in the X.com bookmark archive. Each entry is a snapshot at the time the brief was written; trade-relevant facts decay quickly.",
        '',
        '',
    ]

    for section in SECTION_ORDER:
        items = bucketed.get(section) or []
        if not items:
            continue
        items.sort(key=lambda m: display_ticker(m, m['_slug']).upper())
        parts.append(f'## {section}')
        parts.append('')
        for m in items:
            parts.append(render_entry(m))

    text = '\n'.join(parts).rstrip() + '\n'
    return text


def main() -> int:
    modules = load_modules()
    OUTPUT.write_text(render(modules))
    print(f'Wrote {OUTPUT} ({len(modules)} entries)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
