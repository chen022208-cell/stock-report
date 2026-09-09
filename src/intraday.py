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
from .fetchers import tpex, twse, twse_mis, yahoo

DATA_DIR = DOCS_DIR / "data"
HIST_DB_PATH = _MARKET_DATA_DIR / "intraday_hist.db"  # 每日收盤滾動歷史（intraday-data 分支）
REF_PATH = DATA_DIR / "intraday_ref.json"          # 每日盤前算好的參考值（新高、昨量）
OUT_PATH = DATA_DIR / "intraday.json"              # 盤中頁讀這個
SIGNALS_PATH = DATA_DIR / "intraday_signals.json"  # 今日 A 級累積清單（給深度快報）
NEWSIG_PATH = DATA_DIR / "intraday_new_signal.json"  # 只在有新 A 級時更新（webhook 過濾用）
ALERT_PATH = DATA_DIR / "intraday_alert.json"      # 給 workflow 直接送 Discord 的選股訊號

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


# 一個週期要「敢說是新高」至少需要幾個交易日的歷史。少於門檻就把該週期的
# 高點留成 None——寧可那個週期整個不可用，也不要拿 3 天的資料宣稱「創年新高」。
# （2026-09-07 事故：滾動歷史庫被 Actions cache 清掉只剩 1 天，high_5 到 high_252
#  全等於昨收，於是「創新高」榜跟漲幅榜一模一樣，而且每檔上漲股都白拿
#  long_term_high 那 10 分，開盤 1 分鐘就有 53 檔衝上 A 級、當天 5 篇深度快報
#  全部浪費在假訊號上。）
HIGH_PERIODS = [("high_5", 5, 4), ("high_20", 20, 15), ("high_60", 60, 45),
                ("high_120", 120, 90), ("high_252", 252, 200)]
BACKFILL_IF_FEWER_THAN = 210      # 歷史筆數少於這個就去 Yahoo 補滿一年


def _esb_daily_rows() -> list[dict]:
    """興櫃當日行情 → 跟上市櫃同格式的 {code, close, volume(張)}。

    興櫃沒有「收盤價」的概念（議價／搓合），TPEx 當日行情表的「成交」欄
    （LatestPrice）就是看盤講的股價，這裡拿它當當日收盤代表值。
    TransactionVolume 的單位是**股**（同一列的 BuyingQuantity 是 3000 這種數字），
    要 ÷1000 才跟 MIS／TWSE 對齊成「張」。
    """
    out = []
    for code, row in (tpex.fetch_esb_pricing() or {}).items():
        if row.get("price"):
            out.append({"code": code, "close": row["price"],
                        "volume": (row.get("volume") or 0) / 1000})
    return out


def _backfill_from_yahoo(conn: sqlite3.Connection, codes: dict[str, str]) -> int:
    """把 Yahoo 的一年日線灌進滾動歷史庫。

    為什麼需要這個：`data/intraday_hist.db` 不進 git、靠 Actions cache 跨天保留，
    cache 一 miss 就整個歸零。以前歸零之後程式照跑、照算「創 252 日新高」，
    只是那個 252 日其實只有 1 天——錯得很安靜。現在改成每天盤前檢查，歷史不足
    就直接從 Yahoo 補真的歷史回來，讓正確性不依賴 cache 有沒有命中。
    """
    if not codes:
        return 0
    print(f"[intraday] 歷史不足 {len(codes)} 檔，從 Yahoo 補一年日線…")
    done = 0

    def _store(code: str, res: dict):
        nonlocal done
        bars = res.get("bars") or []
        if not bars:
            return
        conn.executemany(
            "INSERT OR REPLACE INTO daily (code, date, close, volume) VALUES (?,?,?,?)",
            [(code, b["date"], b.get("close"), (b.get("volume") or 0) / 1000)
             for b in bars if b.get("date") and b.get("close")])
        done += 1
        if done % 200 == 0:
            conn.commit()
            print(f"[intraday]   已補 {done}/{len(codes)} 檔")

    yahoo.fetch_many(codes, rng="1y", pause=0.03, on_each=_store)
    conn.commit()
    print(f"[intraday] Yahoo 補歷史完成：{done}/{len(codes)} 檔")
    return done


def sync_ref(keep_days: int = 260) -> Path:
    """每日盤前跑一次：更新 intraday_hist.db → 算好 intraday_ref.json。

    ref 內容：{code: {name, industry, prev_vol, hist_days, high_5..high_252}}
    高點用「收盤價」算（跟盤中比的是即時成交價，收盤高點當作保守的突破門檻）。
    **歷史長度不足以支撐的週期，該欄位留 None**，下游（榜單、突破評分）看到 None
    就整個略過，不會拿短歷史冒充長週期新高。
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # 統一成交量單位為「張」：盤中 MIS 的 v 是「張」，日行情三個來源全都是「股」，
    # 所以三邊都要 ÷1000 才算得對量比。
    # ⚠️ 這裡本來寫著「TPEx TradingShares 已是張」而漏掉上櫃那條除法——**那是錯的**，
    # 欄位名稱 TradingShares 本身就是「股」。後果是**每一檔上櫃股的量比都被除以 1000**：
    # 2026-09-09 實測 3680 家登 prev_vol 存成 2,196,819（實際是 2,197 張），
    # 盤中成交 6,620 張算出來的量比是 0.0×；5314 世紀* 48,844 張算成 0.01×。
    # 症狀是「上櫃股量比全是 0.0×、上市股卻正常」，而且因為量比是漏斗的一層，
    # 這些股票也永遠篩不進 A/B 級名單。判斷方式：上市股的 prev_vol 有小數
    # （÷1000 的結果），壞掉的上櫃股是整數。
    quotes: list[dict] = []
    for q in (twse.fetch_daily_quotes() or []):
        q["volume"] = (q.get("volume") or 0) / 1000
        quotes.append(q)
    for q in (tpex.fetch_daily_quotes() or []):
        q["volume"] = (q.get("volume") or 0) / 1000
        quotes.append(q)
    quotes += _esb_daily_rows()          # 這條本來就有 ÷1000
    today = now_tpe().strftime("%Y-%m-%d")
    conn = _hist_conn()
    conn.executemany(
        "INSERT OR REPLACE INTO daily (code, date, close, volume) VALUES (?,?,?,?)",
        [(q["code"], today, q.get("close"), q.get("volume"))
         for q in quotes if q.get("code") and q.get("close")],
    )
    conn.commit()

    # 歷史長度不足的補真資料回來（cache miss、新上市、剛納入宇集的興櫃都會走這條）
    have = {code: n for code, n in conn.execute(
        "SELECT code, COUNT(*) FROM daily GROUP BY code")}
    need = {code: mkt for code, mkt in _universe()
            if have.get(code, 0) < BACKFILL_IF_FEWER_THAN}
    if need:
        _backfill_from_yahoo(conn, need)

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
    coverage = {field: 0 for field, _, _ in HIGH_PERIODS}
    for code in codes:
        rows = conn.execute(
            "SELECT close, volume FROM daily WHERE code=? ORDER BY date DESC LIMIT 260",
            (code,)).fetchall()
        if not rows:
            continue
        closes = [r[0] for r in rows if r[0]]
        prof = profiles.get(code, {})
        entry = {
            "name": prof.get("full_name") or names.get(code, ""),
            "industry": prof.get("industry", ""),
            "market": prof.get("market", ""),
            "prev_vol": rows[0][1] or 0,
            "hist_days": len(closes),
        }
        for field, window, min_days in HIGH_PERIODS:
            # 樣本數不夠就不給值——「只有 30 天資料」算不出「120 日新高」
            entry[field] = max(closes[:window]) if len(closes) >= min_days else None
            if entry[field] is not None:
                coverage[field] += 1
        ref[code] = entry
    conn.close()
    REF_PATH.write_text(json.dumps(
        {"date": today, "generated": now_tpe().strftime("%Y-%m-%d %H:%M"),
         "coverage": coverage, "stocks": ref},
        ensure_ascii=False), encoding="utf-8")
    print(f"[intraday] intraday_ref.json 更新：{len(ref)} 檔；"
          + "、".join(f"{f}:{n}" for f, n in coverage.items()))
    return REF_PATH


# ── 參考值 / 宇集 ─────────────────────────────────────
def _universe() -> list[tuple[str, str]]:
    """全市場真公司（上市＋上櫃＋興櫃），來自 company_profile。排除 ETF／權證。

    興櫃（esb，約 360 檔）以前被排除在外，所以漲幅榜、量比榜、A/B 級名單裡
    一檔興櫃都不會出現（使用者實際回報）。**興櫃的報價不能走 MIS**——實測
    tse_/otc_/oes_/emg_/esb_ 各種前綴打 mis.twse.com.tw 都回空值，MIS 根本沒有
    這個市場；要走 TPEx 自己的當日行情表（見 _esb_quotes）。
    """
    out = []
    for code, prof in db.all_company_profiles().items():
        mkt = prof.get("market")
        if mkt in ("twse", "tpex", "esb") and code.isdigit() and len(code) == 4:
            out.append((code, mkt))
    return out


def _esb_quotes(codes: set[str]) -> dict[str, dict]:
    """興櫃盤中報價 → 對齊 twse_mis.fetch_quotes 的欄位格式。

    幾個跟上市櫃不一樣、下游必須知道的地方：
    - **沒有開盤價**：興櫃是議價／搓合市場，行情表只有最高／最低／均價／成交。
      所以 `open` 給 0，`above_open` 會是 None，評分時「站上開盤」那個因子直接
      退出、其餘權重按比例補回（不是當成 False 扣分，那等於憑空懲罰興櫃）。
    - **漲跌幅的基準是「前日均價」**（PreviousAveragePrice），不是前一日收盤——
      這是 TPEx 自己行情表的定義，照用並在前端標明，不要偷換成收盤價。
    - 成交量單位是股，÷1000 換成張。
    """
    # ⚠️ TPEx 的興櫃「當日行情表」是**盤後才發布的日表，沒有盤中值**。
    # 2026-09-08 09:32 實測：343 檔全部 date=2026-09-07。以前不檢查日期就照收，
    # 於是每天開盤後整份昨天的興櫃行情被當成即時報價灌進盤中排行——
    # A 級 34 檔裡有 27 檔是興櫃，全部用昨天的漲跌幅排序（7686 掛著 +43.23%
    # 就是 09-07 的數字），使用者一眼看出「這些都是舊資料」。
    # 興櫃盤中沒有任何公開即時來源（MIS 也沒有這個市場），所以正確做法是
    # **盤中就不要有興櫃**，而不是拿昨天的頂替。盤後那一輪日期會對上，自然會回來。
    today = now_tpe().strftime("%Y-%m-%d")
    raw = tpex.fetch_esb_pricing() or {}
    stale = sum(1 for r in raw.values() if r.get("date") and r["date"] != today)
    if stale:
        print(f"[intraday] 興櫃當日行情表還是 {next(iter(raw.values())).get('date')} 的資料"
              f"（{stale}/{len(raw)} 檔），盤中不採用——興櫃沒有盤中即時來源")

    out: dict[str, dict] = {}
    for code, row in raw.items():
        if code not in codes or not row.get("price"):
            continue
        # 日期對不上就整筆丟掉，不要讓昨天的數字混進盤中排行
        if row.get("date") and row["date"] != today:
            continue
        out[code] = {
            "code": code, "name": row.get("name", ""), "price": row["price"],
            "prev_close": row.get("prev_avg") or 0.0,
            "open": 0.0,                       # 興櫃沒有開盤價
            "high": row.get("high") or 0.0, "low": row.get("low") or 0.0,
            "change": row.get("change") or 0.0,
            "change_pct": row.get("change_pct") or 0.0,
            "volume": (row.get("volume") or 0) / 1000,
            "trade_time": "", "quote_date": row.get("date", ""), "ex": "esb",
        }
    return out


def _load_ref() -> dict:
    if REF_PATH.exists():
        try:
            return json.loads(REF_PATH.read_text(encoding="utf-8")).get("stocks", {})
        except Exception as exc:
            print(f"[intraday] 讀 intraday_ref.json 失敗：{exc}")
    print("[intraday] 沒有 intraday_ref.json——量比與新高判斷會略過（先跑一次盤後產生）")
    return {}


def _elapsed_fraction() -> float:
    """**真實**已過盤比例（0~1），沒有地板。用來判斷資料夠不夠可信。"""
    n = now_tpe()
    mins = n.hour * 60 + n.minute + n.second / 60
    if mins <= SESSION_START_MIN:
        return 0.0
    if mins >= SESSION_END_MIN:
        return 1.0
    return (mins - SESSION_START_MIN) / SESSION_LEN


def _session_fraction() -> float:
    """已過盤比例（0~1），**帶地板**。開盤前期樣本太小，量比的分母若用真實比例
    會被除爆（架構書 13.4），所以下限拉到 0.05。

    ⚠️ 這個地板不可以拿來判斷「資料可不可信」——那要用 `_elapsed_fraction()`。
    以前 low_confidence 寫成 `frac < min_confidence_fraction(0.05)`，而 frac 本身
    的地板就是 0.05，於是這個條件**永遠是 False**，開盤第 1 分鐘的訊號照樣被當成
    高可信度。2026-09-07 就是這樣在 09:01:26 一口氣認定 53 檔 A 級、把當天 5 篇
    深度快報的額度全部用在開盤一分鐘的雜訊上。
    """
    return max(0.05, _elapsed_fraction())


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
           above_open: bool | None, bo20: bool | None,
           bo120: bool | None, bo252: bool | None) -> float:
    """架構書第五節的加權綜合分。**缺資料的因子退出、其餘權重按比例補回。**

    「退出」跟「給 0 分」是兩件不同的事，這裡一律用退出：
    - 興櫃沒有開盤價 → `above_open` 是 None，型態只看 20 日新高那一半。
    - 歷史長度不足以判斷 120／252 日新高 → `bo120`／`bo252` 是 None，
      long_term_high 這個因子整個不計分。**不可以退回 False**：那會讓
      「資料不足」跟「確定沒突破」在分數上長得一樣，而更糟的反面是 2026-09-07
      那次事故——歷史只有 1 天時 high_252 等於昨收，於是每檔上漲股都判定
      「創 252 日新高」白拿滿分，開盤一分鐘 53 檔衝上 A 級。
    """
    parts: list[tuple[float, float]] = []  # (weight, normalized 0~1)
    parts.append((w.get("rs_market", 25), _clamp01(rs_mkt / w.get("rs_full_pct", 5.0))))
    if rs_sec is not None:
        parts.append((w.get("rs_sector", 20), _clamp01(rs_sec / w.get("rs_full_pct", 5.0))))
    if vol_ratio is not None:
        parts.append((w.get("volume", 25),
                      _clamp01((vol_ratio - 1) / w.get("vol_full_ratio", 3.0))))
    pat: list[float] = []
    if above_open is not None:
        pat.append(1.0 if above_open else 0.0)
    if bo20 is not None:
        pat.append(1.0 if bo20 else 0.0)
    if pat:
        parts.append((w.get("pattern", 15), sum(pat) / len(pat)))
    if bo252 is not None or bo120 is not None:
        parts.append((w.get("long_term_high", 10),
                      1.0 if bo252 else 0.6 if bo120 else 0.0))
    total_w = sum(p[0] for p in parts)
    if total_w <= 0:
        return 0.0
    return round(sum(p[0] * p[1] for p in parts) / total_w * 100, 1)


# ── 一輪篩選 ─────────────────────────────────────────
def run_once(cfg: dict, ref: dict, disp_codes: set[str]) -> dict:
    iv = cfg.get("intraday", {})
    w = iv.get("weights", {})
    universe = _universe()
    quotes = twse_mis.fetch_quotes([(c, m) for c, m in universe if m != "esb"])
    # 興櫃走 TPEx 當日行情表（MIS 沒有這個市場，見 _esb_quotes）
    quotes.update(_esb_quotes({c for c, m in universe if m == "esb"}))
    taiex = twse_mis.fetch_taiex()
    market_chg = taiex.get("change_pct", 0.0)
    frac = _session_fraction()
    elapsed = _elapsed_fraction()

    # 同業中位數漲幅（直接從當下快照算，不需要類股指數）
    by_ind: dict[str, list[float]] = {}
    for code, q in quotes.items():
        ind = (ref.get(code, {}) or {}).get("industry") or ""
        if ind:
            by_ind.setdefault(ind, []).append(q["change_pct"])
    ind_median = {k: median(v) for k, v in by_ind.items() if len(v) >= 3}

    # 漏斗每一層各刷掉幾檔——這是「為什麼今天只有 N 檔入選」的唯一解釋，
    # 以前只存在於 stdout（Actions log 裡），網站上完全看不到。現在寫進 JSON。
    funnel = {"scanned": len(quotes), "low_price": 0, "disposition": 0,
              "rs_market": 0, "volume_ratio": 0, "momentum": 0, "passed": 0}
    rows = []
    for code, q in quotes.items():
        r = ref.get(code, {}) or {}
        price = q["price"]
        # Layer 0
        if price < iv.get("min_price", 10):
            funnel["low_price"] += 1
            continue
        if code in disp_codes:
            funnel["disposition"] += 1
            continue
        # 指標
        rs_mkt = round(q["change_pct"] - market_chg, 2)
        ind = r.get("industry") or ""
        rs_sec = (round(q["change_pct"] - ind_median[ind], 2)
                  if ind in ind_median else None)
        prev_vol = r.get("prev_vol") or 0
        vol_ratio = (round(q["volume"] / (frac * prev_vol), 2)
                     if (prev_vol > 0 and frac > 0) else None)
        # 興櫃沒有開盤價 → None（未知），不是 False（確定跌破）
        above_open = (price >= q["open"]) if q["open"] > 0 else None
        above_prev = price >= q["prev_close"] > 0
        hi20, hi60, hi120, hi252 = (r.get(k) for k in
                                    ("high_20", "high_60", "high_120", "high_252"))
        # 參考值是 None 代表「歷史不足以判斷」，一路帶著 None 傳給 _score
        bo20 = (price >= hi20) if hi20 else None
        bo60 = (price >= hi60) if hi60 else None
        bo120 = (price >= hi120) if hi120 else None
        bo252 = (price >= hi252) if hi252 else None

        # Layer 1~3 硬門檻
        if rs_mkt < iv.get("rs_market_threshold", 1.0):
            funnel["rs_market"] += 1
            continue
        if vol_ratio is not None and vol_ratio < iv.get("volume_ratio_threshold", 1.5):
            funnel["volume_ratio"] += 1
            continue
        if above_open is False or not above_prev:
            funnel["momentum"] += 1
            continue        # above_open 是 None（興櫃無開盤價）不擋
        # 量比資料不足時的最小樣本保護：開盤初期不硬篩、但標記。
        # 用 elapsed（真實已過盤比例）而不是 frac（有地板），否則永遠不會成立。
        low_confidence = (elapsed < iv.get("min_confidence_fraction", 0.05)
                          or vol_ratio is None)

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
            "ex": q.get("ex") or "",
            "low_confidence": low_confidence,
        })

    funnel["passed"] = len(rows)
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
        "elapsed_fraction": round(elapsed, 3),
        "taiex": taiex,
        "counts": {"A": len(a_all), "B": len(b_all), "shown_per_tier": cap,
                   "scanned": len(quotes), "passed_funnel": len(rows)},
        "tiers": tiers,
        "funnel": funnel,
        "markets": _market_counts(quotes),
        "rankings": _rankings(quotes, ref, frac),
        "sectors": _sector_rotation(quotes, ref),
    }
    return result


# 創新高週期 → ref 裡的欄位。前端「創新高」那個選項會再展開這幾個細分檔位，
# 使用者才能先挑週期再看名單，而不是只有寫死的「創年新高」一種。
NEW_HIGH_PERIODS = [("5d", "5天", "high_5"), ("1m", "1月", "high_20"),
                    ("3m", "3月", "high_60"), ("6m", "半年", "high_120"),
                    ("1y", "1年", "high_252")]


def _market_counts(quotes: dict) -> dict:
    """掃到的檔數依市場別拆開。使用者要能一眼看出興櫃到底有沒有被納入掃描，
    而不是只看到一個「掃描 1946 檔」的總數卻不知道裡面有沒有興櫃。"""
    out = {"twse": 0, "tpex": 0, "esb": 0}
    for q in quotes.values():
        ex = (q.get("ex") or "").lower()
        key = "esb" if ex == "esb" else "tpex" if ex.startswith("otc") else "twse"
        out[key] += 1
    return out


def _rankings(quotes: dict, ref: dict, frac: float, top: int = 30) -> dict:
    vals = list(quotes.values())
    for q in vals:
        r = ref.get(q["code"], {}) or {}
        pv = r.get("prev_vol") or 0
        q["_vr"] = (q["volume"] / (frac * pv)) if (pv > 0 and frac > 0) else 0.0
        for _, _, field in NEW_HIGH_PERIODS:
            q["_" + field] = r.get(field)

    def slim(q):
        # ex（tse／otc／esb）一定要帶出去：前端要拿它組 MIS 的頻道代號
        # （tse_2330.tw / otc_6488.tw）才查得到即時報價。沒有這欄的話前端
        # 只能兩種前綴都猜一次，白白吃掉一半的單次查詢額度。
        return {"code": q["code"], "name": q["name"], "price": q["price"],
                "change_pct": q["change_pct"], "volume": q["volume"],
                "ex": q.get("ex") or "",
                "volume_ratio": round(q["_vr"], 2) if q["_vr"] else None}

    up = sorted(vals, key=lambda q: -q["change_pct"])[:top]
    volr = sorted((q for q in vals if q["_vr"] > 0), key=lambda q: -q["_vr"])[:top]
    turn = sorted(vals, key=lambda q: -(q["price"] * q["volume"]))[:top]
    out = {"gainers": [slim(q) for q in up],
           "volume_ratio": [slim(q) for q in volr],
           "turnover": [slim(q) for q in turn]}
    # 每個週期另外回報「有幾檔的歷史長到足以判斷這個週期」。前端拿它把資料
    # 不足的週期按鈕停用並說明，而不是顯示一份其實算不出來的名單。
    # 2026-09-07 事故：滾動歷史只剩 1 天時 high_5～high_252 全等於昨收，
    # 五個週期的名單於是跟漲幅榜一字不差，使用者一眼就看出「不可能吧」。
    eligible = {}
    for key, _, field in NEW_HIGH_PERIODS:
        pool = [q for q in vals if q.get("_" + field)]
        eligible["new_high_" + key] = len(pool)
        hi = sorted((q for q in pool if q["price"] >= q["_" + field]),
                    key=lambda q: -q["change_pct"])[:top]
        out["new_high_" + key] = [slim(q) for q in hi]
    out["_eligible"] = eligible
    return out


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

    # 開盤初期不要放行訊號。量比是「今累計量 ÷ 已過盤比例 ÷ 昨日全日量」，
    # 開盤前幾分鐘分子分母都還沒長出來，算出來的東西不足以拿去燒掉當天
    # 5 篇深度快報的額度——2026-09-07 全部 5 篇就是在 09:01:26 一次用完的。
    warmup = iv.get("signal_min_fraction", 0.06)      # ≈ 開盤後 16 分鐘
    if result.get("elapsed_fraction", 1.0) < warmup:
        print(f"[intraday] 開盤暖機中（已過盤 {result.get('elapsed_fraction')}"
              f" < {warmup}），暫不產生新訊號")
        return False

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
        _write_alert(new_codes, stocks, result, cfg)
        print(f"[intraday] 新增 A 級深度快報候選：{new_codes}")
    return bool(new_codes)


def _write_alert(new_codes: list[str], stocks: dict, result: dict, cfg: dict) -> None:
    """把新出現的高分訊號寫成一則可以直接送出的推播內容。

    為什麼不走 `docs/_notify_*.json`／daily-notify.yml：那條路吃的是 **main 分支**
    的 push，而盤中迴圈只推 `intraday-data` 分支（刻意不碰 main，免得每分鐘觸發
    Pages 重建）。所以盤中選股的推播由 `intraday.yml` 自己拿 repo secret 送
    Discord，這裡只負責產內容。以前完全沒有這一段，使用者只有在雲端 Routine
    產出深度快報時才會收到通知——而深度快報一天上限 5 篇，等於絕大多數 A 級
    訊號從來不會通知任何人（使用者實際回報「盤中選股沒有通知分數高的股票」）。
    """
    delay = cfg.get("intraday", {}).get("display_delay_min", 15)
    lines = [f"⚡ 盤中 A 級強勢訊號（{len(new_codes)} 檔）  資料時間 {result['as_of']}"]
    for c in new_codes:
        st = stocks[c]
        sig = st.get("signals", {})
        bits = [f"漲幅 {sig.get('change_pct')}%",
                f"領先大盤 {sig.get('rs_market')}%"]
        if sig.get("volume_ratio"):
            bits.append(f"量比 {sig['volume_ratio']}×")
        hits = [lbl for key, lbl in (("breakout_20d", "20日高"),
                                     ("breakout_120d", "120日高"),
                                     ("breakout_252d", "252日高")) if sig.get(key)]
        if hits:
            bits.append("突破 " + "／".join(hits))
        if sig.get("industry"):
            bits.append(sig["industry"])
        lines.append(f"　{c} {st['name']}（{st['peak_score']} 分）：" + "、".join(bits))
    lines.append(f"\n公開頁顯示延遲約 {delay} 分鐘，本通知為內部即時值。"
                 "這是規則計算的訊號，不是投資建議。")
    ALERT_PATH.write_text(json.dumps(
        {"date": result["as_of"][:10], "at": result["as_of"],
         "codes": new_codes, "text": "\n".join(lines)},
        ensure_ascii=False, indent=2), encoding="utf-8")


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
