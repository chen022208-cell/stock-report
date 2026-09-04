"""國際題材追蹤：把國際財經頭條 + 美股類股表現，彙整成與台股題材同格式的敘事。

資料源都免費免申請：
- 頭條：Google News RSS（stdlib 解析，無額外相依）
- 類股表現：yfinance（沿用國際盤 fetcher 的相依）

產出寫進同一個題材知識庫，scope='intl'，這樣退場機制、深度報告門檻全部沿用。
任一步失敗都回空結果，不讓早報開天窗。
"""
from __future__ import annotations

import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

from .. import db, llm
from ..config import DRY_RUN, today_str
from ..fetchers import mock

_UA = {"User-Agent": "Mozilla/5.0 (compatible; stock-report/1.0)"}
_TIMEOUT = 20

NEWS_QUERIES = [
    "stock market Fed rate when:7d",
    "semiconductor AI chips capex when:7d",
    "global markets earnings guidance when:7d",
]

# 類股 ETF 對台股題材最有解釋力的幾個；相對強弱用 S&P 500 當基準
SECTOR_ETFS = [
    ("半導體", "SMH"),
    ("科技", "XLK"),
    ("軟體", "IGV"),
    ("生技", "XBI"),
    ("能源", "XLE"),
    ("金融", "XLF"),
]
BENCHMARK = "^GSPC"


def fetch_headlines(limit: int = 60) -> list[dict]:
    if DRY_RUN:
        return mock.global_headlines()

    seen: set[str] = set()
    out: list[dict] = []
    for q in NEWS_QUERIES:
        url = ("https://news.google.com/rss/search?q="
               + urllib.parse.quote(q)
               + "&hl=en-US&gl=US&ceid=US:en")
        try:
            req = urllib.request.Request(url, headers=_UA)
            raw = urllib.request.urlopen(req, timeout=_TIMEOUT).read()
            root = ET.fromstring(raw)
        except Exception as exc:
            print(f"[global] 頭條擷取失敗（{q}）：{exc}")
            continue
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            if not title or title.lower() in seen:
                continue
            seen.add(title.lower())
            src = item.find("{http://www.w3.org/2005/Atom}source")
            out.append({
                "title": title,
                "source": (src.text if src is not None else "") or "",
                "date": _rss_date(item.findtext("pubDate")),
            })
    return out[:limit]


def _rss_date(raw: str | None) -> str:
    if not raw:
        return ""
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def fetch_sector_performance() -> list[dict]:
    if DRY_RUN:
        return mock.sector_performance()

    try:
        import yfinance as yf
    except Exception:
        return []

    def ret5(sym: str) -> float | None:
        try:
            h = yf.Ticker(sym).history(period="10d", interval="1d")
            closes = [float(x) for x in h["Close"].tolist() if x == x]
            if len(closes) < 6:
                return None
            return (closes[-1] / closes[-6] - 1) * 100
        except Exception:
            return None

    base = ret5(BENCHMARK)
    out = []
    for name, ticker in SECTOR_ETFS:
        r = ret5(ticker)
        if r is None:
            continue
        out.append({
            "name": name, "ticker": ticker,
            "ret_5d": round(r, 1),
            "rel_strength": round(r - base, 1) if base is not None else 0.0,
        })
    return out


def run() -> dict:
    """回傳 {macro_note, themes:[view...]}，並把題材寫入知識庫（scope='intl'）。"""
    headlines = fetch_headlines()
    sectors = fetch_sector_performance()
    if not headlines and not sectors:
        return {"macro_note": "", "themes": []}

    digest = llm.global_theme_digest(headlines, sectors)
    today = today_str()
    views = []
    for t in digest.get("themes", []):
        name = f"〔國際〕{t['name']}"
        stocks = [{"code": tk, "name": tk} for tk in t.get("us_tickers", [])]
        db.upsert_theme(
            name=name, summary=t.get("summary", ""),
            confidence=t.get("confidence", "mid"), verdict=t.get("verdict", "watch"),
            related_stocks=stocks, today=today, scope="intl",
            note=t.get("drivers", ""),
        )
        views.append({
            "name": t["name"],
            "summary": t.get("summary", ""),
            "confidence": t.get("confidence", "mid"),
            "verdict": t.get("verdict", "watch"),
            "drivers": t.get("drivers", ""),
            "us_tickers": t.get("us_tickers", []),
            "tw_readthrough": t.get("tw_readthrough", ""),
        })
    return {
        "macro_note": digest.get("macro_note", ""),
        "themes": views,
        "sectors": sectors,
    }
