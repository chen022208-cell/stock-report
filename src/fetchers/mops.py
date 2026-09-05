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


# ── 公司基本資料（公開資訊觀測站 t05st03）────────────────────────
# 這支是「公司到底在做什麼」的權威來源：回傳的「主要經營業務」是公司自己
# 向主管機關申報的營業項目，比用股票名稱或產業分類去猜可靠得多。
# 2026-09 曾經因為憑名字猜而把做乳房重建的 7686 捷立康寫成 PCB 廠，
# 之後所有公司介紹／SWOT 一律要以這支回來的事實為基礎。
PROFILE_URL = "https://mopsov.twse.com.tw/mops/web/ajax_t05st03"

_PROFILE_LABELS = {
    "主要經營業務": "business",
    "產業類別": "industry",
    "公司成立日期": "founded",
    "上市日期": "listed_twse",
    "上櫃日期": "listed_tpex",
    "興櫃日期": "listed_esb",
    "實收資本額": "capital",
    "董事長": "chairman",
    "總經理": "gm",
    "公司網址": "website",
    "英文簡稱": "eng_abbr",
    "公司簡稱": "abbr",
    "公司名稱": "full_name",
}


def fetch_company_profile(code: str) -> dict:
    """單一個股的公司基本資料。回傳 {business, industry, capital, website, ...}，
    抓不到就回空 dict（呼叫端要能接受沒有資料，不要自己編）。"""
    if DRY_RUN:
        return {}
    import re

    payload = {
        "encodeURIComponent": "1", "step": "1", "firstin": "1", "off": "1",
        "keyword4": "", "code1": "", "TYPEK2": "", "checkbtn": "",
        "queryName": "co_id", "inpuType": "co_id", "TYPEK": "all", "co_id": code,
    }
    try:
        resp = requests.post(PROFILE_URL, data=payload, headers=HEADERS, timeout=30)
        resp.encoding = "utf-8"
        html = resp.text
    except Exception as exc:
        print(f"[mops] {code} 公司基本資料擷取失敗：{exc}")
        return {}

    # 表格是 <td>標籤</td><td>值</td>，把標籤後面第一段文字抓出來
    cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()
             for c in re.split(r"</t[dh]>", html)]
    out: dict = {}
    for i, cell in enumerate(cells[:-1]):
        key = _PROFILE_LABELS.get(cell.replace(" ", ""))
        if key and key not in out:
            value = cells[i + 1].replace("&nbsp", "").strip(" ;　").strip()
            if value and value not in ("-", "--"):
                out[key] = value
    return out


# ── 每月營收（政府開放資料，基本面的事實來源，不經 LLM）────────────
REVENUE_SOURCES = [
    ("twse", "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"),
    ("tpex", "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"),
    # 興櫃月營收：欄位名稱跟上市／上櫃那份一致，同一個 parser 就能吃。
    # （少了這條，之前興櫃約 360 檔的「基本面」區塊全部是空的。）
    ("esb", "https://www.tpex.org.tw/openapi/v1/t187ap05_R"),
]


def _rev_num(value) -> float | None:
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if text in ("", "-", "--", "N/A"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def fetch_monthly_revenue() -> dict[str, dict]:
    """全上市＋上櫃＋興櫃公司最新一期月營收。回傳 {代號: {period, revenue, yoy, mom, ...}}。
    金額單位為千元，直接來自公開資訊觀測站申報值。"""
    if DRY_RUN:
        return {}

    out: dict[str, dict] = {}
    for market, url in REVENUE_SOURCES:
        try:
            rows = requests.get(url, headers=HEADERS, timeout=60).json()
        except Exception as exc:
            print(f"[mops] {market} 月營收擷取失敗：{exc}")
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            code = str(row.get("公司代號", "")).strip()
            ym = str(row.get("資料年月", "")).strip()      # 民國 11507
            if not (code.isdigit() and len(code) == 4 and len(ym) == 5):
                continue
            period = f"{int(ym[:3]) + 1911}-{ym[3:]}"
            out[code] = {
                "period": period,
                "name": str(row.get("公司名稱", "")).strip(),
                "industry": str(row.get("產業別", "")).strip(),
                "market": market,
                "revenue": _rev_num(row.get("營業收入-當月營收")),
                "mom": _rev_num(row.get("營業收入-上月比較增減(%)")),
                "yoy": _rev_num(row.get("營業收入-去年同月增減(%)")),
                "cum_revenue": _rev_num(row.get("累計營業收入-當月累計營收")),
                "cum_yoy": _rev_num(row.get("累計營業收入-前期比較增減(%)")),
            }
    print(f"[mops] 月營收 {len(out)} 檔")
    return out


# ── 全市場公司清單（上市／上櫃／興櫃）──────────────────────────
# 用申報基本資料 t187ap03 的三個資料集當權威名單：只有「真的是公司」的代號，
# 不含 ETF／權證／債券，而且市場別是確定的（stock_index.json 沒有市場別欄位，
# 之前一律當成上市，導致上櫃／興櫃被誤判、前端白打 TWSE 端點）。
COMPANY_LIST_SOURCES = [
    ("twse", "https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
     "公司代號", "公司簡稱", "產業別"),
    ("tpex", "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O",
     "SecuritiesCompanyCode", "CompanyAbbreviation", "SecuritiesIndustryCode"),
    ("esb", "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_R",
     "SecuritiesCompanyCode", "CompanyAbbreviation", "SecuritiesIndustryCode"),
]


def fetch_listed_companies() -> dict[str, dict]:
    """回傳 {代號: {code, name, market, industry}}，market 為 twse/tpex/esb。"""
    if DRY_RUN:
        return {}
    out: dict[str, dict] = {}
    for market, url, k_code, k_name, k_ind in COMPANY_LIST_SOURCES:
        try:
            rows = requests.get(url, headers=HEADERS, timeout=60).json()
        except Exception as exc:
            print(f"[mops] {market} 公司清單擷取失敗：{exc}")
            continue
        if not isinstance(rows, list):
            continue
        got = 0
        for row in rows:
            code = str(row.get(k_code, "")).strip()
            if not (code.isdigit() and len(code) == 4):
                continue
            # 同一家公司可能同時出現在興櫃與上櫃名單（轉板中），以先出現的為準：
            # 順序是 上市 > 上櫃 > 興櫃，剛好就是我們要的優先序。
            if code in out:
                continue
            out[code] = {"code": code, "name": str(row.get(k_name, "")).strip(),
                         "market": market, "industry": str(row.get(k_ind, "")).strip()}
            got += 1
        print(f"[mops] {market} 公司 {got} 檔")
    return out
