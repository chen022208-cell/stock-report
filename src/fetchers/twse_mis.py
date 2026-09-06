"""台股盤中即時報價（TWSE / TPEx 基本市況報導 MIS，免費免申請）。

盤後那套 openapi STOCK_DAY_ALL 只有收盤資料，盤中要用 mis.twse.com.tw 的
getStockInfo.jsp——一次可以帶一批代號（tse_XXXX.tw / otc_XXXX.tw 混在同一個
ex_ch 參數），上市上櫃同一支端點就拿得到。

回傳的關鍵欄位：
  c 代號  n 名稱  z 成交價  y 昨收  o 開盤  h 最高  l 最低
  v 累計成交量(張)  t 最後成交時間  d 日期  ex tse/otc
  u 漲停價  w 跌停價
盤中 z / v / t 會逐分鐘更新；收盤後回傳的是最後一個交易日的定盤值。

⚠️ 這是「即時報價」——公開網站上顯示要延遲一段時間（見 config.yaml 的
intraday.display_delay_min），內部運算可以用即時值。
"""
from __future__ import annotations

import time

import requests

MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://mis.twse.com.tw/stock/index.jsp",
}
BATCH_SIZE = 60          # 一次帶幾檔（MIS 對單次查詢檔數有上限，60 是保守值）
SLEEP_BETWEEN = 0.35     # 批次之間 sleep，避免被 MIS 限流


def _num(value, default: float = 0.0) -> float:
    try:
        text = str(value).replace(",", "").strip()
        if text in ("", "-", "--"):
            return default
        return float(text)
    except (TypeError, ValueError):
        return default


def _ex_ch(code: str, market: str) -> str:
    prefix = "otc" if market in ("tpex", "otc", "esb") else "tse"
    return f"{prefix}_{code}.tw"


def _parse_row(row: dict) -> dict | None:
    code = str(row.get("c", "")).strip()
    price = _num(row.get("z"))
    prev = _num(row.get("y"))
    if not code:
        return None
    # 收盤後 z 可能是 "-"（無成交），退回用參考價 pz
    if price <= 0:
        price = _num(row.get("pz")) or _num(row.get("o"))
    change = price - prev if (price > 0 and prev > 0) else 0.0
    return {
        "code": code,
        "name": str(row.get("n", "")).strip(),
        "price": price,
        "prev_close": prev,
        "open": _num(row.get("o")),
        "high": _num(row.get("h")),
        "low": _num(row.get("l")),
        "change": round(change, 4),
        "change_pct": round(change / prev * 100, 2) if prev > 0 else 0.0,
        "volume": _num(row.get("v")),           # 累計成交量（張）
        "limit_up": _num(row.get("u")),
        "limit_down": _num(row.get("w")),
        "trade_time": str(row.get("t", "")).strip(),
        "quote_date": str(row.get("d", "")).strip(),
        "ex": str(row.get("ex", "")).strip(),
    }


def fetch_quotes(codes_with_market: list[tuple[str, str]]) -> dict[str, dict]:
    """codes_with_market = [(code, market), ...]，market 是 'twse' / 'tpex'。

    回傳 {code: {price, prev_close, open, high, low, change, change_pct, volume, ...}}。
    抓不到的代號就不會出現在結果裡（不拋例外）。
    """
    out: dict[str, dict] = {}
    batch: list[str] = []

    def flush() -> None:
        if not batch:
            return
        try:
            resp = requests.get(
                MIS_URL,
                params={"ex_ch": "|".join(batch), "json": 1, "delay": 0,
                        "_": int(time.time() * 1000)},
                headers=HEADERS, timeout=15,
            )
            data = resp.json()
        except Exception as exc:
            print(f"[mis] 批次擷取失敗（{len(batch)} 檔）：{exc}")
            batch.clear()
            return
        for row in data.get("msgArray", []) or []:
            parsed = _parse_row(row)
            if parsed and parsed["price"] > 0:
                out[parsed["code"]] = parsed
        batch.clear()

    for code, market in codes_with_market:
        batch.append(_ex_ch(code, market))
        if len(batch) >= BATCH_SIZE:
            flush()
            time.sleep(SLEEP_BETWEEN)
    flush()
    return out


def fetch_taiex() -> dict:
    """加權指數（tse_t00.tw）盤中即時，算相對強度用。回傳 {value, prev_close, change_pct}。"""
    try:
        resp = requests.get(
            MIS_URL,
            params={"ex_ch": "tse_t00.tw", "json": 1, "delay": 0,
                    "_": int(time.time() * 1000)},
            headers=HEADERS, timeout=15,
        )
        rows = resp.json().get("msgArray", []) or []
    except Exception as exc:
        print(f"[mis] 加權指數擷取失敗：{exc}")
        return {}
    if not rows:
        return {}
    row = rows[0]
    value = _num(row.get("z")) or _num(row.get("pz"))
    prev = _num(row.get("y"))
    return {
        "value": value,
        "prev_close": prev,
        "change_pct": round((value - prev) / prev * 100, 2) if prev > 0 else 0.0,
        "trade_time": str(row.get("t", "")).strip(),
    }
