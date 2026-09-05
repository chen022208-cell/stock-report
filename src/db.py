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
    deep_dive_slug    TEXT,              -- 已產出深度報告時的檔名
    category          TEXT               -- 題材目錄分類（只有 status='catalog' 的種子題材會填）
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

-- 小型鍵值狀態表：目前只用來記「Google 表單試算表處理到哪一筆時間戳記」，
-- 之後有其他需要跨執行記憶一個小狀態的地方也可以共用，不用每個都開一張表
CREATE TABLE IF NOT EXISTS app_state (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

-- 使用者提交研究（文章／文字）的知識庫：任何人貼進來的內容都先存這裡、
-- 標記驗證狀態，只有 verified 才會真的回寫進題材／個股資料，
-- 不驗證就直接套用會汙染整份報告的真實性
CREATE TABLE IF NOT EXISTS research_notes (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    submitted_at       TEXT NOT NULL,
    source             TEXT,               -- 例如 "GitHub Issue #12"
    title              TEXT,
    raw_excerpt        TEXT,               -- 原文前幾百字，避免整份長文占用資料庫
    summary            TEXT,               -- LLM 整理後的重點摘要
    verified           TEXT DEFAULT 'unverified',  -- verified / unverified / conflicting
    verification_note  TEXT,               -- 為什麼判定成這個狀態（引用哪裡衝突/佐證）
    affected_themes    TEXT,               -- JSON：[{"name":, "impact":, "applied": bool}]
    affected_stocks    TEXT,               -- JSON：[{"code":, "name":, "impact":, "applied": bool}]
    status             TEXT DEFAULT 'pending'  -- pending / applied / rejected
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
        # 既有 data/market.db（部署後累積的正式資料）不會因為 CREATE TABLE IF NOT EXISTS
        # 而補到新欄位，額外做一次安全的加欄位遷移；已存在就忽略錯誤。
        try:
            conn.execute("ALTER TABLE themes ADD COLUMN category TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE themes ADD COLUMN last_analyzed TEXT")
        except sqlite3.OperationalError:
            pass


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


def import_theme_catalog(entries: list[dict], today: str) -> dict:
    """匯入題材目錄種子清單（THEMES.md）。status='catalog'，跟系統即時偵測到的
    'active'/'dormant' 題材分開，不會混進每日報告或首頁的「追蹤中題材」。

    entries = [{"name":, "category":, "thesis":}, ...]
    同名已存在的題材（不管是 catalog 還是系統偵測到的真題材）一律跳過，不覆蓋——
    真題材的追蹤資料比種子清單珍貴，種子清單只是補「還沒被偵測到」的名字進來。
    """
    added, skipped = 0, 0
    with get_conn() as conn:
        for e in entries:
            row = conn.execute("SELECT id FROM themes WHERE name=?", (e["name"],)).fetchone()
            if row:
                skipped += 1
                continue
            conn.execute(
                """INSERT INTO themes (name, summary, first_seen, last_signal_date,
                   confidence, verdict, status, scope, related_stocks, category)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (e["name"], e.get("thesis", ""), today, today, None, None,
                 "catalog", "tw", "[]", e.get("category", "")),
            )
            added += 1
    return {"added": added, "skipped": skipped}


def list_catalog_themes() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM themes WHERE status='catalog' ORDER BY category, name"
        ).fetchall()
        return [dict(r) for r in rows]


def catalog_theme_names() -> list[str]:
    """給 cluster_themes() 當參考名單，讓即時聚類的 LLM 優先套用目錄裡已有的名稱，
    而不是每次自己發明一個相似但不完全一樣的題材名——這樣目錄題材才有機會被
    真的偵測到訊號時直接轉入「追蹤中」，而不是永遠卡在目錄裡。"""
    with get_conn() as conn:
        rows = conn.execute("SELECT name FROM themes WHERE status='catalog'").fetchall()
        return [r["name"] for r in rows]


def catalog_themes_for_analysis(limit: int, today: str, refresh_days: int) -> list[dict]:
    """挑一批「還沒研究過」或「研究太久沒更新」的目錄題材，補齊 117 個題材的分析覆蓋率。
    優先處理從沒分析過的（last_analyzed IS NULL），全部補齊一輪之後才開始按時間
    輪流刷新，不會每天重打全部 117 個的 LLM 成本。
    """
    cutoff = (date.fromisoformat(today) - timedelta(days=refresh_days)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM themes WHERE status='catalog'
               AND (last_analyzed IS NULL OR last_analyzed < ?)
               ORDER BY (last_analyzed IS NOT NULL), last_analyzed ASC
               LIMIT ?""",
            (cutoff, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def update_catalog_analysis(
    theme_id: int, summary: str, confidence: str, verdict: str,
    related_stocks: list[dict], today: str,
) -> None:
    """把目錄題材的研究結果寫回去——刻意不改 status（維持 'catalog'），
    跟系統即時偵測到、真的轉入「追蹤中」的題材保持視覺區隔，但補上摘要／
    信心度／代表股，不再是只有名字跟論點的空殼。"""
    with get_conn() as conn:
        conn.execute(
            """UPDATE themes SET summary=?, confidence=?, verdict=?,
               related_stocks=?, last_analyzed=? WHERE id=?""",
            (summary, confidence, verdict,
             json.dumps(related_stocks, ensure_ascii=False), today, theme_id),
        )


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


def catalog_themes_needing_deep_dive(limit: int) -> list[dict]:
    """挑一批「已經有代表股研究、但還沒產出深度報告」的目錄題材——
    深度報告要靠 related_stocks 才能寫得具體，所以只處理 last_analyzed 已填的。
    """
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM themes WHERE status='catalog'
               AND last_analyzed IS NOT NULL AND deep_dive_slug IS NULL
               ORDER BY id LIMIT ?""",
            (limit,),
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


# ── 小型狀態值 ───────────────────────────────────────
def get_state(key: str, default: str | None = None) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_state(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


# ── 使用者提交研究（文章／文字）知識庫 ──────────────────
def create_research_note(
    submitted_at: str, source: str, title: str, raw_excerpt: str, summary: str,
    verified: str, verification_note: str,
    affected_themes: list[dict], affected_stocks: list[dict],
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO research_notes
               (submitted_at, source, title, raw_excerpt, summary, verified,
                verification_note, affected_themes, affected_stocks, status)
               VALUES (?,?,?,?,?,?,?,?,?, 'pending')""",
            (submitted_at, source, title, raw_excerpt, summary, verified, verification_note,
             json.dumps(affected_themes, ensure_ascii=False),
             json.dumps(affected_stocks, ensure_ascii=False)),
        )
        return cur.lastrowid


def mark_research_note_status(note_id: int, status: str,
                              affected_themes: list[dict] | None = None,
                              affected_stocks: list[dict] | None = None) -> None:
    """套用完（或決定不套用）之後回填最終狀態，affected_* 裡的每一項會標記
    applied=True/False，讓研究筆記頁能誠實顯示哪些真的寫回了題材/個股資料，
    哪些因為驗證不過只留在筆記裡。"""
    with get_conn() as conn:
        if affected_themes is not None:
            conn.execute("UPDATE research_notes SET affected_themes=? WHERE id=?",
                        (json.dumps(affected_themes, ensure_ascii=False), note_id))
        if affected_stocks is not None:
            conn.execute("UPDATE research_notes SET affected_stocks=? WHERE id=?",
                        (json.dumps(affected_stocks, ensure_ascii=False), note_id))
        conn.execute("UPDATE research_notes SET status=? WHERE id=?", (status, note_id))


def append_research_to_theme(theme_name: str, today: str, note: str) -> bool:
    """把「已驗證」的使用者研究引用進題材的時間軸（theme_updates），不直接改
    themes 主表的 summary/confidence——用累加式的更新歷史，之後寫深度報告時
    (get_theme_timeline) 自然會讀到這筆，比直接覆寫欄位安全，也不會讓一次
    使用者提交的內容就永久蓋掉系統既有的判斷。找不到同名題材就回傳 False，
    呼叫端據此判斷這筆研究是不是真的有對應到現有題材。"""
    with get_conn() as conn:
        row = conn.execute("SELECT id, update_count FROM themes WHERE name=?", (theme_name,)).fetchone()
        if not row:
            return False
        conn.execute(
            """INSERT INTO theme_updates (theme_id, date, note, stock_count)
               VALUES (?,?,?,0)
               ON CONFLICT(theme_id, date) DO UPDATE SET note = note || ' ／ ' || excluded.note""",
            (row["id"], today, f"📩 使用者研究：{note}"),
        )
        conn.execute("UPDATE themes SET last_signal_date=? WHERE id=? AND last_signal_date<?",
                    (today, row["id"], today))
        return True


def list_research_notes(limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM research_notes ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["affected_themes"] = json.loads(d.get("affected_themes") or "[]")
            d["affected_stocks"] = json.loads(d.get("affected_stocks") or "[]")
            out.append(d)
        return out


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


def latest_snapshot() -> dict | None:
    """最近一筆市場快照，首頁儀表板用。沒資料回 None。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM market_snapshots ORDER BY date DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
