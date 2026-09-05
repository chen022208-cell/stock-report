"""DRY_RUN 用的假資料。

作用：沒有網路、沒有 API key 時也能把整條流程跑完，用來驗證版面與邏輯。
上線後設 DRY_RUN=0 就會走真實 API，這個檔案不會被呼叫到。
"""
from __future__ import annotations

import random
from datetime import date, timedelta

random.seed(42)   # 固定亂數，讓每次測試輸出一致


def index_summary() -> dict:
    return {
        "taiex_close": 24586.12,
        "taiex_change": 186.42,
        "taiex_change_pct": 0.76,
        "turnover": 384_200_000_000,
        "advancers": 612,
        "decliners": 398,
    }


def institutional_net() -> dict:
    return {
        "foreign_net": 48.2,
        "trust_net": 18.6,
        "dealer_net": -4.4,
        "total_net": 62.4,
    }


_MOCK_STOCKS = [
    ("1590", "亞德客-KY", 6.8, 4.2), ("2049", "上銀", 5.9, 3.1),
    ("4551", "智伸科", 8.2, 5.4), ("2330", "台積電", 1.8, 1.2),
    ("3017", "奇鋐", 6.1, 2.8), ("3661", "世芯-KY", 5.2, 2.4),
    ("6187", "萬潤", 9.4, 6.7), ("2454", "聯發科", 2.1, 1.4),
    ("3324", "雙鴻", 5.5, 2.9), ("1521", "大銀微系統", 7.3, 3.8),
]


def daily_quotes() -> list[dict]:
    quotes = []
    for code, name, pct, vol_ratio in _MOCK_STOCKS:
        close = round(random.uniform(80, 900), 1)
        quotes.append({
            "code": code, "name": name, "close": close,
            "change": round(close * pct / 100, 2), "change_pct": pct,
            "volume": int(vol_ratio * 5_000_000),
            "turnover": int(close * vol_ratio * 5_000_000),
            "volume_ratio": vol_ratio,
        })
    # 補一些平盤股，讓掃描器的篩選邏輯真的有東西可以濾
    for i in range(40):
        quotes.append({
            "code": f"9{i:03d}", "name": f"測試股{i}", "close": 50.0,
            "change": 0.2, "change_pct": 0.4, "volume": 500_000,
            "turnover": 25_000_000, "volume_ratio": 0.9,
        })
    return quotes


def stock_history(code: str, days: int = 120) -> list[dict]:
    """生一段有趨勢的假 K 線，讓技術指標算出來的結果有意義。"""
    rows = []
    price = 100.0
    drift = 0.004 if code in ("1590", "2049", "4551") else -0.001
    today = date.today()
    for i in range(days, 0, -1):
        price *= (1 + drift + random.gauss(0, 0.015))
        price = max(price, 5.0)
        rows.append({
            "date": (today - timedelta(days=i)).isoformat(),
            "open": round(price * 0.995, 2), "high": round(price * 1.012, 2),
            "low": round(price * 0.988, 2), "close": round(price, 2),
            "volume": int(random.uniform(3_000, 12_000) * 1000),
        })
    return rows


def tpex_daily_quotes() -> list[dict]:
    quotes = []
    for code, name, pct in [("6187", "萬潤", 6.7), ("3374", "精材", 5.2), ("5347", "世界", 2.1)]:
        close = round(random.uniform(50, 400), 1)
        quotes.append({
            "code": code, "name": name, "close": close,
            "change": round(close * pct / 100, 2), "change_pct": pct,
            "volume": 3_000_000, "turnover": int(close * 3_000_000), "market": "tpex",
        })
    for i in range(20):
        quotes.append({
            "code": f"8{i:03d}", "name": f"上櫃測試{i}", "close": 40.0,
            "change": 0.1, "change_pct": 0.25, "volume": 300_000,
            "turnover": 12_000_000, "market": "tpex",
        })
    return quotes


def margin_by_stock() -> dict[str, dict]:
    return {
        "1590": {"margin_balance": 4200.0, "margin_change": 320.0,
                 "short_balance": 80.0, "short_change": -10.0},
        "2049": {"margin_balance": 3100.0, "margin_change": -150.0,
                 "short_balance": 60.0, "short_change": 5.0},
        "6187": {"margin_balance": 900.0, "margin_change": 210.0,
                 "short_balance": 40.0, "short_change": 30.0},
    }


def fred_series(series_id: str) -> list[dict]:
    return [{"date": "2026-08-01", "value": 4.2}, {"date": "2026-09-01", "value": 4.1}]


def fred_snapshot() -> list[dict]:
    return [
        {"name": "美國 CPI（年增率換算另計）", "date": "2026-08-01", "value": 314.2, "change": 0.6},
        {"name": "美國失業率", "date": "2026-08-01", "value": 4.1, "change": -0.1},
        {"name": "10年期公債殖利率", "date": "2026-09-01", "value": 4.18, "change": 0.03},
        {"name": "聯邦資金利率", "date": "2026-09-01", "value": 4.75, "change": 0.0},
    ]


def stock_headlines(code: str) -> list[str]:
    return [f"{code} 公司近期營運概況報導", f"{code} 法人關注訂單能見度"]


def news_sentiment_batch() -> dict[str, float]:
    return {"1590": 78, "2049": 62, "6187": 45, "4551": 70, "2330": 66}


def stock_analysis_batch() -> dict[str, dict]:
    template = {
        "company_desc": "自動化傳動元件廠商，產品用於半導體設備與工業自動化產線，客戶集中在少數大型設備商。",
        "swot": {
            "strengths": "技術門檻高於一般傳動元件廠，是主要客戶的長期認證供應商。",
            "weaknesses": "客戶集中度高，單一大客戶砍單會直接反映在營收上。",
            "opportunities": "機器人與半導體設備資本支出上修，帶動訂單能見度延長。",
            "threats": "（推論）陸廠低價競爭壓縮中低階產品毛利率。",
        },
        "price_reason": "技術面站上季線＋法人買超同步出現，加上所屬題材信心度為高，"
                        "訊號一致指向法人態度轉多，而非單純跟隨大盤。",
    }
    return {code: template for code in ("1590", "3017", "4551", "1521", "6187")}


def catalog_theme_research_batch(themes: list[dict]) -> dict[str, dict]:
    """DRY_RUN 用：從既有的假股票池輪流配對代表股給每個題材，其中第一個題材
    刻意配到兩檔漲幅都 >=3% 的股票（1590/2049），讓測試能跑到「當紅」判定
    跟後續深度分析的完整路徑，不用真的懂題材内容。"""
    pool = [{"code": c, "name": n} for c, n, _, _ in _MOCK_STOCKS]
    out = {}
    for i, t in enumerate(themes):
        picks = [pool[i % len(pool)], pool[(i + 1) % len(pool)]]
        out[t["name"]] = {"summary": t.get("summary", ""), "stocks": picks}
    return out


def revenue_yoy() -> dict[str, float]:
    return {"1590": 18.4, "2049": 12.1, "6187": 5.2, "4551": 22.7, "2330": 9.8,
            "3017": -3.4, "3661": 14.0}


def disposition_stocks() -> list[dict]:
    return [
        {"code": "3008", "name": "大立光", "period": "115/09/03～115/09/09",
         "measure": "第一次處置", "reason": "連續三次"},
    ]


def attention_trending() -> list[dict]:
    return [
        {"code": "6187", "name": "萬潤", "note": "115年9月2日至115年9月4日連續三次"},
        {"code": "4551", "name": "智伸科", "note": "115年9月3日至115年9月4日連續二次"},
    ]


def attention_today() -> list[dict]:
    return [
        {"code": "3374", "name": "精材", "info": "股價漲跌幅度過高"},
    ]


def holder_concentration() -> dict[str, float]:
    return {"2330": 84.8, "1590": 62.3, "2049": 58.1, "6187": 41.2, "4551": 37.5}


def industry_map() -> dict[str, str]:
    return {
        "1590": "其他電子業", "2049": "其他電子業", "4551": "機械業", "2330": "半導體業",
        "3017": "電子零組件業", "3661": "半導體業", "6187": "半導體業", "2454": "半導體業",
        "3324": "電子零組件業", "1521": "電機機械業", "1101": "水泥工業", "2891": "金融業",
    }


def listing_dates_twse() -> dict[str, str]:
    # 大部分是老牌股（上市日期很久以前），6187 假設是近期新掛牌，測試「新掛牌觀察」用
    from datetime import date, timedelta
    recent = (date.today() - timedelta(days=45)).strftime("%Y%m%d")
    return {"1590": "20140103", "2049": "19900101", "4551": "19960101",
            "2330": "19940905", "3017": "20000101", "3661": "20120101",
            "6187": recent, "2454": "20010101", "3324": "20110101",
            "1521": "19970101", "1101": "19620209", "2891": "19900101"}


def listing_dates_tpex() -> dict[str, str]:
    return {}


def listing_dates_esb() -> dict[str, str]:
    from datetime import date, timedelta
    recent = (date.today() - timedelta(days=60)).strftime("%Y%m%d")
    return {"7811": recent}


def esb_quotes() -> dict[str, dict]:
    return {"7811": {"code": "7811", "name": "測試興櫃股", "close": 55.0, "change": 3.5,
                     "change_pct": 6.79, "volume": 12000, "turnover": 0.0, "market": "esb"}}


def international_markets() -> dict:
    return {
        "indices": [
            {"name": "道瓊", "close": 45218.0, "change_pct": -0.32},
            {"name": "那斯達克", "close": 19864.0, "change_pct": 0.58},
            {"name": "S&P 500", "close": 6142.0, "change_pct": 0.21},
            {"name": "費半 SOX", "close": 5712.0, "change_pct": 1.24},
        ],
        "macro": [
            {"name": "美元指數", "value": "102.4"},
            {"name": "VIX", "value": "14.2"},
            {"name": "10年美債殖利率", "value": "4.18%"},
            {"name": "台積電 ADR", "value": "+1.1%"},
        ],
    }


def earnings_calls() -> list[dict]:
    return [
        {"code": "2049", "name": "上銀", "time": "14:00",
         "note": "市場關注機器人減速機訂單能見度"},
        {"code": "3017", "name": "奇鋐", "time": "15:00",
         "note": "AI 伺服器散熱出貨展望"},
    ]


def closure_recap(days: int = 6) -> dict:
    names = ["S&P 500", "費半 SOX", "那斯達克", "台積電 ADR"]
    rows = []
    base = date.today()
    for i in range(days, 0, -1):
        d = (base - timedelta(days=i)).isoformat()
        rows.append({"date": d, "changes": {
            n: round(random.uniform(-1.8, 2.2), 2) for n in names
        }})
    cumulative = {n: round(sum(r["changes"][n] for r in rows), 2) for n in names}
    return {"rows": rows, "cumulative": cumulative}


def global_headlines() -> list[dict]:
    return [
        {"title": "Broadcom raises AI chip forecast as Big Tech capex keeps climbing",
         "source": "Reuters", "date": "2026-09-03"},
        {"title": "Fed minutes signal one more cut this year as inflation cools",
         "source": "Bloomberg", "date": "2026-09-02"},
        {"title": "Nvidia data-center revenue beats; supply still tight into 2027",
         "source": "WSJ", "date": "2026-09-02"},
        {"title": "Humanoid robot orders surge as automakers expand pilot lines",
         "source": "Nikkei", "date": "2026-09-01"},
        {"title": "Oil slips on demand worries; OPEC+ holds output steady",
         "source": "Reuters", "date": "2026-09-01"},
    ]


def sector_performance() -> list[dict]:
    return [
        {"name": "半導體", "ticker": "SMH", "ret_5d": 3.4, "rel_strength": 1.9},
        {"name": "科技", "ticker": "XLK", "ret_5d": 2.1, "rel_strength": 0.6},
        {"name": "軟體", "ticker": "IGV", "ret_5d": 1.2, "rel_strength": -0.3},
        {"name": "生技", "ticker": "XBI", "ret_5d": -0.8, "rel_strength": -2.3},
        {"name": "能源", "ticker": "XLE", "ret_5d": -1.6, "rel_strength": -3.1},
    ]


def global_theme_digest() -> dict:
    return {
        "macro_note": "資金續往 AI 供應鏈集中，降息預期支撐風險偏好，能源與防禦類股走弱。",
        "themes": [
            {"name": "AI 資本支出續揚", "summary": "雲端大廠上修資本支出，晶片與伺服器供應鏈訂單能見度延長至 2027。",
             "confidence": "high", "verdict": "real",
             "drivers": "Broadcom 上修財測、Nvidia 資料中心營收優於預期、雲端 capex 指引全面上調",
             "us_tickers": ["NVDA", "AVGO", "AMD"],
             "tw_readthrough": "台積電、伺服器代工（廣達、緯創）、散熱與 ABF 載板族群"},
            {"name": "降息路徑明朗", "summary": "通膨降溫，市場定價今年再一碼，利率敏感型資產受惠。",
             "confidence": "mid", "verdict": "real",
             "drivers": "Fed 會議紀要偏鴿、核心 PCE 連兩月放緩",
             "us_tickers": ["XLF", "IWM"],
             "tw_readthrough": "金融股、高殖利率權值股、有匯損疑慮的出口電子偏空"},
            {"name": "人形機器人放量", "summary": "車廠擴大機器人試產線，減速機與伺服馬達拉貨。",
             "confidence": "mid", "verdict": "watch",
             "drivers": "多家車廠公布機器人導入時程，日系機構上修產業預估",
             "us_tickers": ["TSLA"],
             "tw_readthrough": "上銀、亞德客-KY、大銀微系統等傳動元件廠"},
        ],
    }


def supply_chain_structure() -> dict:
    return {
        "upstream": {"label": "關鍵零組件", "companies": [
            {"code": "1590", "name": "亞德客-KY", "role": "核心"},
        ]},
        "midstream": {"label": "系統整合／組裝", "companies": [
            {"code": "2049", "name": "上銀", "role": "核心"},
            {"code": "1521", "name": "大銀微系統", "role": "邊緣"},
        ]},
        "downstream": {"label": "終端應用", "companies": []},
        "peers": [{"market": "US", "ticker": "TSLA", "name": "Tesla"}],
    }


def research_form_responses() -> list[dict]:
    return [
        {"timestamp": "2026/09/06 上午 9:00:00", "title": "測試：機器人減速機訂單消息",
         "body": "某產業媒體報導，日系機器人大廠上修今年資本支出計畫，帶動台廠上銀、"
                "大銀微系統的減速機訂單能見度延長至明年上半年。"},
    ]


def research_analysis() -> dict:
    return {
        "summary": "文章主張機器人減速機供應鏈訂單能見度延長至明年，並點名上銀與大銀微系統受惠。",
        "verified": "unverified",
        "verification_note": "文章引用單一產業消息來源的訂單預估，屬於無法獨立驗證的具體數字。",
        "affected_themes": [{"name": "機器人減速機", "impact": "文章佐證題材延續性，但訂單數字本身無法驗證"}],
        "affected_stocks": [{"code": "2049", "name": "上銀", "impact": "文章點名為主要受惠廠商之一"}],
    }


def llm_theme_response() -> dict:
    """DRY_RUN 時代替 Claude API 回傳的題材聚類結果。"""
    return {
        "themes": [
            {
                "name": "機器人減速機",
                "summary": "日系機器人大廠上修資本支出，帶動台廠訂單能見度轉佳。",
                "confidence": "high",
                "verdict": "real",
                "reasoning": "產業鏈上下游同步表態，外資投信同步買超，月營收年增加速。",
                "stocks": [
                    {"code": "1590", "name": "亞德客-KY"},
                    {"code": "2049", "name": "上銀"},
                    {"code": "1521", "name": "大銀微系統"},
                ],
            },
            {
                "name": "AI 伺服器散熱",
                "summary": "液冷散熱滲透率提升，台廠散熱模組訂單同步走揚。",
                "confidence": "mid",
                "verdict": "real",
                "reasoning": "族群廣度足夠，但部分個股營收尚未同步反映。",
                "stocks": [
                    {"code": "3017", "name": "奇鋐"},
                    {"code": "3324", "name": "雙鴻"},
                    {"code": "2330", "name": "台積電"},
                ],
            },
        ],
        "orphans": [
            {
                "code": "6187", "name": "萬潤",
                "reason": "爆量上漲但無同族群呼應，亦無明確題材脈絡。",
            }
        ],
    }
