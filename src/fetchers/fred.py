"""FRED（美國聖路易聯準銀行）總經數據，免費但要註冊拿 key，走環境變數 FRED_API_KEY。

沒設定 key 就整個模組回空值——不是壞掉，是使用者還沒申請。免費申請（幾分鐘）：
https://fred.stlouisfed.org/docs/api/api_key.html
"""
from __future__ import annotations

import requests

from ..config import DRY_RUN, FRED_API_KEY
from . import mock

BASE = "https://api.stlouisfed.org/fred/series/observations"
TIMEOUT = 20
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; stock-report/1.0)"}

# 對台股解讀最有用的幾個系列：CPI、失業率、10年期公債殖利率、聯邦資金利率、ISM製造業
SERIES = [
    ("CPIAUCSL", "美國 CPI（年增率換算另計）"),
    ("UNRATE", "美國失業率"),
    ("DGS10", "10年期公債殖利率"),
    ("FEDFUNDS", "聯邦資金利率"),
]


def fetch_series(series_id: str, limit: int = 2) -> list[dict]:
    """回傳最近 limit 筆 {date, value}，由舊到新。沒有 key 或抓不到都回空清單。"""
    if DRY_RUN:
        return mock.fred_series(series_id)
    if not FRED_API_KEY:
        return []
    try:
        resp = requests.get(BASE, headers=HEADERS, timeout=TIMEOUT, params={
            "series_id": series_id, "api_key": FRED_API_KEY, "file_type": "json",
            "sort_order": "desc", "limit": limit,
        })
        resp.raise_for_status()
        data = resp.json()
        obs = data.get("observations", [])
        out = []
        for o in reversed(obs):
            try:
                out.append({"date": o["date"], "value": float(o["value"])})
            except (KeyError, ValueError):
                continue
        return out
    except Exception as exc:
        print(f"[fred] {series_id} 擷取失敗：{exc}")
        return []


def fetch_macro_snapshot() -> list[dict]:
    """回傳 [{name, date, value, change}]，change 是跟前一筆的差。沒有 key 就回空清單，
    上層（早報／月報）用 if 判斷是否顯示這個區塊，不會因為沒 key 就報錯或開天窗。"""
    if DRY_RUN:
        return mock.fred_snapshot()
    if not FRED_API_KEY:
        return []

    out = []
    for series_id, name in SERIES:
        obs = fetch_series(series_id, limit=2)
        if not obs:
            continue
        latest = obs[-1]
        change = round(latest["value"] - obs[-2]["value"], 3) if len(obs) > 1 else None
        out.append({"name": name, "date": latest["date"], "value": latest["value"],
                    "change": change})
    return out
