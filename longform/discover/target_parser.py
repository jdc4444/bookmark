"""Parse timeline prediction targets into ticker-specific normalized targets.

The extractor's `target_value` is tweet-level prose. This module turns it into
a small, explicit target object that can later be resolved against a ticker's
native Stax price series. No network calls happen here.
"""
from __future__ import annotations

import copy
import re
from typing import Any


CURRENCY_MARKS = {
    "$": "USD",
    "US$": "USD",
    "USD": "USD",
    "SEK": "SEK",
    "EUR": "EUR",
    "JPY": "JPY",
    "TWD": "TWD",
    "KRW": "KRW",
}

WORD_MULTIPLES = {
    "double": 2.0,
    "doubles": 2.0,
    "doubled": 2.0,
    "triple": 3.0,
    "triples": 3.0,
    "tripled": 3.0,
}

MONEY_SUFFIX_RE = r"[TtBbMm]|tn|bn|mm|trillion|billion|million"
CURRENCY_RE = r"US\$|\$|USD|SEK|EUR|JPY|TWD|KRW"


def normalize_ticker(ticker: str | None) -> str:
    return (ticker or "").strip().lstrip("$").upper()


def compact_ticker(ticker: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", normalize_ticker(ticker))


def parse_number(text: str | None) -> float | None:
    if text is None:
        return None
    s = str(text).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _base_target(raw: str, metric: str | None, target_date: str | None) -> dict[str, Any]:
    return {
        "basis": "unresolved",
        "direction": None,
        "raw": raw,
        "metric": metric,
        "target_date": target_date,
        "target_price": None,
        "target_price_low": None,
        "target_price_high": None,
        "threshold_price": None,
        "target_currency": None,
        "entry_price_hint": None,
        "return_pct": None,
        "return_pct_low": None,
        "return_pct_high": None,
        "multiple": None,
        "multiple_low": None,
        "multiple_high": None,
        "market_cap": None,
        "market_cap_low": None,
        "market_cap_high": None,
        "market_cap_currency": None,
        "benchmark": None,
        "benchmark_relative_pct": None,
        "unresolved": True,
    }


def _strip_none(target: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in target.items() if v is not None}


def _detect_currency(mark: str | None) -> str | None:
    if not mark:
        return None
    return CURRENCY_MARKS.get(mark.upper())


def _money_multiplier(suffix: str | None) -> float:
    if not suffix:
        return 1.0
    s = suffix.lower()
    if s in ("t", "tn", "trillion"):
        return 1_000_000_000_000.0
    if s in ("b", "bn", "billion"):
        return 1_000_000_000.0
    if s in ("m", "mm", "mn", "million"):
        return 1_000_000.0
    return 1.0


def _target_text(
    prediction_or_row: str | dict[str, Any],
    target_value: str | None,
    target_metric: str | None,
    target_date: str | None,
) -> tuple[str, str, str | None, str | None]:
    if isinstance(prediction_or_row, dict):
        row = prediction_or_row
        prediction = str(row.get("prediction") or "")
        raw = str(row.get("target_value") or prediction or "")
        metric = row.get("target_metric") or target_metric
        date = row.get("target_date") or target_date
        return prediction, raw, metric, date
    prediction = str(prediction_or_row or "")
    raw = str(target_value or prediction or "")
    return prediction, raw, target_metric, target_date


def _find_ticker_arrow(text: str, ticker: str) -> dict[str, Any] | None:
    if not ticker:
        return None
    patterns = [normalize_ticker(ticker), compact_ticker(ticker)]
    for tk in dict.fromkeys(patterns):
        if not tk:
            continue
        pat = re.compile(
            rf"(?:\$?{re.escape(tk)})\b\s+"
            rf"(?P<entry_cur>US\$|\$|USD|SEK|EUR|JPY|TWD|KRW)?\s*"
            rf"(?P<entry>\d+(?:\.\d+)?)\s*"
            rf"(?:->|→|to)\s*"
            rf"(?P<target_cur>US\$|\$|USD|SEK|EUR|JPY|TWD|KRW)?\s*"
            rf"(?P<target>\d+(?:\.\d+)?)",
            re.IGNORECASE,
        )
        m = pat.search(text)
        if not m:
            continue
        entry = parse_number(m.group("entry"))
        target = parse_number(m.group("target"))
        if entry is None or target is None:
            continue
        currency = _detect_currency(m.group("target_cur")) or _detect_currency(m.group("entry_cur"))
        return {
            "entry_price_hint": entry,
            "target_price": target,
            "target_currency": currency,
            "direction": "up" if target >= entry else "down",
        }
    return None


def _parse_benchmark(text: str) -> dict[str, Any] | None:
    m = re.search(
        r"\b(outperform|underperform|beat|lag)\s+"
        r"(?P<bench>\$?[A-Z][A-Z0-9.\-]{0,8}|SPY|QQQ|market|index)\s+"
        r"by\s+(?P<pct>\d+(?:\.\d+)?)\s*%",
        text,
        re.IGNORECASE,
    )
    if not m:
        return None
    pct = parse_number(m.group("pct"))
    if pct is None:
        return None
    verb = m.group(1).lower()
    if verb in ("underperform", "lag"):
        pct = -pct
    return {
        "basis": "benchmark_relative",
        "direction": "up" if pct >= 0 else "down",
        "benchmark": m.group("bench").lstrip("$").upper(),
        "benchmark_relative_pct": pct,
    }


def _is_pure_comparative(text: str) -> bool:
    return bool(re.search(r"\boutperform(?:ing)?\b", text, re.IGNORECASE))


def _is_derivative_target(text: str) -> bool:
    return bool(re.search(r"\b(otm|leaps?|calls?|puts?|options?)\b", text, re.IGNORECASE))


def _is_indirect_pull_higher(prediction: str, ticker: str | None) -> bool:
    """Detect ETF/basket rows where another named stock has the numeric target.

    Example: "Samsung and SK Hynix are likely to double, which would pull EWY
    higher." The double belongs to Samsung/SK Hynix; EWY should stay
    qualitative because no EWY threshold is stated.
    """
    if not ticker or not prediction:
        return False
    variants = [normalize_ticker(ticker), compact_ticker(ticker)]
    for tk in dict.fromkeys(v for v in variants if v):
        pat = re.compile(
            rf"\b(?:would\s+)?(?:pull|pulls|drive|drives|send|sends|push|pushes)\s+"
            rf"\$?{re.escape(tk)}\b\s+(?:higher|up)\b",
            re.IGNORECASE,
        )
        if pat.search(prediction):
            return True
    return False


def _has_floor_language(text: str) -> bool:
    floor_patterns = [
        r"\bundervalued\s+at\b",
        r"\bworth\s+at\s+least\b",
        r"\bat\s+least\b",
        r"\bfloor\b",
        r"\bminimum\s+(?:valuation|market\s*cap|price|target|worth)\b",
        r"\b(?:valuation|market\s*cap|price|target|worth)\s+floor\b",
        r"\bshould\s+be\s+worth\b[^.;,]{0,50}\+",
        r"\b(?:US\$|\$|USD|SEK|EUR|JPY|TWD|KRW)?\s*\d+(?:\.\d+)?\s*"
        rf"(?:{MONEY_SUFFIX_RE})?\+?\s+minimum\b",
    ]
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in floor_patterns)


def _is_revenue_multiple_claim(text: str) -> bool:
    amount_x = r"\b\d+(?:\.\d+)?\s*x\b"
    return any(
        re.search(pattern, text, re.IGNORECASE)
        for pattern in (
            rf"\brevenue\b[^.;:()]*{amount_x}",
            rf"{amount_x}[^.;:()]*\brevenue\b",
            rf"\brevenue\b[^.;:()]*\bramp(?:s|ed|ing)?\b[^.;:()]*{amount_x}",
            rf"\bramp(?:s|ed|ing)?\b[^.;:()]*\brevenue\b[^.;:()]*{amount_x}",
        )
    )


def _parse_market_cap(text: str) -> dict[str, Any] | None:
    # Handles "$10B+ valuation", "$8B-$12B company", "$17-20B market cap".
    context_re = re.compile(r"\b(?:market\s*cap|mc|valuation|company)\b", re.IGNORECASE)
    range_pat = re.compile(
        rf"(?:between\s+)?(?P<cur>{CURRENCY_RE})?\s*"
        r"(?P<a>\d+(?:\.\d+)?)\s*"
        rf"(?P<suffix_a>{MONEY_SUFFIX_RE})?\s*"
        r"(?:-|–|—|\bto\b|\band\b)\s*"
        rf"(?P<cur2>{CURRENCY_RE})?\s*"
        r"(?P<b>\d+(?:\.\d+)?)\s*"
        rf"(?P<suffix_b>{MONEY_SUFFIX_RE})?\+?",
        re.IGNORECASE,
    )
    for m in range_pat.finditer(text):
        window = text[max(0, m.start() - 80):m.end() + 80]
        a = parse_number(m.group("a"))
        b = parse_number(m.group("b"))
        suffix_a = m.group("suffix_a")
        suffix_b = m.group("suffix_b")
        if a is None or b is None or not (suffix_a or suffix_b) or not context_re.search(window):
            continue
        mult_a = _money_multiplier(suffix_a or suffix_b)
        mult_b = _money_multiplier(suffix_b or suffix_a)
        currency = _detect_currency(m.group("cur")) or _detect_currency(m.group("cur2")) or "USD"
        values = [a * mult_a, b * mult_b]
        return {
            "basis": "market_cap_floor" if _has_floor_language(text) else "market_cap",
            "direction": "up",
            "market_cap_low": min(values),
            "market_cap_high": max(values),
            "market_cap_currency": currency,
        }

    single_pat = re.compile(
        rf"(?P<cur>{CURRENCY_RE})?\s*"
        r"(?P<a>\d+(?:\.\d+)?)\s*"
        rf"(?P<suffix>{MONEY_SUFFIX_RE})\+?",
        re.IGNORECASE,
    )
    for m in single_pat.finditer(text):
        window = text[max(0, m.start() - 80):m.end() + 80]
        if not context_re.search(window):
            continue
        a = parse_number(m.group("a"))
        if a is None:
            continue
        mult = _money_multiplier(m.group("suffix"))
        currency = _detect_currency(m.group("cur")) or "USD"
        return {
            "basis": "market_cap_floor" if _has_floor_language(text) else "market_cap",
            "direction": "up",
            "market_cap": a * mult,
            "market_cap_currency": currency,
        }
    return None


def _parse_percent_return(text: str) -> dict[str, Any] | None:
    # Prefer underlying price moves over option-return text.
    move_re = re.compile(
        r"\b(?:rise|rises|rally|rallies|grow|grows|growth|up|return|returns|drop|drops|fall|falls|down)\b"
        r"[^.;,()]{0,40}?"
        r"(?P<a>\d+(?:\.\d+)?)\s*(?:-|–|to)?\s*(?P<b>\d+(?:\.\d+)?)?\s*%\+?",
        re.IGNORECASE,
    )
    matches = list(move_re.finditer(text))
    if not matches:
        pct_re = re.compile(
            r"(?P<a>\d+(?:\.\d+)?)\s*(?:-|–|to)?\s*(?P<b>\d+(?:\.\d+)?)?\s*%\+?\s*"
            r"(?:growth|upside|downside|rally|drop|return)",
            re.IGNORECASE,
        )
        matches = list(pct_re.finditer(text))
    if not matches:
        return None
    m = matches[-1]
    a = parse_number(m.group("a"))
    b = parse_number(m.group("b"))
    if a is None:
        return None
    window = text[max(0, m.start() - 20):m.end() + 20].lower()
    down = bool(re.search(r"\b(drop|drops|fall|falls|down|downside)\b", window))
    vals = [a] if b is None else [a, b]
    vals = [-v for v in vals] if down else vals
    out: dict[str, Any] = {"basis": "percent_return", "direction": "down" if down else "up"}
    if len(vals) == 1:
        out["return_pct"] = vals[0]
    else:
        out["return_pct_low"] = min(vals)
        out["return_pct_high"] = max(vals)
    return out


def _parse_multiple(text: str) -> dict[str, Any] | None:
    m = re.search(
        r"(?P<a>\d+(?:\.\d+)?)\s*(?:-|–|to)?\s*(?P<b>\d+(?:\.\d+)?)?\s*x\b",
        text,
        re.IGNORECASE,
    )
    if m:
        a = parse_number(m.group("a"))
        b = parse_number(m.group("b"))
        if a is None:
            return None
        out: dict[str, Any] = {"basis": "multiple", "direction": "up"}
        if b is None:
            out["multiple"] = a
        else:
            out["multiple_low"] = min(a, b)
            out["multiple_high"] = max(a, b)
        return out
    for word, mult in WORD_MULTIPLES.items():
        if re.search(rf"\b{word}\b", text, re.IGNORECASE):
            return {"basis": "multiple", "direction": "up", "multiple": mult}
    return None


def _parse_direct_price(text: str) -> dict[str, Any] | None:
    # Avoid treating market-cap strings as share-price targets.
    if re.search(r"\b(market\s*cap|valuation|company|revenue|earnings)\b", text, re.IGNORECASE):
        return None
    m = re.search(
        r"(?:price\s*target|pt|valued\s+around|around|reach|hit|at)\s+"
        r"(?P<cur>US\$|\$|USD|SEK|EUR|JPY|TWD|KRW)?\s*(?P<price>\d+(?:\.\d+)?)\b",
        text,
        re.IGNORECASE,
    )
    if not m:
        return None
    price = parse_number(m.group("price"))
    if price is None:
        return None
    return {
        "basis": "price",
        "direction": "up",
        "target_price": price,
        "target_currency": _detect_currency(m.group("cur")),
    }


def _parse_valuation_floor(text: str) -> dict[str, Any] | None:
    if not _has_floor_language(text):
        return None
    if re.search(r"\b(market\s*cap|valuation|company|revenue|earnings)\b", text, re.IGNORECASE):
        return None
    patterns = [
        re.compile(
            rf"(?:worth\s+at\s+least|at\s+least|floor(?:\s+of)?|"
            rf"minimum(?:\s+price)?|should\s+be\s+worth)\s+"
            rf"(?P<cur>{CURRENCY_RE})?\s*(?P<price>\d+(?:\.\d+)?)\+?",
            re.IGNORECASE,
        ),
        re.compile(
            rf"(?P<cur>{CURRENCY_RE})?\s*(?P<price>\d+(?:\.\d+)?)\+?\s+minimum\b",
            re.IGNORECASE,
        ),
    ]
    for pat in patterns:
        m = pat.search(text)
        if not m:
            continue
        price = parse_number(m.group("price"))
        if price is None:
            continue
        return {
            "basis": "valuation_floor",
            "direction": "up",
            "target_price": price,
            "target_currency": _detect_currency(m.group("cur")),
        }
    return None


def parse_target(
    prediction_or_row: str | dict[str, Any],
    target_value: str | None = None,
    target_metric: str | None = None,
    target_date: str | None = None,
    ticker: str | None = None,
) -> dict[str, Any]:
    """Return a normalized target object.

    `ticker` matters for tweet-level strings like "$AAOI $93 -> $162; $SIVE
    7.7 -> 38.5", where each ticker needs a different target.
    """
    prediction, raw, metric, date = _target_text(
        prediction_or_row, target_value, target_metric, target_date,
    )
    text = " ".join(s for s in (raw, prediction) if s).strip()
    target = _base_target(raw, metric, date)

    arrow = _find_ticker_arrow(text, ticker or "")
    if arrow:
        target.update({"basis": "price", "unresolved": False})
        target.update(arrow)
        return _strip_none(target)

    bench = _parse_benchmark(text)
    if bench:
        target.update(bench)
        target["unresolved"] = False
        return _strip_none(target)

    if _is_pure_comparative(text) and not re.search(r"\d", text):
        target.update({"basis": "comparative", "direction": "up", "unresolved": False})
        return _strip_none(target)
    if _is_pure_comparative(text) and re.search(r"\boutperform(?:ing)?\b", text, re.IGNORECASE):
        # "Outperform over the next year" is comparative even when the date
        # contains digits. Numeric benchmark-relative calls are handled above.
        if not re.search(r"\boutperform(?:ing)?\s+\$?[A-Z][A-Z0-9.\-]{0,8}\s+by\s+\d", text, re.IGNORECASE):
            target.update({"basis": "comparative", "direction": "up", "unresolved": False})
            return _strip_none(target)

    if _is_indirect_pull_higher(prediction, ticker):
        target.update({"basis": "other", "direction": "up", "unresolved": False})
        return _strip_none(target)

    for parser in (_parse_market_cap, _parse_percent_return,
                   _parse_valuation_floor, _parse_direct_price):
        parsed = parser(text)
        if parsed:
            target.update(parsed)
            target["unresolved"] = False
            return _strip_none(target)

    if _is_revenue_multiple_claim(text):
        target.update({"basis": "revenue", "direction": "up", "unresolved": False})
        return _strip_none(target)

    if _is_derivative_target(text):
        mult = _parse_multiple(text)
        target.update({"basis": "derivative", "direction": "up", "unresolved": False})
        if mult:
            for key in ("multiple", "multiple_low", "multiple_high"):
                if key in mult:
                    target[key] = mult[key]
        return _strip_none(target)

    parsed = _parse_multiple(text)
    if parsed:
        target.update(parsed)
        target["unresolved"] = False
        return _strip_none(target)

    metric_l = (metric or "").lower()
    if metric_l in {"revenue", "earnings", "catalyst_event", "other"}:
        basis = "catalyst_event" if metric_l == "catalyst_event" else metric_l
        target.update({"basis": basis, "direction": "up", "unresolved": False})
        return _strip_none(target)

    return _strip_none(target)


def resolve_price_target(target: dict[str, Any], entry_price: float | None) -> dict[str, Any]:
    """Fill target prices and threshold from an entry price when possible.

    For up targets, `threshold_price` is the low end of the target. For down
    targets, it is the less severe upper threshold, e.g. a 50-70% drawdown
    resolves at entry * 0.50 rather than entry * 0.30.
    """
    out = copy.deepcopy(target)
    if entry_price is None or entry_price <= 0:
        return out

    basis = out.get("basis")
    direction = out.get("direction")

    if basis == "percent_return":
        if out.get("return_pct") is not None:
            pct = float(out["return_pct"])
            price = entry_price * (1.0 + pct / 100.0)
            out["target_price"] = price
            out["threshold_price"] = price
        else:
            lo = out.get("return_pct_low")
            hi = out.get("return_pct_high")
            if lo is not None and hi is not None:
                prices = [entry_price * (1.0 + float(lo) / 100.0),
                          entry_price * (1.0 + float(hi) / 100.0)]
                out["target_price_low"] = min(prices)
                out["target_price_high"] = max(prices)
                if direction == "down":
                    out["threshold_price"] = max(prices)
                else:
                    out["threshold_price"] = min(prices)

    elif basis == "multiple":
        if out.get("multiple") is not None:
            price = entry_price * float(out["multiple"])
            out["target_price"] = price
            out["threshold_price"] = price
        else:
            lo = out.get("multiple_low")
            hi = out.get("multiple_high")
            if lo is not None and hi is not None:
                out["target_price_low"] = entry_price * float(lo)
                out["target_price_high"] = entry_price * float(hi)
                out["threshold_price"] = out["target_price_low"]

    elif basis in {"price", "valuation_floor"}:
        if out.get("target_price") is not None:
            out["threshold_price"] = out["target_price"]
        elif out.get("target_price_low") is not None:
            out["threshold_price"] = out["target_price_low"]

    return out


def progress_pct(target: dict[str, Any], entry_price: float | None,
                 current_price: float | None) -> float | None:
    threshold = target.get("threshold_price")
    direction = target.get("direction")
    if entry_price is None or current_price is None or threshold is None:
        return None
    entry = float(entry_price)
    current = float(current_price)
    threshold_f = float(threshold)
    denom = abs(threshold_f - entry)
    if denom <= 0:
        return None
    if target.get("basis") == "valuation_floor":
        if current >= threshold_f:
            return 100.0
        return max(0.0, min(((current - entry) / denom) * 100.0, 100.0))
    if direction == "down":
        return ((entry - current) / denom) * 100.0
    return ((current - entry) / denom) * 100.0
