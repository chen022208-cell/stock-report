"""法說會行事曆（公開資訊觀測站 MOPS）。

法說會是題材知識庫「基本面驗證」最重要的第一手來源：
公司在法說會上對訂單能見度的說法，可以直接拿去驗證題材是真是假。

2026 現況（實測）：
- TWSE openapi 沒有法說會資料集（t187ap38_L 其實是股東會／除權息）。
- 改抓 MOPS 的 ajax_t100sb02_1（POST，回傳整頁 HTML 表格），用 pandas 解析。
- 任一步失敗都回空 list，不讓整份報告開天窗。
"""
from __future__ import annotations

import io
from datetime import date, timedelta

import requests

from ..config import DRY_RUN, now_tpe
from . import mock

TIMEOUT = 25
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
MOPS_URL = "https://mopsov.twse.com.tw/mops/web/ajax_t100sb02_1"


def _roc_to_iso(raw: str) -> str | None:
    """民國 115/09/11 → 2026-09-11。"""
    raw = str(raw).strip().replace("-", "/")
    parts = raw.split("/")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    try:
        y = int(parts[0])
        y = y + 1911 if y < 1911 else y
        return f"{y:04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    except ValueError:
        return None


def _fetch_month(roc_year: int, month: int) -> list[dict]:
    """抓某民國年月所有上市公司法說會，回傳含 iso 日期的清單。"""
    try:
        import pandas as pd

        resp = requests.post(MOPS_URL, headers=HEADERS, timeout=TIMEOUT, data={
            "encodeURIComponent": "1", "step": "1", "firstin": "1", "off": "1",
            "TYPEK": "sii", "year": str(roc_year), "month": f"{month:02d}",
        })
        resp.raise_for_status()
        resp.encoding = "utf-8"
        tables = pd.read_html(io.StringIO(resp.text))
    except Exception as exc:
        print(f"[mops] {roc_year}/{month:02d} 法說會擷取失敗：{exc}")
        return []

    target = None
    for t in tables:
        cols = [str(c) for c in t.columns]
        if any("法人說明會" in c for c in cols) or any("召開法人說明會日期" in c for c in cols):
            target = t
            break
    if target is None:
        return []

    def col(df, *keys):
        for c in df.columns:
            if any(k in str(c) for k in keys):
                return c
        return None

    c_code = col(target, "公司代號")
    c_name = col(target, "公司名稱", "公司簡稱")
    c_date = col(target, "召開法人說明會日期", "法人說明會日期")
    c_time = col(target, "召開法人說明會時間", "法人說明會時間")
    c_note = col(target, "法人說明會擇要訊息", "擇要訊息")
    if not (c_code and c_date):
        return []

    out = []
    for _, r in target.iterrows():
        iso = _roc_to_iso(r.get(c_date, ""))
        code = str(r.get(c_code, "")).strip()
        if not iso or not code or code in ("nan", "公司代號"):
            continue
        out.append({
            "code": code,
            "name": str(r.get(c_name, "")).strip() if c_name else "",
            "time": str(r.get(c_time, "")).strip() if c_time else "",
            "note": (str(r.get(c_note, "")).strip()[:120] if c_note else ""),
            "date": iso,
        })
    return out


def fetch_earnings_calls(target: date | None = None) -> list[dict]:
    """回傳指定日期召開法說會的公司清單。"""
    if DRY_RUN:
        return mock.earnings_calls()

    target = target or now_tpe().date()
    target_iso = target.isoformat()
    rows = _fetch_month(target.year - 1911, target.month)
    return [c for c in rows if c["date"] == target_iso]


def fetch_upcoming_calls(days_ahead: int = 7) -> list[dict]:
    """未來 N 天的法說會，給假日功課的「下週行事曆預告」用。"""
    if DRY_RUN:
        return mock.earnings_calls()

    today = now_tpe().date()
    end = today + timedelta(days=days_ahead)
    months = {(today.year - 1911, today.month), (end.year - 1911, end.month)}

    rows: list[dict] = []
    for roc_year, month in months:
        rows.extend(_fetch_month(roc_year, month))

    seen = set()
    upcoming = []
    for c in sorted(rows, key=lambda x: x["date"]):
        key = (c["code"], c["date"])
        if key in seen:
            continue
        seen.add(key)
        if today.isoformat() < c["date"] <= end.isoformat():
            upcoming.append(c)
    return upcoming
