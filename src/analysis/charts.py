"""個股 K 線＋均線＋成交量＋RSI 圖表產出。

純用 matplotlib/mplfinance 就地算圖，不吃 LLM 額度、不用另外呼叫任何 API——
history 資料本身就是 fetch_stock_history() 抓回來的日 K，這裡只是畫圖。
"""
from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd

from ..config import load_config

CHARTS_DIR = Path(__file__).resolve().parent.parent.parent / "docs" / "charts"
CJK_FONT = "Noto Sans CJK TC"


def _to_df(history: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(history)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    return df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                               "close": "Close", "volume": "Volume"})


def _rsi_series(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, pd.NA)
    return (100 - 100 / (1 + rs)).fillna(50)


def render_stock_chart(code: str, name: str, history: list[dict]) -> Path | None:
    """畫 K 線＋均線＋成交量＋RSI 四合一圖，存成 docs/charts/<code>.png。

    history 少於 30 筆（新股、資料不足）就回傳 None，呼叫端自行決定要不要顯示提示。
    """
    if not history or len(history) < 30:
        return None

    cfg = load_config()["technical"]
    df = _to_df(history)
    rsi = _rsi_series(df["Close"])

    mc = mpf.make_marketcolors(up="#e15c4f", down="#4fb07a", edge="inherit",
                                wick="inherit", volume="inherit")
    style = mpf.make_mpf_style(base_mpf_style="nightclouds", marketcolors=mc,
                                facecolor="#171615", figcolor="#171615",
                                gridcolor="#38332e", gridstyle=":",
                                rc={"font.family": CJK_FONT, "text.color": "#f2eee6",
                                    "axes.labelcolor": "#f2eee6", "axes.edgecolor": "#786f63",
                                    "xtick.color": "#aba398", "ytick.color": "#aba398"})

    ma_colors = {5: "#e3ac4e", 20: "#6c93f5", 60: "#b48ce8"}
    ma_plots = [mpf.make_addplot(df["Close"].rolling(p).mean(), width=0.9,
                                  color=ma_colors.get(p, "#e3ac4e"), panel=0)
                for p in cfg["ma_periods"] if len(df) >= p]
    rsi_plot = mpf.make_addplot(rsi, panel=2, color="#e3ac4e", ylabel="RSI", width=1.1)

    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CHARTS_DIR / f"{code}.png"
    fig, axes = mpf.plot(df, type="candle", style=style, volume=True,
                         addplot=ma_plots + [rsi_plot],
                         panel_ratios=(3, 1, 1), figsize=(9, 7),
                         title=f"\n{code} {name}", tight_layout=True,
                         returnfig=True)
    rsi_ax = axes[4] if len(axes) > 4 else axes[-1]
    for level in (cfg["rsi_overbought"], cfg["rsi_oversold"]):
        rsi_ax.axhline(level, color="#786f63", linestyle="--", linewidth=0.7)
    fig.savefig(out_path, dpi=130, facecolor="#171615")
    plt.close(fig)
    return out_path
