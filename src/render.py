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

    path.write_text(_env().get_template("themes.html").render(
        site_title=cfg["site"]["title"],
        generated_at=now_tpe().strftime("%Y-%m-%d %H:%M"),
        rel="", nav_current="themes",
        active_themes=[decorate_theme(t) for t in db.list_themes("active")],
        dormant_themes=[decorate_theme(t) for t in db.list_themes("dormant")],
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
                    "change_pct": s.get("change_pct", 0),
                    "has_score": s["code"] in scored_codes,
                })
    stocks.sort(key=lambda s: (not s["has_score"], s["code"]))
    _write_json("stock_index", {"stocks": stocks})

    path.write_text(_env().get_template("lookup.html").render(
        site_title=cfg["site"]["title"],
        generated_at=now_tpe().strftime("%Y-%m-%d %H:%M"),
        rel="", nav_current="lookup",
    ), encoding="utf-8")
    return path


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
        cells.append({**row, "bg": f"rgba({base[0]},{base[1]},{base[2]},{alpha:.2f})"})
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
                 holders: list[dict], date_label_str: str) -> Path:
    cfg = load_config()
    path = DOCS_DIR / "chips.html"
    path.write_text(_env().get_template("chips.html").render(
        site_title=cfg["site"]["title"],
        generated_at=now_tpe().strftime("%Y-%m-%d %H:%M"),
        rel="", nav_current="chips", date_label=date_label_str,
        inst=inst, margin_top=margin_top, strong=strong, holders=holders,
    ), encoding="utf-8")
    _write_json("chips", {"date": date_label_str, "institutional": inst,
                          "margin_top": margin_top, "strong": strong, "holders": holders})
    return path


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


def render_site() -> list[Path]:
    """重建所有索引頁。每次跑完報告都要呼叫，索引才會包含最新內容。"""
    return [render_index(), render_archive(), render_themes_page(), render_lookup_page(),
            render_submit_page(), render_research_notes()]


def render_submit_page() -> Path:
    """使用者提交研究文章的入口。網站本身是純靜態站沒有後端，所以「上傳」的
    實際路徑是：使用者在這頁打好標題/內容，按下送出後由瀏覽器端 JS 組出一個
    預填好的 GitHub「開新 Issue」網址並開新分頁——使用者本來就是這個 repo
    的擁有者，用 GitHub Issue 當唯一需要的「後端」，不用另外架伺服器或存密鑰。"""
    cfg = load_config()
    path = DOCS_DIR / "submit.html"
    base_url = cfg["site"]["base_url"]  # https://<user>.github.io/<repo>
    repo_slug = base_url.split("://", 1)[-1].split(".github.io/", 1)
    repo_slug = f"{repo_slug[0].split('.')[0]}/{repo_slug[1]}" if len(repo_slug) == 2 else ""

    path.write_text(_env().get_template("submit.html").render(
        site_title=cfg["site"]["title"],
        generated_at=now_tpe().strftime("%Y-%m-%d %H:%M"),
        rel="", nav_current="submit",
        repo_slug=repo_slug,
    ), encoding="utf-8")
    return path


def render_research_notes() -> Path:
    """已提交研究的處理結果：驗證狀態、摘要、有沒有真的回寫進題材庫。"""
    cfg = load_config()
    path = DOCS_DIR / "research.html"

    path.write_text(_env().get_template("research.html").render(
        site_title=cfg["site"]["title"],
        generated_at=now_tpe().strftime("%Y-%m-%d %H:%M"),
        rel="", nav_current="research",
        notes=db.list_research_notes(),
    ), encoding="utf-8")
    return path
