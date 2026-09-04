"""產業彙總：把個股行情依產業別分組算平均漲跌幅，給熱力圖用。

刻意用等權平均而非市值加權——市值資料另外要抓，等權夠用且不會被權值股蓋掉中小型股的動向。
"""
from __future__ import annotations

from collections import defaultdict


def aggregate_by_industry(quotes: list[dict], industry_map: dict[str, str],
                          min_stocks: int = 2) -> list[dict]:
    """回傳依平均漲跌幅排序的產業清單：[{name, avg_change_pct, count}, ...]。

    沒有產業分類的股票（多半是上櫃、或分類資料還沒更新到）歸進「其他」，
    「其他」樣本數通常很大、參考價值低，排序時放最後。
    """
    buckets: dict[str, list[float]] = defaultdict(list)
    for q in quotes:
        industry = industry_map.get(q.get("code", ""), "其他")
        pct = q.get("change_pct")
        if pct is not None:
            buckets[industry].append(pct)

    rows = []
    other = None
    for name, pcts in buckets.items():
        if len(pcts) < min_stocks:
            continue
        row = {"name": name, "avg_change_pct": round(sum(pcts) / len(pcts), 2),
               "count": len(pcts)}
        if name == "其他":
            other = row
        else:
            rows.append(row)

    rows.sort(key=lambda r: r["avg_change_pct"], reverse=True)
    if other:
        rows.append(other)
    return rows
