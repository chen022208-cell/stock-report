"""主程式入口。

用法：
    python -m src.main morning     # 07:00 國際盤
    python -m src.main evening     # 18:00 台股盤後
    python -m src.main monthly     # 每月 12 日 月報完整版
    python -m src.main holiday     # 假日功課
    python -m src.main research    # 處理使用者提交的研究文章（見 submit.html）
    python -m src.main news        # 檢查華爾街見聞即時快訊，重要且相關才推播
    python -m src.main auto        # 自動判斷今天該跑什麼（排程用這個）

本地測試：DRY_RUN=1 python -m src.main evening
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import traceback
from datetime import date, timedelta
from pathlib import Path

# Windows 主控台預設用 cp950（Big5），不是每個中文字/emoji 都能編碼，遇到就會
# 直接把整支腳本炸掉（UnicodeEncodeError），不是排程環境（GitHub Actions／
# Claude Code Routine 多半是 UTF-8 的 Linux）會遇到的問題，但本機執行 print()
# 不該因為主控台編碼不支援某個字就讓整個報告開天窗——這裡改成遇到編碼不了的
# 字元直接替換掉，不拋例外。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

from . import db, llm, notify, prices_db, render
from .analysis import global_themes, industry, review, scoring, screener, technical
from .config import DRY_RUN, load_config, today_str, now_tpe
from .fetchers import (article, fred, google_sheet, international, mops, stock_news,
                       tdcc, tpex, twse, wallstreetcn, yahoo)
from .market_calendar import (classify_day, consecutive_closed_days,
                              is_last_day_before_reopen, next_trading_day,
                              refresh_holidays)
from .notify import send_notification


def _safe(fn, default, label: str):
    """任一資料源失敗都不該讓整份報告開天窗。"""
    try:
        return fn()
    except Exception as exc:
        print(f"[warn] {label} 失敗：{exc}")
        traceback.print_exc()
        return default


def _weekly_digest() -> dict | None:
    """過去 7 天的市場快照彙整，週一早報用。沒有資料回 None。"""
    end = now_tpe().date()
    start = end - timedelta(days=7)
    snaps = db.snapshots_between(start.isoformat(), end.isoformat())
    if not snaps:
        return None

    first, last = snaps[0], snaps[-1]
    o = first.get("taiex_close") or 0
    c = last.get("taiex_close") or 0
    active = db.list_themes("active")
    movers = [t for t in active if (t.get("last_signal_date") or "") >= start.isoformat()]

    return {
        "range": f"{first['date']} ~ {last['date']}",
        "sessions": len(snaps),
        "taiex_open": o,
        "taiex_close": c,
        "taiex_change_pct": round((c / o - 1) * 100, 2) if o else 0.0,
        "foreign_sum": round(sum(s.get("foreign_net") or 0 for s in snaps), 1),
        "trust_sum": round(sum(s.get("trust_net") or 0 for s in snaps), 1),
        "active_theme_count": len(active),
        "theme_movers": [{"name": t["name"], "confidence": t.get("confidence", "mid"),
                          "update_count": t.get("update_count", 1)} for t in movers[:8]],
    }


# ── 早報：國際盤 ───────────────────────────────────────
def run_morning() -> None:
    cfg = load_config()
    today = today_str()
    print(f"[morning] 產出國際盤報告 {today}")

    intl = _safe(international.fetch_international, {}, "國際盤")
    calls = _safe(lambda: mops.fetch_earnings_calls(), [], "法說會行事曆")
    gt = _safe(global_themes.run, {"macro_note": "", "themes": []}, "國際題材追蹤")
    # 沒設 FRED_API_KEY 就回空清單，區塊自動不顯示（見 fred.py 開頭說明如何免費申請）
    macro = _safe(fred.fetch_macro_snapshot, [], "FRED 總經數據")

    commentary = _safe(
        lambda: llm.market_commentary({"international": intl,
                                       "global_themes": gt.get("themes"),
                                       "type": "morning"}),
        "", "早報評論")

    ctx = {
        "report_kind": "早報 · 國際盤摘要",
        "date_label": render.date_label(today),
        "international": intl,
        "intl_commentary": commentary,
        "earnings_calls": calls,
        "global_themes": gt.get("themes", []),
        "global_macro_note": gt.get("macro_note", ""),
        "macro": macro,
    }

    # 週一早報併入週報：台股週五收盤但美股週五晚才交易，週五發會漏掉整個美股交易日
    if now_tpe().weekday() == 0:
        ctx["weekly"] = _safe(_weekly_digest, None, "週報彙整")

    path = render.render_daily(ctx, f"{today}-morning")
    render.render_site()
    print(f"[morning] 完成：{path}")

    # 推播內容：評論之外，把「可能影響台股」的國際題材明白列出來，
    # 不要讓使用者得點進網站才看得到——這是早報推播存在的理由
    notify_body = commentary
    themes_for_push = gt.get("themes", [])
    if themes_for_push:
        lines = [f"- {t['name']}：{t.get('tw_readthrough', t.get('summary', ''))}"
                for t in themes_for_push[:3]]
        notify_body = f"{commentary}\n\n國際題材對台股影響：\n" + "\n".join(lines)
    send_notification(f"早報已產出：{render.date_label(today)}", notify_body)


# ── 盤後：台股完整分析 ─────────────────────────────────
def process_catalog_batch(cfg: dict, today: str, quotes_by_code_all: dict[str, dict]) -> int:
    """題材目錄補齊：117 個種子題材不能永遠只有名字跟一句話論點，每次處理一批
    （優先處理從沒研究過的），用 LLM 既有知識補上代表股，再對照今天真實行情
    判斷現在算不算當紅；當紅且抓到多檔代表股時，全部代表股都做公司介紹＋SWOT，
    不因為原本評分頁前 8～12 檔的上限而漏掉——這是題材目錄專屬的覆蓋率保證，
    跟評分頁那邊「起漲點/新掛牌一定要有分析」是同一個精神、不同的名單來源。

    抽成獨立函式是因為日常 run_evening() 每天只處理一批（控制 LLM 成本），
    但補齊 117 個全部需要跑好幾批，一次性回填時可以在迴圈裡重複呼叫這個函式。
    回傳這批實際處理的題材數，方便呼叫端判斷是否已經補齊完畢（回傳 0）。
    """
    tc_cfg = cfg["theme_catalog"]
    catalog_batch = _safe(
        lambda: db.catalog_themes_for_analysis(tc_cfg["batch_size"], today, tc_cfg["refresh_days"]),
        [], "題材目錄待研究名單")
    if not catalog_batch:
        return 0

    research = _safe(lambda: llm.catalog_theme_research_batch(catalog_batch),
                     {}, "題材目錄研究")
    for theme in catalog_batch:
        r = research.get(theme["name"], {})
        matched = []
        for ref in r.get("stocks", []):
            q = quotes_by_code_all.get(ref.get("code"))
            if q:
                matched.append({"code": ref["code"], "name": q.get("name") or ref.get("name", ""),
                                "change_pct": q.get("change_pct", 0)})

        hot_stocks = [m for m in matched if m["change_pct"] >= tc_cfg["hot_change_pct"]]
        if len(hot_stocks) >= tc_cfg["hot_min_stocks"]:
            verdict, confidence = "hot", "high"
        elif hot_stocks or any(m["change_pct"] > 0 for m in matched):
            verdict, confidence = "warm", "mid"
        else:
            verdict, confidence = "cold", "low"

        if verdict == "hot" and matched:
            deep_input = [{"code": m["code"], "name": m["name"]} for m in matched]
            deep = _safe(lambda d=deep_input: llm.stock_analysis_batch(d),
                        {}, f"題材個股深度分析 {theme['name']}")
            for m in matched:
                if m["code"] in deep:
                    m["analysis"] = deep[m["code"]]

        db.update_catalog_analysis(
            theme["id"], r.get("summary") or theme.get("summary", ""),
            confidence, verdict, matched, today)
    print(f"[catalog] 本批補齊 {len(catalog_batch)} 個")
    return len(catalog_batch)


def process_catalog_deep_dives(cfg: dict, today: str) -> int:
    """題材目錄的標題要能點進去看產業分析深度報告，不能只是純文字名稱。
    每次處理一批「已經有代表股研究、但還沒產出深度報告」的目錄題材（見
    db.catalog_themes_needing_deep_dive()），跟 write_deep_dive() 產「追蹤中」
    真題材深度報告用的是同一套 LLM 提示與 render_article() 樣板，只是 timeline
    留空（目錄題材沒有逐日追蹤軌跡）。批次大小控制在較小值，因為深度報告
    字數遠多於代表股研究，LLM 成本較高。
    """
    batch = _safe(
        lambda: db.catalog_themes_needing_deep_dive(cfg["theme_catalog"]["deep_dive_batch_size"]),
        [], "題材目錄待寫深度報告名單")
    if not batch:
        return 0

    for theme in batch:
        article = _safe(lambda t=theme: llm.write_deep_dive(t, []), {}, f"目錄深度報告 {theme['name']}")
        if not article:
            continue
        slug = render.slugify(theme["name"])
        supply_chain = _safe(lambda t=theme: llm.supply_chain_structure(t),
                             {}, f"目錄供應鏈結構 {theme['name']}")
        if supply_chain:
            db.save_supply_chain(theme["id"], supply_chain, today)
        render.render_article(theme, article, slug, supply_chain)
        db.set_deep_dive_slug(theme["id"], slug)
    print(f"[catalog] 本批寫出深度報告 {len(batch)} 篇")
    return len(batch)


def _all_market_codes() -> list[dict]:
    """全市場公司清單（上市＋上櫃＋興櫃），用申報基本資料 t187ap03 三個資料集。

    刻意不用 stock_index.json：那是從熱力圖行情建出來的，混了 ETF／權證，
    而且沒有市場別欄位（一律當成上市會讓上櫃／興櫃被誤判）。

    另外補一層保險：t187ap03 三個資料集偶爾跟月營收資料集對不齊（例如
    2867 三商美邦人壽、4150 優你康、5371 中強光電、7834 來毅這種會有月營收
    申報、卻沒出現在 t187ap03 名單裡的個股）。把月營收資料集裡多出來的代號
    也一起納進來，後面 sync_company_profiles 會再逐檔去 MOPS t05st03 補
    「主要經營業務」，不會因為名單缺漏就永遠少一頁。
    """
    codes = dict(_safe(mops.fetch_listed_companies, {}, "全市場公司清單"))
    for code, rev in _safe(mops.fetch_monthly_revenue, {}, "全市場月營收").items():
        if code not in codes:
            codes[code] = {
                "code": code,
                "name": rev.get("name", ""),
                "market": rev.get("market", ""),
                "industry": rev.get("industry", ""),
            }
    return list(codes.values())


def sync_company_profiles(today: str, limit: int = 300) -> int:
    """把公司基本資料（主要經營業務等申報值）抓進 company_profile 表。
    這是 company_desc／SWOT 唯一可以依據的事實來源——絕對不要用股票名稱或
    產業分類去推測公司在做什麼。營業項目幾乎不變，抓過的就跳過。"""
    import time

    universe = _all_market_codes()
    if not universe:
        print("[profile] 沒有全市場清單（先跑一次盤後產生 stock_index.json）")
        return 0
    have = db.company_profile_codes()
    pending = [s for s in universe if s["code"] not in have]
    if not pending:
        print(f"[profile] 全市場 {len(universe)} 檔公司基本資料都齊了")
        return 0

    written = 0
    for s in pending[:limit]:
        prof = _safe(lambda: mops.fetch_company_profile(s["code"]), {},
                     f"{s['code']} 公司基本資料")
        if prof.get("business"):
            # t05st03 只有申報全名（台灣積體電路製造股份有限公司），市場通用簡稱
            # （台積電）在 t187ap03 的清單裡，一併帶進去；個股頁標題要用簡稱，
            # 不然彈窗與個股頁會出現一整串申報全名。
            prof = {**prof, "short_name": s.get("name", "")}
            db.upsert_company_profile(s["code"], prof, s.get("market", ""), today)
            written += 1
        time.sleep(0.4)
    print(f"[profile] 本批抓到 {written} 檔（全市場尚缺 {len(pending) - written} 檔）")
    return written


def sync_monthly_revenue(today: str) -> int:
    """全市場月營收（政府開放資料）寫進 monthly_revenue 表，當基本面事實依據。"""
    rows = _safe(mops.fetch_monthly_revenue, {}, "全市場月營收")
    for code, rev in rows.items():
        db.upsert_monthly_revenue(code, rev["period"], rev, today)
    print(f"[fundamental] 月營收寫入 {len(rows)} 檔")
    return len(rows)


def process_stock_swot_batch(cfg: dict, today: str) -> int:
    """【已停用，不要重新接回 run_evening】全市場個股公司介紹＋SWOT 批次回填。

    2026-09-06 移除：這條路是 `llm.company_swot_batch`（以申報營業項目為底＋
    推論），對冷門興櫃／小型股仍可能寫錯公司在做什麼，不是逐檔查證過的事實。
    使用者明確要求「沒有證實的判讀就不要放上站」。個股的公司介紹／SWOT 現在
    只在：(1) 評分頁焦點股（`llm.stock_analysis_batch`，帶技術／籌碼／營收／
    新聞脈絡）(2) 逐檔人工查證過的個股 上出現。其餘個股彈窗只顯示
    基本資料（申報值）＋月營收（政府開放資料）＋題材（本站知識庫）。

    函式保留是為了可能的一次性、逐檔查證後的回填用途；平常不呼叫。"""
    sw = cfg.get("stock_swot", {})

    profiles = db.all_company_profiles()
    if not profiles:
        print("[swot] 還沒有公司基本資料，先跑 sync_company_profiles 再產 SWOT")
        return 0

    revenue = db.latest_monthly_revenue()
    themes_by_code: dict[str, list[str]] = {}
    for t in _safe(db.list_themes_with_stocks, [], "題材相關個股"):
        for s in t.get("stocks", []):
            code = str(s.get("code", "")).strip()
            if code:
                themes_by_code.setdefault(code, []).append(t["name"])

    names = {c: r.get("name", "") for c, r in revenue.items()}
    have = db.stock_analysis_codes(today, sw.get("refresh_days", 180))
    pending = [c for c in sorted(profiles) if c not in have]
    if not pending:
        print(f"[swot] 全市場 {len(profiles)} 檔（有營業項目的）SWOT 都補齊了")
        return 0

    batch_codes = pending[: sw.get("batch_size", 60)]
    batch = []
    for code in batch_codes:
        p = profiles[code]
        batch.append({
            "code": code,
            "name": names.get(code) or p.get("full_name", ""),
            "industry": p.get("industry", ""),
            "business": p.get("business", ""),
            "rev": revenue.get(code, {}),
            "themes": themes_by_code.get(code, []),
        })
    result = _safe(lambda: llm.company_swot_batch(batch), {}, "公司介紹／SWOT")
    written = 0
    for s in batch:
        a = result.get(s["code"])
        if not a or not a.get("company_desc"):
            continue
        db.upsert_stock_analysis(s["code"], s.get("name", ""), a["company_desc"],
                                 a.get("swot", {}), today)
        written += 1
    print(f"[swot] 本批補齊 {written} 檔（已分析個股尚缺 {len(pending) - written} 檔，"
          f"LLM 沒把握的會略過不寫）")
    return written


def _run_intraday_ref() -> None:
    """盤中篩選器每日盤前的參考值更新（新高、昨量），intraday-data 分支用。"""
    from . import intraday
    intraday.sync_ref()


_INTRADAY_NEWSIG_URL = ("https://raw.githubusercontent.com/chen022208-cell/"
                        "stock-report/intraday-data/docs/data/intraday_new_signal.json")


def run_intraday_deep_report() -> None:
    """盤中焦點股深度快報：webhook 觸發時跑一次。

    盤中篩選器抓到新的 A 級標的 → 更新 intraday-data 分支的 intraday_new_signal.json
    → webhook 觸發這個。逐檔上網查證後產出快報，寫 intraday_reports 表 ＋ 產出
    docs/analysis/ 頁面 ＋ 發 Discord。每日上限見 config.yaml。沒有新標的就安靜結束。
    """
    import urllib.request

    cfg = load_config()
    iv = cfg.get("intraday", {})
    cap = iv.get("deep_report_daily_cap", 5)
    min_score = iv.get("deep_report_min_score", 82)
    today = today_str()

    # 取 intraday-data 分支上的新訊號檔
    payload = None
    local = render.DOCS_DIR / "data" / "intraday_new_signal.json"
    if local.exists():
        payload = _safe(lambda: json.loads(local.read_text(encoding="utf-8")), None, "本地新訊號")
    if payload is None:
        try:
            req = urllib.request.Request(_INTRADAY_NEWSIG_URL,
                                         headers={"User-Agent": "Mozilla/5.0"})
            payload = json.loads(urllib.request.urlopen(req, timeout=20).read())
        except Exception as exc:
            print(f"[intraday-report] 取不到 intraday_new_signal.json：{exc}")
            return
    if not payload or payload.get("date") != today:
        print("[intraday-report] 沒有今日的新訊號")
        return

    # 這一批訊號處理過了就跳（webhook 每分鐘都可能觸發）
    stamp = f"{payload.get('date')}|{payload.get('at')}"
    if db.get_state("intraday_deepreport_stamp") == stamp:
        print("[intraday-report] 這批訊號已處理過")
        return

    done = db.intraday_reports_on(today)
    remaining = cap - len(done)
    if remaining <= 0:
        print(f"[intraday-report] 今日已達上限 {cap} 篇")
        db.set_state("intraday_deepreport_stamp", stamp)
        return

    sig_stocks = payload.get("stocks", {})
    profiles = db.all_company_profiles()
    batch = []
    # 每日上限只有幾篇，額度要花在分數最高的標的上。以前是照訊號出現的先後
    # 順序取前 N 檔——先出現的不一定比較強，實測 2026-09-07 當天 5 篇全部被
    # 09:01:26 那一批（開盤第 1 分鐘、當時參考值還是壞的）用光，後面真正跑出
    # 100 分的標的一篇都沒有。改成先依分數由高到低排序再取。
    ordered = sorted(payload.get("new_codes", []),
                     key=lambda c: -(sig_stocks.get(c, {}).get("peak_score") or 0))
    for code in ordered:
        s = sig_stocks.get(code, {})
        if db.intraday_report_exists(code, today):
            continue
        if (s.get("peak_score") or 0) < min_score:
            continue
        prof = profiles.get(code, {})
        batch.append({
            "code": code, "name": s.get("name") or prof.get("full_name", ""),
            "industry": prof.get("industry", ""), "business": prof.get("business", ""),
            "signals": s.get("signals", {}), "peak_score": s.get("peak_score"),
        })
        if len(batch) >= remaining:
            break

    if not batch:
        print("[intraday-report] 沒有符合條件的新標的")
        db.set_state("intraday_deepreport_stamp", stamp)
        return

    result = _safe(lambda: llm.intraday_flash_report(batch), {}, "盤中快報")
    now_s = now_tpe().strftime("%Y-%m-%d %H:%M:%S")
    made = []
    for s in batch:
        a = result.get(s["code"])
        if not a or not a.get("company_desc"):
            continue
        srcs = [x for x in (a.get("sources") or []) if str(x).strip()]
        external = [x for x in srcs if "公開資訊觀測站申報值" not in str(x)]
        if not external:
            print(f"[intraday-report] {s['code']} 無外部來源，略過")
            continue
        db.upsert_intraday_report(
            s["code"], today, now_s, s["name"], "A", s.get("peak_score") or 0.0,
            s.get("signals", {}), a.get("headline", ""), a["company_desc"],
            a.get("swot", {}), srcs, discord_sent=True)
        row = {"code": s["code"], "name": s["name"], "date": today, "reported_at": now_s,
               "tier": "A", "peak_score": s.get("peak_score") or 0.0,
               "signals": s.get("signals", {}), "headline": a.get("headline", ""),
               "company_desc": a["company_desc"], "swot": a.get("swot", {}), "sources": srcs}
        render.render_intraday_report(row)
        made.append(row)

    render.render_intraday_report_index()
    if made:
        lines = [f"⚡ 盤中焦點股快報（{today}）"]
        for m in made:
            chg = m["signals"].get("change_pct")
            lines.append(f"\n**{m['code']} {m['name']}**"
                         + (f"（{chg:+.2f}%）" if chg is not None else "")
                         + (f"\n{m['headline']}" if m.get("headline") else ""))
        base = _safe(notify._site_base_url, "", "站台網址")
        if base:
            lines.append(f"\n{base}/analysis/index.html")
        (render.DOCS_DIR / "_notify_intraday.json").write_text(
            json.dumps({"title": lines[0], "body": "\n".join(lines[1:]).strip(),
                        "url": f"{base}/analysis/index.html" if base else ""},
                       ensure_ascii=False), encoding="utf-8")
    db.set_state("intraday_deepreport_stamp", stamp)
    print(f"[intraday-report] 產出 {len(made)} 篇（今日累計 {len(done) + len(made)}/{cap}）")


def run_verify_stocks() -> None:
    """每天逐檔查證少量個股的公司介紹＋SWOT，寫進 stock_analysis（帶 sources）。

    這是排程「台股個股逐檔查證」用的入口。跟已停用的 process_stock_swot_batch
    不同：這裡每檔都要求 LLM（雲端 CCR session）實際 WebSearch 鉅亨／Goodinfo／
    財報狗／公司官網／年報查證，回覆一定要帶 sources，查不到就不寫那一檔。
    範圍依市值（最新月營收）由大到小，每 refresh_days 天複查一次。
    """
    cfg = load_config()
    sv = cfg.get("stock_verify", {})
    limit = sv.get("daily_count", 10)
    refresh_days = sv.get("refresh_days", 180)
    today = today_str()

    profiles = db.all_company_profiles()
    if not profiles:
        print("[verify] 還沒有公司基本資料，先跑一次盤後產生 company_profile")
        return
    revenue = db.latest_monthly_revenue()
    themes_by_code: dict[str, list[str]] = {}
    for t in _safe(db.list_themes_with_stocks, [], "題材相關個股"):
        for s in t.get("stocks", []):
            code = str(s.get("code", "")).strip()
            if code:
                themes_by_code.setdefault(code, []).append(t["name"])

    fresh = db.stock_analysis_codes(today, refresh_days)   # 已查證且未過期
    prior = db.all_stock_analysis()
    pending = [c for c in profiles if c not in fresh and (profiles[c].get("business") or "").strip()]
    # 依最新月營收（市值代理）由大到小；沒有月營收的排最後
    pending.sort(key=lambda c: -((revenue.get(c) or {}).get("revenue") or 0))
    if not pending:
        print(f"[verify] 全市場有申報營業項目的個股都在 {refresh_days} 天內查證過了")
        return

    batch_codes = pending[:limit]
    batch = []
    for code in batch_codes:
        p = profiles[code]
        batch.append({
            "code": code,
            "name": (revenue.get(code) or {}).get("name") or p.get("full_name", ""),
            "industry": p.get("industry", ""),
            "business": p.get("business", ""),
            "rev": revenue.get(code, {}),
            "themes": themes_by_code.get(code, []),
            "prior_desc": (prior.get(code) or {}).get("company_desc", ""),
        })

    result = _safe(lambda: llm.verify_company_analysis(batch), {}, "逐檔查證公司分析")
    written = 0
    for s in batch:
        a = result.get(s["code"])
        if not a or not a.get("company_desc"):
            continue
        srcs = [x for x in (a.get("sources") or []) if str(x).strip()]
        external = [x for x in srcs if "公開資訊觀測站申報值" not in str(x)]
        if not external:
            print(f"[verify] {s['code']} {s['name']}：沒有申報值以外的來源，略過不寫")
            continue
        db.upsert_stock_analysis(s["code"], s.get("name", ""), a["company_desc"],
                                 a.get("swot", {}), today, sources=srcs)
        written += 1

    if written:
        render.render_stock_analysis_json()
        render.render_stock_info()
    remaining = len(pending) - written
    print(f"[verify] 本批查證寫入 {written} / {len(batch_codes)} 檔"
          f"（尚待查證約 {remaining} 檔，每天 {limit} 檔）")


def _compact_bars(bars: list[dict]) -> list[list]:
    """[{"date","open","high","low","close","volume"}, ...] → [[d,o,h,l,c,v], ...]

    每根 K 棒從約 95 bytes 降到約 37 bytes（-61%）。全市場一輪從 57MB 降到約 22MB，
    使用者點開彈窗要下載的單檔也從 ~48KB 降到 ~19KB。stock-chart.js 讀到陣列型
    bars 會自己展開回物件（見該檔 expandBars）。
    """
    out = []
    for b in bars:
        try:
            out.append([b["date"], b["open"], b["high"], b["low"], b["close"], b.get("volume", 0)])
        except KeyError:
            continue
    return out


def snapshot_offmarket_history(codes: dict[str, str], cfg: dict) -> int:
    """把上櫃／興櫃個股的日 K 抓下來存成 docs/data/tpex_hist/<code>.json。
    瀏覽器對 tpex.org.tw 與 Yahoo 都沒有 CORS，stock-chart.js 只能靠這份快照
    畫上櫃／興櫃圖。上市股票不走這裡（前端能直接即時抓 TWSE STOCK_DAY）。

    資料源用 Yahoo（`fetchers/yahoo.py`），不是 TPEx，原因有兩個：
    1. **興櫃終於有真的開高低收**。TPEx 的 emerging/historical 只給日均價，
       只能畫「均價走勢」而且最新價對不上看盤軟體的成交欄（歷史事故：7686
       捷立康拿日均價 686.33 當股價，跟 TPEx 網站的成交價 802 對不起來）。
       Yahoo 對 7686 回的收盤就是 802，跟當日行情表一致。
    2. **快一個數量級**。TPEx 一檔要打 8 次（一次一個月），Yahoo 一次就給兩年，
       實測 0.1 秒／檔，全市場上櫃＋興櫃約 1250 檔跑完約 2 分鐘——所以不必再
       像以前那樣每天只補 120 檔、輪好幾天才補得完（剛掛牌的 7925 健生、
       7686 捷立康就是還沒輪到，彈窗才會顯示「查無股價資料」）。

    Yahoo 查不到的個股會退回 TPEx 原本那條路，不會因為單一資料源掛掉就整批開天窗。
    """
    from datetime import datetime, timedelta

    out_dir = Path(__file__).resolve().parent.parent / "docs" / "data" / "tpex_hist"
    out_dir.mkdir(parents=True, exist_ok=True)
    sw = cfg.get("stock_swot", {})
    limit = int(sw.get("snapshot_limit", 0)) or None      # 0／未設 = 不限，全市場都補
    fresh_hours = int(sw.get("snapshot_fresh_hours", 20))
    fresh_before = datetime.utcnow() - timedelta(hours=fresh_hours)

    # 興櫃「當日行情表」是一支 bulk API，成本很低：報買／報賣／日均價這些欄位
    # 只有 TPEx 有，Yahoo 沒有，所以留著當彈窗的補充報價列（不再當主要股價來源）。
    esb_pricing = _safe(tpex.fetch_esb_pricing, {}, "興櫃當日行情")

    done = fails = skipped = 0
    for code, name in list(codes.items()):
        if limit and done >= limit:
            break
        if not (code and code.isdigit() and len(code) == 4):
            continue
        fp = out_dir / f"{code}.json"
        market = ""
        if fp.exists():
            try:
                prev = json.loads(fp.read_text(encoding="utf-8"))
                market = prev.get("market", "")
                ts = datetime.fromisoformat(prev.get("updated", "2000-01-01T00:00:00"))
                # 已經是今天抓的真 OHLC 就跳過；舊的 TPEx 均價快照一律重抓，
                # 才會被 Yahoo 的真開高低收換掉。
                if (ts > fresh_before and prev.get("bars")
                        and prev.get("source") == "yahoo" and prev.get("cols")):
                    skipped += 1
                    continue
            except Exception:
                pass

        res = _safe(lambda: yahoo.fetch_daily_history(code, market or "tpex"),
                    {"bars": [], "meta": {}}, f"{code} Yahoo 日K")
        source, bars = "yahoo", res.get("bars") or []
        if not bars:                                   # Yahoo 沒有 → 退回 TPEx
            alt = _safe(lambda: tpex.fetch_offmarket_daily_history(code, months=8),
                        {"bars": [], "market": ""}, f"{code} 上櫃/興櫃歷史")
            bars, source = alt.get("bars") or [], "tpex"
            if bars:
                market = alt["market"]
        if not bars:
            fails += 1
            continue

        # 市場別：優先用既有的（company_profile 申報值），再用 Yahoo 的代號後綴推。
        if not market:
            market = "esb" if code in esb_pricing else (
                "tpex" if str(res.get("symbol", "")).endswith(".TWO") else "tpex")
        payload = {
            "code": code, "name": name or (res.get("meta") or {}).get("name", ""),
            "market": market, "source": source,
            "updated": datetime.utcnow().isoformat(timespec="seconds"),
            "cols": ["d", "o", "h", "l", "c", "v"],
            "bars": _compact_bars(bars),
        }
        # source=="yahoo" 時 bars 已經是真的開高低收，前端不需要再走「均價走勢」
        # 那條退路；latest 只當補充欄位（報買／報賣／日均價）。
        if code in esb_pricing:
            payload["latest"] = esb_pricing[code]
        fp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        done += 1

    print(f"[otc-hist] 上櫃／興櫃日K：新抓/更新 {done} 檔、沿用 {skipped} 檔、查無 {fails} 檔")
    return done


def run_period_facts(days: int = 7) -> None:
    """把「本期間站內已落地、且每一筆都帶日期」的事實列出來，給週報／月報
    Routine 當寫作錨點。

    為什麼需要這個：週報是雲端 Routine 用 WebSearch 寫的，搜尋結果不一定帶
    正確日期，實際踩過的坑是「大立光重新站回 5,000 元大關」被當成本週事件寫
    進報告，但那是好幾週前的事。站內 market_snapshots／themes／daily 這些
    資料每一筆本來就有日期，先把區間內的事實攤出來，Routine 就能拿它對照，
    而不是只靠搜尋結果的語氣判斷新舊。

    輸出到 stdout（Routine 直接讀），不寫檔、不進 git。
    """
    end = now_tpe().date()
    start = end - timedelta(days=days)
    snaps = db.snapshots_between(start.isoformat(), end.isoformat())

    print(f"=== 站內已落地事實：{start} ~ {end}（{days} 天）===")
    if not snaps:
        print("（這個區間沒有市場快照，可能是連假或系統還沒跑過盤後）")
    else:
        first, last = snaps[0], snaps[-1]
        o = first.get("taiex_close") or 0
        c = last.get("taiex_close") or 0
        print(f"\n[加權指數] 交易日 {len(snaps)} 天，{first['date']} 收 {o} → "
              f"{last['date']} 收 {c}"
              + (f"（{(c / o - 1) * 100:+.2f}%）" if o else ""))
        print("[逐日收盤]（每一筆都是站內當日盤後抓的，可以直接引用）")
        def _amt(v):
            # None ＝ 那天的法人資料還沒落地（TWSE 尚未公布），跟「0 億」完全
            # 是兩回事，要分開講，不要讓 Routine 把「沒資料」寫成「沒買賣超」。
            return f"{v:+.1f} 億" if v is not None else "（尚無資料）"

        for sn in snaps:
            print(f"  {sn['date']}  收 {sn.get('taiex_close')}  "
                  f"外資 {_amt(sn.get('foreign_net'))}  投信 {_amt(sn.get('trust_net'))}")
        have = [s for s in snaps if s.get("foreign_net") is not None]
        print(f"[法人合計]（只加總有資料的 {len(have)}/{len(snaps)} 天）"
              f" 外資 {sum(s.get('foreign_net') or 0 for s in have):+.1f} 億　"
              f"投信 {sum(s.get('trust_net') or 0 for s in have):+.1f} 億")

    print("\n[題材訊號]（last_signal_date 在區間內才算本期的事）")
    hit = False
    for t in db.list_themes("active") + db.list_themes("dormant"):
        lsd = t.get("last_signal_date") or ""
        if lsd >= start.isoformat():
            hit = True
            print(f"  {lsd}  {t['name']}（信心度 {t.get('confidence')}／"
                  f"判定 {t.get('verdict')}／累積更新 {t.get('update_count')} 次）")
    if not hit:
        print("  （區間內沒有新的題材訊號）")

    print("\n[逐檔查證過的個股]（有 sources 才列，其餘個股站內沒有查證過的公司分析）")
    ana = db.all_stock_analysis()
    verified = [(c, v) for c, v in ana.items() if v.get("sources")]
    if verified:
        for code, v in sorted(verified, key=lambda kv: kv[1].get("updated_at", ""), reverse=True)[:20]:
            print(f"  {v.get('updated_at', '')}  {code} {v.get('name', '')}")
    else:
        print("  （目前沒有帶查證來源的個股分析）")

    # 研究筆記：即時快訊監控（華爾街見聞）與使用者提交都寫在這張表，區間內的
    # 這些是「站內這段期間實際看過並判定過的訊息」，週報／月報要納入研究範圍，
    # 否則等於每週重新從零搜尋、抓過的東西完全沒被用到。
    print("\n[研究筆記]（即時快訊監控＋使用者提交，區間內）")
    notes = [n for n in db.list_research_notes()
             if (n.get("submitted_at") or "") >= start.isoformat()]
    if notes:
        by_verdict = {"verified": [], "conflicting": [], "unverified": []}
        for n in notes:
            by_verdict.setdefault(n.get("verified", "unverified"), []).append(n)
        label = {"verified": "已驗證（可以當事實引用）",
                 "conflicting": "與既有資料衝突（要講清楚衝突在哪，不要直接採信）",
                 "unverified": "無法驗證（**不可以當成事實寫進報告**，要嘛不寫，"
                               "要嘛自己查證後標明來源）"}
        for key in ("verified", "conflicting", "unverified"):
            rows = by_verdict.get(key) or []
            if not rows:
                continue
            print(f"  ── {label[key]}：{len(rows)} 筆")
            for n in rows[:15]:
                print(f"     {n.get('submitted_at')}  [{n.get('source', '')[:20]}] "
                      f"{(n.get('title') or '')[:46]}")
                if n.get("summary"):
                    print(f"        {n['summary'][:110]}")
    else:
        print("  （區間內沒有研究筆記）")

    print("\n⚠️ 寫報告時：上面每一筆都有日期，凡是要寫成「本期發生」的事，"
          "都要能對應到區間內的日期；WebSearch 查到但無法確認發生日期的事件，"
          "要嘛標明日期、要嘛不要寫成本期新聞。研究筆記裡標為『無法驗證』的，"
          "不可以直接當事實寫進報告。")


def _industry_context(industry: str, codes: list[str], profiles: dict,
                      revenue: dict, themes_by_code: dict, top_n: int = 25) -> tuple[str, dict]:
    """組出「某個產業的事實包」給 LLM，回傳 (context 文字, 統計)。

    重點在**只餵事實**：每家公司附的是公開資訊觀測站的申報主要經營業務原文
    ＋政府開放資料的月營收，不含任何本站判讀。LLM 的工作是在這些事實上做產業層
    綜合分析，而不是憑公司名稱想像它在做什麼——那正是先前寫出假資料的原因。
    """
    rows = []
    for c in codes:
        r = revenue.get(c, {})
        rows.append((c, r.get("revenue") or 0))
    rows.sort(key=lambda x: -x[1])

    total_rev = sum(v for _, v in rows) / 100000.0          # 千元 → 億
    with_rev = [c for c, v in rows if v]
    yoys = [revenue[c]["yoy"] for c in with_rev
            if revenue.get(c, {}).get("yoy") is not None]
    avg_yoy = sum(yoys) / len(yoys) if yoys else None

    lines = [
        f"公司家數：{len(codes)} 檔（其中 {len(with_rev)} 檔有月營收申報）",
        f"合計最新月營收：{total_rev:,.0f} 億元"
        + (f"，家數加權平均年增率 {avg_yoy:+.1f}%" if avg_yoy is not None else ""),
        "",
        f"依最新月營收排序的主要公司（最多 {top_n} 家）——"
        "「主要經營業務」為公開資訊觀測站申報原文，是這家公司在做什麼的權威依據：",
    ]
    for c, _ in rows[:top_n]:
        prof = profiles.get(c, {})
        r = revenue.get(c, {})
        name = prof.get("short_name") or r.get("name") or c
        amt = (r.get("revenue") or 0) / 100000.0
        yoy = r.get("yoy")
        lines.append(
            f"\n- {c} {name}｜月營收 {amt:,.1f} 億"
            + (f"、年增 {yoy:+.1f}%" if yoy is not None else "、無月營收資料")
            + f"\n  申報主要經營業務：{prof.get('business', '（無申報資料）')}"
        )

    rel_themes = sorted({t for c in codes for t in themes_by_code.get(c, [])})
    if rel_themes:
        lines += ["", "本站題材知識庫中與這個產業相關的題材："]
        lines += [f"- {t}" for t in rel_themes[:15]]

    stats = {"member_count": len(codes), "revenue_yi": round(total_rev, 1)}
    return "\n".join(lines), stats


def run_industry_reports() -> None:
    """各產業深度分析（申報產業別，37 類涵蓋全市場 2345 檔）。

    為什麼做這一層：個股層的公司判讀必須逐檔查證，全市場照 40 檔/天要兩個月；
    而產業只有 37 類，而且**每一檔股票一定屬於其中一類**，所以做完 37 份，
    等於每一檔個股都有可靠的產業脈絡可看，過程中完全不需要臆測個別公司
    （送進 LLM 的公司資訊全部是申報營業業務＋政府月營收）。

    每次跑 batch_size 類，refresh_days 天內做過的跳過，所以連跑幾天就會補滿，
    之後定期輪替更新。
    """
    cfg = load_config()
    ic = cfg.get("industry_report", {})
    batch = int(ic.get("batch_size", 6))
    refresh_days = int(ic.get("refresh_days", 90))
    min_members = int(ic.get("min_members", 3))
    today = today_str()

    profiles = db.all_company_profiles()
    revenue = db.latest_monthly_revenue()
    themes_by_code: dict[str, list[str]] = {}
    for t in db.list_themes_with_stocks():
        for st in t.get("stocks", []):
            code = str(st.get("code", "")).strip()
            if code:
                themes_by_code.setdefault(code, []).append(t["name"])

    by_industry: dict[str, list[str]] = {}
    for code, prof in profiles.items():
        ind = (prof.get("industry") or "").strip()
        if ind:
            by_industry.setdefault(ind, []).append(code)

    fresh = db.industry_reports_stale(refresh_days, today)
    # 家數多的先做：涵蓋到的個股最多，效益最高
    pending = sorted(((i, c) for i, c in by_industry.items()
                      if i not in fresh and len(c) >= min_members),
                     key=lambda kv: -len(kv[1]))
    if not pending:
        print(f"[industry] 全部 {len(by_industry)} 類產業都在 {refresh_days} 天內分析過了")
        return

    print(f"[industry] 待分析 {len(pending)} 類，本次做 {min(batch, len(pending))} 類")
    made = 0
    for industry, codes in pending[:batch]:
        context, stats = _industry_context(industry, codes, profiles, revenue, themes_by_code)
        report = _safe(lambda i=industry, c=context: llm.write_industry_report(i, c),
                       {}, f"產業分析 {industry}")
        if not report or not report.get("sections"):
            print(f"[industry] {industry}：產出失敗或內容為空，略過")
            continue
        sources = [x for x in (report.get("sources") or []) if str(x).strip()]
        if not sources:
            # 跟個股逐檔查證同一條規則：沒有查證來源就不寫
            print(f"[industry] {industry}：沒有查證來源，略過不寫")
            continue

        slug = render.slugify(industry)
        row = {
            "slug": slug, "date": today,
            "reported_at": now_tpe().strftime("%Y-%m-%d %H:%M:%S"),
            "title": report.get("title") or f"{industry}：產業深度分析",
            "summary": report.get("summary", ""),
            "sections": report.get("sections") or [],
            "leaders": report.get("leaders") or [],
            "chain": report.get("chain") or [],
            "risks": report.get("risks", ""),
            "outlook": report.get("outlook", ""),
            "sources": sources,
            **stats,
        }
        db.upsert_industry_report(industry, row)
        render.render_industry_report(industry, row)
        made += 1
        print(f"[industry] 已產出：{industry}（{stats['member_count']} 檔，"
              f"{stats['revenue_yi']:,.0f} 億，來源 {len(sources)} 個）")

    if made:
        render.render_industry_index()
        render.render_stock_info()      # 個股頁要帶上「所屬產業有分析」的連結
    done = len(db.all_industry_reports())
    print(f"[industry] 本次產出 {made} 類，累計 {done}/{len(by_industry)} 類")


def run_chart_snapshot() -> None:
    """全市場上櫃＋興櫃日K快照 → docs/data/tpex_hist/<code>.json。

    為什麼是獨立的命令、而且**不進 main 分支**：
    上櫃約 880 檔＋興櫃約 366 檔，每檔兩年日 K 約 48KB，全市場一輪就是 ~60MB，
    而且每個交易日都要重寫一次。放進 git 歷史一年會長到好幾百 MB。所以比照
    盤中資料的做法（`intraday.yml` → `intraday-data` 分支），這份快照推到
    **`chart-data` 分支並 force push、不留歷史**，`docs/data/tpex_hist/` 在
    main 分支是 gitignore 的。前端 stock-chart.js 直接從 raw.githubusercontent
    讀 chart-data 分支。

    上市股票不在這裡：前端能直接打 TWSE STOCK_DAY（那支有 CORS），是真即時資料。
    """
    cfg = load_config()
    codes: dict[str, str] = {}
    for c in _all_market_codes():
        if c.get("market") in ("tpex", "esb") and c.get("code"):
            codes[c["code"]] = c.get("name", "")
    if not codes:
        print("[chart] 取不到全市場清單，略過")
        return
    print(f"[chart] 全市場上櫃／興櫃 {len(codes)} 檔，開始抓 Yahoo 日K…")
    snapshot_offmarket_history(codes, cfg)
    _safe(snapshot_index_history, 0, "大盤／櫃買指數日K")


def snapshot_index_history() -> int:
    """加權指數與櫃買指數的日K → docs/data/tpex_hist/_index_<代號>.json。

    加權指數用 Yahoo ^TWII（實測序列完整）。**櫃買指數不能用 Yahoo ^TWOII**：
    實測最近一個多月的 open/high/low/close 全是 null，而且 meta 的
    regularMarketPrice(269.45) 跟 chartPreviousClose(440.1) 自相矛盾，是壞掉的
    序列。改用 TPEx 自己的 openapi `tpex_index`（權威值，但只回最近幾個交易日），
    逐日累加進快照，歷史會自己長出來——寧可一開始資料短，也不要顯示錯的指數。
    """
    out_dir = Path(__file__).resolve().parent.parent / "docs" / "data" / "tpex_hist"
    out_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime
    written = 0

    tw = _safe(lambda: yahoo.fetch_index_history(yahoo.TAIEX), {"bars": []}, "加權指數")
    if tw.get("bars"):
        (out_dir / "_index_TWII.json").write_text(json.dumps({
            "code": "TWII", "name": "加權指數", "market": "index", "source": "yahoo",
            "updated": datetime.utcnow().isoformat(timespec="seconds"),
            "cols": ["d", "o", "h", "l", "c", "v"],
            "bars": _compact_bars(tw["bars"]),
        }, ensure_ascii=False), encoding="utf-8")
        written += 1

    otc = _safe(tpex.fetch_index_daily, [], "櫃買指數")
    if otc:
        fp = out_dir / "_index_TPEX.json"
        bars: dict[str, dict] = {}
        if fp.exists():                      # 累加：openapi 一次只回最近幾天
            try:
                for b in json.loads(fp.read_text(encoding="utf-8")).get("bars", []):
                    if isinstance(b, list) and len(b) >= 6:   # 壓縮格式
                        b = {"date": b[0], "open": b[1], "high": b[2],
                             "low": b[3], "close": b[4], "volume": b[5]}
                    bars[b["date"]] = b
            except Exception:
                pass
        for b in otc:
            bars[b["date"]] = b
        fp.write_text(json.dumps({
            "code": "TPEX", "name": "櫃買指數", "market": "index", "source": "tpex",
            "updated": datetime.utcnow().isoformat(timespec="seconds"),
            "cols": ["d", "o", "h", "l", "c", "v"],
            "bars": _compact_bars([bars[k] for k in sorted(bars)]),
        }, ensure_ascii=False), encoding="utf-8")
        written += 1
        print(f"[chart] 櫃買指數累積 {len(bars)} 個交易日")
    return written


def run_evening() -> None:
    cfg = load_config()
    today = today_str()
    print(f"[evening] 產出台股盤後報告 {today}")
    watch_codes = {w["code"] for w in cfg["watchlist"]}

    market = _safe(twse.fetch_index_summary, {}, "大盤行情")
    # **報告的日期以資料為準，不是以執行時間為準。**
    # TWSE 的收盤資料集回的是「目前已公布的最新交易日」，不保證等於今天：
    #   - 週末／假日執行 → 拿到的是上一個交易日
    #   - 收盤後太早執行 → 當日資料還沒落地，拿到的是前一個交易日
    # 以前一律用 today_str() 當日期，於是 2026-09-05（週六）被存進一筆其實是
    # 09-04（週五）的收盤資料，整個評分頁標成「9月5日 週六」而數字停在週五，
    # 使用者直接反映「價格%數都卡在上禮拜五」。現在改成跟著資料走。
    # 報告日期一律以「資料本身的日期」為準，不是執行時間。TWSE 收盤資料集回的是
    # 「已公布的最新交易日」，週末/假日執行、或收盤後太早執行都會拿到前一個交易日。
    # market.date 缺席時（FMTQIK 掛了走 MI_INDEX 備援那條）退回「往前找最近的平日」，
    # 也不要用可能是週六的執行日。這樣只要盤後有跑，頁面日期就自動跟著交易日走，
    # 不用等人回報「日期卡在上禮拜」。
    data_date = (market.get("date") or "").strip()
    if not data_date:
        d = date.fromisoformat(today)
        while d.weekday() >= 5:            # 5=六 6=日
            d = d.fromordinal(d.toordinal() - 1)
        data_date = d.isoformat()
    if data_date != today:
        print(f"[evening] 報告日期以資料日 {data_date} 為準（執行日 {today}）")
        today = data_date
    inst = _safe(lambda: twse.fetch_institutional_net(date.fromisoformat(today)),
                 {}, "三大法人")
    if inst and inst.get("date") and inst["date"] != today:
        print(f"[evening] 三大法人資料日期是 {inst['date']}、不是今天 {today}，"
              f"不寫進今日快照（避免重複計算）")
        inst = {}
    quotes = _safe(twse.fetch_daily_quotes, [], "個股行情")
    tpex_quotes = _safe(tpex.fetch_daily_quotes, [], "上櫃個股行情")
    calls = _safe(lambda: mops.fetch_earnings_calls(), [], "法說會")
    inst_by_stock = _safe(lambda: twse.fetch_institutional_by_stock(), {}, "個股法人買賣超")
    tpex_inst_by_stock = _safe(tpex.fetch_institutional_by_stock, {}, "上櫃個股法人買賣超")
    inst_by_stock = {**inst_by_stock, **tpex_inst_by_stock}

    # 上市 + 上櫃合併成一份全市場清單，後面掃描/熱力圖/籌碼都吃這份
    all_quotes = quotes + tpex_quotes
    print(f"[evening] 上市 {len(quotes)} 檔、上櫃 {len(tpex_quotes)} 檔")

    # 落地當日全市場收盤/成交量（反正已經抓過，零額外成本），餵給下面的量能倍數查詢
    if not DRY_RUN:
        prices_db.save_quotes(all_quotes, today)

    def cached_history_fn(code: str, days: int) -> list[dict]:
        """量能倍數只需要成交量，優先查本地快取；快取天數不足才即時抓 TWSE（並寫回快取）。

        避免每天對幾十~上百檔候選股逐一打歷史 K 線 API——那是高波動日盤後執行
        拖到 8 分鐘以上的主因。快取每天靠 save_quotes() 多長一天，即時抓的檔數
        會隨快取累積而遞減。
        """
        # 「快取夠不夠」要看是誰在問，不能用一個固定門檻。
        # 量能倍數（scan_strong_stocks，days=25）只需要 ~21 天；
        # 起漲點雷達（scan_breakout_candidates，days=90）要 65 天以上才算得出
        # 月線／季線與 20 日均量。
        # 以前一律拿 11 當門檻，於是：強勢股掃描先跑 → fetch_stock_history(code, 25)
        # 截斷成 25 筆寫進快取 → 之後起漲點雷達要 90 天時，25 >= 11 成立、直接回那
        # 25 筆 → detect_fresh_breakout 需要 65 筆 → 永遠回「歷史資料不足」。
        # 「起漲點雷達 0 檔」每天都是 0 就是這樣來的（2026-09-07 實測：27 檔預篩
        # 候選裡有 16 檔卡在這個死區，快取剛好都是 25 筆）。
        if not DRY_RUN:
            need = 11 if days <= 30 else 65
            cached = prices_db.get_history(code, today, limit=days)
            if len(cached) >= need:
                return cached
        hist = twse.fetch_stock_history(code, days)
        if hist and not DRY_RUN:
            prices_db.save_history(code, hist)
        return hist

    # 漲跌家數：openapi 無現成資料集，由全個股行情自行統計
    if all_quotes:
        market = {**market, **twse.compute_breadth(all_quotes)}

    if market:
        db.save_market_snapshot(today, {**market, **inst})

    # 產業熱力圖：上市公司基本資料的產業別 + 全市場今日漲跌
    industry_map = _safe(twse.fetch_industry_map, {}, "產業分類")
    heatmap_rows = industry.aggregate_by_industry(all_quotes, industry_map) if industry_map else []
    if heatmap_rows:
        render.render_heatmap(heatmap_rows, render.date_label(today))

    # 籌碼儀表板：法人（大盤）+ 資券增減前 10 + 強勢股
    margin_twse = _safe(twse.fetch_margin_by_stock, {}, "融資融券（上市）")
    margin_tpex = _safe(tpex.fetch_margin_by_stock, {}, "融資融券（上櫃）")
    margin_all = {**margin_twse, **margin_tpex}
    quotes_by_code_all = {q["code"]: q for q in all_quotes}
    margin_top = []
    for code, m in margin_all.items():
        q = quotes_by_code_all.get(code)
        if q and m.get("margin_change"):
            margin_top.append({**m, "code": code, "name": q.get("name", ""),
                               "market": q.get("market", "twse")})
    margin_top.sort(key=lambda x: abs(x["margin_change"]), reverse=True)

    # 第一層：強勢股掃描（兩段式：先零成本過濾，再對候選抓歷史算量能）
    # 歷史優先查快取（見 cached_history_fn）；上櫃在快取沒累積起來前仍會即時抓，
    # 但抓不到 TWSE 歷史時 volume_ratio 留 None，不影響篩選
    strong = screener.scan_strong_stocks(all_quotes, cfg, cached_history_fn)
    print(f"[evening] 強勢股 {len(strong)} 檔")

    # 「今日漲幅榜」＝單純照漲幅排，不做任何量能篩選。
    # 為什麼要另外存一份：scan_strong_stocks() 會用 min_volume_ratio 濾掉
    # 「漲停但量沒爆」的股票，最後又用「量能倍數 × 漲幅」排序——那是動能排序，
    # 不是漲幅排序。籌碼頁卻把它標成「今日漲幅前段」，於是 2026-09-04 的
    # 2426 鼎元（+10.00%、成交值 62 億）、2327 國巨*（+9.98%、318 億）
    # 這種真正的漲停股完全不在榜上，使用者比對後判定「資料是錯的」。
    # 篩掉低價與極低量只是為了排除無法實際成交的雜訊，不動排序邏輯。
    gainers = sorted(
        (q for q in all_quotes
         if q.get("change_pct") is not None
         and (q.get("close") or 0) >= 10
         and (q.get("turnover") or 0) >= 10_000_000),
        key=lambda q: -q["change_pct"])[:20]
    print(f"[evening] 今日漲幅榜 {len(gainers)} 檔"
          + (f"（第一名 {gainers[0]['code']} {gainers[0]['change_pct']:+.2f}%）" if gainers else ""))

    # 起漲點雷達：跟強勢股掃描分開跑，門檻故意放低，抓「剛突破＋爆量」
    # 而不是「已經漲很多」——同樣靠 cached_history_fn 省 API
    breakout_candidates = _safe(
        lambda: screener.scan_breakout_candidates(all_quotes, cfg, cached_history_fn),
        [], "起漲點雷達")
    print(f"[evening] 起漲點雷達 {len(breakout_candidates)} 檔")

    # 新掛牌觀察：上市/上櫃基本資料本來就要抓（產業分類用），多拿一個欄位不用額外成本；
    # 興櫃是更早期的階段，資料集跟行情機制都跟上市/上櫃不同，額外抓一份，
    # 但故意不併進 quotes_by_code_all（興櫃是議價/搓合市場，混進熱力圖／
    # 強勢股掃描會失真），只用來查這裡要顯示的個股
    esb_quotes = _safe(tpex.fetch_esb_quotes, {}, "興櫃行情")
    listing_dates = {
        **_safe(twse.fetch_listing_dates, {}, "上市日期"),
        **_safe(tpex.fetch_listing_dates, {}, "上櫃日期"),
        **_safe(tpex.fetch_esb_listing_dates, {}, "興櫃掛牌日期"),
    }
    new_listings = screener.find_new_listings(
        {**quotes_by_code_all, **esb_quotes}, listing_dates, cfg["new_listing"]["days"])
    print(f"[evening] 新掛牌觀察 {len(new_listings)} 檔")

    # 題材目錄補齊：見 process_catalog_batch() 說明；每天的例行報告只處理一批，
    # 控制 LLM 成本，全部補齊需要好幾天（或用一次性回填腳本跑好幾批）
    process_catalog_batch(cfg, today, quotes_by_code_all)
    process_catalog_deep_dives(cfg, today)
    # 個股資料頁只放「事實」：公司基本資料（公開資訊觀測站 t05st03 申報值）
    # 與月營收（政府開放資料），兩者都不經 LLM，全市場照抓沒問題。
    _safe(lambda: sync_company_profiles(
        today, cfg.get("stock_swot", {}).get("profile_limit", 200)), 0, "公司基本資料")
    _safe(lambda: sync_monthly_revenue(today), 0, "全市場月營收")
    # ⚠️ 不再做「全市場個股 SWOT」批次回填。2026-09-06 使用者要求：沒有逐檔
    #    查證的判讀就不要放上站。company_swot_batch 那條路是「以申報營業項目
    #    為底＋推論」，對冷門股仍可能寫錯，不是查證過的事實。個股的公司介紹
    #    ／SWOT 只在評分頁焦點股（stock_analysis_batch，有訊號脈絡）與逐檔
    #    人工查證過的個股上出現，其餘個股彈窗只顯示基本資料＋月營收＋題材。

    holder_codes = {q["code"] for q in strong} | watch_codes
    holder_concentration = _safe(lambda: tdcc.fetch_holder_concentration(holder_codes),
                                 {}, "集保股權分散表")
    holders = sorted(
        ({"code": c, "name": quotes_by_code_all.get(c, {}).get("name", ""), "pct": p}
         for c, p in holder_concentration.items()),
        key=lambda x: x["pct"], reverse=True,
    )
    if heatmap_rows or margin_top or strong:
        # 三大法人「個股」買賣超排行：T86 的分項欄位（外資／投信／自營商各自
        # 的買超前 10、賣超前 10）。大盤合計本來就有，但看不出是誰在買哪一檔。
        inst_detail = _safe(twse.fetch_institutional_detail_by_stock, {},
                            "個股三大法人買賣超明細")
        inst_rank = render.build_inst_rank(inst_detail) if inst_detail else {}
        render.render_chips(inst, margin_top[:10], strong[:10], holders,
                            render.date_label(today), inst_rank=inst_rank,
                            gainers=gainers)

    # 第二層：題材聚類（含孤立訊號分流）
    # 帶上題材目錄的既有名稱，讓 LLM 優先套用目錄裡的名字而不是自己發明相似的新名，
    # 這樣目錄題材才有機會在真的被偵測到訊號時直接轉入「追蹤中」
    call_context = "\n".join(f"{c['code']} {c['name']} 法說會：{c['note']}" for c in calls)
    known_theme_names = _safe(db.catalog_theme_names, [], "題材目錄名單")
    clustered = _safe(lambda: llm.cluster_themes(strong, call_context, known_theme_names),
                      {"themes": [], "orphans": []}, "題材聚類")

    themes_raw = clustered.get("themes", [])
    orphans = clustered.get("orphans", [])

    # 第三層：寫進題材知識庫（有就更新、沒有才新建）
    themes_view = []
    for t in themes_raw:
        # 相關個股當日法人買賣超合計（張），給深度報告時間軸佐證籌碼方向
        theme_inst = round(sum(
            inst_by_stock.get(s.get("code", ""), 0) for s in t.get("stocks", [])
        ) / 1000, 1)
        theme_id = db.upsert_theme(
            name=t["name"], summary=t.get("summary", ""),
            confidence=t.get("confidence", "mid"), verdict=t.get("verdict", "unknown"),
            related_stocks=t.get("stocks", []), today=today,
            note=t.get("reasoning", ""), inst_net=theme_inst,
        )
        stored = db.get_theme(t["name"]) or {}
        view = render.decorate_theme({**stored, **t})
        view["stocks"] = t.get("stocks", [])
        view["tracked_days"] = stored.get("update_count", 1)
        themes_view.append(view)

        # 判斷快照：現在存下來，14/30 天後才能回頭驗證
        for stock in t.get("stocks", []):
            q = next((x for x in all_quotes if x["code"] == stock.get("code")), None)
            if q:
                db.save_judgment(today, stock["code"], stock.get("name", ""),
                                 t["name"], t.get("confidence", "mid"),
                                 "theme_pick", q["close"], market.get("taiex_close", 0))

    # 黑馬：不套題材，走獨立風險標記（先補上孤立訊號個股的量能倍數）
    quotes_by_code = {q["code"]: q for q in all_quotes}
    orphan_quotes = [quotes_by_code[o["code"]] for o in orphans
                     if o.get("code") in quotes_by_code]
    screener.attach_volume_ratio(orphan_quotes, cached_history_fn)
    dark_horses = screener.identify_dark_horses(orphans, quotes_by_code, cfg)
    for dh in dark_horses:
        db.save_judgment(today, dh["code"], dh["name"], "", "",
                         "dark_horse", dh.get("close", 0), market.get("taiex_close", 0))

    # 技術分析：只對入選個股跑，省算力
    # 題材聚類／黑馬都是 LLM 產物，萬一那次呼叫失敗（例如 JSON 解析錯），兩者都會是空的；
    # 用強勢股清單當底，技術面／評分才不會整個開天窗
    # 評分候選池：動能強勢股之外，也要納入「今日真正漲最多的」。
    # 只用 strong 的話，因為它被量能門檻縮得很窄，評分頁天天都是同一批名字
    # （使用者回報「怎麼感覺都是這幾檔」）。
    candidates = {s["code"]: s["name"] for s in strong}
    candidates.update({g["code"]: g["name"] for g in gainers[:10]})
    candidates.update({s["code"]: s["name"] for t in themes_raw for s in t.get("stocks", [])})
    candidates.update({dh["code"]: dh["name"] for dh in dark_horses})

    # 起漲點雷達／新掛牌股一定要有技術面＋後面的公司介紹／SWOT，不能因為候選股
    # 數量上限（見下面 [:12]）被排擠掉——這兩份名單本身就故意抓得少，全部保留
    priority_codes = {b["code"]: b["name"] for b in breakout_candidates}
    priority_codes.update({n["code"]: n["name"] for n in new_listings})

    technicals = []
    ranked_codes = [c for c in candidates if c not in priority_codes][:12]
    for code in list(priority_codes) + ranked_codes:
        name = priority_codes.get(code) or candidates[code]
        hist = _safe(lambda c=code: twse.fetch_stock_history(c, cfg["technical"]["lookback_days"]),
                     [], f"{code} 歷史股價")
        result = technical.analyze_stock(code, hist, cfg)
        result.update({"name": f"{code} {name}", "is_watchlist": code in watch_codes})
        technicals.append(result)
    technicals.sort(key=lambda x: not x["is_watchlist"])

    # 五面向評分：技術/籌碼/基本/題材四軸規則計算；新聞面對同一份候選名單抓標題、
    # 一次 LLM 呼叫批次判讀（不是逐股呼叫），成本跟著候選股數量线性但可控
    revenue_yoy = _safe(twse.fetch_revenue_yoy, {}, "月營收年增率")
    theme_conf_by_code = {s.get("code"): t.get("confidence")
                          for t in themes_raw for s in t.get("stocks", [])}
    news_input = [
        {"code": t["code"], "name": t["name"].split(" ", 1)[-1],
         "headlines": _safe(lambda c=t["code"], n=t["name"]: stock_news.fetch_stock_headlines(c, n),
                            [], f"{t['code']} 新聞標題")}
        for t in technicals
    ]
    news_scores = _safe(lambda: llm.news_sentiment_batch(news_input), {}, "新聞面評分")

    score_rows = []
    for t in technicals:
        code = t["code"]
        margin_change = margin_all.get(code, {}).get("margin_change")
        s = scoring.score_stock(
            grade=t.get("grade"),
            inst_net=inst_by_stock.get(code),
            margin_change=margin_change,
            revenue_yoy=revenue_yoy.get(code),
            theme_confidence=theme_conf_by_code.get(code),
            news=news_scores.get(code),
        )
        score_rows.append({**s, "code": code, "name": t["name"]})
    score_rows.sort(key=lambda x: (x["composite"] is None, -(x["composite"] or 0)))

    # 個股深度分析：評分頁前幾名補上公司介紹＋SWOT＋漲跌原因，不等系統累積足夠訊號才做
    theme_name_by_code = {s.get("code"): t["name"]
                          for t in themes_raw for s in t.get("stocks", [])}
    headlines_by_code = {n["code"]: n["headlines"] for n in news_input}
    grade_by_code = {t["code"]: t.get("grade", {}) for t in technicals}
    # 起漲點雷達／新掛牌股一定要有公司介紹＋SWOT，不受排名前 8 名這個上限限制
    must_analyze = [r for r in score_rows if r["code"] in priority_codes]
    ranked_analysis = [r for r in score_rows if r["code"] not in priority_codes][:8]
    analysis_input = []
    for r in must_analyze + ranked_analysis:
        code = r["code"]
        grade = grade_by_code.get(code, {})
        signals = "、".join(grade.get("notes", [])) or grade.get("label", "")
        m = margin_all.get(code, {})
        chip_parts = []
        net = inst_by_stock.get(code)
        if net:
            chip_parts.append(f"三大法人合計{'買超' if net >= 0 else '賣超'}{abs(net) / 1000:.0f}張")
        if m.get("margin_change"):
            chip_parts.append(f"融資{'增加' if m['margin_change'] >= 0 else '減少'}"
                              f"{abs(m['margin_change']) / 1000:.0f}張")
        analysis_input.append({
            "code": code, "name": r["name"].split(" ", 1)[-1],
            "signals": signals,
            "chip_note": "；".join(chip_parts),
            "revenue_yoy": revenue_yoy.get(code),
            "theme": theme_name_by_code.get(code),
            "headlines": headlines_by_code.get(code, []),
        })
    stock_analyses = _safe(lambda: llm.stock_analysis_batch(analysis_input), {}, "個股深度分析")
    for r in score_rows:
        if r["code"] in stock_analyses:
            r["analysis"] = stock_analyses[r["code"]]

    if score_rows:
        render.render_scores(score_rows, render.date_label(today))

    # 處置股預警：官方公布的處置中／接近門檻／今日新注意，直接轉譯不重寫規則引擎
    disposition = _safe(twse.fetch_disposition_stocks, [], "處置股票")
    attention_trending = _safe(twse.fetch_attention_trending, [], "注意累計接近門檻")
    attention_today = _safe(twse.fetch_attention_today, [], "今日新注意股票")
    if disposition or attention_trending or attention_today:
        render.render_disposition(disposition, attention_trending, attention_today,
                                  render.date_label(today))

    # 自選股命中，置頂
    hits = screener.watchlist_hits(themes_raw, dark_horses, cfg["watchlist"])

    # 給評論用的補充資料：值得關注的個股（評分前 5）、處置/注意概況、國際題材對照
    watch_stocks = [
        {"code": r["code"], "name": r["name"], "composite": r["composite"],
         "technical": r["technical"], "chip": r["chip"], "fundamental": r["fundamental"],
         "theme": r["theme"]}
        for r in score_rows[:5] if r["composite"] is not None
    ]
    disposition_summary = {
        "in_disposition": [f"{d['code']} {d['name']}" for d in disposition],
        "approaching": [f"{t['code']} {t['name']}：{t['note']}" for t in attention_trending],
    }
    intl_themes = [{"name": t["name"], "summary": t.get("summary", "")}
                  for t in db.list_themes("active") if t.get("scope") == "intl"]

    commentary = _safe(
        lambda: llm.market_commentary({
            "market": market, "institutional": inst, "themes": themes_raw,
            "watch_stocks": watch_stocks, "disposition_summary": disposition_summary,
            "intl_themes": intl_themes, "type": "evening",
        }),
        "", "盤後評論")

    ctx = {
        "report_kind": "盤後 · 每日市場摘要",
        "date_label": render.date_label(today),
        "market": market, "inst": inst,
        "watchlist_hits": hits,
        "themes": themes_view,
        "dark_horses": dark_horses,
        "technicals": technicals,
        "earnings_calls": calls,
        "commentary": commentary,
        "watch_stocks": watch_stocks,
        "disposition_count": len(disposition) + len(attention_trending),
        "breakout_candidates": breakout_candidates,
        "new_listings": new_listings,
    }

    path = render.render_daily(ctx, f"{today}-evening")

    # 三個選股訊號另外落地成 picks.json，「選股雷達」頁才有固定入口可看
    _safe(lambda: render.save_picks(breakout_candidates, new_listings, dark_horses,
                                    render.date_label(today)), None, "選股雷達資料")

    # 上櫃／興櫃個股的日 K 後端快照：瀏覽器對 tpex.org.tw 沒有 CORS，stock-chart.js
    # 抓不到即時資料時會退而讀 docs/data/tpex_hist/<code>.json。只快照會出現在
    # 選股雷達／評分頁的上櫃興櫃代號，數量有限。
    # 先排今天出現在網站上的（新掛牌／黑馬／起漲點），再輪其餘全市場上櫃興櫃，
    # 每次補一批，久了每檔上櫃興櫃個股都會有 K 線可看。
    # 全市場那一輪已經交給 .github/workflows/chart-data.yml（每天 17:30 推到
    # chart-data 分支）——快照檔在 main 是 gitignore 的，盤後在這裡重抓一次
    # 只是白花 3 分鐘、結果也不會被 commit。這裡只補「今天真的出現在網站上」
    # 的少數上櫃／興櫃（新掛牌／黑馬／起漲點），讓盤後當下就看得到圖，
    # 不必等晚上那支 workflow。
    otc_codes = {n["code"]: n["name"] for n in new_listings}
    otc_codes.update({dh["code"]: dh["name"] for dh in dark_horses})
    otc_codes.update({b["code"]: b.get("name", "") for b in breakout_candidates})
    _safe(lambda: snapshot_offmarket_history(otc_codes, cfg), 0, "上櫃／興櫃歷史K線")

    # 題材生命週期：退場機制
    lc = cfg["theme_lifecycle"]
    changed = db.apply_theme_lifecycle(today, lc["dormant_after_days"],
                                       lc["archive_after_declines"])
    if changed["dormant"] or changed["archived"]:
        print(f"[evening] 題材狀態更新：{changed}")

    # 深度報告：只有夠格的題材才動用重量級分析
    for theme in db.themes_ready_for_deep_dive(lc["deep_dive_min_days"]):
        if theme.get("deep_dive_slug"):
            continue
        timeline = db.get_theme_timeline(theme["id"])
        article = _safe(lambda t=theme, tl=timeline: llm.write_deep_dive(t, tl),
                        {}, f"深度報告 {theme['name']}")
        if article:
            slug = render.slugify(theme["name"])
            supply_chain = _safe(lambda t=theme: llm.supply_chain_structure(t),
                                 {}, f"供應鏈結構 {theme['name']}")
            if supply_chain:
                db.save_supply_chain(theme["id"], supply_chain, today)
            render.render_article(theme, article, slug, supply_chain)
            db.set_deep_dive_slug(theme["id"], slug)
            print(f"[evening] 深度報告已產出：{theme['name']}")

    render.render_site()
    print(f"[evening] 完成：{path}")
    # commentary 是 LLM 寫的；LLM 不可用時（沒有 API key、雲端 session 卡住）它是空字串，
    # 那樣推播出去就只有一行標題，等於在洗版。改用手上現成的規則計算數字組一段
    # 摘要——這些全是申報值／官方行情，不是推論，沒有 LLM 也講得出當天發生什麼事。
    send_notification(f"盤後報告已產出：{render.date_label(today)}",
                      commentary or _evening_fallback_body(market, gainers, strong, score_rows))


def _evening_fallback_body(market: dict, gainers: list[dict], strong: list[dict],
                           score_rows: list[dict]) -> str:
    """LLM 沒產出評論時的推播內文，全部由當日行情直接算出來。

    刻意只寫「事實」：指數、漲跌家數、漲幅榜、評分最高的幾檔。不做任何解讀
    （為什麼漲、後續看法），因為那需要查證過的依據——沒有 LLM 就沒有那一層，
    寧可少講也不要猜。
    """
    lines: list[str] = []
    close = market.get("taiex_close")
    if close:
        chg = market.get("taiex_change") or 0
        pct = market.get("taiex_change_pct") or 0
        lines.append(f"加權指數 {close:,.0f}（{chg:+.0f}／{pct:+.2f}%）"
                     + (f"，成交值 {market['turnover'] / 100000000:,.0f} 億"
                        if market.get("turnover") else ""))
    adv, dec = market.get("advancers"), market.get("decliners")
    if adv or dec:
        lines.append(f"上漲 {adv or 0} 檔、下跌 {dec or 0} 檔")
    if gainers:
        top = "、".join(f"{g['code']} {g['name']} {g['change_pct']:+.2f}%"
                        for g in gainers[:5])
        lines.append(f"漲幅榜：{top}")
    if strong:
        lines.append(f"量能配合的強勢股 {len(strong)} 檔")
    scored = [r for r in score_rows if r.get("composite") is not None][:3]
    if scored:
        top = "、".join(f"{r['code']} {r['name'].split(' ', 1)[-1]}（{r['composite']:.1f}）"
                       for r in scored)
        lines.append(f"五面向評分最高：{top}")
    if not lines:
        return ""
    lines.append("（本則為當日行情統計，未含個股解讀）")
    return "\n".join(lines)


# ── 月報：含事後驗證 ───────────────────────────────────
def run_monthly() -> None:
    cfg = load_config()
    today = today_str()
    print(f"[monthly] 產出月報 {today}")

    def price_fn(code: str, _d: str) -> float | None:
        hist = twse.fetch_stock_history(code, 5)
        return hist[-1]["close"] if hist else None

    def index_fn(_d: str) -> float | None:
        return twse.fetch_index_summary().get("taiex_close")

    summary = _safe(
        lambda: review.run_review(today, cfg["review"]["horizons_days"], price_fn, index_fn),
        {}, "事後驗證")

    lines = []
    for horizon, data in summary.items():
        lines.append(f"【{horizon} 天回顧】{review.format_scorecard(data['scorecard'])}")
    verdict_text = "\n".join(lines) or "尚無足夠驗證樣本。"

    # 月報涵蓋「剛結束的上一個completed月」（月營收次月 10 日前才公告完畢）
    first_of_month = now_tpe().date().replace(day=1)
    target_month = (first_of_month - timedelta(days=1)).strftime("%Y-%m")

    path = _safe(lambda: render.render_monthly(target_month, summary), None, "月報頁面")
    if path:
        print(f"[monthly] 完成：{path}")
    print(f"[monthly] {verdict_text}")
    render.render_site()
    send_notification(f"{target_month} 月報已產出", verdict_text)


# ── 假日功課 ───────────────────────────────────────────
def run_holiday() -> None:
    cfg = load_config()
    today = today_str()
    print(f"[holiday] 產出假日功課 {today}")

    themes = [render.decorate_theme(t) for t in db.list_themes("active")]
    upcoming = _safe(lambda: mops.fetch_upcoming_calls(7), [], "下週法說會")

    ctx = {
        "report_kind": "假日功課",
        "date_label": render.date_label(today),
        "themes": themes,
        "earnings_calls": upcoming,
        "commentary": (f"目前追蹤中題材 {len(themes)} 個。"
                       f"下一個交易日為 {next_trading_day()}。"),
    }

    # 長假（如農曆年）開紅盤前一日：彙整休市期間國際盤逐日變化，避免開盤被跳空嚇到
    closed_days = consecutive_closed_days()
    if is_last_day_before_reopen() and closed_days > 2:
        recap = _safe(lambda: international.fetch_closure_recap(closed_days),
                      {"rows": [], "cumulative": {}}, "假期國際盤彙整")
        if recap.get("rows"):
            ctx["report_kind"] = "假期功課 · 長假國際盤彙整"
            ctx["closure_recap"] = recap
            ctx["closure_days"] = closed_days

    path = render.render_daily(ctx, f"{today}-holiday")
    render.render_site()
    print(f"[holiday] 完成：{path}")


# ── 題材目錄匯入（THEMES.md → 題材知識庫，status='catalog'） ─────
def run_import_catalog() -> None:
    import re as _re

    today = today_str()
    md_path = Path(__file__).resolve().parent.parent / "THEMES.md"
    if not md_path.exists():
        print("[catalog] 找不到 THEMES.md，略過")
        return

    text = md_path.read_text(encoding="utf-8")
    sections = _re.split(r"\n## ", text)
    entries = []
    for sec in sections[1:]:
        title_line, _, body = sec.partition("\n")
        title_line = title_line.strip()
        if title_line.startswith("待補") or title_line.startswith("待辦") or title_line.startswith("現況"):
            continue
        category = title_line
        for line in body.splitlines():
            line = line.strip()
            if not line.startswith("|") or line.startswith("|---") or "一句話論點" in line:
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 4:
                continue
            _, name, thesis, _status = cells[0], cells[1], cells[2], cells[3]
            if name and name != "題材":
                entries.append({"name": name, "category": category, "thesis": thesis})

    result = db.import_theme_catalog(entries, today)
    print(f"[catalog] 匯入完成：新增 {result['added']}、略過（已存在）{result['skipped']}，"
         f"共解析 {len(entries)} 筆")
    render.render_site()


# ── 使用者研究提交（GitHub Issue → 分析 → 回寫題材庫）─────────
# 靜態網站沒有後端，「從網頁上傳」的路是：submit.html 引導使用者建立一個
# 貼標籤 research-submission 的 GitHub Issue（他們本來就是 repo owner，
# 不用額外帳號系統）。這裡定期（人工或排程）掃還沒處理的 issue，逐篇分析、
# 嚴格驗證後才回寫題材庫，最後留言告知結果並關閉 issue。
RESEARCH_LABEL = "research-submission"


def _issues_from_file(env_var: str) -> list[dict] | None:
    """雲端沙盒沒有 `gh`（也連不出去），改由 GitHub Actions 先把 issue 撈成 JSON
    commit 進 repo，Routine 端設環境變數指過來。回 None＝沒設，交回原本的 gh 流程。"""
    path = os.environ.get(env_var, "").strip()
    if not path:
        return None
    try:
        data = json.loads(open(path, encoding="utf-8").read() or "[]")
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception as exc:
        print(f"[research] 讀取 {env_var} 指定的 issue 檔失敗：{exc}")
        return []


def _gh_issue_list() -> list[dict]:
    pre = _issues_from_file("RESEARCH_ISSUES_FILE")
    if pre is not None:
        return pre
    try:
        out = subprocess.run(
            ["gh", "issue", "list", "--label", RESEARCH_LABEL, "--state", "open",
             "--json", "number,title,body,url"],
            capture_output=True, text=True, check=True, timeout=30,
        )
        return json.loads(out.stdout or "[]")
    except Exception as exc:
        print(f"[research] 讀取 GitHub Issue 失敗（可能沒裝 gh 或未登入）：{exc}")
        return []


TOPIC_LABEL = "topic-request"


def _gh_topic_issue_list() -> list[dict]:
    """讀「點播深度主題」的 Issue（提交研究頁的第三種模式）。"""
    pre = _issues_from_file("RESEARCH_TOPIC_ISSUES_FILE")
    if pre is not None:
        return pre
    try:
        out = subprocess.run(
            ["gh", "issue", "list", "--label", TOPIC_LABEL, "--state", "open",
             "--json", "number,title,body,url"],
            capture_output=True, text=True, check=True, timeout=30,
        )
        return json.loads(out.stdout or "[]")
    except Exception as exc:
        print(f"[topic] 讀取 GitHub Issue 失敗（可能沒裝 gh 或未登入）：{exc}")
        return []


TOPIC_PREFIX = "[主題點播]"
TOPIC_NOTIFY_PATH = "docs/_notify_topic.json"

# 使用者提交頁是公開的，什麼都可能被貼進來（廣告、閒聊、其他領域的問題、想操縱
# LLM 的指令）。在丟進 LLM 之前先用關鍵字粗篩一層，明顯跟台股／投資無關的直接
# 擋掉，省一次 LLM 呼叫、也不讓雜訊進資料庫。LLM 那邊還有 relevant 欄位做第二道。
_INVEST_HINTS = (
    "投資", "股", "台股", "上市", "上櫃", "興櫃", "加權", "大盤", "指數", "盤中", "盤後",
    "開盤", "收盤", "財報", "營收", "年增", "月增", "eps", "毛利", "法人", "外資", "投信",
    "自營", "籌碼", "除權", "除息", "董事會", "股利", "減資", "增資", "產業", "供應鏈",
    "題材", "概念股", "龍頭", "訂單", "產能", "出貨", "報價", "半導體", "晶片", "晶圓",
    "封測", "ic設計", "記憶體", "面板", "pcb", "載板", "cowos", "矽光子", "cpo", "散熱",
    "伺服器", "機器人", "電動車", "生技", "航運", "金融", "鋼鐵", "塑化", "綠能", "重電",
    "etf", "期貨", "選擇權", "權證", "券商", "融資", "融券", "本益比", "殖利率",
    "聯準會", "央行", "升息", "降息", "利率", "通膨", "cpi", "gdp", "匯率", "台幣",
    "美元", "美股", "美債", "那斯達克", "費半", "道瓊", "標普", "關稅", "財政部",
    "金管會", "證交所", "櫃買", "護國神山", "台積", "聯發科", "鴻海", "nvidia", "tsmc",
    "stock", "share", "invest", "equity", "earnings", "revenue", "semiconductor",
    "nasdaq", "s&p", " fed ", "inflation", "tariff", "bond yield",
)
_STOCK_CODE_RE = re.compile(r"(?<!\d)[1-9]\d{3}(?!\d)\s*[（(]?\s*[一-鿿]{1,4}")


_STOCK_NAMES_CACHE: set[str] | None = None


def _known_stock_names() -> set[str]:
    """全市場公司的簡稱＋全名（去掉「股份有限公司」尾綴）。

    點播主題常常就是「金居」「仁新」「台積」這種 2～3 字的公司簡稱——比關鍵字
    清單還前面就被 `len < 4` 擋掉了（2026-09-07 實際發生：金居 8358、仁新 6696
    兩筆表單提交都被粗篩誤殺）。這份名單讓「輸入是一個真實個股名」也算數。
    """
    global _STOCK_NAMES_CACHE
    if _STOCK_NAMES_CACHE is None:
        names: set[str] = set()
        for code, prof in (_safe(db.all_company_profiles, {}, "公司名單") or {}).items():
            names.add(code)
            for key in ("short_name", "full_name"):
                v = (prof.get(key) or "").strip()
                if v:
                    names.add(v)
                    names.add(v.replace("股份有限公司", "").replace("*", "").strip())
        _STOCK_NAMES_CACHE = {n for n in names if n}
    return _STOCK_NAMES_CACHE


def _looks_investment_related(text: str) -> bool:
    """粗篩：這段文字看起來跟台股／投資有沒有關。

    寧可放寬（沾到邊就算 True），真正的把關交給 LLM 的 relevant 欄位；這裡只擋
    「一個投資關鍵字都沒有」的明顯雜訊。
    """
    t = (text or "").strip().lower()
    if not t:
        return False
    if any(h in t for h in _INVEST_HINTS):
        return True
    if _STOCK_CODE_RE.search(text or ""):
        return True
    # 輸入本身就是一個真實個股名稱（例如「金居」「仁新」）：整串精準命中，
    # 或整串是某家公司全名的前綴（「台積」→「台積電」）。不做反向 substring
    # 比對，否則「買樂透明牌」會因為某公司名含「樂透」而誤判。
    raw = (text or "").strip()
    if 2 <= len(raw) <= 10:
        names = _known_stock_names()
        if raw in names or any(nm.startswith(raw) for nm in names if len(nm) >= len(raw)):
            return True
    # 走到這裡：不是關鍵字、不是股號、也不是已知個股名 → 當雜訊擋掉
    # （跟改動前的行為一致；真正的把關是 LLM 的 relevant 欄位）
    return False


def _topic_notify_text(r: dict) -> str:
    """點播報告的通知內容：主題 ＋ 一句總結 ＋ 相關個股。

    以前這裡把整份報告（各段重點）都塞進 Discord，太長。改成只放摘要＋個股，
    完整內容看 PDF——`_write_topic_notify()` 會把 `url` 設成 PDF，daily-notify.yml
    會在結尾補「完整報告：<PDF 連結>」。
    """
    # 標題由 daily-notify.yml 以 **粗體** 顯示（來自 notify payload 的 title），
    # 這裡不再重複，只放摘要＋個股＋來源數。
    row = r["row"]
    parts = []
    if row.get("summary"):
        parts.append(" ".join(str(row["summary"]).split())[:320])
    stocks = row.get("stocks") or []
    if stocks:
        parts += ["", "**相關個股**：" + "、".join(
            f"{x.get('code','')} {x.get('name','')}" for x in stocks[:8])]
    if r.get("sources"):
        parts.append(f"\n查證來源／出處 {len(r['sources'])} 個")
    return "\n".join(parts).strip()


def _write_topic_notify(entries: list[dict]) -> None:
    """點播報告的推播內容 → docs/_notify_topic.json（**獨立於早報那個通道**）。

    刻意不共用 `_notify_payload.json`：那是早報／盤後的檔案，內容會被當天的
    例行報告覆蓋，而且 send_notification() 結尾固定接「完整報告：站台首頁」，
    點播通知連過去根本不是使用者要的那份報告。格式是陣列，一次跑出多篇也不會
    互相蓋掉（daily-notify.yml 會逐篇送出）。
    """
    if not entries:
        return
    path = render.DOCS_DIR / "_notify_topic.json"
    # url 優先用 PDF：daily-notify.yml 會接「完整報告：<url>」。PDF 產不出來
    # （weasyprint 不可用）就退回 HTML 頁。notify_title 讓「貼連結／貼文章分析」
    # 這種也走同一個通道但標題不一樣。
    payload = [{"title": e.get("notify_title") or f"點播主題報告：{e['title']}",
                "body": _topic_notify_text(e),
                "url": e.get("pdf_url") or e.get("url", "")} for e in entries]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[topic] 已寫入推播內容 {path.name}（{len(entries)} 篇）")


def _resolve_topic_company(text: str) -> dict | None:
    """把點播主題對到站內的申報基本資料（`company_profile`）。

    為什麼需要：點播只給一個字串，LLM 拿到「捷立康」三個字就得自己上網查，
    查不夠就依規則放棄——2026-09-08 實際發生，`捷立康 深度報告` 被判「查不到
    足夠的外部來源」而不產出。但 7686 捷立康生物科技本來就在 company_profile
    裡，申報營業項目寫得清清楚楚（醫療器材製造業／精密化學材料製造業／
    研究發展服務業）。站內已經有的權威事實不該讓 LLM 從零猜起。

    比對順序：主題裡出現的 4 碼代號 → 簡稱完全相同 → 簡稱是主題的子字串。
    最後一條要求簡稱至少 2 個字且真的包含在主題裡，避免「台」這種一個字
    亂命中一堆公司。回 {code, name, full_name, market, business} 或 None。
    """
    t = (text or "").strip()
    if not t:
        return None
    try:
        profiles = db.all_company_profiles()
    except Exception as exc:
        print(f"[topic] 讀申報基本資料失敗，略過公司對應：{exc}")
        return None

    def short_of(p: dict) -> str:
        """company_profile 的 `name` 欄位多半是空的，只有 full_name
        （例如「捷立康生物科技股份有限公司」）。去掉法人格式後綴才比得到簡稱。"""
        s = (p.get("name") or "").strip()
        if s:
            return s
        s = (p.get("full_name") or "").strip()
        for suffix in ("股份有限公司", "有限公司", "公司"):
            if s.endswith(suffix):
                s = s[: -len(suffix)]
                break
        return s.strip()

    def pack(code: str, p: dict) -> dict:
        return {"code": code, "name": short_of(p) or (p.get("full_name") or ""),
                "full_name": p.get("full_name") or "", "market": p.get("market") or "",
                "business": p.get("business") or ""}

    for code in re.findall(r"\d{4}", t):
        if code in profiles:
            return pack(code, profiles[code])

    # 申報全名推不出俗稱（「台灣積體電路製造」推不到「台積電」），而
    # stock_index.json 存的正是看盤用的俗稱。兩份合起來比才涵蓋得完整。
    aliases: dict[str, str] = {}          # 名稱 → 代號
    try:
        idx = json.loads(
            (render.DOCS_DIR / "data" / "stock_index.json").read_text(encoding="utf-8"))
        for s in idx.get("stocks", []):
            nm = str(s.get("name") or "").strip()
            if len(nm) >= 2 and str(s.get("code") or "") in profiles:
                aliases.setdefault(nm, s["code"])
    except Exception:
        pass                               # 沒有這份檔案就只靠申報全名，不算錯
    for code, p in profiles.items():
        nm = short_of(p)
        if len(nm) >= 2:
            aliases.setdefault(nm, code)

    # 主題可能是「捷立康」也可能是「捷立康 深度報告」，所以整串和每個詞都要試。
    # 取命中字數最長的，避免短字串誤命中（「新」之類）。
    cands = [t] + [w for w in re.split(r"[\s,，、/／]+", t) if len(w) >= 2]
    best_code, best_len = None, 0
    for cand in cands:
        for nm, code in aliases.items():
            if nm == cand:
                return pack(code, profiles[code])
            hit = nm if nm in cand else (cand if nm.startswith(cand) else "")
            if hit and len(hit) > best_len:
                best_code, best_len = code, len(hit)
    return pack(best_code, profiles[best_code]) if best_code else None


def _make_topic_report(topic_title: str, detail: str, source_desc: str,
                       today: str, known: list[str]) -> dict | None:
    """產出一篇主題點播報告：查證 → 寫表 → 產頁 → 寫研究筆記。

    抽成共用函式是因為點播有兩個入口，兩邊規則必須一致：
      · GitHub Issue（標籤 topic-request）
      · Google 表單（標題以「[主題點播]」開頭）——網頁上那個免登入的路徑
    回傳 {slug, path, sources} 成功；來源不足或產出失敗回 None（呼叫端據此回覆使用者）。
    """
    topic = (topic_title or "").strip()
    if not topic:
        return None
    prompt_topic = topic
    if detail and detail.strip() and detail.strip() != topic:
        prompt_topic = f"{topic}\n補充說明：{detail.strip()}"

    # 站內已經有申報基本資料的話，把代號／全名／申報營業項目一起給出去。
    # 這是公開資訊觀測站的申報值（事實層），不是推論——給了之後 LLM 才知道
    # 要查哪一家、用什麼關鍵字查，不會因為只拿到一個簡稱就查不到而放棄。
    company = _resolve_topic_company(f"{topic} {detail or ''}")
    if company:
        print(f"[topic] 主題對應到 {company['code']} {company['name']}"
              f"（{company['market']}），附上申報營業項目一起查證")
        prompt_topic += (
            f"\n\n【站內已有的申報基本資料（公開資訊觀測站申報值，屬於已查證事實）】"
            f"\n股票代號：{company['code']}"
            f"\n公司全名：{company['full_name'] or company['name']}"
            f"\n市場別：{company['market']}"
            f"\n主要經營業務（申報）：{company['business'] or '（申報資料未載明）'}"
            f"\n以上為申報值，可直接引用；其餘內容仍須自行查證外部來源。")

    gate = load_config().get("research_intake", {}).get("require_investment_topic", True)
    # 分開檢查 topic／detail，不要串成一個字串再判斷：點播表單的 body 常常就是
    # topic 本身的重複值（例如 topic="金居"、detail="金居"），串起來變成
    # 「金居 金居」，既不是任何已知公司名稱的精準命中、也不是任何公司名稱的
    # 前綴，會被誤判成非投資相關（2026-09-07 實際踩到，金居 8358、仁新 6696
    # 兩筆點播都被這樣擋下）。
    # 對得到申報基本資料＝這就是一家真實的上市櫃公司，本身即為投資相關，
    # 不必再過關鍵字粗篩——粗篩比對的是「已知公司名稱」清單，碰到
    # 「捷立康 深度報告」這種帶後綴的寫法會整串比不中而誤擋
    # （2026-09-08 實際踩到：使用者點播後只得到「查不到足夠的外部來源」，
    # 但 7686 捷立康生物科技明明就在 company_profile 裡）。
    if gate and not company and not (
            _looks_investment_related(topic) or _looks_investment_related(detail or "")):
        print(f"[topic] {topic[:20]}：非投資相關，關鍵字粗篩擋下，不處理")
        return None

    report = _safe(lambda: llm.write_topic_report(prompt_topic, known), {},
                   f"主題報告 {topic[:20]}")
    if not report or not report.get("sections"):
        return None
    if report.get("relevant") is False:
        print(f"[topic] {topic[:20]}：LLM 判定非投資相關，不產出")
        return None

    sources = [x for x in (report.get("sources") or []) if str(x).strip()]
    if not sources:
        # 跟其他產出線同一條規則：沒有外部查證來源就不留檔
        print(f"[topic] {topic[:20]}：查不到外部來源，不產出")
        return None

    slug = render.slugify(topic)[:40]
    row = {
        "date": today, "reported_at": now_tpe().strftime("%Y-%m-%d %H:%M:%S"),
        "topic": topic, "title": report.get("title") or topic,
        "summary": report.get("summary", ""),
        "sections": report.get("sections") or [],
        "stocks": report.get("stocks") or [],
        "risks": report.get("risks", ""), "sources": sources, "issue_no": None,
    }
    # 標題收斂成單行：prompt 裡帶了「補充說明：…」的換行，LLM 有時會整段回聲成
    # 標題，那樣頁面標題與研究筆記會變成多行。
    row["title"] = " ".join(str(row["title"]).split())[:80] or topic

    db.upsert_topic_report(slug, row)
    path = render.render_topic_report(slug, row)
    pdf = _safe(lambda: render.render_topic_pdf(slug, row), None, f"主題報告 PDF {slug}")
    if pdf:
        render.render_topic_report(slug, row)   # 重繪，讓「下載 PDF」連結出現在頁面上
    _safe(lambda: db.create_research_note(
        submitted_at=today, source=source_desc, title=row["title"],
        raw_excerpt=topic, summary=row["summary"], verified="verified",
        verification_note=f"系統實際查證後產出，附 {len(sources)} 個外部來源。"
                          f"報告內標「（推論）」者為推論而非查證事實。",
        affected_themes=[], affected_stocks=row["stocks"],
        link=f"analysis/{today}-{slug}.html",
    ), None, "研究筆記（主題點播）")

    base = load_config()['site']['base_url']
    url = f"{base}/analysis/{today}-{slug}.html"
    pdf_url = f"{base}/analysis/{today}-{slug}.pdf" if pdf else ""
    return {"slug": slug, "path": path, "sources": sources, "title": row["title"],
            "url": url, "pdf_url": pdf_url, "row": row}


def run_topic_requests() -> None:
    """使用者點播主題 → 系統做完整研究 → 產出報告頁存回網站。

    這是提交研究頁的第三種模式：前兩種是使用者「貼文章／貼連結」讓系統驗證，
    這一種是使用者只給一個主題，由系統自己上網查證後寫一份深度報告。

    因為沒有原文可以比對，查證責任全在產出端——llm.write_topic_report() 的
    system 提示要求 sources 至少 2 個外部來源，這裡再擋一次：沒有來源的報告
    直接不寫，並在 Issue 上說明原因，不留一篇沒有查證的文章在網站上。

    產出位置沿用 CLAUDE.md 講的 docs/analysis/<日期>-<主題slug>.html。
    """
    today = today_str()
    issues = _safe(_gh_topic_issue_list, [], "讀取主題點播")
    if not issues:
        print("[topic] 沒有待處理的主題點播")
        return

    known = list(dict.fromkeys(
        _safe(db.catalog_theme_names, [], "題材目錄名單")
        + [t["name"] for t in db.list_all_themes()]
    ))

    made = 0
    produced: list[dict] = []
    for issue in issues:
        number = issue["number"]
        topic = (issue.get("title") or "").strip()
        detail = (issue.get("body") or "").strip()
        if detail and detail != topic:
            topic = f"{topic}\n補充說明：{detail}"
        if not topic:
            _gh_issue_comment_and_close(number, "沒有讀到主題內容，已關閉。")
            continue

        print(f"[topic] 處理 Issue #{number}：{topic[:40]}")
        title = (issue.get("title") or "").strip()
        if title.startswith(TOPIC_PREFIX):          # 前端會帶這個前綴，去掉才是主題本身
            title = title[len(TOPIC_PREFIX):].strip()
        result = _make_topic_report(
            title, issue.get("body", ""),
            f"主題點播（GitHub Issue #{number}）", today, known)
        if not result:
            _gh_issue_comment_and_close(
                number, "這個主題查不到可靠的外部來源、或產出失敗，依站內規則不產出報告"
                        "（寧可不寫，也不放未經查證的內容）。可以換個更具體的講法再試。")
            continue

        made += 1
        produced.append(result)
        url = f"{load_config()['site']['base_url']}/analysis/{today}-{result['slug']}.html"
        _gh_issue_comment_and_close(
            number, f"已產出主題報告：{result['title']}\n\n{url}\n\n"
                    f"查證來源 {len(result['sources'])} 個。報告同時列在研究筆記與分析清單頁。")
        print(f"[topic] 已產出 {result['path'].name}")

    if made:
        render.render_intraday_report_index()
        render.render_research_notes()      # 點播結果也會出現在研究筆記頁
        _safe(lambda: _write_topic_notify(produced), None, "點播推播")
        print(f"[topic] 產出 {made} 篇主題報告")


def _gh_issue_comment_and_close(number: int, comment: str) -> None:
    try:
        subprocess.run(["gh", "issue", "comment", str(number), "--body", comment],
                       check=True, timeout=30)
        subprocess.run(["gh", "issue", "close", str(number)], check=True, timeout=30)
    except Exception as exc:
        print(f"[research] 回覆／關閉 Issue #{number} 失敗：{exc}")


def _process_research_submission(source: str, title: str, body: str, today: str,
                                  known_theme_names: list[str], label: str,
                                  record_off_topic: bool = True) -> dict | None:
    """分析一篇提交＋寫進研究筆記表；回傳結果字典給呼叫端決定要不要留言／關閉
    Issue。抽成共用函式是因為現在有兩個提交來源（GitHub Issue、Google 表單），
    分析與驗證邏輯不該重複兩份。"""
    # 「貼連結網址」模式：提交內容整段就是一個網址。以前這裡沒有任何抓取動作，
    # 直接把那行網址當成「原文」丟給 LLM，等於什麼內容都沒給，當然分析不出東西
    # （使用者實際回報：「感覺只有貼文章的會成功」）。這裡先把網頁抓成純文字。
    source_url = article.looks_like_url(body)
    if source_url:
        fetched = _safe(lambda: article.fetch_article(source_url),
                        {"ok": False, "error": "抓取程序異常"}, f"抓取連結 {label}")
        if not fetched.get("ok"):
            # 抓不到就誠實記一筆，不要拿空內容硬分析出一篇看似有結論的東西
            reason = fetched.get("error", "未知原因")
            print(f"[research] {label} 連結抓取失敗：{reason}")
            note_id = db.create_research_note(
                submitted_at=today, source=f"{source}（連結）", title=title or source_url,
                raw_excerpt=source_url,
                summary=f"這個連結無法取得內容，因此沒有進行分析。原因：{reason}",
                verified="unverified",
                verification_note="連結內容抓取失敗，系統未對其做任何判斷，"
                                  "也沒有回寫任何既有資料。可以改用「貼文字內容」"
                                  "把文章內文直接貼上。",
                affected_themes=[], affected_stocks=[])
            db.mark_research_note_status(note_id, "pending", [], [])
            return {"verified": "unverified", "affected_themes": [], "affected_stocks": [],
                    "summary": f"連結抓取失敗：{reason}",
                    "verification_note": "未進行分析"}
        # 把網頁標題與出處一起帶進去，LLM 才知道這段文字是從哪來的
        body = (f"（以下內容擷取自使用者提供的網址：{source_url}"
                + (f"，網頁標題：{fetched.get('title')}" if fetched.get("title") else "")
                + f"）\n\n{fetched['text']}")
        if not title:
            title = fetched.get("title") or source_url
        source = f"{source}｜{source_url}"

    result = _safe(lambda: llm.analyze_research_submission(body, known_theme_names),
                   {}, f"研究分析 {label}")
    if not result:
        return None

    if result.get("relevant") is False:
        # LLM 判定跟台股／投資無關：留一筆極簡記錄讓提交者看得到「收到但沒處理」，
        # 不動題材、不產 PDF、不發 Discord。呼叫端看到 off_topic 就當略過。
        # 即時快訊監控那條路沒有提交者，不需要留筆記（record_off_topic=False）。
        if record_off_topic:
            _safe(lambda: db.mark_research_note_status(db.create_research_note(
                submitted_at=today, source=source,
                title=title or "（非投資相關提交）", raw_excerpt=body[:300],
                summary=result.get("summary", "非投資相關，系統未做分析。"),
                verified="unverified",
                verification_note=result.get(
                    "verification_note",
                    "內容與台股／投資無關，系統未做分析，也未動任何既有資料。"),
                affected_themes=[], affected_stocks=[]), "pending", [], []),
                None, "研究筆記（非投資相關）")
        print(f"[research] {label}：LLM 判定非投資相關，已略過")
        return {"verified": "unverified", "off_topic": True, "affected_themes": [],
                "affected_stocks": [], "summary": result.get("summary", ""),
                "verification_note": result.get("verification_note", "非投資相關")}

    verified = result.get("verified", "unverified")
    affected_themes = result.get("affected_themes", [])
    affected_stocks = result.get("affected_stocks", [])

    # 只有明確判定 verified，且真的對應到既有題材，才回寫進題材知識庫；
    # conflicting／unverified 一律只留在研究筆記裡，不動任何既有資料
    for t in affected_themes:
        t["applied"] = db.append_research_to_theme(t["name"], today, t.get("impact", "")) \
            if verified == "verified" else False
    for s in affected_stocks:
        s["applied"] = False  # 目前不直接改個股歷史資料，只記錄關聯供研究筆記頁參考

    # 分析記錄也產一份 PDF＋（透過研究筆記的「看完整報告」連結）掛上去，
    # 讓貼連結／貼文章跟主題點播一樣有可下載的東西、也能發 Discord。
    disp_title = title or result.get("summary", "")[:24] or "研究提交分析"
    slug = (render.slugify(disp_title)[:32]
            + "-" + hashlib.md5(source.encode("utf-8")).hexdigest()[:6])
    sub_row = {
        "title": disp_title, "date": today, "source": source, "verified": verified,
        "summary": result.get("summary", ""),
        "verification_note": result.get("verification_note", ""),
        "themes": affected_themes, "stocks": affected_stocks,
    }
    pdf = _safe(lambda: render.render_submission_pdf(slug, sub_row), None,
                f"提交分析 PDF {label}")
    link = f"analysis/{today}-sub-{slug}.pdf" if pdf else ""

    note_id = db.create_research_note(
        submitted_at=today, source=source, title=title, raw_excerpt=body[:500],
        summary=result.get("summary", ""), verified=verified,
        verification_note=result.get("verification_note", ""),
        affected_themes=affected_themes, affected_stocks=affected_stocks,
        link=link,
    )
    db.mark_research_note_status(note_id, "applied" if verified == "verified" else "pending",
                                 affected_themes, affected_stocks)
    result["affected_themes"] = affected_themes
    if pdf:
        base = load_config()["site"]["base_url"]
        result["report"] = {
            "title": disp_title,
            "pdf_url": f"{base}/{link}",
            "row": {"summary": result.get("summary", ""), "stocks": affected_stocks,
                    "sections": []},
            "sources": [source] if source.startswith("http") else [],
            "verified": verified,
        }
    return result


def run_research_intake() -> None:
    today = today_str()
    _topic_gate = load_config().get("research_intake", {}).get(
        "require_investment_topic", True)
    known_theme_names = list(dict.fromkeys(
        _safe(db.catalog_theme_names, [], "題材目錄名單")
        + [t["name"] for t in db.list_all_themes()]
    ))
    processed = 0
    topic_made: list[dict] = []      # 點播成功的，最後統一寫進獨立的推播檔
    topic_failed: list[str] = []
    source_errors: list[str] = []    # 「來源讀不到」，跟「來源是空的」分開記
    topic_seen: set[str] = set()     # 本輪已處理的主題（大小寫無關），用來去重
    submission_reports: list[dict] = []   # 貼連結／貼文章分析出來、有產 PDF 的，也發 Discord

    # 來源一：GitHub Issue（給熟悉 GitHub 的人，例如你自己）
    issues = _safe(_gh_issue_list, [], "讀取使用者研究提交（GitHub）")
    for issue in issues:
        title, body, number = issue.get("title", ""), issue.get("body", ""), issue["number"]
        print(f"[research] 處理 Issue #{number}：{title}")
        if _topic_gate and not (_looks_investment_related(title) or _looks_investment_related(body)):
            _gh_issue_comment_and_close(
                number, "這則提交看起來跟台股／投資無關，系統未做分析（提交頁只處理台股、"
                        "個股、投資題材、影響台股的總體經濟等內容）。")
            continue
        result = _process_research_submission(
            f"GitHub Issue #{number}（{issue.get('url', '')}）", title, body, today,
            known_theme_names, f"#{number}")
        if not result:
            _gh_issue_comment_and_close(number, "分析失敗（LLM 呼叫或解析出錯），請確認內容格式或稍後再試。")
            continue
        if result.get("off_topic"):
            _gh_issue_comment_and_close(number, "已收到，但系統判定內容與台股／投資無關，未做分析。")
            continue
        processed += 1
        _report_result_to_issue(number, result)
        if result.get("report"):
            submission_reports.append(result["report"])

    # 來源二：Google 表單（給不需要 GitHub 帳號的訪客，見 submit.html）
    # 用「已處理過幾列」而不是時間戳記字串來判斷新提交——Google 表單的時間戳記
    # 是「2026/9/5 下午 8:32:07」這種格式，字串比較在日期/時間進位時會比錯
    # （例如 "9/15" 字串會排在 "9/5" 前面），但表單本來就只會照送出順序把
    # 新回覆附加在試算表最後一列，所以看列數比看時間字串可靠。
    cfg = load_config()
    csv_url = cfg.get("research_intake", {}).get("google_sheet_csv_url", "")
    if csv_url:
        # 讀不到（網路被擋、Google 掛掉）跟「沒有新提交」是完全不同的兩件事，
        # 不能都當成空清單靜靜跳過——見 FormFetchError 的說明。
        try:
            rows = google_sheet.fetch_form_responses(csv_url)
        except google_sheet.FormFetchError as exc:
            rows = []
            source_errors.append(f"Google 表單：{exc}")
        last_count = int(db.get_state("research_form_row_count", "0"))
        new_rows = rows[last_count:]
        for row in new_rows:
            title = (row.get("title") or "").strip()
            # 「🎯 點播深度主題」在網頁上是走這條免登入的表單送出的，標題會帶
            # 「[主題點播]」前綴。以前這裡沒有分流，等於把使用者輸入的「主題關鍵字」
            # 當成一篇文章去做真偽驗證——關鍵字沒有內容可驗證，所以什麼都產不出來，
            # 研究筆記上也看不到東西（使用者實際回報）。這裡改成分流到點播那條線。
            if title.startswith(TOPIC_PREFIX):
                topic = title[len(TOPIC_PREFIX):].strip()
                # 同一輪重複送同一個主題（大小寫不同也算），只做一次——使用者
                # 常常按了沒反應就再送一次，逐筆做等於同樣的查證跑好幾遍，
                # 還會產生 slug 只差大小寫的近乎重複頁面。
                if topic.casefold() in topic_seen:
                    print(f"[research] 主題「{topic}」本輪已處理過，略過重複提交")
                    continue
                topic_seen.add(topic.casefold())
                print(f"[research] 處理主題點播（{row['timestamp']}）：{topic}")
                made = _make_topic_report(
                    topic, row.get("body", ""),
                    f"主題點播 · Google 表單（{row['timestamp']}）",
                    today, known_theme_names)
                if made:
                    processed += 1
                    topic_made.append(made)
                    print(f"[research] 主題報告已產出：{made['path'].name}")
                else:
                    # 產不出來也要留一筆，不然使用者送出後完全看不到任何回應
                    _safe(lambda t=topic, ts=row["timestamp"]: db.create_research_note(
                        submitted_at=today,
                        source=f"主題點播 · Google 表單（{ts}）", title=t, raw_excerpt=t,
                        summary="這個主題查不到足夠的外部來源，依站內規則不產出報告。",
                        verified="unverified",
                        verification_note="系統實際查證後仍找不到可靠外部來源，"
                                          "寧可不寫，也不放未經查證的內容。"
                                          "可以換個更具體的講法再點播一次。",
                        affected_themes=[], affected_stocks=[]),
                        None, "研究筆記（點播失敗）")
                    # 失敗也要講一聲，不然使用者會一直等一則永遠不會來的通知
                    topic_failed.append(topic)
                    processed += 1
                continue

            print(f"[research] 處理表單提交（{row['timestamp']}）：{title}")
            if _topic_gate and not (_looks_investment_related(title) or _looks_investment_related(row["body"])):
                print(f"[research] 表單提交（{row['timestamp']}）：非投資相關，關鍵字粗篩擋下，略過")
                _safe(lambda ts=row["timestamp"]: db.mark_research_note_status(
                    db.create_research_note(
                        submitted_at=today, source=f"Google 表單提交（{ts}）",
                        title=title or "（非投資相關提交）", raw_excerpt=row["body"][:300],
                        summary="非投資相關，系統未做分析。", verified="unverified",
                        verification_note="內容與台股／投資無關，系統未做分析，也未動任何既有資料。",
                        affected_themes=[], affected_stocks=[]), "pending", [], []),
                    None, "研究筆記（非投資相關）")
                continue
            result = _process_research_submission(
                f"Google 表單提交（{row['timestamp']}）", title, row["body"], today,
                known_theme_names, row["timestamp"])
            if result and not result.get("off_topic"):
                processed += 1
                if result.get("report"):
                    submission_reports.append(result["report"])
        if new_rows:
            db.set_state("research_form_row_count", str(len(rows)))

    # 來源三：GitHub Issue（標籤 topic-request）＝主題點播的另一個入口。
    # 併進這一支處理，而不是另外養一支 Routine：兩者都是「使用者提交了東西、
    # 要盡快回應」，分成兩支等於同樣的空跑成本付兩次，而使用者實際用的是網頁
    # 表單那條路。合併後只要一支跑高頻就夠。
    for issue in _safe(_gh_topic_issue_list, [], "讀取主題點播（GitHub）"):
        number = issue["number"]
        title = (issue.get("title") or "").strip()
        if title.startswith(TOPIC_PREFIX):
            title = title[len(TOPIC_PREFIX):].strip()
        print(f"[research] 處理主題點播 Issue #{number}：{title[:40]}")
        made = _make_topic_report(title, issue.get("body", ""),
                                  f"主題點播（GitHub Issue #{number}）",
                                  today, known_theme_names)
        if made:
            topic_made.append(made)
            processed += 1
            _gh_issue_comment_and_close(
                number, f"已產出主題報告：{made['title']}\n\n{made['url']}\n\n"
                        f"查證來源 {len(made['sources'])} 個。"
                        f"報告同時列在研究筆記與分析清單頁。")
        else:
            topic_failed.append(title)
            processed += 1
            _gh_issue_comment_and_close(
                number, "這個主題查不到可靠的外部來源、或產出失敗，依站內規則不產出"
                        "報告（寧可不寫，也不放未經查證的內容）。可以換個更具體的講法再試。")

    if processed == 0:
        print("[research] 目前沒有待處理的使用者研究提交")
        _raise_if_sources_broken(source_errors)
        return
    render.render_site()
    print(f"[research] 本批處理 {processed} 篇提交")

    # 點播是使用者自己要的東西，走獨立通道、直接送報告內容。
    # ⚠️ _write_topic_notify() 每次都會「重寫整個檔案」，所以成功與失敗的通知
    # 一定要先合成同一個 list、只呼叫一次。之前分開呼叫的版本，失敗那則會把
    # 前面成功的整批蓋掉（實際發生過：2 篇成功被 1 則失敗覆蓋）。
    _verdict_label = {"verified": "已驗證", "conflicting": "與既有資料衝突",
                      "unverified": "無法獨立驗證"}
    entries = list(topic_made) + [{
        **rep,
        "notify_title": f"研究提交分析（{_verdict_label.get(rep.get('verified'), '')}）："
                        f"{rep['title']}",
        "url": rep.get("pdf_url", ""),
    } for rep in submission_reports] + [{
        "title": f"「{t}」查不到足夠外部來源", "url": "", "sources": [],
        "row": {"summary": "依站內規則不產出報告（寧可不寫，也不放未經查證的"
                           "內容）。可以換個更具體的講法再點播一次；"
                           "結果已記在研究筆記頁。", "sections": []},
    } for t in topic_failed]
    _safe(lambda: _write_topic_notify(entries), None, "點播推播")

    # 沒產出獨立報告的一般提交（例如連結抓取失敗）仍走既有的每日彙總通道
    plain = processed - len(topic_made) - len(topic_failed) - len(submission_reports)
    if plain > 0:
        _safe(lambda: send_notification(
            f"📝 已處理 {plain} 筆研究提交",
            f"驗證結果與是否回寫題材，見研究筆記頁：\n"
            f"{load_config()['site']['base_url']}/research.html"),
            None, "研究提交通知")

    # 有做完的事都做完、通知都送出去了，最後才讓整輪失敗——這樣「部分來源掛掉」
    # 不會連帶把另一個來源已經處理好的東西吞掉。
    _raise_if_sources_broken(source_errors)


def _raise_if_sources_broken(errors: list[str]) -> None:
    """有提交來源根本讀不到時，讓這一輪以非 0 結束。

    這支程式被雲端 Routine 叫起來跑，Routine 的規則是「跑順且沒有新提交就安靜
    結束、只有失敗才通知」。所以「來源讀不到」一定要真的失敗——否則它看起來
    就跟「今天沒人提交」一模一樣，使用者送出表單後只會得到永遠的沉默。
    """
    if not errors:
        return
    for e in errors:
        print(f"[research] ⚠ 提交來源讀取失敗：{e}")
    raise SystemExit(
        "研究提交來源讀不到（見上方 ⚠），這一輪無法確認有沒有新提交。"
        "若錯誤是 Tunnel connection failed / 403，多半是雲端環境的 Network access "
        "被設成 Trusted，見 CLAUDE.md 與 MIGRATION.md。")


def _report_result_to_issue(number: int, result: dict) -> None:
    verified = result.get("verified", "unverified")
    status_label = {"verified": "已驗證並套用", "conflicting": "與既有資料衝突，未套用",
                    "unverified": "無法獨立驗證，未套用"}[verified]
    applied_names = [t["name"] for t in result.get("affected_themes", []) if t.get("applied")]
    comment = (f"**分析結果：{status_label}**\n\n{result.get('summary', '')}\n\n"
              f"判定理由：{result.get('verification_note', '')}\n\n"
              + (f"已回寫題材：{'、'.join(applied_names)}\n\n" if applied_names else "")
              + "詳見網站「研究筆記」頁面。")
    _gh_issue_comment_and_close(number, comment)
    print(f"[research] Issue #{number} 完成，判定：{verified}")


# ── 即時快訊監控（華爾街見聞 live/global）─────────────────
# 跟使用者研究提交共用同一套「分析＋嚴格驗證」邏輯（_process_research_submission），
# 差別只是來源換成即時快訊：先用來源自己的重要度分數粗篩一輪（省 LLM 成本，
# 大多數快訊跟台股完全無關），只有夠重要的才進一步分析跟不跟現有題材/個股有關；
# 只有「夠重要且真的跟台股題材/個股有關」才推播通知，避免每則國際新聞都推播
# 造成通知疲勞。目前沒有排程自動跑，要另外設一個跑得比每日報告更頻繁的 Routine。
def run_news_monitor() -> None:
    cfg = load_config()
    nm = cfg.get("news_monitor", {})
    today = today_str()

    feed = _safe(lambda: wallstreetcn.fetch_live_feed(nm.get("channel", "global-channel"),
                                                       nm.get("fetch_limit", 30)),
                [], "即時快訊")
    if not feed:
        print("[news] 目前抓不到即時快訊")
        return

    last_id = int(db.get_state("news_monitor_last_id", "0"))
    score_min = nm.get("score_min", 2)
    # feed 是新到舊排列；第一次執行時沒有 last_id，只記錄目前最新一則的 id 當基準，
    # 不要把過去幾十則舊快訊一次性全部拿去分析（浪費成本，而且都是舊聞）
    if last_id == 0:
        db.set_state("news_monitor_last_id", str(max(it["id"] for it in feed)))
        print(f"[news] 第一次執行，記錄基準 id，下次才開始比對新快訊")
        return

    candidates = [it for it in feed if it["id"] > last_id and it["score"] >= score_min]
    if not candidates:
        db.set_state("news_monitor_last_id", str(max(it["id"] for it in feed)))
        print("[news] 沒有新的重要快訊")
        return

    known_theme_names = list(dict.fromkeys(
        _safe(db.catalog_theme_names, [], "題材目錄名單")
        + [t["name"] for t in db.list_all_themes()]
    ))
    notified = 0
    recorded = 0        # 有寫進 research_notes 的則數（重繪條件）
    news_notify: list[dict] = []      # 這一輪要推的快訊，最後一次寫檔（避免互相覆蓋）
    digest: list[dict] = []           # 這一輪所有新記錄的筆記（含 unverified），最後發一則彙整
    for it in sorted(candidates, key=lambda x: x["id"]):
        text = f"{it['title']}\n{it['text']}" if it["title"] else it["text"]
        result = _process_research_submission(
            f"華爾街見聞即時快訊 #{it['id']}", it["title"] or "即時快訊", text, today,
            known_theme_names, f"news#{it['id']}", record_off_topic=False)
        if not result or result.get("off_topic"):
            continue
        # 每一則有分析出結果的快訊都會被 _process_research_submission() 寫進
        # research_notes 表，不管最後有沒有推播——所以要重繪的條件是「有寫入」，
        # 不是「有推播」。以前用 notified 當條件，導致絕大多數快訊（unverified、
        # 或沒對到題材的）只進了 market.db 卻沒重繪 research.html，研究筆記頁
        # 一路落後好幾十則；直到有人在別處跑 `main site` 才會一次全部冒出來。
        recorded += 1
        affected = result.get("affected_themes", [])
        digest.append({
            "title": it.get("title") or (text or "")[:30],
            "verified": result.get("verified") or "unverified",
            "summary": (result.get("summary") or "")[:70],
            "themes": [t["name"] for t in affected],
        })
        # 以前條件是「verified/conflicting **而且** 有對應到既有題材」才推播。
        # 但一則快訊查證通過、只是還沒被歸進任何題材，本身就值得知道——
        # 使用者回報研究筆記那些內容都沒有 Discord 通知。改成：查證結果明確
        # （verified／conflicting）就推，有沒有對到題材只影響內文怎麼寫。
        # unverified 仍然不推（那是「無法獨立查證」，推了只是雜訊）。
        if result.get("verified") in ("verified", "conflicting"):
            names = "、".join(t["name"] for t in affected) if affected else "尚未歸入既有題材"
            verdict = {"verified": "已驗證", "conflicting": "與既有資料衝突"}.get(
                result.get("verified"), result.get("verified"))
            # 以前這裡直接呼叫 send_notification()，而那支寫的是
            # `_notify_payload.json`——早報／盤後共用的那一個檔。寫在迴圈裡等於
            # 一輪抓到 N 則就互相覆蓋 N-1 次，**只有最後一則會送到 Discord**，
            # 其餘靜靜消失；還可能被當天的例行報告蓋掉。改成收集起來，最後一次
            # 寫進獨立的 `_notify_news.json`（陣列，逐則送出）。
            news_notify.append({
                "title": f"📰 快訊：{it['title'] or text[:30]}",
                "body": f"{result.get('summary', '')}\n\n"
                        f"相關題材：{names}\n判定：{verdict}",
                "url": f"{load_config()['site']['base_url']}/research.html",
            })
            notified += 1

    # 研究筆記彙整：使用者反映「研究筆記那些內容都沒有 Discord 通知」。
    # 但逐則推 unverified 的快訊只會洗版，所以改成**一輪一則彙整**：把這一輪
    # 新記錄的全部列出來、各自標明查證狀態，重要的（verified／conflicting）
    # 仍然另外單獨推一則完整內容。
    if digest:
        VERD = {"verified": "✅ 已驗證", "conflicting": "⚠️ 與既有資料衝突",
                "unverified": "❔ 無法獨立驗證"}
        lines = [f"📝 研究筆記新增 {len(digest)} 則", ""]
        for x in digest[:12]:
            lines.append(f"{VERD.get(x['verified'], x['verified'])}　{x['title'][:44]}")
            if x["summary"]:
                lines.append(f"　　{x['summary']}")
            if x["themes"]:
                lines.append(f"　　題材：{'、'.join(x['themes'][:3])}")
        if len(digest) > 12:
            lines.append(f"（另有 {len(digest) - 12} 則，見研究筆記頁）")
        lines.append("\n只有標「已驗證」的才會回寫題材庫；其餘僅留存不影響報告。")
        news_notify.append({
            "title": f"研究筆記新增 {len(digest)} 則",
            "body": "\n".join(lines),
            "url": f"{load_config()['site']['base_url']}/research.html",
        })

    if news_notify:
        (render.DOCS_DIR / "_notify_news.json").write_text(
            json.dumps(news_notify, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[news] 已寫入推播內容 _notify_news.json（{len(news_notify)} 則）")

    db.set_state("news_monitor_last_id", str(max(it["id"] for it in feed)))
    if recorded:
        render.render_site()
    print(f"[news] 檢查 {len(candidates)} 則重要快訊，記錄 {recorded} 則、推播 {notified} 則")


# ── 自動分支（排程呼叫這個） ───────────────────────────
def run_auto(slot: str) -> None:
    """slot = morning / evening，由 cron 傳入時段，再由行事曆決定實際跑什麼。"""
    refresh_holidays()
    kind = classify_day()
    print(f"[auto] slot={slot} 今日類型={kind}")

    if kind == "full_holiday":
        if slot == "morning":
            run_holiday()
        return

    if slot == "morning":
        run_morning()
        return

    # slot == evening
    if kind == "trading":
        run_evening()
    else:
        print("[auto] 台股今日休市，略過盤後報告")
        if is_last_day_before_reopen():
            print("[auto] 長假最後一日，產出假期彙整")
            run_holiday()


def main() -> None:
    db.init_db()
    mode = sys.argv[1] if len(sys.argv) > 1 else "auto"

    dispatch = {
        "morning": run_morning,
        "evening": run_evening,
        "monthly": run_monthly,
        "holiday": run_holiday,
        "site": lambda: render.render_site(),
        "catalog": run_import_catalog,
        "research": run_research_intake,
        "news": run_news_monitor,
        "verify-stocks": run_verify_stocks,
        "intraday-ref": _run_intraday_ref,
        "intraday-report": run_intraday_deep_report,
        "topic-requests": run_topic_requests,
        "chart-snapshot": run_chart_snapshot,
        "industry-reports": run_industry_reports,
        "weekly-facts": lambda: run_period_facts(7),
        "monthly-facts": lambda: run_period_facts(31),
    }

    if mode == "auto":
        run_auto(sys.argv[2] if len(sys.argv) > 2 else "evening")
    elif mode == "intraday":
        from . import intraday as _iv
        args = sys.argv[2:]
        loop = "--loop" in args
        until = None
        interval = 60
        for i, a in enumerate(args):
            if a == "--until" and i + 1 < len(args):
                until = args[i + 1]
            if a == "--interval" and i + 1 < len(args):
                interval = int(args[i + 1])
        _iv.run(loop=loop, until=until, interval=interval)
    elif mode in dispatch:
        dispatch[mode]()
    else:
        print(f"未知模式：{mode}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
