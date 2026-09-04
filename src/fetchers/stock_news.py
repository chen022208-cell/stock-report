"""個股新聞頭條（Google News RSS），給五面向評分的新聞面軸用。

跟 global_themes.py 的頭條抓取共用同一招（stdlib 解析 RSS，無額外相依），
差別是這裡查詢綁定「個股名稱＋代號」而不是總體市場關鍵字。
只在 main.py 對候選股（通常 <= 12 檔）呼叫，不是對全市場，避免對 Google News 發太多請求。
"""
from __future__ import annotations

import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from ..config import DRY_RUN
from . import mock

_UA = {"User-Agent": "Mozilla/5.0 (compatible; stock-report/1.0)"}
_TIMEOUT = 15


def fetch_stock_headlines(code: str, name: str, limit: int = 5) -> list[str]:
    if DRY_RUN:
        return mock.stock_headlines(code)

    query = f'"{name}" {code} when:5d'
    url = ("https://news.google.com/rss/search?q="
           + urllib.parse.quote(query) + "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant")
    try:
        req = urllib.request.Request(url, headers=_UA)
        raw = urllib.request.urlopen(req, timeout=_TIMEOUT).read()
        root = ET.fromstring(raw)
    except Exception as exc:
        print(f"[stock_news] {code} {name} 頭條擷取失敗：{exc}")
        return []

    titles = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        if title:
            titles.append(title)
        if len(titles) >= limit:
            break
    return titles
