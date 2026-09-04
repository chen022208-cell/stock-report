"""技術分析：均線、KD、RSI、MACD、量價關係。

用 pandas 手算而不裝 pandas-ta，理由是少一個相依套件、CI 跑更快，
而且這幾個指標的公式都很短，自己算反而看得懂在做什麼。
"""
from __future__ import annotations

import pandas as pd


def _to_df(history: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(history)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def compute_indicators(history: list[dict], cfg: dict) -> dict:
    """算出所有指標的最新值。資料不足時回傳 {}，由呼叫端決定怎麼處理。"""
    t = cfg["technical"]
    df = _to_df(history)
    if df.empty or len(df) < 60:
        return {}

    close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]

    # 均線
    mas = {p: close.rolling(p).mean() for p in t["ma_periods"]}

    # RSI(14)
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, pd.NA)
    rsi = (100 - 100 / (1 + rs)).fillna(50)

    # KD(9)
    low_min, high_max = low.rolling(9).min(), high.rolling(9).max()
    rsv = ((close - low_min) / (high_max - low_min).replace(0, pd.NA) * 100).fillna(50)
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()

    # MACD(12,26,9)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()

    vol_ma20 = vol.rolling(20).mean()

    return {
        "close": round(float(close.iloc[-1]), 2),
        "ma": {p: round(float(s.iloc[-1]), 2) for p, s in mas.items() if pd.notna(s.iloc[-1])},
        "rsi": round(float(rsi.iloc[-1]), 1),
        "k": round(float(k.iloc[-1]), 1),
        "d": round(float(d.iloc[-1]), 1),
        "macd_dif": round(float(dif.iloc[-1]), 3),
        "macd_dea": round(float(dea.iloc[-1]), 3),
        "macd_cross_up": bool(dif.iloc[-1] > dea.iloc[-1] and dif.iloc[-2] <= dea.iloc[-2]),
        "volume_ratio": round(float(vol.iloc[-1] / vol_ma20.iloc[-1]), 2)
                        if pd.notna(vol_ma20.iloc[-1]) and vol_ma20.iloc[-1] > 0 else None,
        "price_change_5d": round(float((close.iloc[-1] / close.iloc[-6] - 1) * 100), 2)
                           if len(close) > 6 else 0.0,
        "recent_high": round(float(high.tail(60).max()), 2),
        "recent_low": round(float(low.tail(60).min()), 2),
    }


def grade(ind: dict, cfg: dict) -> dict:
    """把指標翻譯成一句人話的評級。

    分級邏輯刻意保守：任何過熱訊號都會壓過多頭訊號，
    因為漏掉一次上漲的代價，遠小於在高點追進去的代價。
    """
    if not ind:
        return {"label": "資料不足", "tone": "neutral", "notes": ["歷史資料不足以計算指標"]}

    t = cfg["technical"]
    notes: list[str] = []
    bull = bear = 0

    # 均線排列
    ma = ind.get("ma", {})
    if all(p in ma for p in (5, 20, 60)):
        if ma[5] > ma[20] > ma[60]:
            notes.append("均線多頭排列")
            bull += 2
        elif ma[5] < ma[20] < ma[60]:
            notes.append("均線空頭排列")
            bear += 2
        else:
            notes.append("均線糾結，方向未明")

        if ind["close"] > ma[60]:
            notes.append("站上季線")
            bull += 1
        else:
            notes.append("季線之下")
            bear += 1

    # 過熱
    if ind["rsi"] >= t["rsi_overbought"]:
        notes.append(f"RSI {ind['rsi']} 進入超買區")
        bear += 2
    elif ind["rsi"] <= t["rsi_oversold"]:
        notes.append(f"RSI {ind['rsi']} 進入超賣區")

    if ind["k"] >= 80 and ind["d"] >= 80:
        notes.append("KD 高檔鈍化風險")
        bear += 1

    # 量價關係
    vr = ind.get("volume_ratio")
    if vr:
        if vr >= 1.5 and ind["price_change_5d"] > 0:
            notes.append(f"量增價漲（量能 {vr} 倍）")
            bull += 1
        elif vr < 0.8 and ind["price_change_5d"] > 3:
            notes.append("量縮價漲，價量背離")
            bear += 2

    if ind.get("macd_cross_up"):
        notes.append("MACD 金叉")
        bull += 1

    # 過熱訊號優先於多頭訊號
    if bear >= 3:
        label, tone = "過熱警訊", "warn"
    elif bull >= 3 and bear <= 1:
        label, tone = "多頭健康", "bull"
    elif bear > bull:
        label, tone = "偏弱", "bear"
    else:
        label, tone = "區間整理", "neutral"

    return {
        "label": label,
        "tone": tone,
        "notes": notes,
        "support": ind.get("recent_low"),
        "resistance": ind.get("recent_high"),
    }


def analyze_stock(code: str, history: list[dict], cfg: dict) -> dict:
    ind = compute_indicators(history, cfg)
    return {"code": code, "indicators": ind, "grade": grade(ind, cfg)}
