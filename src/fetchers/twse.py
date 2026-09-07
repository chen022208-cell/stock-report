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


# openapi.twse.com.tw 會依來源 IP 擋掉部分機房（回 HTTP 200 但內容是
# 「因為安全性考量，您所執行的頁面無法呈現」的 HTML）。2026-09-07 的雲端盤後
# 就是這樣整批抓不到資料——網路是通的、只是被證交所擋。
# www.twse.com.tw/rwd/zh 的同名端點沒有這個限制，所以每一支都準備一條備援路徑，
# 並把 RWD 的 {fields, data} 轉成跟 openapi 一樣的欄位名，下游 parser 不用改。
_SECURITY_BLOCK = "FOR SECURITY REASONS"

_RWD_FALLBACK = {
    # openapi 路徑 → (RWD 路徑, 額外參數, 欄位對應；None＝直接用中文欄位名)
    "/exchangeReport/FMTQIK": ("/afterTrading/FMTQIK", {}, None),
    "/exchangeReport/MI_INDEX": ("/afterTrading/MI_INDEX", {"type": "IND"}, None),
    "/exchangeReport/STOCK_DAY_ALL": (
        "/afterTrading/MI_INDEX", {"type": "ALLBUT0999"},
        {"證券代號": "Code", "證券名稱": "Name", "成交股數": "TradeVolume",
         "成交筆數": "Transaction", "成交金額": "TradeValue", "開盤價": "OpeningPrice",
         "最高價": "HighestPrice", "最低價": "LowestPrice", "收盤價": "ClosingPrice"}),
}


def _rows_from_rwd(payload: dict, colmap: dict | None) -> list[dict]:
    """RWD 的 {fields, data} 或 {tables:[{fields,data}]} → list[dict]。

    漲跌在 RWD 是拆成「漲跌(+/-)」與「漲跌價差」兩欄（前者常帶 HTML 顏色標記），
    合併成 openapi 的單一 Change 欄位，_parse_signed() 才讀得到正負號。
    """
    blocks = payload.get("tables")
    if not isinstance(blocks, list) or not blocks:
        blocks = [payload]
    out: list[dict] = []
    for blk in blocks:
        fields = blk.get("fields") or []
        data = blk.get("data") or []
        if not fields or not data:
            continue
        # 多表回應（MI_INDEX）要挑出真的有個股資料的那一張
        if colmap and not any("證券代號" in str(f) for f in fields):
            continue
        for row in data:
            d = {str(f): row[i] if i < len(row) else "" for i, f in enumerate(fields)}
            sign = str(d.get("漲跌(+/-)", "") or "")
            diff = str(d.get("漲跌價差", "") or "")
            if diff:
                neg = ("-" in sign) or ("green" in sign.lower())
                d["Change"] = ("-" if neg else "") + diff.lstrip("+-")
            if colmap:
                d.update({en: d.get(zh, "") for zh, en in colmap.items()})
            out.append(d)
        if out:
            break
    return out


def _get_via_rwd(path: str) -> list[dict] | None:
    spec = _RWD_FALLBACK.get(path)
    if not spec:
        return None
    rwd_path, extra, colmap = spec
    from datetime import datetime, timedelta, timezone
    # RWD 要帶日期。用台北時間的今天當起點，但**往前找最近一個有資料的交易日**——
    # 盤後排程常在台北時間過午夜才跑，直接用「今天」會拿到
    # 「很抱歉，沒有符合條件的資料!」而不是錯誤，看起來像抓不到東西。
    day = datetime.now(timezone(timedelta(hours=8)))
    payload = None
    for back in range(6):
        params = {"response": "json", **extra,
                  "date": (day - timedelta(days=back)).strftime("%Y%m%d")}
        try:
            resp = requests.get(f"{RWD}{rwd_path}", params=params,
                                headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            got = resp.json()
        except Exception as exc:
            print(f"[twse] RWD 備援 {rwd_path} 也失敗：{exc}")
            return None
        if got.get("stat") == "OK":
            payload = got
            break
    if payload is None:
        print(f"[twse] RWD 備援 {rwd_path}：往前 6 天都沒有資料")
        return None
    rows = _rows_from_rwd(payload, colmap)
    if rows:
        print(f"[twse] {path} 走 www.twse.com.tw，取得 {len(rows)} 列")
    return rows or None


def _get(path: str) -> list[dict] | None:
    """證交所盤後資料。**有 RWD 對應的一律優先走 www.twse.com.tw。**

    ⚠️ `openapi.twse.com.tw` 穩定落後一個交易日（2026-09-08 實測：openapi 的
    FMTQIK 末筆是 1150904、STOCK_DAY_ALL 也是 1150904，而 www.twse.com.tw 的
    同名端點已經有 115/09/07）。以前主走 openapi，於是每天的盤後報告都比實際
    market 慢一天——使用者反覆回報「資料都是舊的」「沒有跟著更新」，真正的根因
    在這裡，不只是排程沒跑。openapi 保留當備援（RWD 偶爾維護時頂上）。
    """
    if path in _RWD_FALLBACK:
        rows = _get_via_rwd(path)
        if rows:
            return rows
        print(f"[twse] RWD 沒取到 {path}，改試 openapi（可能落後一個交易日）")
    try:
        resp = requests.get(f"{BASE}{path}", headers=HEADERS, timeout=TIMEOUT)
        # 被證交所擋時是 HTTP 200 + HTML，不是錯誤碼——要看內容才判斷得出來
        if _SECURITY_BLOCK in resp.text[:600].upper():
            print(f"[twse] openapi{path} 被來源限制擋下")
            return None
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else None
    except Exception as exc:
        print(f"[twse] {path} 擷取失敗：{exc}")
        return None if path in _RWD_FALLBACK else _get_via_rwd(path)


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


def _roc_to_iso(value) -> str:
    """民國日期字串（1150904）→ ISO（2026-09-04）。看不懂就回空字串。"""
    text = str(value or "").strip().replace("/", "")
    if len(text) == 7 and text.isdigit():
        return f"{int(text[:3]) + 1911}-{text[3:5]}-{text[5:]}"
    return ""


def fetch_index_summary() -> dict:
    """大盤收盤行情：加權指數、漲跌（含正負號）、漲跌%、成交值，**外加資料本身的日期**。

    `date` 這個欄位是必要的，不能靠呼叫端假設「今天跑的就是今天的資料」。
    FMTQIK 回的是當月已公布的交易日，最後一列是「目前最新一筆」——盤後報告
    如果在 TWSE 還沒把當日資料落地時執行，最後一列就是**前一個交易日**。
    2026-09-07 查出來的實際後果：資料庫裡 2026-09-04 那筆存的是 09-03（週四）
    的收盤 45857.66、2026-09-05（週六，根本沒開盤）那筆存的是 09-04（週五）的
    46551.13，整個評分頁的日期標籤都往後錯一個交易日，使用者看到的是
    「日期寫週六、數字停在週五」。
    """
    if DRY_RUN:
        return mock.index_summary()

    result: dict[str, Any] = {}

    # 主來源：FMTQIK（成交量值 + 加權指數 + 漲跌，一次到位）
    rows = _get("/exchangeReport/FMTQIK")
    if rows:
        last = rows[-1]
        result["date"] = _roc_to_iso(last.get("Date") or last.get("日期"))
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


def fetch_institutional_net(target: date | None = None) -> dict:
    """三大法人買賣超（單位：億元）。18:00 才抓的主因就是等這份資料落地。

    openapi /v1/fund/BFI82U 已失效，改走 RWD。回傳 data 為陣列：
      [單位名稱, 買進金額, 賣出金額, 買賣差額]
    列包含：自營商(自行買賣)、自營商(避險)、投信、外資及陸資(不含外資自營商)、外資自營商、合計

    ⚠️ **一定要帶日期，而且要檢查回傳的 date**。這支端點不帶 dayDate 時會回
    「最新一個有資料的交易日」，不管你以為今天是哪一天。實際踩到的後果：
    2026-09-05 那天 TWSE 還沒有當日資料，盤後把 09-04 的數字原封不動存進
    09-05 的 market_snapshots，兩天的外資／投信／自營商完全一樣（562.13／
    -9.11／63.7），週報與月報把區間內的法人買賣超一加就變成兩倍。

    回傳值多一個 "date"（YYYY-MM-DD，資料實際所屬日期），呼叫端必須自己比對
    是不是自己要的那一天，不符就不要當成當日資料存起來。
    """
    if DRY_RUN:
        return mock.institutional_net()

    params = {"type": "day"}
    if target:
        params["dayDate"] = target.strftime("%Y%m%d")
    data = _get_rwd("/fund/BFI82U", params)
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
    raw = str(data.get("date", "")).strip()          # 西元 20260904
    out["date"] = (f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
                   if len(raw) == 8 and raw.isdigit() else "")
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


def fetch_institutional_detail_by_stock(target: date | None = None) -> dict[str, dict]:
    """個股三大法人買賣超，**外資／投信／自營商分開**（單位：股）。

    `fetch_institutional_by_stock()` 只回三者合計，籌碼頁想分別列出各法人的
    買超／賣超排行就不夠用了。T86 本來就有分項欄位，這裡直接拆開：
      外資 = 外陸資買賣超（不含外資自營商）＋ 外資自營商買賣超
      投信 = 投信買賣超
      自營 = 自營商買賣超（自行買賣＋避險，T86 已有合計欄）

    回傳 {代號: {"name":, "foreign":, "trust":, "dealer":, "total":}}。
    """
    if DRY_RUN:
        return {}
    target = target or date.today()
    data = _get_rwd("/fund/T86", {"date": target.strftime("%Y%m%d"),
                                  "selectType": "ALLBUT0999"})
    if not data:
        return {}
    fields = data.get("fields", [])

    def idx(name: str) -> int | None:
        try:
            return fields.index(name)
        except ValueError:
            return None

    i_code, i_name = idx("證券代號"), idx("證券名稱")
    i_fgn = idx("外陸資買賣超股數(不含外資自營商)")
    i_fgn_d = idx("外資自營商買賣超股數")
    i_trust = idx("投信買賣超股數")
    i_dealer = idx("自營商買賣超股數")
    i_total = idx("三大法人買賣超股數")
    if i_code is None or i_total is None:
        return {}

    def cell(row, i):
        return _parse_signed(row[i]) if i is not None and i < len(row) else 0.0

    out: dict[str, dict] = {}
    for row in data.get("data", []):
        if len(row) <= i_code:
            continue
        code = str(row[i_code]).strip()
        if not code:
            continue
        out[code] = {
            "name": str(row[i_name]).strip() if i_name is not None and i_name < len(row) else "",
            "foreign": cell(row, i_fgn) + cell(row, i_fgn_d),
            "trust": cell(row, i_trust),
            "dealer": cell(row, i_dealer),
            "total": cell(row, i_total),
        }
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


def fetch_listing_dates() -> dict[str, str]:
    """股票代號 → 上市日期（YYYYMMDD）。跟 fetch_industry_map 同一份基本資料，
    只是多抓一個欄位，給「新掛牌觀察」用來判斷掛牌天數。"""
    if DRY_RUN:
        return mock.listing_dates_twse()
    rows = _get("/opendata/t187ap03_L")
    if not rows:
        return {}
    out = {}
    for row in rows:
        code = str(row.get("公司代號", "")).strip()
        listed = str(row.get("上市日期", "")).strip()
        if code and listed:
            out[code] = listed
    return out


def fetch_revenue_yoy() -> dict[str, float]:
    """上市公司最新月營收年增率（%）。回傳 {代號: YoY%}，給五面向評分的基本面軸用。"""
    if DRY_RUN:
        return mock.revenue_yoy()
    rows = _get("/opendata/t187ap05_L")
    if not rows:
        return {}
    out = {}
    for row in rows:
        code = str(row.get("公司代號", "")).strip()
        yoy = row.get("營業收入-去年同月增減(%)")
        if code and yoy not in (None, ""):
            out[code] = _num(yoy)
    return out


def fetch_disposition_stocks() -> list[dict]:
    """目前公布中的處置股票。"""
    if DRY_RUN:
        return mock.disposition_stocks()
    rows = _get("/announcement/punish")
    if not rows:
        return []
    out = []
    for row in rows:
        code = str(row.get("Code", "")).strip()
        if not code:
            continue
        out.append({
            "code": code, "name": row.get("Name", ""),
            "period": row.get("DispositionPeriod", ""),
            "measure": row.get("DispositionMeasures", ""),
            "reason": row.get("ReasonsOfDisposition", ""),
        })
    return out


def fetch_attention_trending() -> list[dict]:
    """注意累計次數可能達處置標準——TWSE 官方直接公布「還差幾次」的文字敘述。"""
    if DRY_RUN:
        return mock.attention_trending()
    rows = _get("/announcement/notetrans")
    if not rows:
        return []
    out = []
    for row in rows:
        code = str(row.get("Code", "")).strip()
        if not code:
            continue
        out.append({
            "code": code, "name": row.get("Name", ""),
            "note": row.get("RecentlyMetAttentionSecuritiesCriteria", ""),
        })
    return out


def fetch_attention_today() -> list[dict]:
    """今日新公布的注意股票。"""
    if DRY_RUN:
        return mock.attention_today()
    rows = _get("/announcement/notice")
    if not rows:
        return []
    out = []
    for row in rows:
        code = str(row.get("Code", "")).strip()
        if not code:
            continue
        out.append({
            "code": code, "name": row.get("Name", ""),
            "info": row.get("TradingInfoForAttention", ""),
        })
    return out


def fetch_margin_by_stock() -> dict[str, dict]:
    """個股融資融券餘額與當日增減（股數）。回傳 {代號: {...}}。"""
    if DRY_RUN:
        return mock.margin_by_stock()

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
