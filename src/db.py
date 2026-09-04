"""SQLite 資料層：題材知識庫、判斷快照、每日市場快照。

用 SQLite 的理由：單一檔案、免架服務、可直接 commit 進 git 當版本備份。
資料量大到撐不住時再換 Postgres，schema 不用重寫。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any, Iterator

from .config import DB_PATH, DATA_DIR

SCHEMA = """
-- 題材知識庫主表：一個題材一列，持續更新而非每天新增
CREATE TABLE IF NOT EXISTS themes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL UNIQUE,
    summary           TEXT,              -- 一句話說明
    first_seen        TEXT NOT NULL,     -- 首次建檔日
    last_signal_date  TEXT NOT NULL,     -- 最後一次有新訊號
    update_count      INTEGER DEFAULT 1,
    confidence        TEXT,              -- high / mid / low
    verdict           TEXT,              -- real / watch / unknown
    status            TEXT DEFAULT 'active',   -- active / dormant / archived
    scope             TEXT DEFAULT 'tw',       -- tw / intl
    related_stocks    TEXT,              -- JSON list
    deep_dive_slug    TEXT               -- 已產出深度報告時的檔名
);

-- 題材每日更新軌跡：深度報告的時間軸就從這裡長出來
CREATE TABLE IF NOT EXISTS theme_updates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    theme_id      INTEGER NOT NULL REFERENCES themes(id),
    date          TEXT NOT NULL,
    confidence    TEXT,
    verdict       TEXT,
    note          TEXT,
    stock_count   INTEGER,
    inst_net      REAL,                  -- 當日相關個股法人買賣超合計（億）
    UNIQUE(theme_id, date)
);

-- 判斷快照：系統每次說「值得留意」就存一筆，之後回頭驗證準不準
CREATE TABLE IF NOT EXISTS judgment_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    date           TEXT NOT NULL,
    stock_code     TEXT NOT NULL,
    stock_name     TEXT,
    theme_name     TEXT,
    confidence     TEXT,
    judgment_type  TEXT,                 -- theme_pick / dark_horse / technical
    price_at_call  REAL,
    index_at_call  REAL,                 -- 當日加權指數，用來算超額報酬
    reviewed_14d   INTEGER DEFAULT 0,
    reviewed_30d   INTEGER DEFAULT 0,
    UNIQUE(date, stock_code, judgment_type)
);

-- 驗證結果
CREATE TABLE IF NOT EXISTS review_results (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id   INTEGER NOT NULL REFERENCES judgment_snapshots(id),
    horizon_days  INTEGER NOT NULL,
    review_date   TEXT NOT NULL,
    stock_return  REAL,
    index_return  REAL,
    excess_return REAL,                  -- 個股 - 大盤，這才是有意義的指標
    UNIQUE(snapshot_id, horizon_days)
);

-- 每日市場快照：週報/月報直接從這裡彙整，不用重抓
CREATE TABLE IF NOT EXISTS market_snapshots (
    date          TEXT PRIMARY KEY,
    taiex_close   REAL,
    taiex_change  REAL,
    turnover      REAL,
    foreign_net   REAL,
    trust_net     REAL,
    dealer_net    REAL,
    advancers     INTEGER,
    decliners     INTEGER,
    payload       TEXT                   -- 完整 JSON，保留彈性
);

CREATE INDEX IF NOT EXISTS idx_theme_updates_date ON theme_updates(date);
CREATE INDEX IF NOT EXISTS idx_snapshots_date ON judgment_snapshots(date);
CREATE INDEX IF NOT EXISTS idx_themes_status ON themes(status);

-- 供應鏈結構：題材成熟到值得寫深度報告時，順便請 LLM 產出一次，之後有新訊號再修正
CREATE TABLE IF NOT EXISTS supply_chains (
    theme_id      INTEGER PRIMARY KEY REFERENCES themes(id),
    structure     TEXT NOT NULL,        -- JSON：upstream/midstream/downstream + 公司角色
    updated_date  TEXT NOT NULL
);
"""


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)


# ── 題材知識庫 ─────────────────────────────────────────
def upsert_theme(
    name: str,
    summary: str,
    confidence: str,
    verdict: str,
    related_stocks: list[dict],
    today: str,
    scope: str = "tw",
    note: str = "",
    inst_net: float = 0.0,
) -> int:
    """有就更新、沒有才新建 —— 這是「不重複研究」的核心。"""
    with get_conn() as conn:
        row = conn.execute("SELECT id, update_count FROM themes WHERE name = ?", (name,)).fetchone()
        stocks_json = json.dumps(related_stocks, ensure_ascii=False)

        if row:
            theme_id = row["id"]
            conn.execute(
                """UPDATE themes SET summary=?, last_signal_date=?, update_count=?,
                   confidence=?, verdict=?, related_stocks=?, status='active'
                   WHERE id=?""",
                (summary, today, row["update_count"] + 1, confidence, verdict,
                 stocks_json, theme_id),
            )
        else:
            cur = conn.execute(
                """INSERT INTO themes (name, summary, first_seen, last_signal_date,
                   confidence, verdict, scope, related_stocks)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (name, summary, today, today, confidence, verdict, scope, stocks_json),
            )
            theme_id = cur.lastrowid

        conn.execute(
            """INSERT OR REPLACE INTO theme_updates
               (theme_id, date, confidence, verdict, note, stock_count, inst_net)
               VALUES (?,?,?,?,?,?,?)""",
            (theme_id, today, confidence, verdict, note, len(related_stocks), inst_net),
        )
    return theme_id


def get_theme(name: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM themes WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None


def get_theme_timeline(theme_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM theme_updates WHERE theme_id=? ORDER BY date", (theme_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def list_themes(status: str = "active") -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM themes WHERE status=? ORDER BY last_signal_date DESC, update_count DESC",
            (status,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_all_themes() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM themes ORDER BY last_signal_date DESC, update_count DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def theme_confidence_series(theme_id: int) -> list[str]:
    """題材歷次更新的信心度序列（由舊到新），給折線圖用。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT confidence FROM theme_updates WHERE theme_id=? ORDER BY date",
            (theme_id,),
        ).fetchall()
        return [r["confidence"] or "mid" for r in rows]


def apply_theme_lifecycle(today: str, dormant_after_days: int, archive_after_declines: int) -> dict:
    """退場機制：讓知識庫不會被半年前退燒的題材塞爆。"""
    rank = {"low": 0, "mid": 1, "high": 2}
    cutoff = (date.fromisoformat(today) - timedelta(days=dormant_after_days)).isoformat()
    changed = {"dormant": [], "archived": []}

    with get_conn() as conn:
        for row in conn.execute("SELECT * FROM themes WHERE status='active'").fetchall():
            # 條件一：太久沒有新訊號
            if row["last_signal_date"] < cutoff:
                conn.execute("UPDATE themes SET status='dormant' WHERE id=?", (row["id"],))
                changed["dormant"].append(row["name"])
                continue

            # 條件二：信心度連續下滑且已到低檔
            hist = conn.execute(
                "SELECT confidence FROM theme_updates WHERE theme_id=? ORDER BY date DESC LIMIT ?",
                (row["id"], archive_after_declines + 1),
            ).fetchall()
            vals = [rank.get(h["confidence"], 1) for h in hist]
            if len(vals) > archive_after_declines and vals[0] == 0:
                # vals 是由新到舊，持續下滑代表越新的值越小
                if all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1)) and vals[0] < vals[-1]:
                    conn.execute("UPDATE themes SET status='archived' WHERE id=?", (row["id"],))
                    changed["archived"].append(row["name"])
    return changed


def themes_ready_for_deep_dive(min_days: int) -> list[dict]:
    """追蹤夠久且信心度夠高 → 值得動用重量級的產業分析。"""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM themes
               WHERE status='active' AND update_count >= ?
                 AND confidence IN ('high','mid')
                 AND scope = 'tw'
               ORDER BY update_count DESC""",
            (min_days,),
        ).fetchall()
        return [dict(r) for r in rows]


def set_deep_dive_slug(theme_id: int, slug: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE themes SET deep_dive_slug=? WHERE id=?", (slug, theme_id))


def save_supply_chain(theme_id: int, structure: dict, today: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO supply_chains (theme_id, structure, updated_date)
               VALUES (?,?,?)
               ON CONFLICT(theme_id) DO UPDATE SET structure=excluded.structure,
                                                    updated_date=excluded.updated_date""",
            (theme_id, json.dumps(structure, ensure_ascii=False), today),
        )


def get_supply_chain(theme_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT structure FROM supply_chains WHERE theme_id=?", (theme_id,)
        ).fetchone()
        return json.loads(row["structure"]) if row else None


# ── 判斷快照 / 事後驗證 ────────────────────────────────
def save_judgment(
    today: str, stock_code: str, stock_name: str, theme_name: str,
    confidence: str, judgment_type: str, price: float, index_level: float,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO judgment_snapshots
               (date, stock_code, stock_name, theme_name, confidence,
                judgment_type, price_at_call, index_at_call)
               VALUES (?,?,?,?,?,?,?,?)""",
            (today, stock_code, stock_name, theme_name, confidence,
             judgment_type, price, index_level),
        )


def pending_reviews(today: str, horizon_days: int) -> list[dict]:
    target = (date.fromisoformat(today) - timedelta(days=horizon_days)).isoformat()
    col = f"reviewed_{horizon_days}d"
    with get_conn() as conn:
        try:
            rows = conn.execute(
                f"SELECT * FROM judgment_snapshots WHERE date <= ? AND {col}=0", (target,)
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in rows]


def save_review(snapshot_id: int, horizon_days: int, review_date: str,
                stock_return: float, index_return: float) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO review_results
               (snapshot_id, horizon_days, review_date, stock_return,
                index_return, excess_return)
               VALUES (?,?,?,?,?,?)""",
            (snapshot_id, horizon_days, review_date, stock_return,
             index_return, stock_return - index_return),
        )
        col = f"reviewed_{horizon_days}d"
        conn.execute(f"UPDATE judgment_snapshots SET {col}=1 WHERE id=?", (snapshot_id,))


def review_scorecard(horizon_days: int) -> list[dict]:
    """依信心度分組統計超額報酬 —— 用來檢驗判斷邏輯到底準不準。"""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT s.confidence,
                      COUNT(*) AS n,
                      ROUND(AVG(r.excess_return), 2) AS avg_excess,
                      ROUND(AVG(CASE WHEN r.excess_return > 0 THEN 1.0 ELSE 0.0 END) * 100, 1) AS win_rate
               FROM review_results r
               JOIN judgment_snapshots s ON s.id = r.snapshot_id
               WHERE r.horizon_days = ?
               GROUP BY s.confidence
               ORDER BY CASE s.confidence WHEN 'high' THEN 0 WHEN 'mid' THEN 1 ELSE 2 END""",
            (horizon_days,),
        ).fetchall()
        return [dict(r) for r in rows]


# ── 市場快照 ───────────────────────────────────────────
def save_market_snapshot(today: str, data: dict[str, Any]) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO market_snapshots
               (date, taiex_close, taiex_change, turnover, foreign_net,
                trust_net, dealer_net, advancers, decliners, payload)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (today, data.get("taiex_close"), data.get("taiex_change"),
             data.get("turnover"), data.get("foreign_net"), data.get("trust_net"),
             data.get("dealer_net"), data.get("advancers"), data.get("decliners"),
             json.dumps(data, ensure_ascii=False)),
        )


def snapshots_between(start: str, end: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM market_snapshots WHERE date BETWEEN ? AND ? ORDER BY date",
            (start, end),
        ).fetchall()
        return [dict(r) for r in rows]
