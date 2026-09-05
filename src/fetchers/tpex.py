"""上櫃（TPEx）資料擷取，openapi.tpex.org.tw，免費免申請。

TPEx 的 openapi 欄位是英文（跟 TWSE 中文欄位不同），但同樣不穩定、偶爾會回
非 JSON（520 之類），一律 try/except 回空值。daily_close_quotes 裡混了大量
ETF／債券／權證，只留 4 碼純數字代號當作個股。
"""
from __future__ import annotations

import time
from datetime import date

import requests

from ..config import DRY_RUN
from . import mock

BASE = "https://www.tpex.org.tw/openapi/v1"
# 個股日成交資訊（一次回一個月）；瀏覽器端對這支沒有 CORS，只能後端抓、
# 快照成 docs/data/tpex_hist/<code>.json 給 stock-chart.js 當上櫃／興櫃的退路。
TRADINGSTOCK = "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock"
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


def fetch_stock_daily_history(code: str, months: int = 8) -> list[dict]:
    """單一上櫃／興櫃個股近 N 個月的日 K（OHLCV），供後端快照。

    tradingStock 端點一次回一個月，欄位順序：
    [日期(民國 115/09/01), 成交張數, 成交仟元, 開, 高, 低, 收, 漲跌, 筆數]。
    volume 存成「股」（張數 × 1000），對齊 TWSE STOCK_DAY，讓 stock-chart.js
    同一套 `volume / 1000` 邏輯換算成張。抓不到就回空 list，不丟例外。
    """
    if DRY_RUN:
        return []

    bars: dict[str, dict] = {}
    today = date.today()
    year, month = today.year, today.month
    for _ in range(max(1, months)):
        try:
            resp = requests.get(
                TRADINGSTOCK,
                params={"code": code, "date": f"{year}/{month:02d}/01", "response": "json"},
                headers=HEADERS, timeout=TIMEOUT,
            )
            resp.raise_for_status()
            payload = resp.json()
            tables = payload.get("tables") or []
            data = (tables[0].get("data") if tables else None) or []
            for row in data:
                try:
                    roc = str(row[0]).strip().split("/")
                    iso = f"{int(roc[0]) + 1911}-{int(roc[1]):02d}-{int(roc[2]):02d}"
                    close = _num(row[6])
                    if close <= 0:
                        continue
                    bars[iso] = {
                        "date": iso,
                        "open": _num(row[3]), "high": _num(row[4]),
                        "low": _num(row[5]), "close": close,
                        "volume": int(_num(row[1]) * 1000),
                    }
                except (IndexError, ValueError):
                    continue
        except Exception as exc:
            print(f"[tpex] {code} {year}/{month:02d} 歷史擷取失敗：{exc}")
        month -= 1
        if month == 0:
            month, year = 12, year - 1
        time.sleep(0.3)

    return [bars[k] for k in sorted(bars)]


ESB_HISTORICAL = "https://www.tpex.org.tw/www/zh-tw/emerging/historical"


def fetch_esb_daily_history(code: str, months: int = 8) -> list[dict]:
    """單一興櫃個股近 N 個月歷史行情。興櫃是議價／搓合市場，沒有開盤／收盤，
    只有當日成交最高、最低、加權平均價——用均價當 close、前一日均價當 open，
    畫出來是「均價走勢」而不是嚴格的 K 棒（stock-chart.js 會標註）。

    emerging/historical 欄位：
    [日期, 成交股數, 成交金額, 最高, 最低, 加權平均價, 筆數, （後面一組多為 0）]
    """
    if DRY_RUN:
        return []

    rows: dict[str, dict] = {}
    today = date.today()
    year, month = today.year, today.month
    for _ in range(max(1, months)):
        try:
            resp = requests.get(
                ESB_HISTORICAL,
                params={"code": code, "date": f"{year}/{month:02d}/01", "response": "json"},
                headers=HEADERS, timeout=TIMEOUT,
            )
            resp.raise_for_status()
            tables = resp.json().get("tables") or []
            data = (tables[0].get("data") if tables else None) or []
            for row in data:
                try:
                    roc = str(row[0]).strip().split("/")
                    iso = f"{int(roc[0]) + 1911}-{int(roc[1]):02d}-{int(roc[2]):02d}"
                    high, low, avg = _num(row[3]), _num(row[4]), _num(row[5])
                    if avg <= 0:
                        continue
                    rows[iso] = {
                        "date": iso, "open": avg, "high": high or avg,
                        "low": low or avg, "close": avg,
                        "volume": int(_num(row[1])),
                    }
                except (IndexError, ValueError):
                    continue
        except Exception as exc:
            print(f"[tpex] {code} {year}/{month:02d} 興櫃歷史擷取失敗：{exc}")
        month -= 1
        if month == 0:
            month, year = 12, year - 1
        time.sleep(0.3)

    ordered = [rows[k] for k in sorted(rows)]
    for i in range(1, len(ordered)):          # open 用前一日均價，讓走勢連續
        ordered[i]["open"] = ordered[i - 1]["close"]
    return ordered


def fetch_offmarket_daily_history(code: str, months: int = 8) -> dict:
    """先試上櫃（tradingStock），沒有再試興櫃（emerging/historical）。
    回傳 {"bars": [...], "market": "tpex"|"esb"|""}，bars 空代表兩邊都查不到。"""
    bars = fetch_stock_daily_history(code, months)
    if bars:
        return {"bars": bars, "market": "tpex"}
    bars = fetch_esb_daily_history(code, months)
    if bars:
        return {"bars": bars, "market": "esb"}
    return {"bars": [], "market": ""}


def fetch_esb_pricing() -> dict[str, dict]:
    """興櫃「當日行情表」完整欄位，對齊 tpex.org.tw/zh-tw/esb/trading/info/pricing.html
    那張表：前日均價／報買價／報賣價／日最高／日最低／日均價／成交／成交量。

    重點：一般看盤說的「股價」是 LatestPrice（該表的「成交」欄），不是 Average
    （「日均價」）。興櫃歷史行情端點只給得到日均價，所以最新報價要靠這支補。
    """
    if DRY_RUN:
        return {}
    rows = _get("tpex_esb_latest_statistics")
    if not rows:
        return {}
    out: dict[str, dict] = {}
    for row in rows:
        code = str(row.get("SecuritiesCompanyCode", "")).strip()
        if not _is_stock_code(code):
            continue
        price = _num(row.get("LatestPrice"))
        prev = _num(row.get("PreviousAveragePrice"))
        if price <= 0:
            continue
        raw_date = str(row.get("Date", "")).strip()      # 民國 1150904
        iso = ""
        if len(raw_date) == 7 and raw_date.isdigit():
            iso = f"{int(raw_date[:3]) + 1911}-{raw_date[3:5]}-{raw_date[5:]}"
        out[code] = {
            "date": iso,
            "name": str(row.get("CompanyName", "")).strip(),
            "price": price,                              # 成交（最後成交價）
            "prev_avg": prev,                            # 前日均價
            "change": round(price - prev, 2) if prev > 0 else 0.0,
            "change_pct": round((price - prev) / prev * 100, 2) if prev > 0 else 0.0,
            "high": _num(row.get("Highest")),
            "low": _num(row.get("Lowest")),
            "avg": _num(row.get("Average")),             # 日均價
            "bid": _num(row.get("BuyingPrice")),
            "ask": _num(row.get("SellingPrice")),
            "volume": _num(row.get("TransactionVolume")),
        }
    return out
