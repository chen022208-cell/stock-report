"""國際盤擷取（yfinance，免 API key、免申請）。

原本用 Stooq，2026 起 Stooq 對程式抓取加了 JS 瀏覽器驗證，CSV 端點全數失效，
改用 yfinance（Yahoo Finance 非官方介面）。回傳格式與先前完全相同，上層邏輯不用改。
任一項失敗就略過該項，不影響其他；全部失敗才退回內建預設值。
"""
from __future__ import annotations

from ..config import DRY_RUN
from . import mock

# 對台股開盤最有解釋力的四個指數：費半權重最高，因為電子股佔台股市值大宗
INDICES = [
    ("^DJI", "道瓊"),
    ("^IXIC", "那斯達克"),
    ("^GSPC", "S&P 500"),
    ("^SOX", "費半 SOX"),
]

MACRO = [
    ("DX-Y.NYB", "美元指數", "{:,.2f}"),
    ("CL=F", "西德州原油", "{:,.2f}"),
    ("GC=F", "黃金", "{:,.1f}"),
    ("^TNX", "10年美債殖利率", "{:.2f}%"),
    ("^VIX", "VIX 波動率", "{:.2f}"),
]


def _last_change(symbol: str) -> tuple[float, float] | None:
    """回傳 (最新收盤, 對前一交易日漲跌%)。抓不到回 None。"""
    try:
        import yfinance as yf

        hist = yf.Ticker(symbol).history(period="7d", interval="1d")
        closes = [float(x) for x in hist["Close"].tolist() if x == x]  # 濾掉 NaN
        if len(closes) < 2:
            return None
        close, prev = closes[-1], closes[-2]
        if prev == 0:
            return None
        return close, round((close - prev) / prev * 100, 2)
    except Exception as exc:
        print(f"[intl] {symbol} 擷取失敗：{exc}")
        return None


CLOSURE_TRACK = [
    ("^GSPC", "S&P 500"),
    ("^SOX", "費半 SOX"),
    ("^IXIC", "那斯達克"),
    ("TSM", "台積電 ADR"),
]


def fetch_closure_recap(days: int) -> dict:
    """長假期間國際盤逐日變化彙整，給農曆年開紅盤前的「假期功課」用。

    回傳 {"rows": [{date, changes:{名稱: pct}}...], "cumulative": {名稱: pct}}。
    """
    if DRY_RUN:
        return mock.closure_recap(days)

    try:
        import yfinance as yf
    except Exception:
        return {"rows": [], "cumulative": {}}

    span = max(days + 4, 7)
    series: dict[str, list[tuple[str, float]]] = {}
    for sym, name in CLOSURE_TRACK:
        try:
            h = yf.Ticker(sym).history(period=f"{span}d", interval="1d")
            series[name] = [
                (idx.strftime("%Y-%m-%d"), float(v))
                for idx, v in h["Close"].items() if v == v
            ]
        except Exception as exc:
            print(f"[intl] {sym} 假期彙整擷取失敗：{exc}")

    dates = sorted({d for pts in series.values() for d, _ in pts})[-days:]
    rows = []
    for d in dates:
        changes = {}
        for name, pts in series.items():
            lut = dict(pts)
            keys = [k for k, _ in pts]
            if d in lut and keys.index(d) > 0:
                prev = pts[keys.index(d) - 1][1]
                if prev:
                    changes[name] = round((lut[d] - prev) / prev * 100, 2)
        rows.append({"date": d, "changes": changes})

    cumulative = {}
    for name, pts in series.items():
        window = [v for dd, v in pts if dd in dates]
        if len(window) >= 2 and window[0]:
            cumulative[name] = round((window[-1] - window[0]) / window[0] * 100, 2)
    return {"rows": rows, "cumulative": cumulative}


def fetch_international() -> dict:
    """回傳指數與總經指標。"""
    if DRY_RUN:
        return mock.international_markets()

    indices = []
    for symbol, name in INDICES:
        result = _last_change(symbol)
        if result:
            indices.append({"name": name, "close": result[0], "change_pct": result[1]})

    macro = []
    for symbol, name, fmt in MACRO:
        result = _last_change(symbol)
        if result:
            macro.append({"name": name, "value": f"{fmt.format(result[0])}（{result[1]:+.2f}%）"})

    # 台積電 ADR 對台股開盤最直接，單獨處理
    adr = _last_change("TSM")
    if adr:
        macro.append({"name": "台積電 ADR", "value": f"{adr[0]:,.2f}（{adr[1]:+.2f}%）"})

    if not indices:
        print("[intl] 所有國際指數擷取失敗，改用快取／預設值")
        return mock.international_markets()

    return {"indices": indices, "macro": macro}
