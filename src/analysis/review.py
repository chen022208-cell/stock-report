"""事後驗證：回頭檢查過去的判斷準不準。

這是整套系統從「播報工具」變成「分析系統」的關鍵。
核心指標是超額報酬（個股報酬 − 大盤同期報酬）——
大盤漲 10% 時個股漲 8%，那不叫成功，那叫落後。
"""
from __future__ import annotations

from .. import db


def run_review(today: str, horizons: list[int], price_fn, index_fn) -> dict:
    """對到期的判斷快照做驗證。

    price_fn(code, date) -> float | None
    index_fn(date) -> float | None
    由呼叫端注入，測試時可替換成假資料。
    """
    summary = {}

    for horizon in horizons:
        pending = db.pending_reviews(today, horizon)
        done = 0

        for snap in pending:
            now_price = price_fn(snap["stock_code"], today)
            now_index = index_fn(today)
            if not now_price or not now_index:
                continue
            if not snap.get("price_at_call") or not snap.get("index_at_call"):
                continue

            stock_ret = (now_price / snap["price_at_call"] - 1) * 100
            index_ret = (now_index / snap["index_at_call"] - 1) * 100
            db.save_review(snap["id"], horizon, today,
                           round(stock_ret, 2), round(index_ret, 2))
            done += 1

        summary[horizon] = {
            "reviewed": done,
            "scorecard": db.review_scorecard(horizon),
        }

    return summary


def format_scorecard(scorecard: list[dict]) -> str:
    """把統計結果翻成一句人話的結論。"""
    if not scorecard:
        return "尚無足夠的驗證樣本，需再累積一段時間。"

    by_conf = {row["confidence"]: row for row in scorecard}
    high, mid = by_conf.get("high"), by_conf.get("mid")

    if high and mid and high["n"] >= 3 and mid["n"] >= 3:
        if high["avg_excess"] > mid["avg_excess"]:
            return (f"高信心度組平均超額報酬 {high['avg_excess']}%（勝率 {high['win_rate']}%），"
                    f"優於中信心度組的 {mid['avg_excess']}%，判斷邏輯目前有效。")
        return (f"高信心度組平均超額報酬 {high['avg_excess']}% 並未優於中信心度組的 "
                f"{mid['avg_excess']}%，信心度的加權方式需要檢討。")

    total = sum(r["n"] for r in scorecard)
    return f"目前累積 {total} 筆驗證樣本，樣本數仍偏少，結論僅供參考。"
