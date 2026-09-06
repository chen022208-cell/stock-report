"""盤中強勢股篩選（方案①：GitHub Actions 迴圈式執行，證交所免費資料）。

跟每日盤後那套的差別：
- 盤後：openapi STOCK_DAY_ALL 收盤資料，一天一次，寫進主網站
- 盤中：mis.twse.com.tw 即時報價，盤中每 ~60 秒一輪，寫到 `intraday-data` 分支，
        前端每 45 秒重讀，不觸發 GitHub Pages 重建、不跟每日盤後排程撞

多層漏斗（參考 ZK 那份架構書，Layer 4 五檔委買賣資料源不支援，略過）：
  Layer 0 資格：排除警示/處置股、低價股
  Layer 1 相對強度：對大盤（加權指數）、對同業（同產業個股漲幅中位數）
  Layer 2 量能：昨量比（今累計量 ÷ 已過盤比例 ÷ 昨日全日量），時間校正
  Layer 3 動能/型態：站上開盤 + 站上昨收、20/60/120/252 日新高

綜合評分 0~100 → A(≥tier_a) / B(≥tier_b) / C。A 級寫進 intraday_signals.json，
之後由 webhook 觸發的雲端 Routine 做深度快報（見 run_intraday_deep_report）。
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from statistics import median

from . import db
from .config import DATA_DIR as _MARKET_DATA_DIR, DOCS_DIR, load_config, now_tpe
from .fetchers import tpex, twse, twse_mis

DATA_DIR = DOCS_DIR / "data"
HIST_DB_PATH = _MARKET_DATA_DIR / "intraday_hist.db"  # 每日收盤滾動歷史（intraday-data 分支）
REF_PATH = DATA_DIR / "intraday_ref.json"          # 每日盤前算好的參考值（新高、昨量）
OUT_PATH = DATA_DIR / "intraday.json"              # 盤中頁讀這個
SIGNALS_PATH = DATA_DIR / "intraday_signals.json"  # 今日 A 級累積清單（給深度快報）
NEWSIG_PATH = DATA_DIR / "intraday_new_signal.json"  # 只在有新 A 級時更新（webhook 過濾用）

SESSION_START_MIN = 9 * 60            # 09:00
SESSION_END_MIN = 13 * 60 + 30       # 13:30
SESSION_LEN = SESSION_END_MIN - SESSION_START_MIN  # 270 分鐘


_HIST_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily (
    code TEXT NOT NULL, date TEXT NOT NULL,
    close REAL, volume REAL,
    PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_daily_code_date ON daily(code, date);
"""


def _hist_conn() -> sqlite3.Connection:
    HIST_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(HIST_DB_PATH)
    conn.executescript(_HIST_SCHEMA)
    return conn


def sync_ref(keep_days: int = 260) -> Path:
    """每日盤前跑一次：抓昨日全市場收盤 → 更新 intraday_hist.db → 算好 intraday_ref.json。

    ref 內容：{code: {name, industry, prev_vol, high_20, high_60, high_120, high_252}}
    高點用「收盤價」算（跟盤中比的是即時成交價，收盤高點當作保守的突破門檻）。
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # 統一成交量單位為「張」：TWSE STOCK_DAY_ALL 的 TradeVolume 是「股」要 ÷1000；
    # TPEx TradingShares 已是「張」；盤中 MIS 的 v 也是「張」——三邊對齊才算得對量比。
    quotes: list[dict] = []
    for q in (twse.fetch_daily_quotes() or []):
        q["volume"] = (q.get("volume") or 0) / 1000
        quotes.append(q)
    quotes += tpex.fetch_daily_quotes() or []
    today = now_tpe().strftime("%Y-%m-%d")
    conn = _hist_conn()
    conn.executemany(
        "INSERT OR REPLACE INTO daily (code, date, close, volume) VALUES (?,?,?,?)",
        [(q["code"], today, q.get("close"), q.get("volume"))
         for q in quotes if q.get("code") and q.get("close")],
    )
    # 修剪：每檔只留最近 keep_days 筆
    codes = [r[0] for r in conn.execute("SELECT DISTINCT code FROM daily")]
    for code in codes:
        conn.execute(
            """DELETE FROM daily WHERE code=? AND date NOT IN (
                 SELECT date FROM daily WHERE code=? ORDER BY date DESC LIMIT ?)""",
            (code, code, keep_days))
    conn.commit()

    profiles = db.all_company_profiles()
    names = {}
    try:
        idx = json.loads((DATA_DIR / "stock_index.json").read_text(encoding="utf-8"))
        names = {s["code"]: s.get("name", "") for s in idx.get("stocks", []) if s.get("code")}
    except Exception:
        pass

    ref: dict[str, dict] = {}
    for code in codes:
        rows = conn.execute(
            "SELECT close, volume FROM daily WHERE code=? ORDER BY date DESC LIMIT 260",
            (code,)).fetchall()
        if not rows:
            continue
        closes = [r[0] for r in rows if r[0]]
        prof = profiles.get(code, {})
        ref[code] = {
            "name": prof.get("full_name") or names.get(code, ""),
            "industry": prof.get("industry", ""),
            "prev_vol": rows[0][1] or 0,
            "high_20": max(closes[:20]) if closes else None,
            "high_60": max(closes[:60]) if closes else None,
            "high_120": max(closes[:120]) if closes else None,
            "high_252": max(closes[:252]) if closes else None,
        }
    conn.close()
    REF_PATH.write_text(json.dumps(
        {"date": today, "generated": now_tpe().strftime("%Y-%m-%d %H:%M"), "stocks": ref},
        ensure_ascii=False), encoding="utf-8")
    print(f"[intraday] intraday_ref.json 更新：{len(ref)} 檔")
    return REF_PATH


# ── 參考值 / 宇集 ─────────────────────────────────────
def _universe() -> list[tuple[str, str]]:
    """全市場真公司（上市＋上櫃），來自 company_profile。排除 ETF／權證。"""
    out = []
    for code, prof in db.all_company_profiles().items():
        mkt = prof.get("market")
        if mkt in ("twse", "tpex") and code.isdigit() and len(code) == 4:
            out.append((code, mkt))
    return out


def _load_ref() -> dict:
    if REF_PATH.exists():
        try:
            return json.loads(REF_PATH.read_text(encoding="utf-8")).get("stocks", {})
        except Exception as exc:
            print(f"[intraday] 讀 intraday_ref.json 失敗：{exc}")
    print("[intraday] 沒有 intraday_ref.json——量比與新高判斷會略過（先跑一次盤後產生）")
    return {}


def _session_fraction() -> float:
    """已過盤比例（0~1）。開盤前給地板值，避免比率型指標樣本太小爆衝（架構書 13.4）。"""
    n = now_tpe()
    mins = n.hour * 60 + n.minute + n.second / 60
    if mins <= SESSION_START_MIN:
        return 0.03
    if mins >= SESSION_END_MIN:
        return 1.0
    return max(0.05, (mins - SESSION_START_MIN) / SESSION_LEN)


def _market_status() -> str:
    n = now_tpe()
    mins = n.hour * 60 + n.minute
    if mins < SESSION_START_MIN - 15:
        return "closed"
    if mins < SESSION_START_MIN:
        return "pre_open"
    if mins <= SESSION_END_MIN:
        return "open"
    if mins <= SESSION_END_MIN + 30:
        return "closing"
    return "closed"


# ── 評分 ─────────────────────────────────────────────
def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _score(w: dict, *, rs_mkt: float, rs_sec: float | None, vol_ratio: float | None,
           above_open: bool, bo20: bool, bo120: bool, bo252: bool) -> float:
    """架構書第五節的加權綜合分。缺資料的因子退出、其餘權重按比例補回。"""
    parts: list[tuple[float, float]] = []  # (weight, normalized 0~1)
    parts.append((w.get("rs_market", 25), _clamp01(rs_mkt / w.get("rs_full_pct", 5.0))))
    if rs_sec is not None:
        parts.append((w.get("rs_sector", 20), _clamp01(rs_sec / w.get("rs_full_pct", 5.0))))
    if vol_ratio is not None:
        parts.append((w.get("volume", 25),
                      _clamp01((vol_ratio - 1) / w.get("vol_full_ratio", 3.0))))
    pattern = (0.5 if above_open else 0.0) + (0.5 if bo20 else 0.0)
    parts.append((w.get("pattern", 15), pattern))
    lt_high = 1.0 if bo252 else 0.6 if bo120 else 0.0
    parts.append((w.get("long_term_high", 10), lt_high))
    total_w = sum(p[0] for p in parts)
    if total_w <= 0:
        return 0.0
    return round(sum(p[0] * p[1] for p in parts) / total_w * 100, 1)


# ── 一輪篩選 ─────────────────────────────────────────
def run_once(cfg: dict, ref: dict, disp_codes: set[str]) -> dict:
    iv = cfg.get("intraday", {})
    w = iv.get("weights", {})
    universe = _universe()
    quotes = twse_mis.fetch_quotes(universe)
    taiex = twse_mis.fetch_taiex()
    market_chg = taiex.get("change_pct", 0.0)
    frac = _session_fraction()

    # 同業中位數漲幅（直接從當下快照算，不需要類股指數）
    by_ind: dict[str, list[float]] = {}
    for code, q in quotes.items():
        ind = (ref.get(code, {}) or {}).get("industry") or ""
        if ind:
            by_ind.setdefault(ind, []).append(q["change_pct"])
    ind_median = {k: median(v) for k, v in by_ind.items() if len(v) >= 3}

    rows = []
    for code, q in quotes.items():
        r = ref.get(code, {}) or {}
        price = q["price"]
        # Layer 0
        if price < iv.get("min_price", 10):
            continue
        if code in disp_codes:
            continue
        # 指標
        rs_mkt = round(q["change_pct"] - market_chg, 2)
        ind = r.get("industry") or ""
        rs_sec = (round(q["change_pct"] - ind_median[ind], 2)
                  if ind in ind_median else None)
        prev_vol = r.get("prev_vol") or 0
        vol_ratio = (round(q["volume"] / (frac * prev_vol), 2)
                     if (prev_vol > 0 and frac > 0) else None)
        above_open = price >= q["open"] > 0
        above_prev = price >= q["prev_close"] > 0
        hi20, hi60, hi120, hi252 = (r.get(k) for k in
                                    ("high_20", "high_60", "high_120", "high_252"))
        bo20 = bool(hi20 and price >= hi20)
        bo60 = bool(hi60 and price >= hi60)
        bo120 = bool(hi120 and price >= hi120)
        bo252 = bool(hi252 and price >= hi252)

        # Layer 1~3 硬門檻
        if rs_mkt < iv.get("rs_market_threshold", 1.0):
            continue
        if vol_ratio is not None and vol_ratio < iv.get("volume_ratio_threshold", 1.5):
            continue
        if not (above_open and above_prev):
            continue
        # 量比資料不足時的最小樣本保護：開盤初期不硬篩、但標記
        low_confidence = frac < iv.get("min_confidence_fraction", 0.05) or vol_ratio is None

        score = _score(w, rs_mkt=rs_mkt, rs_sec=rs_sec, vol_ratio=vol_ratio,
                       above_open=above_open, bo20=bo20, bo120=bo120, bo252=bo252)
        tier = ("A" if score >= iv.get("tier_a", 80)
                else "B" if score >= iv.get("tier_b", 60) else "C")
        rows.append({
            "code": code, "name": q["name"], "price": price,
            "change_pct": q["change_pct"], "volume": q["volume"],
            "rs_market": rs_mkt, "rs_sector": rs_sec, "volume_ratio": vol_ratio,
            "above_open": above_open, "above_prev_close": above_prev,
            "breakout_20d": bo20, "breakout_60d": bo60,
            "breakout_120d": bo120, "breakout_252d": bo252,
            "industry": ind, "score": score, "tier": tier,
            "low_confidence": low_confidence,
        })

    rows.sort(key=lambda x: -x["score"])
    cap = iv.get("list_cap", 40)
    a_all = [r for r in rows if r["tier"] == "A"]
    b_all = [r for r in rows if r["tier"] == "B"]
    tiers = {"A": a_all[:cap], "B": b_all[:cap]}
    result = {
        "as_of": now_tpe().strftime("%Y-%m-%d %H:%M:%S"),
        "display_delay_min": iv.get("display_delay_min", 15),
        "market_status": _market_status(),
        "session_fraction": round(frac, 3),
        "taiex": taiex,
        "counts": {"A": len(a_all), "B": len(b_all), "shown_per_tier": cap,
                   "scanned": len(quotes), "passed_funnel": len(rows)},
        "tiers": tiers,
        "rankings": _rankings(quotes, ref, frac),
        "sectors": _sector_rotation(quotes, ref),
    }
    return result


def _rankings(quotes: dict, ref: dict, frac: float, top: int = 30) -> dict:
    vals = list(quotes.values())
    for q in vals:
        r = ref.get(q["code"], {}) or {}
        pv = r.get("prev_vol") or 0
        q["_vr"] = (q["volume"] / (frac * pv)) if (pv > 0 and frac > 0) else 0.0
        q["_hi252"] = r.get("high_252")

    def slim(q):
        return {"code": q["code"], "name": q["name"], "price": q["price"],
                "change_pct": q["change_pct"], "volume": q["volume"],
                "volume_ratio": round(q["_vr"], 2) if q["_vr"] else None}

    up = sorted(vals, key=lambda q: -q["change_pct"])[:top]
    volr = sorted((q for q in vals if q["_vr"] > 0), key=lambda q: -q["_vr"])[:top]
    turn = sorted(vals, key=lambda q: -(q["price"] * q["volume"]))[:top]
    new_hi = sorted((q for q in vals if q["_hi252"] and q["price"] >= q["_hi252"]),
                    key=lambda q: -q["change_pct"])[:top]
    return {"gainers": [slim(q) for q in up],
            "volume_ratio": [slim(q) for q in volr],
            "turnover": [slim(q) for q in turn],
            "new_high_1y": [slim(q) for q in new_hi]}


def _sector_rotation(quotes: dict, ref: dict) -> list[dict]:
    by_ind: dict[str, list[float]] = {}
    for code, q in quotes.items():
        ind = (ref.get(code, {}) or {}).get("industry") or ""
        if ind:
            by_ind.setdefault(ind, []).append(q["change_pct"])
    out = []
    for ind, chgs in by_ind.items():
        if len(chgs) < 3:
            continue
        strong = sum(1 for c in chgs if c >= 3)
        out.append({"industry": ind, "count": len(chgs),
                    "median_change_pct": round(median(chgs), 2),
                    "strong_count": strong})
    out.sort(key=lambda x: -x["median_change_pct"])
    return out[:20]


# ── 深度快報訊號累積 ──────────────────────────────────
def _update_signals(result: dict, cfg: dict) -> bool:
    """把今日新出現的 A 級標的累積進 intraday_signals.json。
    有新標的時回傳 True 並更新 intraday_new_signal.json（webhook 靠它判斷要不要觸發）。"""
    iv = cfg.get("intraday", {})
    today = now_tpe().strftime("%Y-%m-%d")
    min_score = iv.get("deep_report_min_score", 82)

    prev = {}
    if SIGNALS_PATH.exists():
        try:
            prev = json.loads(SIGNALS_PATH.read_text(encoding="utf-8"))
        except Exception:
            prev = {}
    if prev.get("date") != today:
        prev = {"date": today, "stocks": {}}
    stocks = prev.get("stocks", {})

    new_codes = []
    for r in result["tiers"]["A"]:
        if r["score"] < min_score or r["low_confidence"]:
            continue
        if r["code"] in stocks:
            # 更新最高分/最新狀態，但不算「新」
            stocks[r["code"]]["peak_score"] = max(stocks[r["code"]]["peak_score"], r["score"])
            stocks[r["code"]]["last_seen"] = result["as_of"]
            continue
        stocks[r["code"]] = {
            "code": r["code"], "name": r["name"], "tier": "A",
            "peak_score": r["score"], "first_seen": result["as_of"],
            "last_seen": result["as_of"],
            "signals": {k: r[k] for k in
                        ("change_pct", "rs_market", "rs_sector", "volume_ratio",
                         "breakout_20d", "breakout_120d", "breakout_252d", "industry")},
        }
        new_codes.append(r["code"])

    prev["stocks"] = stocks
    prev["updated"] = result["as_of"]
    SIGNALS_PATH.write_text(json.dumps(prev, ensure_ascii=False, indent=2), encoding="utf-8")

    if new_codes:
        NEWSIG_PATH.write_text(json.dumps(
            {"date": today, "at": result["as_of"], "new_codes": new_codes,
             "stocks": {c: stocks[c] for c in new_codes}},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[intraday] 新增 A 級深度快報候選：{new_codes}")
    return bool(new_codes)


# ── 進入點 ───────────────────────────────────────────
def _disp_codes() -> set[str]:
    codes = set()
    for fn in (twse.fetch_disposition_stocks, twse.fetch_attention_today):
        for row in fn() or []:
            c = str(row.get("code") or row.get("Code") or "").strip()
            if c:
                codes.add(c)
    return codes


def run(loop: bool = False, until: str | None = None, interval: int = 60) -> None:
    """loop=False 跑一輪就結束；loop=True 每 interval 秒一輪，直到台北時間 until (HH:MM)。"""
    cfg = load_config()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ref = _load_ref()
    disp = _disp_codes()
    disp_refreshed = now_tpe()

    def one() -> None:
        nonlocal disp, disp_refreshed
        if (now_tpe() - disp_refreshed).total_seconds() > 1800:
            disp = _disp_codes()
            disp_refreshed = now_tpe()
        result = run_once(cfg, ref, disp)
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        _update_signals(result, cfg)
        c = result["counts"]
        print(f"[intraday] {result['as_of']} 掃 {c['scanned']} 檔｜"
              f"過漏斗 {c['passed_funnel']}｜A {c['A']}｜B {c['B']}", flush=True)

    if not loop:
        one()
        return

    end_min = None
    if until:
        hh, mm = until.split(":")
        end_min = int(hh) * 60 + int(mm)
    while True:
        one()
        if end_min is not None:
            n = now_tpe()
            if n.hour * 60 + n.minute >= end_min:
                print(f"[intraday] 到 {until}，收工")
                return
        time.sleep(interval)
