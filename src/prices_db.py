"""個股每日收盤 / 成交量快取（`data/prices.db`，獨立於 `market.db`，不進 git）。

存在的理由：量能倍數（當日量 / 20 日均量）過去每次都要對候選股逐檔打
TWSE 歷史 K 線 API，掃描階段可能有 90 檔候選，一天下來變成幾百次帶延遲
重試的請求，高波動日曾把單次盤後執行拖到 8 分鐘以上。

盤後行情本來就會一次性抓下全市場報價（STOCK_DAY_ALL + TPEx），這裡把那份
報價每天落地一筆，量能倍數改查本地資料庫；只有快取天數不足時才退回即時抓
歷史（同時把抓到的結果寫回快取），讓每天需要即時抓的檔數隨快取累積而遞減。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .config import DATA_DIR

PRICES_DB_PATH = DATA_DIR / "prices.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_daily (
    code    TEXT NOT NULL,
    date    TEXT NOT NULL,
    close   REAL,
    volume  REAL,
    PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_stock_daily_code_date ON stock_daily(code, date);
"""


@contextmanager
def _get_conn() -> Iterator[sqlite3.Connection]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(PRICES_DB_PATH)
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def save_quotes(quotes: list[dict], today: str) -> None:
    """把當日全市場報價（已經抓過、零額外成本）落地一筆，餵給未來的量能倍數查詢。"""
    if not quotes:
        return
    with _get_conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO stock_daily (code, date, close, volume) VALUES (?,?,?,?)",
            [(q["code"], today, q.get("close"), q.get("volume")) for q in quotes if q.get("code")],
        )


def save_history(code: str, hist: list[dict]) -> None:
    """即時抓歷史時順手把整段寫回快取，該檔之後就不用再即時抓。"""
    if not hist:
        return
    with _get_conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO stock_daily (code, date, close, volume) VALUES (?,?,?,?)",
            [(code, h["date"], h.get("close"), h.get("volume")) for h in hist if h.get("date")],
        )


def get_history(code: str, as_of: str, limit: int = 25) -> list[dict]:
    """回傳截至 as_of（含）為止、最近 limit 筆的 {date, close, volume}，依日期由舊到新。

    刻意保持跟 twse.fetch_stock_history() 一樣的輸出形狀（升冪排序），這樣呼叫端
    （screener.attach_volume_ratio 的 hist[-21:-1] 切法）完全不用改。
    """
    with _get_conn() as conn:
        rows = conn.execute(
            """SELECT date, close, volume FROM stock_daily
               WHERE code = ? AND date <= ?
               ORDER BY date DESC LIMIT ?""",
            (code, as_of, limit),
        ).fetchall()
    return [{"date": r[0], "close": r[1], "volume": r[2]} for r in reversed(rows)]
