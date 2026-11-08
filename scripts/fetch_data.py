#!/usr/bin/env python3
"""Build data.json for the yorulmaz.nl dashboard.

Sources, all first-party and each requested exactly once per run:
  * Vanguard Global Stock Index Fund (IE00BFPM9N11) NAV, from ABN AMRO's public
    fund screener page. One GET, no retries. If it does not answer 200 the
    previous NAV is retained and flagged stale rather than hammering the site.
  * Tweakers, NOS, RTL Nieuws and CNN Türk RSS.
  * CNN has published no RSS since April 2023, so headlines come from
    lite.cnn.com - CNN's own lightweight text site.

Runs once per weekday from GitHub Actions after the 11:00 CET NAV cut-off.
Stdlib only, no dependencies.
"""

from __future__ import annotations

import json
import re
import html
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data.json"

# Descriptive UA: identifies the caller so ABN AMRO can see this is one polite
# request per day from a personal dashboard rather than a crawler.
UA = "yorulmaz.nl-dashboard/1.0 (personal dashboard, 1 request/day; +https://yorulmaz.nl)"
TIMEOUT = 25
MAX_ITEMS = 6
HISTORY_LIMIT = 180

FUND_URL = "https://fondsen.abnamro.nl/fundscreener/nl/fund/IE00BFPM9N11"

FEEDS = [
    {
        "key": "tweakers",
        "label": "Tweakers",
        "site": "https://tweakers.net",
        "kind": "rss",
        "url": "https://tweakers.net/feeds/nieuws.xml",
    },
    {
        "key": "nos",
        "label": "NOS",
        "site": "https://nos.nl",
        "kind": "rss",
        "url": "https://feeds.nos.nl/nosnieuwsalgemeen",
    },
    {
        "key": "rtl",
        "label": "RTL Nieuws",
        "site": "https://www.rtlnieuws.nl",
        "kind": "rss",
        "url": "https://www.rtlnieuws.nl/rss.xml",
    },
    {
        "key": "cnn",
        "label": "CNN",
        "site": "https://www.cnn.com",
        "kind": "cnn_lite",
        "url": "https://lite.cnn.com/",
        "note": "lite.cnn.com: CNN publishes no RSS since 2023",
    },
    {
        "key": "cnnturk",
        "label": "CNN Türk",
        "site": "https://www.cnnturk.com",
        "kind": "rss",
        "url": "https://www.cnnturk.com/feed/rss/all/news",
    },
]

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

# lite.cnn.com links carry the publication date in the path: /2026/11/08/slug
CNN_RE = re.compile(r'<a href="(/\d{4}/\d{2}/\d{2}/[^"]+)"[^>]*>(.*?)</a>', re.S)

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


def to_iso(rfc822: str | None) -> str | None:
    if not rfc822:
        return None
    try:
        return parsedate_to_datetime(rfc822.strip()).astimezone(timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        return None


def dutch_number(raw: str) -> float:
    """"1.234,56" -> 1234.56 ; "447,34" -> 447.34"""
    return float(raw.replace(".", "").replace(",", "."))


def parse_rss(raw: bytes, limit: int = MAX_ITEMS) -> list[dict]:
    root = ET.fromstring(raw)
    channel = root.find("channel")
    if channel is None:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in channel.findall("item"):
        title = clean(item.findtext("title"), 200)
        link = (item.findtext("link") or "").strip()
        if not title or not link or link in seen:
            continue
        seen.add(link)
        out.append({"title": title, "link": link, "published": to_iso(item.findtext("pubDate"))})
        if len(out) >= limit:
            break
    return out


def parse_cnn_lite(raw: bytes, limit: int = MAX_ITEMS) -> list[dict]:
    doc = raw.decode("utf-8", "ignore")
    out: list[dict] = []
    seen: set[str] = set()
    for href, inner in CNN_RE.findall(doc):
        title = clean(inner, 200)
        link = "https://www.cnn.com" + href
        if len(title) < 25 or link in seen:
            continue  # skip nav crumbs and one-word stubs
        seen.add(link)
        y, m, d = href[1:11].split("/")
        out.append(
            {
                "title": title,
                "link": link,
                "published": f"{y}-{m}-{d}T00:00:00+00:00",
            }
        )
        if len(out) >= limit:
            break
    return out


def load_previous() -> dict:
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


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
        history = history[-HISTORY_LIMIT:]
        print(f"  fund: {nav} EUR as of {as_of}", flush=True)
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


def build_news(previous: dict) -> dict:
    prev_news = previous.get("news") or {}
    news: dict[str, dict] = {}

    for feed in FEEDS:
        key = feed["key"]
        print(f"  news: GET {feed['url']}", flush=True)
        status, raw = fetch(feed["url"])

        items: list[dict] = []
        if status == 200 and raw:
            try:
                if feed["kind"] == "cnn_lite":
                    items = parse_cnn_lite(raw)
                else:
                    items = parse_rss(raw)
            except Exception as exc:  # noqa: BLE001
                print(f"  ! {key} parse failed: {exc}", flush=True)

        if not items:
            prev_items = (prev_news.get(key) or {}).get("items") or []
            if prev_items:
                items = prev_items
                print(f"  news: {key} unusable, keeping previous {len(items)} items", flush=True)

        entry = {
            "label": feed["label"],
            "site": feed["site"],
            "feed": feed["url"] if feed["kind"] == "rss" else None,
            "items": items,
        }
        if feed.get("note"):
            entry["note"] = feed["note"]
        if not items:
            entry["error"] = True
        news[key] = entry

        print(f"  news: {key} -> {len(items)} items", flush=True)

    return news


def main() -> int:
    previous = load_previous()
    print("Fetching sources (one request each)...", flush=True)

    payload = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "fund": build_fund(previous),
        "news": build_news(previous),
    }

    DATA_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {DATA_FILE.relative_to(ROOT)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
