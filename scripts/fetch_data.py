#!/usr/bin/env python3
"""Build data.json for the yorulmaz.nl dashboard.

Sources, all first-party and each requested exactly once per run:
  * Vanguard Global Stock Index Fund (IE00BFPM9N11) NAV, from ABN AMRO's public
    fund screener page. One GET, no retries. If it does not answer 200 the
    previous NAV is retained and flagged stale rather than hammering the site.
  * Gold spot and USD/EUR from their public APIs (see URLs below).

News sites are plain outbound links in index.html - no feeds are fetched.

Runs once per weekday from GitHub Actions after the 11:00 CET NAV cut-off.
Stdlib only, no dependencies.
"""

from __future__ import annotations

import json
import re
import html
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data.json"

# Descriptive UA: identifies the caller so ABN AMRO can see this is one polite
# request per day from a personal dashboard rather than a crawler.
UA = "yorulmaz.nl-dashboard/1.0 (personal dashboard, 1 request/day; +https://yorulmaz.nl)"
TIMEOUT = 25
HISTORY_LIMIT = 180

FUND_URL = "https://fondsen.abnamro.nl/fundscreener/nl/fund/IE00BFPM9N11"
QUOTES_URL = "https://fondsen.abnamro.nl/fundscreener/nl/charts/quotes?fundId=1234"
GOLD_URL = "https://api.gold-api.com/price/XAU"
FX_URL = "https://open.er-api.com/v6/latest/USD"
# NBP (Poland's central bank) publishes an official daily gold price per gram,
# keyless, plus its own EUR/PLN mid rates - used once to seed the gold trend.
GOLD_SEED_URL = "https://api.nbp.pl/api/cenyzlota/last/60/?format=json"
GOLD_SEED_FX_URL = "https://api.nbp.pl/api/exchangerates/rates/a/eur/last/60/?format=json"

TROY_OUNCE_G = 31.1034768
HISTORY_POINTS = 90
# Below this many stored days, seed the trend from ABN AMRO's own chart series.
SEED_THRESHOLD = 40

# "Koers  €447,34  ...  Koersdatum  06-11-2026" in the server-rendered page.
FUND_RE = re.compile(r"Koers.*?€\s*([\d.,]+).*?Koersdatum.*?(\d{2}-\d{2}-\d{4})", re.S)

# Every figure on the page sits in a repeating row: f-title(label) then value.
ROW_RE = re.compile(
    r'<div class="fund-details-row">\s*<div class="f-title">\s*(?:<div>\s*)?([^<]+?)\s*(?:</div>\s*)?</div>\s*<div>\s*([^<]*?)\s*</div>',
    re.S,
)

# ABN AMRO's own published performance figures, shown as-is (not recomputed here).
PERF_LABELS = {
    "Lopende jaar": "ytd",
    "1 week": "1w",
    "1 maand": "1m",
    "3 maanden": "3m",
    "6 maanden": "6m",
    "1 jaar": "1y",
    "3 jaar (op jaarbasis)": "3y",
    "5 jaar (op jaarbasis)": "5y",
    "Sinds start (op jaarbasis)": "since",
}

TAG_RE = re.compile(r"<[^>]+>")


def fetch(url: str) -> tuple[int | None, bytes | None]:
    """One GET. No retries - a failure just leaves the previous data in place."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "nl,en;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        print(f"  ! {url} -> HTTP {exc.code}", flush=True)
        return exc.code, None
    except Exception as exc:  # noqa: BLE001 - never fail the whole run on one source
        print(f"  ! {url} -> {type(exc).__name__}: {exc}", flush=True)
        return None, None


def clean(text: str | None, limit: int = 240) -> str:
    if not text:
        return ""
    text = html.unescape(TAG_RE.sub(" ", text))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].rstrip()


def dutch_number(raw: str) -> float:
    """1.234,56" -> 1234.56 ; "447,34" -> 447.34"""
    return float(raw.replace(".", "").replace(",", "."))


def load_previous() -> dict:
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def parse_quotes(raw: bytes) -> list[tuple[str, float]]:
    """ABN AMRO's chart series: indexed values (base 100). Weekends repeat Friday."""
    doc = json.loads(raw)
    rows = ((doc.get("quotes") or {}).get("rows")) or {}
    out: list[tuple[str, float]] = []
    for stamp, cell in rows.items():
        try:
            value = cell["c"][1]["v"]
            day = datetime.fromtimestamp(int(stamp), timezone.utc).date()
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if not isinstance(value, (int, float)):
            continue
        if day.weekday() >= 5:  # no NAV movement over the weekend
            continue
        out.append((day.isoformat(), float(value)))
    out.sort(key=lambda point: point[0])
    return out


def seed_fund_history(nav: float, history: list[dict]) -> list[dict]:
    """Backfill the trend from ABN AMRO's own chart series, rescaled to today's NAV.

    The series is exactly proportional to the NAV (verified against their
    published 1-week return), so scaling the last point onto today's NAV
    reproduces the real historical values.
    """
    if len(history) >= SEED_THRESHOLD:
        return history

    print(f"  fund: only {len(history)} stored points, seeding from chart series", flush=True)
    status, raw = fetch(QUOTES_URL)
    if status != 200 or not raw:
        return history

    try:
        series = parse_quotes(raw)
    except Exception as exc:  # noqa: BLE001
        print(f"  ! fund chart parse failed: {exc}", flush=True)
        return history

    if len(series) < 2 or not series[-1][1]:
        return history

    scale = nav / series[-1][1]
    seeded = {day: round(value * scale, 2) for day, value in series[-HISTORY_POINTS:]}
    for row in history:  # values already recorded from the fund page win
        seeded[row["date"]] = row["nav"]

    merged = [{"date": day, "nav": seeded[day]} for day in sorted(seeded)]
    print(f"  fund: trend now has {len(merged)} points", flush=True)
    return merged[-HISTORY_LIMIT:]


def seed_gold_history(history: list[dict]) -> list[dict]:
    """Backfill the gold trend from NBP's official daily price, converted to EUR.

    NBP quotes gold per gram in PLN and publishes EUR/PLN mid rates for the same
    days, so one division per day gives EUR per gram. Done only while history is
    thin; after that the daily spot snapshots take over.
    """
    if len(history) >= SEED_THRESHOLD:
        return history

    print(f"  gold: only {len(history)} stored points, seeding from NBP", flush=True)
    gold_status, gold_raw = fetch(GOLD_SEED_URL)
    fx_status, fx_raw = fetch(GOLD_SEED_FX_URL)
    if gold_status != 200 or fx_status != 200 or not gold_raw or not fx_raw:
        return history

    try:
        gold_rows = json.loads(gold_raw)
        rates = json.loads(fx_raw)["rates"]
        by_day = {row["effectiveDate"]: row["mid"] for row in rates if row.get("mid")}
    except Exception as exc:  # noqa: BLE001
        print(f"  ! gold seed parse failed: {exc}", flush=True)
        return history

    seeded: dict[str, float] = {}
    for row in gold_rows:
        day = row.get("data")
        price = row.get("cena")
        rate = by_day.get(day)
        if day and isinstance(price, (int, float)) and rate:
            seeded[day] = round(price / rate, 2)

    if len(seeded) < 2:
        return history

    for row in history:  # snapshots already recorded win over seeded values
        seeded[row["date"]] = row["eur_per_gram"]

    merged = [{"date": day, "eur_per_gram": seeded[day]} for day in sorted(seeded)]
    print(f"  gold: trend now has {len(merged)} points", flush=True)
    return merged[-HISTORY_POINTS:]


def build_gold(previous: dict) -> dict:
    prev_gold = previous.get("gold") or {}
    history = [
        row
        for row in (prev_gold.get("history") or [])
        if isinstance(row, dict) and row.get("date") and row.get("eur_per_gram") is not None
    ]

    print(f"  gold: GET {GOLD_URL}", flush=True)
    spot_status, spot_raw = fetch(GOLD_URL)
    print(f"  gold: GET {FX_URL}", flush=True)
    fx_status, fx_raw = fetch(FX_URL)

    usd_per_oz = eur_usd = gram_eur = None
    if spot_status == 200 and fx_status == 200 and spot_raw and fx_raw:
        try:
            usd_per_oz = float(json.loads(spot_raw)["price"])
            eur_usd = float(json.loads(fx_raw)["rates"]["EUR"])
            gram_eur = round(usd_per_oz / TROY_OUNCE_G * eur_usd, 2)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! gold parse failed: {exc}", flush=True)
            usd_per_oz = eur_usd = gram_eur = None

    gold: dict = {"stale": False}
    today = datetime.now(timezone.utc).date().isoformat()

    if gram_eur is not None:
        history = seed_gold_history(history)
        history = [row for row in history if row["date"] != today] + [
            {"date": today, "eur_per_gram": gram_eur}
        ]
        history.sort(key=lambda row: row["date"])
        history = history[-HISTORY_POINTS:]
        print(f"  gold: {gram_eur} EUR/g on {today}", flush=True)
    elif prev_gold.get("eur_per_gram") is not None:
        gram_eur = prev_gold["eur_per_gram"]
        usd_per_oz = prev_gold.get("usd_per_oz")
        eur_usd = prev_gold.get("eur_usd")
        gold["stale"] = True
        print("  gold: fetch unusable, keeping previous value", flush=True)

    gold.update(
        {
            "usd_per_oz": usd_per_oz,
            "eur_usd": eur_usd,
            "eur_per_gram": gram_eur,
            "as_of": today if not gold["stale"] else prev_gold.get("as_of"),
            "history": history,
        }
    )
    return gold


def build_fund(previous: dict) -> dict:
    prev_fund = previous.get("fund") or {}
    history: list[dict] = []
    for row in prev_fund.get("history") or []:
        if isinstance(row, dict) and row.get("date") and row.get("nav") is not None:
            history.append({"date": row["date"], "nav": row["nav"]})

    print(f"  fund: GET {FUND_URL}", flush=True)
    status, raw = fetch(FUND_URL)

    fund = {
        "isin": "IE00BFPM9N11",
        "name": "Vanguard Global Stock Index Fund",
        "share_class": "Institutional Plus EUR Acc",
        "abn_code": "95522",
        "currency": "EUR",
        "stale": False,
    }

    nav = None
    as_of = None
    performance: dict[str, float] = {}
    if status == 200 and raw:
        doc = raw.decode("utf-8", "ignore")
        rows = {clean(k): v.strip() for k, v in ROW_RE.findall(doc)}

        # Koers arrives as "€447,34"
        koers = rows.get("Koers", "")
        koers_match = re.search(r"([\d.,]+)", koers)
        if koers_match:
            try:
                nav = round(dutch_number(koers_match.group(1)), 2)
            except Exception as exc:  # noqa: BLE001
                print(f"  ! fund nav parse failed: {exc}", flush=True)

        datum = rows.get("Koersdatum", "")
        if re.fullmatch(r"\d{2}-\d{2}-\d{4}", datum):
            d, m, y = datum.split("-")
            as_of = f"{y}-{m}-{d}"

        # Fallback if the row layout ever changes under us.
        if nav is None or as_of is None:
            match = FUND_RE.search(doc)
            if match:
                try:
                    nav = nav if nav is not None else round(dutch_number(match.group(1)), 2)
                    if as_of is None:
                        d, m, y = match.group(2).split("-")
                        as_of = f"{y}-{m}-{d}"
                except Exception as exc:  # noqa: BLE001
                    print(f"  ! fund fallback parse failed: {exc}", flush=True)

        for label, key in PERF_LABELS.items():
            raw_val = rows.get(label, "").replace("%", "").strip()
            if raw_val:
                try:
                    performance[key] = dutch_number(raw_val)
                except Exception:  # noqa: BLE001
                    pass

    if nav is not None and as_of is not None:
        history = [h for h in history if h["date"] != as_of] + [{"date": as_of, "nav": nav}]
        history.sort(key=lambda h: h["date"])
        print(f"  fund: {nav} EUR as of {as_of}", flush=True)
        history = seed_fund_history(nav, history)
        history = history[-HISTORY_LIMIT:]
    elif prev_fund.get("nav") is not None:
        nav = prev_fund.get("nav")
        as_of = prev_fund.get("as_of")
        history = prev_fund.get("history") or history
        fund["stale"] = True
        print(f"  fund: fetch unusable, keeping previous {nav} ({as_of})", flush=True)
    else:
        print("  ! fund: no data at all this run", flush=True)

    fund["nav"] = nav
    fund["as_of"] = as_of
    fund["history"] = history
    fund["performance"] = performance or (prev_fund.get("performance") or {})

    if nav is not None and len(history) >= 2:
        fund["previous_nav"] = history[-2]["nav"]
        fund["change"] = round(nav - history[-2]["nav"], 2)
        fund["change_pct"] = round((nav / history[-2]["nav"] - 1) * 100, 2)
    else:
        fund["previous_nav"] = None
        fund["change"] = None
        fund["change_pct"] = None

    return fund


def main() -> int:
    previous = load_previous()
    print("Fetching sources (one request each)...", flush=True)

    payload = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "fund": build_fund(previous),
        "gold": build_gold(previous),
    }

    DATA_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {DATA_FILE.relative_to(ROOT)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
