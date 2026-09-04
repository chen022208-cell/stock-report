"""台股資料擷取（TWSE 開放 API + RWD JSON，免費免申請）。

每個函式都保證「失敗回傳空值而不拋例外」——單一資料源掛掉不該讓整份報告開天窗。

2026 現況（實測）：
- openapi.twse.com.tw/v1/fund/* 全數回傳瀏覽器驗證頁，不能用。
  三大法人改走 www.twse.com.tw/rwd/zh/fund/*（回傳 stat/fields/data 陣列格式）。
- openapi.twse.com.tw/v1/exchangeReport/* 與 /v1/opendata/* 正常。
- 大盤行情用 FMTQIK（收盤、漲跌、成交值一次到位），MI_INDEX 當備援。
- 漲跌家數 openapi 沒有直接資料集，改由當日全個股行情自行統計。
"""
from __future__ import annotations

import time
from datetime import date
from typing import Any, Callable

import requests

from ..config import DRY_RUN
from . import mock

BASE = "https://openapi.twse.com.tw/v1"
RWD = "https://www.twse.com.tw/rwd/zh"
TIMEOUT = 25
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}


def _get(path: str) -> list[dict] | None:
    try:
        resp = requests.get(f"{BASE}{path}", headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else None
    except Exception as exc:
        print(f"[twse] {path} 擷取失敗：{exc}")
        return None


def _get_rwd(path: str, params: dict) -> dict | None:
    """www.twse.com.tw 的 RWD JSON：回傳 {stat, fields, data:[[...], ...]}。"""
    for attempt in range(3):
        try:
            resp = requests.get(f"{RWD}{path}", params={**params, "response": "json"},
                                headers=HEADERS, timeout=TIMEOUT)
            if resp.status_code in (428, 429, 503):
                time.sleep(3 + attempt * 3)
                continue
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and data.get("stat") == "OK":
                return data
            return None
        except Exception as exc:
            if attempt == 2:
                print(f"[twse] RWD {path} 擷取失敗：{exc}")
    return None


def _num(value: Any) -> float:
    """TWSE 回傳的數字都是帶逗號的字串，還可能是 '--'。"""
    if value is None:
        return 0.0
    text = str(value).replace(",", "").replace("+", "").strip()
    if text in ("", "--", "-", "N/A"):
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def fetch_index_summary() -> dict:
    """大盤收盤行情：加權指數、漲跌（含正負號）、漲跌%、成交值。"""
    if DRY_RUN:
        return mock.index_summary()

    result: dict[str, Any] = {}

    # 主來源：FMTQIK（成交量值 + 加權指數 + 漲跌，一次到位）
    rows = _get("/exchangeReport/FMTQIK")
    if rows:
        last = rows[-1]
        close = _num(last.get("TAIEX") or last.get("發行量加權股價指數"))
        change = _parse_signed(last.get("Change") or last.get("漲跌點數"))
        if close > 0:
            result["taiex_close"] = close
            result["taiex_change"] = change
            prev = close - change
            result["taiex_change_pct"] = round(change / prev * 100, 2) if prev > 0 else 0.0
            result["turnover"] = _num(last.get("TradeValue") or last.get("成交金額"))

    # 備援：MI_INDEX
    if "taiex_close" not in result:
        mi = _get("/exchangeReport/MI_INDEX")
        for row in mi or []:
            name = row.get("指數") or row.get("Name") or ""
            if "發行量加權股價指數" in name and "報酬" not in name:
                close = _num(row.get("收盤指數") or row.get("ClosingIndex"))
                pct = _parse_signed(row.get("漲跌百分比") or row.get("ChangePercent"))
                pts = _parse_signed(row.get("漲跌點數") or row.get("Change"))
                # 漲跌點數欄位常不帶負號，用漲跌百分比的正負補回方向
                if pct < 0 and pts > 0:
                    pts = -pts
                result.update({"taiex_close": close, "taiex_change": pts,
                               "taiex_change_pct": pct})
                break

    return result or mock.index_summary()


def _parse_signed(value: Any) -> float:
    """保留正負號的數字解析。'+123.4' / '-123.4' / '123.4' / '<p style...>-1.2' 都能吃。"""
    if value is None:
        return 0.0
    text = str(value).replace(",", "").strip()
    neg = text.startswith("-") or "green" in text.lower()  # 有些欄位用顏色標跌
    text = text.lstrip("+-")
    # 去掉可能夾帶的 HTML
    digits = "".join(c for c in text if c.isdigit() or c == ".")
    if digits in ("", "."):
        return 0.0
    try:
        num = float(digits)
    except ValueError:
        return 0.0
    return -num if neg else num


def compute_breadth(quotes: list[dict]) -> dict:
    """由當日全個股行情統計漲跌家數（openapi 無現成資料集）。"""
    adv = sum(1 for q in quotes if q.get("change", 0) > 0)
    dec = sum(1 for q in quotes if q.get("change", 0) < 0)
    return {"advancers": adv, "decliners": dec} if (adv or dec) else {}


def fetch_institutional_net() -> dict:
    """三大法人買賣超（單位：億元）。18:00 才抓的主因就是等這份資料落地。

    openapi /v1/fund/BFI82U 已失效，改走 RWD。回傳 data 為陣列：
      [單位名稱, 買進金額, 賣出金額, 買賣差額]
    列包含：自營商(自行買賣)、自營商(避險)、投信、外資及陸資(不含外資自營商)、外資自營商、合計
    """
    if DRY_RUN:
        return mock.institutional_net()

    data = _get_rwd("/fund/BFI82U", {"type": "day"})
    if not data:
        return {}

    # 列名開頭：外資及陸資(不含外資自營商) / 外資自營商 / 投信 /
    #           自營商(自行買賣) / 自營商(避險) / 合計
    out = {"foreign_net": 0.0, "trust_net": 0.0, "dealer_net": 0.0}
    for row in data.get("data", []):
        if len(row) < 4:
            continue
        name = str(row[0]).strip()
        net = _parse_signed(row[3]) / 1e8  # 買賣差額，元 → 億
        if name.startswith("外資及陸資"):
            out["foreign_net"] += net
        elif name.startswith("投信"):
            out["trust_net"] += net
        elif name.startswith("自營商"):
            out["dealer_net"] += net
    out = {k: round(v, 2) for k, v in out.items()}
    out["total_net"] = round(sum(out.values()), 2)
    return out


def fetch_daily_quotes() -> list[dict]:
    """全上市個股當日行情，強勢股掃描的原料。"""
    if DRY_RUN:
        return mock.daily_quotes()

    rows = _get("/exchangeReport/STOCK_DAY_ALL")
    if not rows:
        return []

    quotes = []
    for row in rows:
        close = _num(row.get("ClosingPrice"))
        change = _parse_signed(row.get("Change"))
        if close <= 0:
            continue
        prev = close - change
        quotes.append({
            "code": row.get("Code", ""),
            "name": row.get("Name", ""),
            "close": close,
            "change": change,
            "change_pct": round(change / prev * 100, 2) if prev > 0 else 0.0,
            "volume": _num(row.get("TradeVolume")),
            "turnover": _num(row.get("TradeValue")),
        })
    return quotes


def fetch_institutional_by_stock(target: date | None = None) -> dict[str, float]:
    """個股三大法人買賣超合計（股數）。給題材知識庫的 inst_net 欄位用。

    回傳 {股票代號: 買賣超股數}。抓不到回空 dict。
    """
    if DRY_RUN:
        return {}
    target = target or date.today()
    data = _get_rwd("/fund/T86", {"date": target.strftime("%Y%m%d"),
                                  "selectType": "ALLBUT0999"})
    if not data:
        return {}
    fields = data.get("fields", [])
    try:
        code_i = fields.index("證券代號")
        net_i = fields.index("三大法人買賣超股數")
    except ValueError:
        return {}
    out: dict[str, float] = {}
    for row in data.get("data", []):
        if len(row) <= max(code_i, net_i):
            continue
        out[str(row[code_i]).strip()] = _parse_signed(row[net_i])
    return out


# TWSE 產業別代碼對照（t187ap03_L 的「產業別」欄位回傳的是代碼不是名稱）
INDUSTRY_CODE_NAME = {
    "01": "水泥工業", "02": "食品工業", "03": "塑膠工業", "04": "紡織纖維",
    "05": "電機機械", "06": "電器電纜", "08": "玻璃陶瓷", "09": "造紙工業",
    "10": "鋼鐵工業", "11": "橡膠工業", "12": "汽車工業", "13": "電子工業",
    "14": "建材營造", "15": "航運業", "16": "觀光事業", "17": "金融保險",
    "18": "貿易百貨", "19": "綜合", "20": "其他業", "21": "化學工業",
    "22": "生技醫療業", "23": "油電燃氣業", "24": "半導體業", "25": "電腦及週邊設備業",
    "26": "光電業", "27": "通信網路業", "28": "電子零組件業", "29": "電子通路業",
    "30": "資訊服務業", "31": "其他電子業", "32": "文化創意業", "33": "農業科技業",
    "34": "電子商務", "35": "綠能環保", "36": "數位雲端", "80": "管理顧問業",
    "91": "存託憑證", "97": "閉鎖性公司", "99": "未分類",
}


def fetch_industry_map() -> dict[str, str]:
    """股票代號 → 產業別。來源：上市公司基本資料，月更新即可，抓不到回空 dict。"""
    if DRY_RUN:
        return mock.industry_map()

    rows = _get("/opendata/t187ap03_L")
    if not rows:
        return {}
    out = {}
    for row in rows:
        code = str(row.get("公司代號", "")).strip()
        industry_code = str(row.get("產業別", "")).strip()
        if code and industry_code:
            out[code] = INDUSTRY_CODE_NAME.get(industry_code, industry_code)
    return out


def fetch_margin_by_stock() -> dict[str, dict]:
    """個股融資融券餘額與當日增減（股數）。回傳 {代號: {...}}。"""
    if DRY_RUN:
        return {}

    rows = _get("/exchangeReport/MI_MARGN")
    if not rows:
        return {}

    out: dict[str, dict] = {}
    for row in rows:
        code = str(row.get("股票代號", "")).strip()
        if not code:
            continue
        out[code] = {
            "margin_balance": _num(row.get("融資今日餘額")),
            "margin_change": _num(row.get("融資買進")) - _num(row.get("融資賣出"))
                             - _num(row.get("融資現金償還")),
            "short_balance": _num(row.get("融券今日餘額")),
            "short_change": _num(row.get("融券賣出")) - _num(row.get("融券買進"))
                            - _num(row.get("融券現券償還")),
        }
    return out


def fetch_stock_history(code: str, days: int = 120) -> list[dict]:
    """個股日 K，技術分析用。TWSE 是按月查，抓最近幾個月再截斷。

    TWSE 對這支端點有速率限制（約每秒數次），迴圈間插入短暫延遲避免 429。
    """
    if DRY_RUN:
        return mock.stock_history(code, days)

    today = date.today()
    out: list[dict] = []
    months_needed = days // 20 + 2

    for offset in range(months_needed):
        year, month = today.year, today.month - offset
        while month <= 0:
            month += 12
            year -= 1
        payload = _stock_day_month(code, year, month)
        for row in payload:
            try:
                roc_date = row[0].split("/")
                iso = f"{int(roc_date[0]) + 1911}-{roc_date[1]}-{roc_date[2]}"
                out.append({
                    "date": iso,
                    "open": _num(row[3]), "high": _num(row[4]),
                    "low": _num(row[5]), "close": _num(row[6]),
                    "volume": _num(row[1]),
                })
            except (IndexError, ValueError):
                continue
        time.sleep(0.6)

    out.sort(key=lambda r: r["date"])
    # 去重（跨月查詢邊界可能重複）
    seen = set()
    deduped = []
    for r in out:
        if r["date"] in seen:
            continue
        seen.add(r["date"])
        deduped.append(r)
    return deduped[-days:]


def _stock_day_month(code: str, year: int, month: int, retries: int = 2) -> list[list]:
    for attempt in range(retries + 1):
        try:
            resp = requests.get(
                "https://www.twse.com.tw/exchangeReport/STOCK_DAY",
                params={"response": "json", "date": f"{year}{month:02d}01", "stockNo": code},
                headers=HEADERS, timeout=TIMEOUT,
            )
            if resp.status_code in (428, 429, 503):
                # TWSE 過量時會回這幾種狀態，退避後重試
                time.sleep(3 + attempt * 3)
                continue
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("stat") == "OK":
                return payload.get("data", [])
            return []
        except Exception as exc:
            if attempt == retries:
                print(f"[twse] {code} {year}-{month:02d} 歷史股價擷取失敗：{exc}")
            else:
                time.sleep(1 + attempt)
    return []
