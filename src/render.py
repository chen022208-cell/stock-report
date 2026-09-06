"""Jinja2 渲染：把分析結果變成 docs/ 底下的靜態網頁。

輸出到 docs/ 是為了直接餵給 GitHub Pages —— 不需要另外架伺服器。
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import db
from .config import DOCS_DIR, TEMPLATE_DIR, load_config, now_tpe

CONF_LABEL = {"high": "高", "mid": "中高", "low": "低"}
VERDICT_LABEL = {"real": "偏真實", "watch": "待觀察", "unknown": "未判定",
                 "hot": "🔥 當紅", "warm": "偏溫", "cold": "尚無訊號"}

WEEKDAY = ["一", "二", "三", "四", "五", "六", "日"]


def _asset_version() -> str:
    """靠檔案內容雜湊做 cache-busting，而不是每天都變的日期字串——
    stock-chart.js 這種靜態資源被瀏覽器／GitHub Pages CDN 快取後，改了程式碼
    使用者卻看不到更新，就是靠這個 query string 逼瀏覽器重新抓最新版本。"""
    import hashlib
    js_path = DOCS_DIR / "assets" / "stock-chart.js"
    if not js_path.exists():
        return "0"
    return hashlib.md5(js_path.read_bytes()).hexdigest()[:8]


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
    )
    env.globals["asset_v"] = _asset_version()
    return env


def slugify(text: str) -> str:
    """題材名稱 → 檔名。中文保留，只清掉檔案系統不接受的字元。"""
    cleaned = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", text).strip("-")
    return cleaned or "theme"


def date_label(iso: str) -> str:
    from datetime import date
    d = date.fromisoformat(iso)
    return f"{d.year}年{d.month}月{d.day}日．週{WEEKDAY[d.weekday()]}"


def decorate_theme(theme: dict) -> dict:
    theme = dict(theme)
    theme["confidence_label"] = CONF_LABEL.get(theme.get("confidence"), "未定")
    theme["verdict_label"] = VERDICT_LABEL.get(theme.get("verdict"), "未判定")
    if isinstance(theme.get("related_stocks"), str):
        try:
            theme["stocks"] = json.loads(theme["related_stocks"])
        except json.JSONDecodeError:
            theme["stocks"] = []
    return theme


def render_daily(context: dict, out_name: str) -> Path:
    """產出每日報告。檔名用日期，才能累積成可查閱的歷史。"""
    cfg = load_config()
    env = _env()
    tpl = env.get_template("daily.html")

    context.setdefault("site_title", cfg["site"]["title"])
    context.setdefault("generated_at", now_tpe().strftime("%Y-%m-%d %H:%M"))
    context.setdefault("rel", "")

    reports_dir = DOCS_DIR / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{out_name}.html"

    ctx = dict(context)
    ctx["rel"] = "../"
    path.write_text(tpl.render(**ctx), encoding="utf-8")
    return path


def render_monthly(month: str, review_summary: dict, extra: dict | None = None) -> Path:
    """月報頁面：月度市場回顧 + 題材增減 + 信心度分組成績單。

    month           "YYYY-MM"
    review_summary  review.run_review() 的回傳（{horizon: {reviewed, scorecard}}）
    """
    from . import db, viz
    cfg = load_config()
    extra = extra or {}

    start, end = f"{month}-01", f"{month}-31"
    snaps = db.snapshots_between(start, end)

    market_recap = {}
    if snaps:
        first, last = snaps[0], snaps[-1]
        o = first.get("taiex_close") or 0
        c = last.get("taiex_close") or 0
        market_recap = {
            "open": o, "close": c,
            "change_pct": round((c / o - 1) * 100, 2) if o else 0.0,
            "trading_days": len(snaps),
            "foreign_sum": round(sum(s.get("foreign_net") or 0 for s in snaps), 1),
            "trust_sum": round(sum(s.get("trust_net") or 0 for s in snaps), 1),
            "avg_turnover": round(
                sum(s.get("turnover") or 0 for s in snaps) / len(snaps) / 1e8, 0),
            "taiex_spark": viz.sparkline(
                [s["taiex_close"] for s in snaps if s.get("taiex_close")],
                stroke="var(--red)" if c >= o else "var(--green)"),
        }

    all_themes = db.list_all_themes()
    new_themes = [decorate_theme(t) for t in all_themes
                  if start <= (t.get("first_seen") or "") <= end]
    scored_themes = []
    for t in all_themes:
        if t.get("status") == "archived" and not (start <= (t.get("last_signal_date") or "") <= end):
            continue
        series = db.theme_confidence_series(t["id"])
        if len(series) < 2:
            continue
        view = decorate_theme(t)
        view["spark"] = viz.confidence_trend(series)
        view["trend"] = ("上升" if _CONF_RANK.get(series[-1], 2) > _CONF_RANK.get(series[0], 2)
                         else "下滑" if _CONF_RANK.get(series[-1], 2) < _CONF_RANK.get(series[0], 2)
                         else "持平")
        scored_themes.append(view)

    horizons = []
    for horizon, data in sorted(review_summary.items()):
        horizons.append({
            "days": horizon,
            "reviewed": data.get("reviewed", 0),
            "scorecard": data.get("scorecard", []),
        })

    inst_bars = viz.diverging_bars(
        [("外資", market_recap.get("foreign_sum", 0)),
         ("投信", market_recap.get("trust_sum", 0))], unit="億"
    ) if market_recap else ""

    d = date.fromisoformat(f"{month}-01")
    # 檔名補上日，維持 YYYY-MM-DD-kind 四段格式，索引頁才能正確解析
    path = DOCS_DIR / "reports" / f"{month}-01-monthly.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_env().get_template("monthly.html").render(
        site_title=cfg["site"]["title"],
        generated_at=now_tpe().strftime("%Y-%m-%d %H:%M"),
        rel="../",
        report_kind="月報 · 月度回顧與事後驗證",
        month_label=f"{d.year}年{d.month}月",
        date_label=f"{d.year}年{d.month}月",
        market=market_recap,
        inst_bars=inst_bars,
        new_themes=new_themes,
        scored_themes=scored_themes,
        horizons=horizons,
        viz_css=viz.VIZ_CSS,
        **extra,
    ), encoding="utf-8")
    return path


_CONF_RANK = {"low": 1, "mid": 2, "high": 3}


def render_article(theme: dict, article: dict, slug: str,
                   supply_chain: dict | None = None) -> Path:
    cfg = load_config()
    tpl = _env().get_template("article.html")

    theme = decorate_theme(theme)
    stocks = theme.get("stocks", [])

    articles_dir = DOCS_DIR / "articles"
    articles_dir.mkdir(parents=True, exist_ok=True)
    path = articles_dir / f"{slug}.html"

    path.write_text(tpl.render(
        site_title=cfg["site"]["title"],
        generated_at=now_tpe().strftime("%Y-%m-%d %H:%M"),
        rel="../",
        theme=theme,
        article=article,
        stock_count=len(stocks),
        supply_chain=supply_chain or {},
    ), encoding="utf-8")
    return path


def _collect_reports() -> list[dict]:
    reports_dir = DOCS_DIR / "reports"
    if not reports_dir.exists():
        return []

    entries = []
    for f in sorted(reports_dir.glob("*.html"), reverse=True):
        stem = f.stem                      # 例如 2026-09-03-evening
        parts = stem.split("-")
        if len(parts) < 3:
            continue
        iso = "-".join(parts[:3])
        kind_key = parts[3] if len(parts) > 3 else "daily"
        kind = {"morning": "早報", "evening": "盤後", "holiday": "假日功課",
                "weekly": "週報", "monthly": "月報"}.get(kind_key, "報告")
        entries.append({
            "path": f"reports/{f.name}",
            "date": iso,
            "kind": kind,
            "title": f"{date_label(iso)}　{kind}",
            "date_label": date_label(iso),
        })
    return entries


def _read_json(name: str):
    """讀 render 階段自己吐的 docs/data/*.json，首頁儀表板組合資料用。
    檔案可能是舊資料（例如今天只跑早報沒跑盤後）或還不存在，一律容錯回 None。"""
    path = DOCS_DIR / "data" / f"{name}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def render_index() -> Path:
    cfg = load_config()
    reports = _collect_reports()
    themes = [decorate_theme(t) for t in db.list_themes("active")]
    themes.sort(key=lambda t: {"high": 0, "mid": 1, "low": 2}.get(t.get("confidence"), 3))

    market = db.latest_snapshot()
    if market and market.get("payload"):
        # 資料表欄位是固定的幾個，taiex_change_pct 等其餘欄位只存在 payload JSON 裡
        try:
            market = {**json.loads(market["payload"]), **market}
        except (json.JSONDecodeError, TypeError):
            pass
    heatmap_data = _read_json("heatmap")
    chips_data = _read_json("chips")
    scores_data = _read_json("scores")
    disposition_data = _read_json("disposition")

    top_industries = []
    if heatmap_data and heatmap_data.get("industries"):
        rows = [r for r in heatmap_data["industries"] if r.get("name") != "其他"]
        top_industries = sorted(rows, key=lambda r: abs(r["avg_change_pct"]), reverse=True)[:4]
        # 首頁這排本來是純 div、點了完全沒反應，使用者以為是「沒資料」。
        # 補上 slug，模板才能連到熱力圖對應產業的錨點。
        top_industries = [{**r, "slug": slugify(r["name"])} for r in top_industries]

    top_score = None
    if scores_data and scores_data.get("rows"):
        scored = [r for r in scores_data["rows"] if r.get("composite") is not None]
        if scored:
            top_score = max(scored, key=lambda r: r["composite"])

    disposition_count = 0
    if disposition_data:
        disposition_count = (len(disposition_data.get("disposition", []))
                             + len(disposition_data.get("trending", [])))

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    path = DOCS_DIR / "index.html"
    path.write_text(_env().get_template("index.html").render(
        site_title=cfg["site"]["title"],
        generated_at=now_tpe().strftime("%Y-%m-%d %H:%M"),
        rel="", nav_current="index",
        latest=reports[0] if reports else None,
        recent_reports=reports[:6],
        active_themes=themes[:10],
        top_themes=themes[:3],
        market=market,
        top_industries=top_industries,
        institutional=(chips_data or {}).get("institutional"),
        top_strong=((chips_data or {}).get("strong") or [])[:3],
        top_score=top_score,
        disposition_count=disposition_count,
    ), encoding="utf-8")
    return path


def render_archive() -> Path:
    cfg = load_config()
    reports = _collect_reports()

    grouped_map = defaultdict(list)
    for r in reports:
        grouped_map[r["date"][:7]].append(r)
    grouped = sorted(grouped_map.items(), reverse=True)

    path = DOCS_DIR / "archive.html"
    path.write_text(_env().get_template("archive.html").render(
        site_title=cfg["site"]["title"],
        generated_at=now_tpe().strftime("%Y-%m-%d %H:%M"),
        rel="", nav_current="archive",
        grouped=grouped,
    ), encoding="utf-8")
    return path


def render_themes_page() -> Path:
    cfg = load_config()
    path = DOCS_DIR / "themes.html"

    catalog_by_category = {}
    for t in db.list_catalog_themes():
        catalog_by_category.setdefault(t.get("category") or "其他", []).append(decorate_theme(t))

    # 還沒產出深度報告的題材，至少要看得到累積的追蹤軌跡，不然那張卡片
    # 除了一句摘要什麼都沒有，跟旁邊有「完整產業分析」連結的卡片一比就像壞掉。
    def with_timeline(t: dict) -> dict:
        view = decorate_theme(t)
        if not view.get("deep_dive_slug"):
            view["timeline"] = db.get_theme_timeline(t["id"])[-6:]
        return view

    path.write_text(_env().get_template("themes.html").render(
        site_title=cfg["site"]["title"],
        generated_at=now_tpe().strftime("%Y-%m-%d %H:%M"),
        rel="", nav_current="themes",
        active_themes=[with_timeline(t) for t in db.list_themes("active")],
        dormant_themes=[with_timeline(t) for t in db.list_themes("dormant")],
        catalog_by_category=catalog_by_category,
    ), encoding="utf-8")
    return path


def render_lookup_page() -> Path:
    """個股查詢頁：合併熱力圖（全市場漲跌幅）與評分頁（有完整分析的個股），
    產出 docs/data/stock_index.json 給前端 JS 做純前端搜尋，不需要後端。"""
    cfg = load_config()
    path = DOCS_DIR / "lookup.html"

    heatmap_data = _read_json("heatmap")
    scores_data = _read_json("scores")
    scored_codes = {r["code"] for r in (scores_data or {}).get("rows", [])}

    # 市場別一律以申報基本資料（t187ap03）為準，不要從熱力圖硬猜——熱力圖同時
    # 含上市與上櫃，全部標成 twse 會讓 3441 聯一光這種上櫃股被標錯市場。
    profiles = db.all_company_profiles()
    snap = _snapshot_meta()

    def market_of(code: str) -> str:
        return (profiles.get(code, {}).get("market")
                or snap.get(code, {}).get("market") or "")

    stocks = []
    seen = set()
    if heatmap_data:
        for industry in heatmap_data.get("industries", []):
            for s in industry.get("stocks", []):
                if s["code"] in seen:
                    continue
                seen.add(s["code"])
                stocks.append({
                    "code": s["code"], "name": s["name"],
                    "industry": industry.get("name", ""),
                    "market": market_of(s["code"]),
                    "change_pct": s.get("change_pct", 0),
                    "has_score": s["code"] in scored_codes,
                })

    # 熱力圖只涵蓋有當日行情的上市／上櫃，**興櫃完全不在裡面**（議價市場，
    # 刻意不混進熱力圖與強勢股掃描），剛掛牌的個股也還沒有行情——結果就是
    # 個股查詢頁搜不到任何興櫃股票。這裡再用「申報基本資料」＋「盤後快照」
    # 補一輪，讓搜尋涵蓋上市／上櫃／興櫃全市場。
    for code in sorted(set(profiles) | set(snap)):
        if code in seen:
            continue
        prof = profiles.get(code, {})
        market = market_of(code)
        name = prof.get("short_name") or snap.get(code, {}).get("name") or ""
        if not name:
            # 申報全名 → 短名（「健生實業股份有限公司」→「健生實業」），
            # 沒有中文短名時至少不要顯示空白。
            full = prof.get("full_name", "")
            for tail in ("股份有限公司", "有限公司", "公司"):
                if full.endswith(tail):
                    full = full[: -len(tail)]
                    break
            name = full
        seen.add(code)
        stocks.append({
            "code": code, "name": name,
            "industry": prof.get("industry", ""),
            "market": market, "change_pct": None,
            "has_score": code in scored_codes,
        })

    stocks.sort(key=lambda s: (not s["has_score"], s["code"]))
    _write_json("stock_index", {"stocks": stocks})

    path.write_text(_env().get_template("lookup.html").render(
        site_title=cfg["site"]["title"],
        generated_at=now_tpe().strftime("%Y-%m-%d %H:%M"),
        rel="", nav_current="lookup",
    ), encoding="utf-8")
    return path


def _snapshot_meta() -> dict[str, dict]:
    """讀 docs/data/tpex_hist/*.json，回傳 {代號: {"name":, "market":}}。

    上櫃／興櫃在熱力圖行情裡不一定出現（興櫃根本不進熱力圖，剛掛牌的也還沒有
    當日行情），中文名與市場別只有這份盤後快照有——個股查詢頁與個股資料頁
    都靠它補，否則 7925 健生、7686 捷立康那種剛掛牌的興櫃會變成「有代號沒名字」
    而且搜尋不到。"""
    out: dict[str, dict] = {}
    for p in (DOCS_DIR / "data" / "tpex_hist").glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        out[p.stem] = {"name": d.get("name", ""), "market": d.get("market", "")}
    return out


def _industry_peers(code: str, prof: dict, by_industry: dict, profiles: dict,
                    revenue: dict, analysis: dict, names: dict, snap: dict,
                    limit: int = 12) -> dict:
    """同一個申報產業別的其他個股（依最新月營收由大到小）。純申報值，不含推論。"""
    ind = (prof or {}).get("industry", "").strip()
    if not ind:
        return {}
    peers = []
    for c in by_industry.get(ind, []):
        if c == code or len(peers) >= limit:
            continue
        peers.append({"code": c, "name": _display_name(
            c, profiles.get(c, {}), analysis.get(c, {}), revenue.get(c, {}), names, snap)})
    return {"industry": ind, "peers": peers, "total": len(by_industry.get(ind, []))}


def _display_name(code: str, prof: dict, ana: dict, rev: dict,
                  names: dict, snap: dict) -> str:
    """個股顯示名。市場通用簡稱（台積電）優先於申報全名（台灣積體電路製造…）。

    company_profile.short_name 排第一，是因為它是唯一「進 git、離線也在」的
    簡稱來源：熱力圖索引不含興櫃，盤後快照目錄又是 gitignore 的（只存在
    chart-data 分支），只靠那兩個的話本機重繪就會退化成申報全名。
    """
    return ((prof or {}).get("short_name") or names.get(code, "")
            or snap.get(code, {}).get("name")
            or ana.get("name", "") or rev.get("name", "") or _short_name(prof))


def _short_name(prof: dict) -> str:
    """申報全名 → 顯示用短名（「健生實業股份有限公司」→「健生實業」）。"""
    full = (prof or {}).get("full_name", "") or ""
    for tail in ("股份有限公司", "有限公司", "公司"):
        if full.endswith(tail):
            return full[: -len(tail)]
    return full


def _write_json(name: str, data) -> None:
    """render 階段同步吐一份 JSON，跟 HTML 同源同資料，給未來的 App/前端直接讀，
    不用另外架 API——docs/data/*.json 本身也是靜態檔，GitHub Pages 直接served。"""
    data_dir = DOCS_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / f"{name}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def render_heatmap(industries: list[dict], date_label_str: str) -> Path:
    cfg = load_config()
    peak = max((abs(row["avg_change_pct"]) for row in industries), default=1.0) or 1.0
    cells = []
    for row in industries:
        pct = row["avg_change_pct"]
        intensity = min(abs(pct) / peak, 1.0)
        base = (219, 84, 74) if pct >= 0 else (79, 158, 113)
        alpha = 0.18 + intensity * 0.72
        # slug 當錨點：首頁「產業熱力」那排要能點進來直接展開對應的產業格子
        cells.append({**row, "slug": slugify(row["name"]),
                      "bg": f"rgba({base[0]},{base[1]},{base[2]},{alpha:.2f})"})
    path = DOCS_DIR / "heatmap.html"
    path.write_text(_env().get_template("heatmap.html").render(
        site_title=cfg["site"]["title"],
        generated_at=now_tpe().strftime("%Y-%m-%d %H:%M"),
        rel="", nav_current="heatmap", date_label=date_label_str,
        cells=cells,
    ), encoding="utf-8")
    _write_json("heatmap", {"date": date_label_str, "industries": industries})
    return path


def render_chips(inst: dict, margin_top: list[dict], strong: list[dict],
                 holders: list[dict], date_label_str: str,
                 inst_rank: dict | None = None) -> Path:
    cfg = load_config()
    path = DOCS_DIR / "chips.html"
    inst_rank = inst_rank or {}
    path.write_text(_env().get_template("chips.html").render(
        site_title=cfg["site"]["title"],
        generated_at=now_tpe().strftime("%Y-%m-%d %H:%M"),
        rel="", nav_current="chips", date_label=date_label_str,
        inst=inst, margin_top=margin_top, strong=strong, holders=holders,
        inst_rank=inst_rank,
    ), encoding="utf-8")
    _write_json("chips", {"date": date_label_str, "institutional": inst,
                          "margin_top": margin_top, "strong": strong,
                          "holders": holders, "inst_rank": inst_rank})
    return path


def build_inst_rank(detail: dict[str, dict], top_n: int = 10) -> dict:
    """個股法人買賣超 → 外資／投信／自營商各自的買超前 N ／賣超前 N。

    T86 的分項欄位本來就有，之前只用了「三大法人合計」，籌碼頁因此看不出
    是哪一個法人在買。單位由股換成張（1 張 = 1000 股），跟頁面其他地方一致。
    ETF／權證不濾掉——法人買賣超本來就會集中在 ETF，硬濾反而失真。
    """
    out: dict[str, dict] = {}
    for key in ("foreign", "trust", "dealer"):
        rows = [{"code": c, "name": v.get("name", ""), "lots": v.get(key, 0.0) / 1000.0}
                for c, v in detail.items() if v.get(key)]
        rows.sort(key=lambda r: r["lots"], reverse=True)
        out[key] = {
            "buy": [r for r in rows[:top_n] if r["lots"] > 0],
            "sell": [r for r in reversed(rows[-top_n:]) if r["lots"] < 0],
        }
    return out


def render_disposition(disposition: list[dict], trending: list[dict],
                       today_list: list[dict], date_label_str: str) -> Path:
    cfg = load_config()
    path = DOCS_DIR / "disposition.html"
    path.write_text(_env().get_template("disposition.html").render(
        site_title=cfg["site"]["title"],
        generated_at=now_tpe().strftime("%Y-%m-%d %H:%M"),
        rel="", nav_current="disposition", date_label=date_label_str,
        disposition=disposition, trending=trending, today_list=today_list,
    ), encoding="utf-8")
    _write_json("disposition", {"date": date_label_str, "disposition": disposition,
                                "trending": trending, "today": today_list})
    return path


def render_scores(rows: list[dict], date_label_str: str) -> Path:
    cfg = load_config()
    path = DOCS_DIR / "scores.html"
    path.write_text(_env().get_template("scores.html").render(
        site_title=cfg["site"]["title"],
        generated_at=now_tpe().strftime("%Y-%m-%d %H:%M"),
        rel="", nav_current="scores", date_label=date_label_str, rows=rows,
    ), encoding="utf-8")
    _write_json("scores", {"date": date_label_str, "rows": rows})
    return path


def rerender_market_pages() -> list[Path]:
    """用 docs/data/*.json 重建熱力圖／籌碼／評分／處置四頁。

    這四頁本來只有 run_evening() 帶著當天新抓的資料才會產出，`main.py site`
    完全碰不到它們——結果是「只改樣板」的修改（例如加一個共用區塊）要等到
    下一次盤後跑完才會生效，本機也沒辦法先看效果。這裡改成從盤後已經落地的
    JSON 重繪，資料還是同一份，只是樣板套用即時生效。

    JSON 不存在（還沒跑過盤後）就跳過那一頁，不當成錯誤。
    """
    out: list[Path] = []
    heat = _read_json("heatmap")
    if heat and heat.get("industries"):
        out.append(render_heatmap(heat["industries"], heat.get("date", "")))

    chips = _read_json("chips")
    if chips:
        out.append(render_chips(
            chips.get("institutional") or {}, chips.get("margin_top") or [],
            chips.get("strong") or [], chips.get("holders") or [],
            chips.get("date", ""), inst_rank=chips.get("inst_rank") or {}))

    scores = _read_json("scores")
    if scores and scores.get("rows"):
        out.append(render_scores(scores["rows"], scores.get("date", "")))

    disp = _read_json("disposition")
    if disp:
        out.append(render_disposition(
            disp.get("disposition") or [], disp.get("trending") or [],
            disp.get("today") or [], disp.get("date", "")))
    return out


def render_stock_page() -> Path:
    """獨立個股頁 docs/stock.html?code=XXXX。

    使用者反映「點開後希望有自己的頁面，不要長這樣（彈窗）」。刻意做成一頁吃
    query string，而不是每檔生一個 HTML：全市場 2300+ 檔各生一頁等於再多幾 MB
    的靜態檔要進 git，而內容本來就是前端從 stock_info/<code>.json 動態組的，
    一頁就夠。網址仍然是 stock.html?code=2330，可以分享、可以加書籤。
    版面與資料跟彈窗共用 stock-chart.js 的 renderInto()，不會兩邊長不一樣。
    """
    cfg = load_config()
    path = DOCS_DIR / "stock.html"
    path.write_text(_env().get_template("stock.html").render(
        site_title=cfg["site"]["title"],
        generated_at=now_tpe().strftime("%Y-%m-%d %H:%M"),
        rel="", nav_current="lookup",
    ), encoding="utf-8")
    return path


def render_site() -> list[Path]:
    """重建所有索引頁。每次跑完報告都要呼叫，索引才會包含最新內容。"""
    return [render_index(), render_archive(), render_themes_page(), render_lookup_page(),
            render_submit_page(), render_research_notes(),
            render_weekly_index(), render_monthly_deep_index(), render_picks_page(),
            render_intraday_page(), render_intraday_report_index(), render_stock_page(),
            render_stock_analysis_json(), render_stock_info(),
            *rerender_market_pages()]


def render_stock_analysis_json() -> Path:
    """把資料庫裡所有個股的公司介紹＋SWOT 匯出成 docs/data/stock_analysis.json，
    個股查詢頁的前端 JS 直接讀這個，點展開就看得到公司分析，不用後端。"""
    _write_json("stock_analysis", db.all_stock_analysis())
    return DOCS_DIR / "data" / "stock_analysis.json"


def render_stock_info() -> Path:
    """每檔個股一份 docs/data/stock_info/<code>.json，內容分成三層：

    1. profile：公司基本資料（公開資訊觀測站申報值，事實）
    2. rev：最新月營收＋YoY/MoM（政府開放資料，事實）
    3. themes / desc / swot：本站題材歸類與 LLM 產出的判讀（明確標示為判讀）

    做成一檔一個小檔案而不是一份大 JSON，是因為全市場有 2300 檔，
    彈窗只需要當下那一檔，不用讓每個頁面都載入幾 MB。
    """
    profiles = db.all_company_profiles()
    revenue = db.latest_monthly_revenue()
    analysis = db.all_stock_analysis()
    themes_by_code: dict[str, list[str]] = {}
    for t in db.list_themes_with_stocks():
        for s in t.get("stocks", []):
            code = str(s.get("code", "")).strip()
            if code:
                themes_by_code.setdefault(code, [])
                if t["name"] not in themes_by_code[code]:
                    themes_by_code[code].append(t["name"])

    names = {}
    try:
        idx = json.loads((DOCS_DIR / "data" / "stock_index.json").read_text(encoding="utf-8"))
        names = {s["code"]: s.get("name", "") for s in idx.get("stocks", []) if s.get("code")}
    except Exception:
        pass

    # 「同產業個股」：用申報產業別（t187ap03）分組，純事實、不做任何推論。
    # 全市場 2345 檔裡只有 167 檔被題材知識庫歸過類，其餘 2189 檔的「相關題材」
    # 區塊永遠是一句「尚未歸入任何題材」，等於整區沒東西可看。題材歸類本身
    # 需要逐檔查證、不能硬塞（那正是先前寫出假資料的原因），但「同一個申報
    # 產業別還有哪些公司」是申報值直接推出來的事實，可以每一檔都有。
    # 排序用最新月營收由大到小，讓使用者先看到該產業的主要公司。
    by_industry: dict[str, list[str]] = {}
    for code, prof in profiles.items():
        ind = (prof.get("industry") or "").strip()
        if ind:
            by_industry.setdefault(ind, []).append(code)
    for ind, members in by_industry.items():
        members.sort(key=lambda c: -(revenue.get(c, {}).get("revenue") or 0))

    out_dir = DOCS_DIR / "data" / "stock_info"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 有後端快照的上櫃／興櫃也要有檔案：前端靠 profile.market 決定要不要
    # 直接走快照，沒有這個標記就會先白打 36 次必定失敗的 TWSE 請求。
    snap = _snapshot_meta()
    snap_market = {c: m["market"] for c, m in snap.items()}

    # 個股資料頁的範圍＝有申報基本資料的公司（申報營業項目 t187ap03 全市場 2341 檔）
    # ＋有後端盤後快照的上櫃／興櫃。題材成員「只用來標註既有個股頁」，不會憑
    # 題材歸類就多生一頁——否則〔國際〕總經題材裡的 SPY／NVDA／MSFT 這些
    # 美股 ETF 也會被生成一份空的個股頁（沒有 profile／月營收／SWOT），
    # 那些標的是週報總經段落在講的，不屬於這裡的「全台股個股」範圍。
    codes = set(profiles) | set(revenue) | set(analysis) | set(snap_market)
    # 清掉不再屬於範圍的舊檔（例如曾經因題材歸類生成的美股 ETF 頁），
    # 避免 stock_info_index 與實體檔案對不上、彈窗載到殘檔。
    for stale in out_dir.glob("*.json"):
        if stale.stem not in codes:
            stale.unlink()
    index = []
    for code in sorted(codes):
        prof = dict(profiles.get(code, {}))
        if not prof.get("market") and snap_market.get(code):
            prof["market"] = snap_market[code]
        rev = revenue.get(code, {})
        ana = analysis.get(code, {})
        payload = {
            "code": code,
            # 名稱優先序刻意讓「市場通用短名」排最前面：月營收資料集帶的是
            # 申報全名，之前排在前面，彈窗標題就會出現「2330 台灣積體電路製造
            # 股份有限公司」而不是「2330 台積電」。後面兩層是給剛掛牌的興櫃
            # （7925 健生、7686 捷立康）用的，否則會變成有代號沒名字。
            "name": _display_name(code, prof, ana, rev, names, snap),
            "profile": {k: prof.get(k, "") for k in
                        ("full_name", "industry", "business", "capital",
                         "founded", "listed", "market", "website")} if prof else {},
            "rev": {k: rev.get(k) for k in
                    ("period", "revenue", "yoy", "mom", "cum_revenue", "cum_yoy")} if rev else {},
            "themes": themes_by_code.get(code, []),
            "industry_peers": _industry_peers(code, prof, by_industry, profiles,
                                              revenue, analysis, names, snap),
            "desc": ana.get("company_desc", ""),
            "swot": ana.get("swot", {}),
            "sources": ana.get("sources", []),
            "updated": ana.get("updated_at", ""),
        }
        (out_dir / f"{code}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        index.append(code)

    _write_json("stock_info_index", {"codes": index})
    print(f"[render] 個股資料頁 {len(index)} 檔（docs/data/stock_info/）")
    return DOCS_DIR / "data" / "stock_info_index.json"


def save_picks(breakout: list, new_listings: list, dark_horses: list, date_label_str: str) -> None:
    """盤後把三個選股訊號落地成 docs/data/picks.json，讓 render_picks_page() 有東西讀。
    這三個訊號本來只在每日報告 HTML 裡出現、沒有獨立頁面，使用者找不到——
    落地成 JSON 之後就能有一個固定的「選股雷達」頁。"""
    _write_json("picks", {
        "date_label": date_label_str,
        "breakout": breakout,
        "new_listings": new_listings,
        "dark_horses": dark_horses,
    })


def render_intraday_page() -> Path:
    """盤中強勢股：只是一層殼，實際資料由前端 JS 每 45 秒去 intraday-data 分支抓
    docs/data/intraday.json 重繪（盤中每分鐘更新、不觸發 Pages 重建）。"""
    cfg = load_config()
    path = DOCS_DIR / "intraday.html"
    path.write_text(_env().get_template("intraday.html").render(
        site_title=cfg["site"]["title"],
        generated_at=now_tpe().strftime("%Y-%m-%d %H:%M"),
        rel="", nav_current="intraday",
    ), encoding="utf-8")
    return path


def render_intraday_report(row: dict) -> Path:
    """單篇盤中快報 → docs/analysis/<date>-<code>.html。"""
    cfg = load_config()
    out_dir = DOCS_DIR / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{row['date']}-{row['code']}.html"
    path.write_text(_env().get_template("analysis.html").render(
        site_title=cfg["site"]["title"],
        generated_at=now_tpe().strftime("%Y-%m-%d %H:%M"),
        rel="../", nav_current="", r=row,
    ), encoding="utf-8")
    return path


def render_topic_report(slug: str, row: dict) -> Path:
    """使用者點播的主題報告 → docs/analysis/<date>-<slug>.html。

    沿用 CLAUDE.md 指定的 docs/analysis/ 資料夾（那裡本來就規劃成
    「<日期>-<股票代號或主題slug>.html」），跟盤中個股快報放在一起，
    由同一個清單頁列出。
    """
    cfg = load_config()
    out_dir = DOCS_DIR / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{row['date']}-{slug}.html"
    path.write_text(_env().get_template("topic_report.html").render(
        site_title=cfg["site"]["title"],
        generated_at=now_tpe().strftime("%Y-%m-%d %H:%M"),
        rel="../", nav_current="", r=row,
    ), encoding="utf-8")
    return path


def render_intraday_report_index() -> Path:
    """盤中快報清單 → docs/analysis/index.html。"""
    cfg = load_config()
    out_dir = DOCS_DIR / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "index.html"
    path.write_text(_env().get_template("analysis_index.html").render(
        site_title=cfg["site"]["title"],
        generated_at=now_tpe().strftime("%Y-%m-%d %H:%M"),
        rel="../", nav_current="",
        cap=cfg.get("intraday", {}).get("deep_report_daily_cap", 5),
        reports=db.all_intraday_reports(300),
        topic_reports=db.all_topic_reports(300),
    ), encoding="utf-8")
    return path


def render_picks_page() -> Path:
    """選股雷達：起漲點／新掛牌（含興櫃）／黑馬，讀 docs/data/picks.json。"""
    cfg = load_config()
    path = DOCS_DIR / "picks.html"
    data = _read_json("picks") or {}

    path.write_text(_env().get_template("picks.html").render(
        site_title=cfg["site"]["title"],
        generated_at=now_tpe().strftime("%Y-%m-%d %H:%M"),
        rel="", nav_current="picks",
        date_label=data.get("date_label", ""),
        breakout=data.get("breakout", []),
        new_listings=data.get("new_listings", []),
        dark_horses=data.get("dark_horses", []),
    ), encoding="utf-8")
    return path


def _render_pdf_index(subdir: str, json_key: str, nav_key: str, page_title: str,
                      description: str, empty_message: str) -> Path:
    """週報／深度月報首頁共用邏輯：兩者都是「一份 PDF 配一段摘要」的清單頁。

    刻意不讓產出 PDF 的 Routine 自己手寫這個 index.html——之前那樣做真的
    出過 bug（手寫的 nav 連結忘記加 ../，因為這頁在子目錄下，結果點什麼都
    404）。現在改成 Routine 只需要在 docs/data/<json_key>.json 追加一筆
    {filename, title, summary, date_label}，這裡統一用 base.html 樣板重繪，
    nav 的 rel 前綴一定是對的，不會再重蹈覆轍。
    """
    cfg = load_config()
    path = DOCS_DIR / subdir / "index.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    reports = _read_json(json_key) or []

    path.write_text(_env().get_template("pdf_report_index.html").render(
        site_title=cfg["site"]["title"],
        generated_at=now_tpe().strftime("%Y-%m-%d %H:%M"),
        rel="../", nav_current=nav_key,
        page_title=page_title, description=description,
        section_label="本" + ("週" if nav_key == "weekly" else "月") + "報告",
        reports=list(reversed(reports)), empty_message=empty_message,
    ), encoding="utf-8")
    return path


def render_weekly_index() -> Path:
    return _render_pdf_index(
        "weekly", "weekly_reports", "weekly", "週報：全球總經＋台股深度研究",
        "每週五盤後產出，涵蓋全球總體經濟與台股市場的深度研究，由 Claude 排程 agent 產出。"
        "分析文章形式，明確區分已驗證事實與推論，不構成投資建議。",
        "尚無週報，下一個週五盤後會開始更新這裡。")


def render_monthly_deep_index() -> Path:
    return _render_pdf_index(
        "monthly-deep", "monthly_deep_reports", "monthly-deep",
        "深度月報：全球總經＋台股結構＋焦點個股",
        "每月 1 號產出，聚焦全球總經趨勢、台股結構性變化，以及當月表現最突出的個股深度分析。"
        "跟每月 12 號的事後績效回顧是不同的兩份報告。",
        "尚無深度月報，下個月 1 號會開始更新這裡。")


def render_submit_page() -> Path:
    """使用者提交研究文章的入口，網站本身是純靜態站沒有後端。提供兩種提交方式：
    1. Google 表單（推薦給一般訪客）：頁面內用隱藏 iframe 送出表單，不用登入、
       不會跳轉頁面——真正的「零門檻」路徑，但需要使用者先在自己的 Google
       帳號設定好表單／試算表發布（見 CLAUDE.md），把三個公開網址填進
       config.yaml 的 research_intake。
    2. GitHub Issue（給熟悉 GitHub 的人，例如站長自己）：按下送出後開新分頁到
       預填好的「開新 Issue」網址，一鍵完成，不用密鑰。
    表單設定尚未填好時，頁面只顯示 GitHub Issue 這條路徑。"""
    cfg = load_config()
    path = DOCS_DIR / "submit.html"
    base_url = cfg["site"]["base_url"]  # https://<user>.github.io/<repo>
    repo_slug = base_url.split("://", 1)[-1].split(".github.io/", 1)
    repo_slug = f"{repo_slug[0].split('.')[0]}/{repo_slug[1]}" if len(repo_slug) == 2 else ""

    ri = cfg.get("research_intake", {})
    form_ready = bool(ri.get("google_form_action_url") and ri.get("google_form_entry_title")
                      and ri.get("google_form_entry_body"))

    path.write_text(_env().get_template("submit.html").render(
        site_title=cfg["site"]["title"],
        generated_at=now_tpe().strftime("%Y-%m-%d %H:%M"),
        rel="", nav_current="submit",
        repo_slug=repo_slug,
        form_ready=form_ready,
        form_action_url=ri.get("google_form_action_url", ""),
        form_entry_title=ri.get("google_form_entry_title", ""),
        form_entry_body=ri.get("google_form_entry_body", ""),
    ), encoding="utf-8")
    return path


def render_research_notes() -> Path:
    """已提交研究的處理結果：驗證狀態、摘要、有沒有真的回寫進題材庫。

    theme 標籤要能點進去看該題材的深度報告，不然只是看得到名字、點不開，
    使用者會以為壞掉——這裡額外查一次每個題材有沒有 deep_dive_slug，
    有的話補上連結。"""
    cfg = load_config()
    path = DOCS_DIR / "research.html"

    notes = db.list_research_notes()
    theme_slug_cache: dict[str, str | None] = {}
    for n in notes:
        for t in n.get("affected_themes", []):
            name = t.get("name", "")
            if name not in theme_slug_cache:
                theme_row = db.get_theme(name)
                theme_slug_cache[name] = (theme_row or {}).get("deep_dive_slug")
            t["slug"] = theme_slug_cache[name]

    path.write_text(_env().get_template("research.html").render(
        site_title=cfg["site"]["title"],
        generated_at=now_tpe().strftime("%Y-%m-%d %H:%M"),
        rel="", nav_current="research",
        notes=notes,
    ), encoding="utf-8")
    return path
