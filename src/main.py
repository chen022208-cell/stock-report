"""主程式入口。

用法：
    python -m src.main morning     # 07:00 國際盤
    python -m src.main evening     # 18:00 台股盤後
    python -m src.main monthly     # 每月 12 日 月報完整版
    python -m src.main holiday     # 假日功課
    python -m src.main research    # 處理使用者提交的研究文章（見 submit.html）
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

from . import db, llm, prices_db, render
from .analysis import global_themes, industry, review, scoring, screener, technical
from .config import DRY_RUN, load_config, today_str, now_tpe
from .fetchers import fred, google_sheet, international, mops, stock_news, tdcc, tpex, twse
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
    }

    if mode == "auto":
        run_auto(sys.argv[2] if len(sys.argv) > 2 else "evening")
    elif mode in dispatch:
        dispatch[mode]()
    else:
        print(f"未知模式：{mode}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
