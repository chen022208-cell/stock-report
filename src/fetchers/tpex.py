"""上櫃（TPEx）資料擷取，openapi.tpex.org.tw，免費免申請。

TPEx 的 openapi 欄位是英文（跟 TWSE 中文欄位不同），但同樣不穩定、偶爾會回
非 JSON（520 之類），一律 try/except 回空值。daily_close_quotes 裡混了大量
ETF／債券／權證，只留 4 碼純數字代號當作個股。
"""
from __future__ import annotations

import requests

from ..config import DRY_RUN
from . import mock

BASE = "https://www.tpex.org.tw/openapi/v1"
TIMEOUT = 25
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}


def _get(path: str) -> list[dict] | None:
    try:
        resp = requests.get(f"{BASE}/{path}", headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else None
    except Exception as exc:
        print(f"[tpex] {path} 擷取失敗：{exc}")
        return None


def _num(value) -> float:
    if value is None:
        return 0.0
    text = str(value).replace(",", "").strip()
    if text in ("", "--", "-", "N/A"):
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _is_stock_code(code: str) -> bool:
    return code.isdigit() and len(code) == 4


def fetch_daily_quotes() -> list[dict]:
    """上櫃個股當日行情，格式對齊 twse.fetch_daily_quotes()。"""
    if DRY_RUN:
        return mock.tpex_daily_quotes()

    rows = _get("tpex_mainboard_daily_close_quotes")
    if not rows:
        return []

    quotes = []
    for row in rows:
        code = str(row.get("SecuritiesCompanyCode", "")).strip()
        if not _is_stock_code(code):
            continue
        close = _num(row.get("Close"))
        change = _num(str(row.get("Change", "")).replace("+", ""))
        if close <= 0:
            continue
        prev = close - change
        quotes.append({
            "code": code,
            "name": str(row.get("CompanyName", "")).strip(),
            "close": close,
            "change": change,
            "change_pct": round(change / prev * 100, 2) if prev > 0 else 0.0,
            "volume": _num(row.get("TradingShares")),
            "turnover": _num(row.get("TransactionAmount")),
            "market": "tpex",
        })
    return quotes


def fetch_institutional_by_stock() -> dict[str, float]:
    """上櫃個股三大法人合計買賣超（股數）。回傳 {代號: 買賣超}。"""
    if DRY_RUN:
        return {}

    rows = _get("tpex_3insti_daily_trading")
    if not rows:
        return {}

    out: dict[str, float] = {}
    for row in rows:
        code = str(row.get("SecuritiesCompanyCode", "")).strip()
        if not _is_stock_code(code):
            continue
        total = row.get("TotalDifference")
        if total is not None:
            out[code] = _num(total)
    return out


def fetch_margin_by_stock() -> dict[str, dict]:
    """上櫃個股融資融券餘額與使用率。回傳 {代號: {...}}。"""
    if DRY_RUN:
        return {}

    rows = _get("tpex_mainboard_margin_balance")
    if not rows:
        return {}

    out: dict[str, dict] = {}
    for row in rows:
        code = str(row.get("SecuritiesCompanyCode", "")).strip()
        if not _is_stock_code(code):
            continue
        out[code] = {
            "margin_balance": _num(row.get("MarginPurchaseBalance")),
            "margin_change": _num(row.get("MarginPurchase")) - _num(row.get("MarginSales")),
            "short_balance": _num(row.get("ShortSaleBalance")),
            "short_change": _num(row.get("ShortSale")) - _num(row.get("ShortConvering")),
        }
    return out


def fetch_listing_dates() -> dict[str, str]:
    """股票代號 → 上櫃日期（YYYYMMDD）。給「新掛牌觀察」判斷掛牌天數用。"""
    if DRY_RUN:
        return mock.listing_dates_tpex()
    rows = _get("mopsfin_t187ap03_O")
    if not rows:
        return {}
    out: dict[str, str] = {}
    for row in rows:
        code = str(row.get("SecuritiesCompanyCode", "")).strip()
        listed = str(row.get("DateOfListing", "")).strip()
        if _is_stock_code(code) and listed:
            out[code] = listed
    return out


def fetch_esb_listing_dates() -> dict[str, str]:
    """股票代號 → 興櫃掛牌日期（YYYYMMDD）。興櫃是上市/上櫃之前更早期的階段，
    跟 fetch_listing_dates()（上櫃）是不同的資料集（t187ap03_R vs _O）。"""
    if DRY_RUN:
        return mock.listing_dates_esb()
    rows = _get("mopsfin_t187ap03_R")
    if not rows:
        return {}
    out: dict[str, str] = {}
    for row in rows:
        code = str(row.get("SecuritiesCompanyCode", "")).strip()
        listed = str(row.get("DateOfListing", "")).strip()
        if _is_stock_code(code) and listed:
            out[code] = listed
    return out


def fetch_esb_quotes() -> dict[str, dict]:
    """興櫃個股當日成交行情，格式對齊 fetch_daily_quotes()。興櫃是議價/搓合
    市場（不是連續競價），用最新成交價對比前一日均價當漲跌幅的近似值。
    回傳 {代號: quote}，不是 list——興櫃只用來查特定代號，不像上市/上櫃
    要整批塞進熱力圖/強勢股掃描（交易機制不同，混進去會失真）。"""
    if DRY_RUN:
        return mock.esb_quotes()
    rows = _get("tpex_esb_latest_statistics")
    if not rows:
        return {}
    out: dict[str, dict] = {}
    for row in rows:
        code = str(row.get("SecuritiesCompanyCode", "")).strip()
        if not _is_stock_code(code):
            continue
        close = _num(row.get("LatestPrice"))
        prev = _num(row.get("PreviousAveragePrice"))
        if close <= 0:
            continue
        change = close - prev
        out[code] = {
            "code": code,
            "name": str(row.get("CompanyName", "")).strip(),
            "close": close,
            "change": round(change, 2),
            "change_pct": round(change / prev * 100, 2) if prev > 0 else 0.0,
            "volume": _num(row.get("TransactionVolume")),
            "turnover": 0.0,
            "market": "esb",
        }
    return out
