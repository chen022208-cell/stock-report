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
VERDICT_LABEL = {"real": "偏真實", "watch": "待觀察", "unknown": "未判定"}

WEEKDAY = ["一", "二", "三", "四", "五", "六", "日"]


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
    )


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


def render_article(theme: dict, article: dict, slug: str) -> Path:
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


def render_index() -> Path:
    cfg = load_config()
    reports = _collect_reports()
    themes = [decorate_theme(t) for t in db.list_themes("active")]

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    path = DOCS_DIR / "index.html"
    path.write_text(_env().get_template("index.html").render(
        site_title=cfg["site"]["title"],
        generated_at=now_tpe().strftime("%Y-%m-%d %H:%M"),
        rel="",
        latest=reports[0] if reports else None,
        recent_reports=reports[:15],
        active_themes=themes[:10],
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
        rel="",
        grouped=grouped,
    ), encoding="utf-8")
    return path


def render_themes_page() -> Path:
    cfg = load_config()
    path = DOCS_DIR / "themes.html"
    path.write_text(_env().get_template("themes.html").render(
        site_title=cfg["site"]["title"],
        generated_at=now_tpe().strftime("%Y-%m-%d %H:%M"),
        rel="",
        active_themes=[decorate_theme(t) for t in db.list_themes("active")],
        dormant_themes=[decorate_theme(t) for t in db.list_themes("dormant")],
    ), encoding="utf-8")
    return path


def render_site() -> list[Path]:
    """重建所有索引頁。每次跑完報告都要呼叫，索引才會包含最新內容。"""
    return [render_index(), render_archive(), render_themes_page()]
