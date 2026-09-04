"""AI 五面向評分：技術／籌碼／基本／題材四軸是規則計算，新聞面靠 LLM 批次判讀
（見 llm.news_sentiment_batch，只在 main.py 對候選股跑，不是對全市場，成本可控）。
輪到某軸真的沒資料時該軸留 None，前端顯示「未評分」，不硬湊一個假分數。
每一軸都是 0–100，數字本身沒有絕對意義，拿來跟同批候選股比較排序才有意義。
"""
from __future__ import annotations

TONE_SCORE = {"bull": 85, "neutral": 55, "bear": 35, "warn": 30}


def _clip(v: float, lo: float = 0, hi: float = 100) -> float:
    return max(lo, min(hi, v))


def technical_score(grade: dict) -> float | None:
    if not grade or grade.get("tone") not in TONE_SCORE:
        return None
    return float(TONE_SCORE[grade["tone"]])


def chip_score(inst_net: float | None, margin_change: float | None) -> float | None:
    """法人買賣超 + 融資增減，都是正向規模影響分數，用 tanh 式壓縮避免極端值把分數打死。"""
    if inst_net is None and margin_change is None:
        return None
    score = 50.0
    if inst_net:
        score += _clip(inst_net / 2000 * 20, -20, 20)   # 買超 2000 張約 +20 分
    if margin_change:
        score += _clip(margin_change / 3000 * 10, -10, 10)
    return round(_clip(score), 1)


def fundamental_score(revenue_yoy: float | None) -> float | None:
    if revenue_yoy is None:
        return None
    # 年增 0% = 50 分，每 +/-10 個百分點約 +/-15 分，上下限鎖住
    return round(_clip(50 + revenue_yoy * 1.5), 1)


def theme_score(confidence: str | None, is_core: bool = True) -> float | None:
    if not confidence:
        return None
    base = {"high": 85, "mid": 60, "low": 35}.get(confidence, 50)
    return float(base if is_core else base - 15)


def composite(scores: dict[str, float | None]) -> float | None:
    """有算出來的軸才計入平均，缺的軸不拖累分數也不灌水。"""
    valid = [v for v in scores.values() if v is not None]
    if not valid:
        return None
    return round(sum(valid) / len(valid), 1)


def score_stock(*, grade: dict | None = None, inst_net: float | None = None,
                margin_change: float | None = None, revenue_yoy: float | None = None,
                theme_confidence: str | None = None,
                news: float | None = None) -> dict:
    axes = {
        "technical": technical_score(grade) if grade else None,
        "chip": chip_score(inst_net, margin_change),
        "fundamental": fundamental_score(revenue_yoy),
        "theme": theme_score(theme_confidence),
        "news": round(_clip(news), 1) if news is not None else None,
    }
    return {**axes, "composite": composite(axes)}
