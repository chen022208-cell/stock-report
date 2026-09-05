"""華爾街見聞即時快訊（wallstreetcn.com/live/global）。

網頁本身是前端渲染的 SPA，純 HTTP 抓不到內容，但背後真正的資料來自
api.wallstreetcn.com 的公開 JSON API（不需要金鑰），直接打這個比較穩定。
"""
from __future__ import annotations

import requests

from ..config import DRY_RUN
from . import mock

API = "https://api.wallstreetcn.com/apiv1/content/lives"
TIMEOUT = 20
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
          "Referer": "https://wallstreetcn.com/live/global"}


def fetch_live_feed(channel: str = "global-channel", limit: int = 30) -> list[dict]:
    """回傳 [{"id":, "title":, "text":, "score":, "display_time":}, ...]，
    新到舊排列（API 本身的順序）。score 是華爾街見聞自己的重要度評分，
    數字越大越重要，不是 0/1 布林值。"""
    if DRY_RUN:
        return mock.wallstreetcn_live_feed()
    try:
        resp = requests.get(API, params={"channel": channel, "client": "pc", "limit": limit},
                            headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        items = (data.get("data") or {}).get("items") or []
    except Exception as exc:
        print(f"[wallstreetcn] 即時快訊擷取失敗：{exc}")
        return []

    out = []
    for it in items:
        title = (it.get("title") or "").strip()
        text = (it.get("content_text") or "").strip()
        if not title and not text:
            continue
        out.append({
            "id": it.get("id"),
            "title": title,
            "text": text,
            "score": it.get("score") or 0,
            "display_time": it.get("display_time") or 0,
        })
    return out
