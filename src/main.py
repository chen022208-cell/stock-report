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

import json
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
from .fetchers import (fred, google_sheet, international, mops, stock_news,
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
    for code in payload.get("new_codes", []):
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
    inst = _safe(twse.fetch_institutional_net, {}, "三大法人")
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
        if not DRY_RUN:
            cached = prices_db.get_history(code, today, limit=days)
            if len(cached) >= 11:
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
        render.render_chips(inst, margin_top[:10], strong[:10], holders,
                            render.date_label(today))

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
    candidates = {s["code"]: s["name"] for s in strong}
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
    otc_codes = {n["code"]: n["name"] for n in new_listings}
    otc_codes.update({dh["code"]: dh["name"] for dh in dark_horses})
    otc_codes.update({b["code"]: b.get("name", "") for b in breakout_candidates})
    for c in _all_market_codes():
        if c["market"] in ("tpex", "esb"):
            otc_codes.setdefault(c["code"], c["name"])
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
    send_notification(f"盤後報告已產出：{render.date_label(today)}", commentary)


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


def _gh_issue_list() -> list[dict]:
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


def _gh_issue_comment_and_close(number: int, comment: str) -> None:
    try:
        subprocess.run(["gh", "issue", "comment", str(number), "--body", comment],
                       check=True, timeout=30)
        subprocess.run(["gh", "issue", "close", str(number)], check=True, timeout=30)
    except Exception as exc:
        print(f"[research] 回覆／關閉 Issue #{number} 失敗：{exc}")


def _process_research_submission(source: str, title: str, body: str, today: str,
                                  known_theme_names: list[str], label: str) -> dict | None:
    """分析一篇提交＋寫進研究筆記表；回傳結果字典給呼叫端決定要不要留言／關閉
    Issue。抽成共用函式是因為現在有兩個提交來源（GitHub Issue、Google 表單），
    分析與驗證邏輯不該重複兩份。"""
    result = _safe(lambda: llm.analyze_research_submission(body, known_theme_names),
                   {}, f"研究分析 {label}")
    if not result:
        return None

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

    note_id = db.create_research_note(
        submitted_at=today, source=source, title=title, raw_excerpt=body[:500],
        summary=result.get("summary", ""), verified=verified,
        verification_note=result.get("verification_note", ""),
        affected_themes=affected_themes, affected_stocks=affected_stocks,
    )
    db.mark_research_note_status(note_id, "applied" if verified == "verified" else "pending",
                                 affected_themes, affected_stocks)
    result["affected_themes"] = affected_themes
    return result


def run_research_intake() -> None:
    today = today_str()
    known_theme_names = list(dict.fromkeys(
        _safe(db.catalog_theme_names, [], "題材目錄名單")
        + [t["name"] for t in db.list_all_themes()]
    ))
    processed = 0

    # 來源一：GitHub Issue（給熟悉 GitHub 的人，例如你自己）
    issues = _safe(_gh_issue_list, [], "讀取使用者研究提交（GitHub）")
    for issue in issues:
        title, body, number = issue.get("title", ""), issue.get("body", ""), issue["number"]
        print(f"[research] 處理 Issue #{number}：{title}")
        result = _process_research_submission(
            f"GitHub Issue #{number}（{issue.get('url', '')}）", title, body, today,
            known_theme_names, f"#{number}")
        if not result:
            _gh_issue_comment_and_close(number, "分析失敗（LLM 呼叫或解析出錯），請確認內容格式或稍後再試。")
            continue
        processed += 1
        _report_result_to_issue(number, result)

    # 來源二：Google 表單（給不需要 GitHub 帳號的訪客，見 submit.html）
    # 用「已處理過幾列」而不是時間戳記字串來判斷新提交——Google 表單的時間戳記
    # 是「2026/9/5 下午 8:32:07」這種格式，字串比較在日期/時間進位時會比錯
    # （例如 "9/15" 字串會排在 "9/5" 前面），但表單本來就只會照送出順序把
    # 新回覆附加在試算表最後一列，所以看列數比看時間字串可靠。
    cfg = load_config()
    csv_url = cfg.get("research_intake", {}).get("google_sheet_csv_url", "")
    if csv_url:
        rows = _safe(lambda: google_sheet.fetch_form_responses(csv_url), [], "讀取使用者研究提交（表單）")
        last_count = int(db.get_state("research_form_row_count", "0"))
        new_rows = rows[last_count:]
        for row in new_rows:
            print(f"[research] 處理表單提交（{row['timestamp']}）：{row['title']}")
            result = _process_research_submission(
                f"Google 表單提交（{row['timestamp']}）", row["title"], row["body"], today,
                known_theme_names, row["timestamp"])
            if result:
                processed += 1
        if new_rows:
            db.set_state("research_form_row_count", str(len(rows)))

    if processed == 0:
        print("[research] 目前沒有待處理的使用者研究提交")
        return
    render.render_site()
    print(f"[research] 本批處理 {processed} 篇提交")


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
    for it in sorted(candidates, key=lambda x: x["id"]):
        text = f"{it['title']}\n{it['text']}" if it["title"] else it["text"]
        result = _process_research_submission(
            f"華爾街見聞即時快訊 #{it['id']}", it["title"] or "即時快訊", text, today,
            known_theme_names, f"news#{it['id']}")
        if not result:
            continue
        affected = result.get("affected_themes", [])
        if result.get("verified") in ("verified", "conflicting") and affected:
            names = "、".join(t["name"] for t in affected)
            send_notification(f"📰 快訊：{it['title'] or text[:30]}",
                             f"{result.get('summary', '')}\n\n相關題材：{names}")
            notified += 1

    db.set_state("news_monitor_last_id", str(max(it["id"] for it in feed)))
    if notified:
        render.render_site()
    print(f"[news] 檢查 {len(candidates)} 則重要快訊，推播 {notified} 則")


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
        "chart-snapshot": run_chart_snapshot,
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
