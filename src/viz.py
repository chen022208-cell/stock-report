"""輕量 SVG 圖表產生器。

GitHub Pages 是靜態站，刻意不引入任何 JS 圖表庫——
這幾種圖用字串拼 SVG 就夠，檔案小、載入快、無外部相依。
所有顏色走 CSS 變數（templates/base.html 定義），自動吃深色主題與台股紅漲綠跌。
"""
from __future__ import annotations

from html import escape

_CONF_RANK = {"low": 1, "mid": 2, "high": 3}


def _points(values: list[float], w: float, h: float, pad: float) -> list[tuple[float, float]]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    span = hi - lo or 1.0
    n = len(values)
    step = (w - 2 * pad) / (n - 1) if n > 1 else 0
    return [
        (pad + i * step, h - pad - (v - lo) / span * (h - 2 * pad))
        for i, v in enumerate(values)
    ]


def sparkline(values: list[float], width: int = 260, height: int = 60,
              stroke: str = "var(--gold)") -> str:
    """一條沒有座標軸的趨勢線，給題材信心度、指數走勢用。"""
    pts = _points(values, width, height, 6)
    if len(pts) < 2:
        return ""
    path = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    last_x, last_y = pts[-1]
    return (
        f'<svg viewBox="0 0 {width} {height}" class="spark" '
        f'preserveAspectRatio="none" role="img" aria-hidden="true">'
        f'<path d="{path}" fill="none" stroke="{stroke}" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round"/>'
        f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="2.6" fill="{stroke}"/>'
        f'</svg>'
    )


def confidence_trend(confidences: list[str], width: int = 260, height: int = 60) -> str:
    """把 ['low','mid','high',...] 畫成階梯折線。"""
    vals = [float(_CONF_RANK.get(c, 2)) for c in confidences]
    return sparkline(vals, width, height)


def diverging_bars(rows: list[tuple[str, float]], width: int = 420,
                   row_h: int = 26, unit: str = "") -> str:
    """以 0 為中軸的左右橫條，給法人買賣超趨勢用。

    rows = [(標籤, 數值), ...]，正值往右（紅／漲色），負值往左（綠／跌色）。
    """
    if not rows:
        return ""
    peak = max((abs(v) for _, v in rows), default=1.0) or 1.0
    label_w = 78
    bar_area = width - label_w - 60
    mid = label_w + bar_area / 2
    h = row_h * len(rows) + 8
    parts = [
        f'<svg viewBox="0 0 {width} {h}" class="dbars" role="img" '
        f'aria-label="法人買賣超趨勢">',
        f'<line x1="{mid}" y1="4" x2="{mid}" y2="{h - 4}" '
        f'stroke="var(--hairline)" stroke-width="1"/>',
    ]
    for i, (label, v) in enumerate(rows):
        y = 4 + i * row_h
        cy = y + row_h / 2
        length = abs(v) / peak * (bar_area / 2)
        color = "var(--red)" if v >= 0 else "var(--green)"
        x = mid if v >= 0 else mid - length
        parts.append(
            f'<text x="{label_w - 8}" y="{cy + 4:.0f}" text-anchor="end" '
            f'class="dbars-label">{escape(str(label))}</text>'
        )
        parts.append(
            f'<rect x="{x:.1f}" y="{y + 5:.0f}" width="{length:.1f}" '
            f'height="{row_h - 12}" rx="2" fill="{color}"/>'
        )
        parts.append(
            f'<text x="{width - 6}" y="{cy + 4:.0f}" text-anchor="end" '
            f'class="dbars-value">{v:+.0f}{escape(unit)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


VIZ_CSS = """
  .spark{width:100%; height:60px; display:block;}
  .dbars{width:100%; height:auto; display:block; margin:6px 0 4px;}
  .dbars-label{fill:var(--text-secondary); font-size:12px;
               font-family:'Noto Sans TC',sans-serif;}
  .dbars-value{fill:var(--text-muted); font-size:12px;
               font-family:'JetBrains Mono',monospace;}
"""
