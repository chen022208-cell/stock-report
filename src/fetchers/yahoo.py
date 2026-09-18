"""Yahoo Finance 圖表 API（query1.finance.yahoo.com/v8/finance/chart），免費免申請。

為什麼需要這一支：
- **興櫃**：TPEx 的 emerging/historical 只給得到「日均價」，畫不出真正的 K 棒，
  最新價也對不上看盤軟體的「成交」欄（歷史事故：7686 捷立康拿日均價 686.33
  當股價顯示，跟 TPEx 網站的成交價 802 對不起來）。Yahoo 對興櫃直接給
  開高低收，實測 7686 回的收盤就是 802，跟 TPEx 當日行情表一致。
- **上櫃**：一支請求就能拿兩年日 K，比 tradingStock 端點「一次一個月、要打 8 次」
  快一個數量級，全市場 1200+ 檔跑完只要約 2 分鐘。
- **櫃買指數**：^TWOII（加權指數是 ^TWII），TWSE/TPEx 的開放資料都沒有現成的
  指數歷史序列。

⚠️ 這支**沒有 CORS 標頭**，瀏覽器端 fetch 一定失敗，只能後端抓、快照成
docs/data/tpex_hist/<code>.json 給 stock-chart.js 讀。上市股票前端仍走
TWSE STOCK_DAY（那支有 CORS，是真正的即時資料），不要改成 Yahoo。

代號後綴：上市 .TW，上櫃與興櫃都是 .TWO。市場別偶爾會跟申報資料對不齊
（剛轉上市／剛從興櫃升上櫃），所以查不到時會自動換另一個後綴再試一次。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import requests

from ..config import DRY_RUN

CHART = "https://query1.finance.yahoo.com/v8/finance/chart"
TIMEOUT = 20
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

# 加權指數／櫃買指數。Yahoo 的指數代號跟個股不同，另外列出來當常數用。
TAIEX = "^TWII"
TPEX_INDEX = "^TWOII"

_SUFFIX = {"twse": ".TW", "tpex": ".TWO", "esb": ".TWO"}

_session: requests.Session | None = None


def _sess() -> requests.Session:
    """共用連線：全市場快照要打上千次，每次重連 TLS 會慢很多。"""
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update(HEADERS)
    return _session


def _symbol(code: str, market: str) -> str:
    return f"{code}{_SUFFIX.get(market, '.TW')}"


def _parse(payload: dict) -> dict:
    """chart API 回傳 → {"bars": [...], "meta": {...}}，格式對齊 tpex 那邊的
    快照結構（date/open/high/low/close/volume），stock-chart.js 不用改資料格式。

    Yahoo 的 quote 陣列允許出現 null（停牌、無成交的交易日），那種列直接丟掉，
    留著會讓前端算均線與背離時撞到 null。"""
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not result:
        return {"bars": [], "meta": {}}
    stamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    opens, highs = quote.get("open") or [], quote.get("high") or []
    lows, closes = quote.get("low") or [], quote.get("close") or []
    volumes = quote.get("volume") or []

    bars = []
    for i, ts in enumerate(stamps):
        try:
            c = closes[i]
            if c is None:
                continue
            o, h, l = opens[i], highs[i], lows[i]
            # 興櫃常有「有成交價但沒有完整開高低」的日子，缺的用收盤補，
            # 至少 K 棒畫得出來而且不會是假的區間。
            bars.append({
                "date": datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%Y-%m-%d"),
                "open": round(float(o if o is not None else c), 4),
                "high": round(float(h if h is not None else c), 4),
                "low": round(float(l if l is not None else c), 4),
                "close": round(float(c), 4),
                "volume": int(volumes[i] or 0) if i < len(volumes) else 0,
            })
        except (IndexError, TypeError, ValueError):
            continue

    meta = result.get("meta") or {}
    return {"bars": bars, "meta": {
        "price": meta.get("regularMarketPrice"),
        "prev_close": meta.get("chartPreviousClose") or meta.get("previousClose"),
        "name": meta.get("longName") or meta.get("shortName") or "",
        "currency": meta.get("currency", ""),
        "exchange": meta.get("fullExchangeName", ""),
    }}


def _chart(symbol: str, rng: str, interval: str = "1d") -> dict:
    try:
        resp = _sess().get(f"{CHART}/{symbol}",
                           params={"range": rng, "interval": interval}, timeout=TIMEOUT)
        if resp.status_code == 404:
            return {"bars": [], "meta": {}}
        resp.raise_for_status()
        return _parse(resp.json())
    except Exception as exc:
        print(f"[yahoo] {symbol} 擷取失敗：{exc}")
        return {"bars": [], "meta": {}}


def fetch_daily_history(code: str, market: str = "twse", rng: str = "2y") -> dict:
    """單一個股的日 K。回傳 {"bars": [...], "meta": {...}, "symbol": "..."}。

    市場別對不上時（申報資料偶爾落後於實際掛牌狀態）自動換另一個後綴重試，
    所以呼叫端就算把興櫃標成上櫃、或市場別留空，一樣查得到。"""
    if DRY_RUN:
        return {"bars": [], "meta": {}, "symbol": ""}

    tried = []
    for suffix in (_SUFFIX.get(market, ".TW"), ".TWO" if market == "twse" else ".TW"):
        symbol = f"{code}{suffix}"
        if symbol in tried:
            continue
        tried.append(symbol)
        res = _chart(symbol, rng)
        if res["bars"]:
            return {**res, "symbol": symbol}
    return {"bars": [], "meta": {}, "symbol": tried[0] if tried else ""}


def fetch_index_history(symbol: str = TAIEX, rng: str = "2y") -> dict:
    """大盤／櫃買指數日 K。symbol 用本模組的 TAIEX / TPEX_INDEX 常數。"""
    if DRY_RUN:
        return {"bars": [], "meta": {}, "symbol": symbol}
    return {**_chart(symbol, rng), "symbol": symbol}


def fetch_many(codes: dict[str, str], rng: str = "2y", pause: float = 0.05,
               on_each=None) -> dict[str, dict]:
    """批次抓 {代號: 市場別}。實測連續 25 檔無限流、平均 0.1 秒／檔，
    全市場上櫃＋興櫃約 1250 檔跑完約 2 分鐘，所以不需要像 TPEx 那樣分批輪流補。
    pause 是保險用的小間隔，真的被限流時把它調大即可。"""
    out: dict[str, dict] = {}
    for code, market in codes.items():
        res = fetch_daily_history(code, market, rng)
        if res["bars"]:
            out[code] = res
        if on_each:
            on_each(code, res)
        if pause:
            time.sleep(pause)
    return out


# ── 漲跌幅的唯一來源 ───────────────────────────────────
# 2026-09-18：同一個「漲跌幅」原本在好幾個地方各算各的——興櫃拿「前日均價」當基準、
# 上櫃收盤後停在 13:29 那一筆、我補的版本又拿 Yahoo 日線前一根 K 推昨收——
# 結果有的對有的錯，使用者要求「直接用 Yahoo 的漲跌，根本不用計算」。
# 實測 spark 端點給的就是 Yahoo 頁面上顯示的那兩個數：現價 regularMarketPrice、
# 昨收 chartPreviousClose（range=1d 時才是真的昨收；range 拉長會變成區間起點前一天）。
# 而且這個昨收跟日線 K 不一定一致：7942 spark 昨收 503（＝TPEx 當日行情表的成交），
# 日線那根卻是 505；7686 spark 867、日線 872。所以**不要從 K 線推昨收**，一律用這支。
# Yahoo 帶現成漲跌% 的 v7/quote 要登入憑證（實測 401），spark 沒有 % 欄位，
# 漲跌幅就是 Yahoo 這兩個數字直接相除，跟 Yahoo 頁面顯示的一致。
# 批次上限 20 檔（實測 30 檔以上回 400）。
SPARK_URL = "https://query1.finance.yahoo.com/v7/finance/spark"
_QUOTE_CACHE: dict[str, dict] = {}


def fetch_quotes(codes: dict[str, str], use_cache: bool = True) -> dict[str, dict]:
    """{代號: 市場別(twse/tpex/esb)} → {代號: {price, prev_close, change, change_pct, time}}。
    市場別標錯（申報資料落後於實際掛牌）時會自動換另一個後綴再查一次。"""
    if DRY_RUN or not codes:
        return {}
    out: dict[str, dict] = {}
    todo = {c: m for c, m in codes.items()
            if not (use_cache and c in _QUOTE_CACHE)}
    for c in codes:
        if use_cache and c in _QUOTE_CACHE:
            out[c] = _QUOTE_CACHE[c]

    def batch(pairs: list[tuple[str, str]]) -> None:
        for i in range(0, len(pairs), 20):
            chunk = pairs[i:i + 20]
            sym2code = {sym: code for code, sym in chunk}
            try:
                r = _sess().get(SPARK_URL, params={"symbols": ",".join(sym2code),
                                                   "range": "1d", "interval": "1d"},
                                timeout=30)
                results = (r.json().get("spark") or {}).get("result") or []
            except Exception as exc:
                print(f"[yahoo] spark 批次失敗：{exc}")
                continue
            for it in results:
                code = sym2code.get(it.get("symbol"))
                try:
                    meta = it["response"][0]["meta"]
                except Exception:
                    continue
                px, pc = meta.get("regularMarketPrice"), meta.get("chartPreviousClose")
                if not code or not px or not pc:
                    continue
                t = meta.get("regularMarketTime")
                d = (datetime.fromtimestamp(t, timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d") if t else ""
                out[code] = _QUOTE_CACHE[code] = {
                    "date": d,
                    "price": float(px), "prev_close": float(pc),
                    "change": round(float(px) - float(pc), 4),
                    "change_pct": round((float(px) - float(pc)) / float(pc) * 100, 2),
                    "volume": meta.get("regularMarketVolume"),      # 股
                    "high": meta.get("regularMarketDayHigh"),
                    "low": meta.get("regularMarketDayLow"),
                    "time": meta.get("regularMarketTime"),
                }
            time.sleep(0.05)

    batch([(c, f"{c}{_SUFFIX.get(m, '.TW')}") for c, m in todo.items()])
    missing = [c for c in todo if c not in out]
    if missing:                                    # 後綴猜錯的再試另一個
        batch([(c, f"{c}{'.TW' if _SUFFIX.get(todo[c], '.TW') == '.TWO' else '.TWO'}")
               for c in missing])
    return out
