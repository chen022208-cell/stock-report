"""第一層：強勢股掃描 + 黑馬判定。

刻意不預設任何產業分類 —— 黑馬之所以是黑馬，就是因為它不在既有的分類裡。
只看「漲幅 + 量能異常」這種客觀訊號，讓題材歸納交給下一層的 LLM。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from . import technical


def scan_strong_stocks(quotes: list[dict], cfg: dict, history_fn=None) -> list[dict]:
    """依漲幅、量能、成交金額篩出今日強勢股。

    兩段式篩選以省網路：先用「漲幅 + 成交金額」這種零成本條件過一遍（全市場約
    1400 檔會縮到數十檔），只對通過的候選去抓歷史股價算量能倍數，再做最終篩選。
    history_fn(code, days) -> list[dict]；DRY_RUN 或 quotes 已帶 volume_ratio 時可省略。
    """
    s = cfg["screener"]

    pre = [
        q for q in quotes
        if q.get("change_pct", 0) >= s["min_change_pct"]
        and q.get("turnover", 0) >= s["min_turnover"]
    ]
    pre.sort(key=lambda x: x.get("change_pct", 0), reverse=True)
    # 限制實際要抓歷史的檔數，避免對 TWSE 發出過多請求
    pre = pre[: max(s["top_n"] * 3, 40)]

    if history_fn is not None:
        attach_volume_ratio(pre, history_fn)

    picked = []
    for q in pre:
        ratio = q.get("volume_ratio")
        if ratio is not None and ratio < s["min_volume_ratio"]:
            continue
        picked.append(q)

    picked.sort(key=lambda x: (x.get("volume_ratio") or 0) * x.get("change_pct", 0), reverse=True)
    return picked[: s["top_n"]]


def attach_volume_ratio(quotes: list[dict], history_fn) -> list[dict]:
    """補上量能倍數（當日量 / 20日均量）。

    history_fn 由呼叫端注入，方便測試時替換掉真實 API。
    只對傳進來的清單逐檔抓歷史，呼叫端要自行先縮小範圍。
    """
    for q in quotes:
        if q.get("volume_ratio") is not None:
            continue
        try:
            hist = history_fn(q["code"], 25)
        except Exception:
            hist = []
        vols = [h["volume"] for h in hist[-21:-1] if h.get("volume")]
        avg = sum(vols) / len(vols) if vols else 0
        q["volume_ratio"] = round(q["volume"] / avg, 2) if avg > 0 else None
    return quotes


def scan_breakout_candidates(quotes: list[dict], cfg: dict, history_fn) -> list[dict]:
    """起漲點雷達：找「剛站上月線/季線＋爆量」的個股，刻意跟 scan_strong_stocks
    分開——那支只抓「今天已經漲很多」的，這支要抓「可能才剛開始」的，門檻
    故意放低（漲幅門檻遠低於 scan_strong_stocks），漲太多的會在
    technical.detect_fresh_breakout() 裡被判定為「已延伸」而排除。
    """
    b = cfg.get("breakout", {})
    pre = [
        q for q in quotes
        if q.get("change_pct", 0) >= b.get("min_change_pct", 1.5)
        and q.get("turnover", 0) >= b.get("min_turnover", 30000000)
    ]
    pre.sort(key=lambda x: x.get("turnover", 0), reverse=True)
    pre = pre[: b.get("pre_filter_top_n", 80)]

    picked = []
    for q in pre:
        try:
            hist = history_fn(q["code"], 90)
        except Exception:
            hist = []
        result = technical.detect_fresh_breakout(hist, cfg)
        if result.get("is_breakout"):
            picked.append({**result, "code": q["code"], "name": q.get("name", ""),
                          "close": q.get("close", 0), "change_pct": q.get("change_pct", 0)})

    picked.sort(key=lambda x: (x["days_ago"], -x["volume_ratio"]))
    return picked[: b.get("top_n", 10)]


def find_new_listings(quotes_by_code: dict[str, dict], listing_dates: dict[str, str],
                      days: int = 180) -> list[dict]:
    """新掛牌觀察：上市/上櫃/興櫃未滿 N 天的個股，不管今天漲跌，單純因為「夠新」
    值得留意——新股沒有長期籌碼歷史，波動特性跟老牌股不一樣。
    """
    cutoff = (date.today() - timedelta(days=days)).strftime("%Y%m%d")
    results = []
    for code, listed in listing_dates.items():
        if not listed or listed < cutoff:
            continue
        q = quotes_by_code.get(code)
        if not q:
            continue
        try:
            listed_date = date(int(listed[:4]), int(listed[4:6]), int(listed[6:8]))
            days_listed = (date.today() - listed_date).days
        except (ValueError, IndexError):
            continue
        results.append({
            "code": code, "name": q.get("name", ""), "close": q.get("close", 0),
            "change_pct": q.get("change_pct", 0), "days_listed": days_listed,
            "listed_date": listed_date.isoformat(), "market": q.get("market", "twse"),
        })
    results.sort(key=lambda x: x["days_listed"])
    return results


def identify_dark_horses(
    orphans: list[dict], quotes_by_code: dict[str, dict], cfg: dict
) -> list[dict]:
    """把 LLM 標記為「孤立訊號」的個股，加上風險欄位。

    重點：黑馬不給「信心度」，只給「風險標記」——
    資訊不對稱的標的，用同一套信心度語言會誤導人。
    """
    dh_cfg = cfg["dark_horse"]
    results = []

    for orphan in orphans:
        code = orphan.get("code", "")
        quote = quotes_by_code.get(code, {})
        ratio = quote.get("volume_ratio") or 0

        risk_flags = []
        if ratio >= dh_cfg["volume_ratio"]:
            risk_flags.append(f"量能達 20 日均量 {ratio:.1f} 倍")
        risk_flags.append("無同族群呼應，題材脈絡不明")
        risk_flags.append("籌碼未經法人驗證")

        results.append({
            "code": code,
            "name": orphan.get("name") or quote.get("name", ""),
            "reason": orphan.get("reason", ""),
            "volume_ratio": ratio,
            "change_pct": quote.get("change_pct", 0),
            "close": quote.get("close", 0),
            "risk_flags": risk_flags,
            "risk_level": "高" if ratio >= dh_cfg["volume_ratio"] else "中",
            "advice": "建議列為觀察而非追蹤標的",
        })
    return results


def mark_watchlist(items: list[dict], watchlist: list[dict]) -> list[dict]:
    """標記自選股並置頂 —— 你最在意的永遠是手上那幾檔。"""
    codes = {w["code"] for w in watchlist}
    for item in items:
        item["is_watchlist"] = item.get("code") in codes
    items.sort(key=lambda x: not x.get("is_watchlist", False))
    return items


def watchlist_hits(
    themes: list[dict], dark_horses: list[dict], watchlist: list[dict]
) -> list[dict]:
    """今天有哪些自選股被掃到，放報告最上方。"""
    codes = {w["code"]: w["name"] for w in watchlist}
    hits: list[dict[str, Any]] = []

    for theme in themes:
        for stock in theme.get("stocks", []):
            if stock.get("code") in codes:
                hits.append({
                    "code": stock["code"],
                    "name": codes[stock["code"]],
                    "context": f"出現在題材「{theme['name']}」",
                    "confidence": theme.get("confidence", ""),
                })

    for dh in dark_horses:
        if dh.get("code") in codes:
            hits.append({
                "code": dh["code"],
                "name": codes[dh["code"]],
                "context": "被標記為異常訊號／疑似黑馬",
                "confidence": "",
            })

    return hits
