"""Claude API 封裝。

省成本的兩個關鍵設計：
1. 批次呼叫 —— 當日所有候選股一次丟進去聚類，不是一檔問一次。
2. 深度報告只在題材通過門檻時才產出，不是每天每個題材都寫。
"""
from __future__ import annotations

import json
import re

import requests

from .config import ANTHROPIC_API_KEY, DRY_RUN, load_config
from .fetchers import mock

API_URL = "https://api.anthropic.com/v1/messages"


def _call(system: str, user: str, max_tokens: int | None = None) -> str:
    cfg = load_config()["llm"]
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("缺少 ANTHROPIC_API_KEY 環境變數")

    resp = requests.post(
        API_URL,
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": cfg["model"],
            "max_tokens": max_tokens or cfg["max_tokens"],
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    return "".join(block.get("text", "") for block in data.get("content", []))


def _parse_json(text: str) -> dict:
    """LLM 偶爾會包 markdown code fence，剝掉再解析。"""
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    print("[llm] JSON 解析失敗，回傳空結果")
    return {}


# ── 題材聚類 ───────────────────────────────────────────
CLUSTER_SYSTEM = """你是一位有 20 年經驗的台股產業分析師。

任務：從今日強勢股清單中，歸納出共同的題材敘事，並判斷題材真假。

判斷準則（重要）：
- 真題材：產業鏈上下游同步表態、法人買超、營收有對應成長、國際同業有呼應
- 假題材／炒作：只有孤立一兩檔在噴、融資暴增但法人未跟進、股價漲但基本面無對應

孤立訊號處理（重要）：
若某檔個股找不到同族群呼應（少於 2 檔相關個股），不要硬套題材敘事給它。
硬套會產出牽強甚至誤導的分析。請把它放進 orphans，說明為何無法歸類。

信心度：high / mid / low
真假判定：real / watch

只輸出 JSON，不要有任何前後說明文字或 markdown 標記，格式如下：
{
  "themes": [
    {"name": "題材名稱", "summary": "一句話說明", "confidence": "high",
     "verdict": "real", "reasoning": "判斷理由",
     "stocks": [{"code": "1234", "name": "公司名"}]}
  ],
  "orphans": [{"code": "1234", "name": "公司名", "reason": "無法歸類的原因"}]
}"""


def cluster_themes(strong_stocks: list[dict], context: str = "") -> dict:
    if DRY_RUN:
        return mock.llm_theme_response()

    lines = [
        f"{s['code']} {s['name']}　漲幅 {s.get('change_pct', 0)}%　"
        f"量能 {s.get('volume_ratio', 'N/A')} 倍"
        for s in strong_stocks
    ]
    user = "今日強勢股清單：\n" + "\n".join(lines)
    if context:
        user += f"\n\n補充資訊：\n{context}"

    try:
        return _parse_json(_call(CLUSTER_SYSTEM, user))
    except Exception as exc:
        print(f"[llm] 題材聚類失敗：{exc}")
        return {"themes": [], "orphans": []}


# ── 每日評論 ───────────────────────────────────────────
COMMENTARY_SYSTEM = """你是台股資深分析師，替一位有程式與投資背景的讀者撰寫盤前/盤後短評。
這篇文字會直接推播到讀者的手機通知，不是只放在網頁裡，所以最重要的事實要放最前面。

如果 type 是 "morning"（早報）：
- 開頭第一句就要點出「今天對台股影響最大」的一件國際事件或數據（不是隨便列指數漲跌）
- global_themes 欄位如果有內容，要講清楚這對台股哪個族群、哪些個股是順風還是逆風
- 沒有明顯重大事件時要老實說「隔夜盤面平淡，無單一事件主導」，不要硬掰一個出來

輸入的 JSON 裡如果有這些欄位，一定要在評論中實際運用，不要只看大盤數字：
- watch_stocks：五面向評分較高的候選股，要點名至少 1-2 檔，說明為什麼值得留意
  （哪一軸分數突出：技術面轉強、籌碼集中、營收動能、還是題材帶動）
- disposition_summary：處置中／注意累計接近門檻的股票概況，有的話要提醒風險
  （不是叫人避開，是讓讀者知道這些股票流動性受限）
- intl_themes：近期追蹤中的國際題材，跟今天台股的資金流向對照，
  講出「符合」或「背離」——例如國際 AI 資本支出題材強，但台股相關股today表現平淡，
  這種背離比順風時更值得寫出來

要求：
- 繁體中文，4-6 句，直接寫結論不要鋪陳
- 解讀數字之間的關聯（資金輪動、內外資態度差異、台股與國際的連動或背離），不要只複述數字
- 不要用「投資建議」的語氣，這是資訊整理不是推薦
只輸出評論本文。"""


def market_commentary(payload: dict) -> str:
    if DRY_RUN:
        return ("費半領漲那斯達克，AI 伺服器供應鏈延續強勢，台積電 ADR 同步走高，"
                "對台股電子權值股偏正面。外資與投信同步買超，資金集中在機器人與散熱族群，"
                "傳產金融表現平淡，顯示盤面集中度偏高。留意 1590 亞德客-KY，"
                "技術面與題材面評分同步走高，機器人減速機供應鏈的動能持續獲得法人與籌碼面驗證。"
                "3008 大立光目前處於處置期間，交易流動性受限，追蹤但不宜貿然進出。")
    try:
        return _call(COMMENTARY_SYSTEM,
                     json.dumps(payload, ensure_ascii=False, indent=2), 1200).strip()
    except Exception as exc:
        print(f"[llm] 評論產出失敗：{exc}")
        return ""


# ── 國際題材彙整 ───────────────────────────────────────
GLOBAL_SYSTEM = """你是一位追蹤全球資金流與產業輪動的策略分析師。

輸入：一週的國際財經頭條（來源含 Reuters/Bloomberg/WSJ 等）＋ 美股類股 ETF 近期表現。

任務：
1. 歸納本週國際市場的 2-4 個當紅題材敘事（AI 資本支出、降息路徑、地緣風險…）。
2. 每個題材判斷「機構態度一致性」：頭條與類股表現是否互相驗證。
   - 高：多家媒體同向報導 + 對應類股走強 + 無明顯反面聲音
   - 中：敘事成形但類股表現分歧，或有雜音
   - 低：只有零星報導，缺乏價格面佐證
3. 標出每個題材對應的美股代表標的，以及對「台股」的傳導路徑（哪些台廠或族群受影響）。

只輸出 JSON，不要 markdown 標記：
{
  "macro_note": "一句話總結本週國際盤的資金氛圍",
  "themes": [
    {"name": "題材名稱", "summary": "一句話說明",
     "confidence": "high|mid|low", "verdict": "real|watch",
     "drivers": "推動這個敘事的關鍵事件/數據",
     "us_tickers": ["NVDA", "AVGO"],
     "tw_readthrough": "對台股的傳導：受惠族群或個股"}
  ]
}"""


def global_theme_digest(headlines: list[dict], sectors: list[dict]) -> dict:
    if DRY_RUN:
        return mock.global_theme_digest()

    head_lines = "\n".join(f"- {h['title']}（{h.get('source', '')}）" for h in headlines[:60])
    sec_lines = "\n".join(
        f"- {s['name']}（{s['ticker']}）近5日 {s['ret_5d']:+.1f}%，"
        f"相對 S&P {s['rel_strength']:+.1f}pt" for s in sectors
    )
    user = (f"本週國際財經頭條：\n{head_lines}\n\n"
            f"美股類股 ETF 表現：\n{sec_lines}")
    try:
        return _parse_json(_call(GLOBAL_SYSTEM, user, 3000))
    except Exception as exc:
        print(f"[llm] 國際題材彙整失敗：{exc}")
        return {"macro_note": "", "themes": []}


# ── 新聞面評分 ─────────────────────────────────────────
NEWS_SENTIMENT_SYSTEM = """你是台股新聞情緒分析師。輸入是多檔個股各自的近期新聞標題。

任務：針對每一檔股票，把該股的新聞標題整體氣氛換算成 0-100 分：
- 80-100：多則正面新聞（營收創高、大單、法人喊多、產能滿載）
- 50-70：中性或正負摻雜，沒有明顯偏向
- 20-40：偏負面（財報不如預期、裁員、訴訟、法人調降）
- 標題太少（1-2 則）或內容跟營運無關（純股價創新高型態新聞、活動公告）就給 50 分，不要過度解讀

一次處理輸入裡的所有股票，只輸出 JSON，不要 markdown 標記：
{"1234": 72, "5678": 45}
key 是股票代號，value 是 0-100 的整數分數。"""


def news_sentiment_batch(stocks: list[dict]) -> dict[str, float]:
    """stocks = [{"code":, "name":, "headlines": [...]}, ...]，一次呼叫評完整批，省成本。"""
    if DRY_RUN:
        return mock.news_sentiment_batch()

    usable = [s for s in stocks if s.get("headlines")]
    if not usable:
        return {}

    lines = []
    for s in usable:
        lines.append(f"{s['code']} {s['name']}：")
        for h in s["headlines"]:
            lines.append(f"  - {h}")
    user = "\n".join(lines)

    try:
        result = _parse_json(_call(NEWS_SENTIMENT_SYSTEM, user, 800))
        return {k: float(v) for k, v in result.items() if isinstance(v, (int, float))}
    except Exception as exc:
        print(f"[llm] 新聞面評分失敗：{exc}")
        return {}


# ── 個股深度分析（評分頁用：公司介紹＋SWOT＋漲跌原因） ──────
STOCK_ANALYSIS_SYSTEM = """你是台股個股研究員，針對評分頁上列出的每一檔股票，寫一份簡短但具體的分析。

輸入每檔股票會附上：目前技術面訊號、籌碼面數據（法人買賣超、資券變化）、
營收年增率、所屬題材（如有）、近期新聞標題。

任務：針對每一檔，輸出：
1. company_desc：這家公司主要做什麼生意、在產業鏈的角色（1-2 句，具體到產品/客戶，不要空話）
2. swot：
   - strengths：具體優勢（技術門檻、市占、客戶結構）
   - weaknesses：具體弱點（毛利率、集中度風險、規模）
   - opportunities：機會（新產品/新客戶/產業趨勢，要結合輸入的題材或營收數據）
   - threats：威脅（競爭、原物料、匯率、客戶集中風險）
3. price_reason：根據輸入的技術面訊號＋籌碼面數據＋新聞，具體解釋「今天/近期為什麼漲跌」，
   要點名是哪個因素主導（技術面突破、法人買超、營收超預期、題材帶動、還是純粹跟隨大盤/族群）；
   如果數據不足以判斷，就老實說「訊號不足，暫無法判斷主要驅動因素」，不要瞎掰

寫作要求：
- 繁體中文，每個欄位 1-3 句，具體、可驗證，不要用「值得留意」「表現亮眼」這類空話
- swot 四個欄位都要填，找不到明顯威脅或弱點時也要給出產業常見的合理推論，並註明是推論
- 不使用投資建議語氣

一次處理輸入裡的所有股票，只輸出 JSON，不要 markdown 標記：
{
  "1234": {
    "company_desc": "...",
    "swot": {"strengths": "...", "weaknesses": "...", "opportunities": "...", "threats": "..."},
    "price_reason": "..."
  }
}
key 是股票代號。"""


def stock_analysis_batch(stocks: list[dict]) -> dict[str, dict]:
    """stocks = [{"code":, "name":, "signals":, "chip_note":, "revenue_yoy":,
    "theme":, "headlines": [...]}, ...]，一次呼叫評完整批，省成本。"""
    if DRY_RUN:
        return mock.stock_analysis_batch()

    if not stocks:
        return {}

    lines = []
    for s in stocks:
        lines.append(f"{s['code']} {s['name']}：")
        if s.get("signals"):
            lines.append(f"  技術面訊號：{s['signals']}")
        if s.get("chip_note"):
            lines.append(f"  籌碼面：{s['chip_note']}")
        if s.get("revenue_yoy") is not None:
            lines.append(f"  營收年增率：{s['revenue_yoy']}%")
        if s.get("theme"):
            lines.append(f"  所屬題材：{s['theme']}")
        for h in s.get("headlines", []):
            lines.append(f"  新聞：{h}")
    user = "\n".join(lines)

    try:
        return _parse_json(_call(STOCK_ANALYSIS_SYSTEM, user, 4000))
    except Exception as exc:
        print(f"[llm] 個股深度分析失敗：{exc}")
        return {}


# ── 供應鏈結構 ─────────────────────────────────────────
SUPPLY_CHAIN_SYSTEM = """你是台股產業鏈研究員。輸入一個題材的名稱、摘要與目前追蹤到的相關個股。

任務：把這個題材畫成一張簡化的供應鏈結構圖：
1. 分成上游／中游／下游三段（依這個題材的產業性質命名段落，不要死板套用「原料/製造/銷售」）
2. 已知的相關個股要分到對的段落，並標角色：
   - "核心"：該段落裡技術門檻高、對題材直接受惠的公司
   - "邊緣"：沾到題材但受惠程度不明顯、可能只是蹭
3. 找不到相關個股的段落可以留空陣列，不要硬塞
4. 如果你知道這個題材有對應的美股／日股／韓股同類公司，列在 peers 裡（不確定就留空陣列，不要瞎猜代號）

只輸出 JSON，不要 markdown 標記：
{
  "upstream": {"label": "段落名稱", "companies": [{"code":"1234","name":"公司","role":"核心"}]},
  "midstream": {"label": "段落名稱", "companies": []},
  "downstream": {"label": "段落名稱", "companies": []},
  "peers": [{"market": "US", "ticker": "NVDA", "name": "公司"}]
}"""


def supply_chain_structure(theme: dict) -> dict:
    if DRY_RUN:
        return mock.supply_chain_structure()
    stocks = theme.get("related_stocks") or theme.get("stocks") or []
    user = (f"題材：{theme['name']}\n摘要：{theme.get('summary', '')}\n"
            f"目前追蹤到的相關個股：{json.dumps(stocks, ensure_ascii=False)}")
    try:
        return _parse_json(_call(SUPPLY_CHAIN_SYSTEM, user, 1500))
    except Exception as exc:
        print(f"[llm] 供應鏈結構產出失敗：{exc}")
        return {}


# ── 深度報告 ───────────────────────────────────────────
DEEP_DIVE_SYSTEM = """你是有 20 年經驗的產業分析師，撰寫題材深度報告。

依以下結構撰寫，繁體中文，總長 800-1200 字：
1. 事件背景 —— 題材如何出現、如何演變至今
2. 訂單與財務驗證 —— 營收、法人籌碼是否支持這個題材
3. 個股比較 —— 誰是核心受惠者、誰是蹭題材的邊緣廠商
4. 風險與觀察重點 —— 什麼情況下這個題材會轉弱
5. 後續追蹤指標 —— 接下來要盯什麼

寫作要求：
- 有論述脈絡的段落文章，不是條列式摘要
- 明確區分「已驗證的事實」與「尚待觀察的推論」
- 不使用投資建議語氣

只輸出 JSON，不要 markdown 標記：
{
  "title": "報告標題",
  "summary_points": ["摘要重點1", "摘要重點2", "摘要重點3"],
  "sections": [{"heading": "段落標題", "body": "段落內容"}],
  "risks": "風險說明",
  "tracking": ["追蹤指標1", "追蹤指標2"]
}"""


def write_deep_dive(theme: dict, timeline: list[dict], extra: str = "") -> dict:
    if DRY_RUN:
        return {
            "title": f"{theme['name']}：台廠訂單能見度全面轉佳",
            "summary_points": [
                f"本題材自 {theme['first_seen']} 建檔，累積更新 {theme['update_count']} 次，"
                "為目前追蹤中信心度最高者。",
                "外資與投信近兩週同步買超相關供應鏈個股，法人態度與基本面訊號一致。",
                "需留意題材已反映一段漲幅，追高前建議搭配技術面過熱指標確認。",
            ],
            "sections": [
                {"heading": "事件背景",
                 "body": f"本題材最早於 {theme['first_seen']} 在盤後強勢股掃描中被系統標記，"
                         "當時僅有零星個股呼應，信心度評級為低。此後題材的個股廣度持續擴大，"
                         "產業鏈上下游陸續出現對應表態，符合真實題材的判斷特徵。"},
                {"heading": "訂單與財務驗證",
                 "body": "供應鏈核心廠商的月營收年增率較題材出現前明顯加速，與股價反應方向一致。"
                         "法人買超集中在營收成長幅度最高的個股，顯示資金配置與基本面驗證結果相符，"
                         "而非無差別炒作整個族群。"},
                {"heading": "個股比較",
                 "body": "核心受惠者具備技術門檻與訂單能見度，毛利率高於同業平均；"
                         "邊緣廠商雖然股價同步上漲，但營收未同步反映，屬於蹭題材性質，"
                         "應標記為觀察而非追蹤標的。"},
            ],
            "risks": "供應鏈平均漲幅已高，部分個股技術面進入相對過熱區間。"
                     "若後續營收成長率未能持續驗證，或終端客戶資本支出計畫調整，題材延續性可能轉弱。",
            "tracking": [
                "核心客戶下一次法說會：資本支出計畫是否延續上修",
                "供應鏈個股次月營收：年增率是否維持",
                "外資／投信買超是否持續集中於核心受惠股",
            ],
        }

    user = (f"題材：{theme['name']}\n"
            f"摘要：{theme.get('summary', '')}\n"
            f"首次建檔：{theme['first_seen']}，已更新 {theme['update_count']} 次\n"
            f"目前信心度：{theme.get('confidence')}／判定：{theme.get('verdict')}\n"
            f"相關個股：{theme.get('related_stocks')}\n\n"
            f"追蹤軌跡：\n{json.dumps(timeline, ensure_ascii=False, indent=2)}")
    if extra:
        user += f"\n\n補充：\n{extra}"

    try:
        return _parse_json(_call(DEEP_DIVE_SYSTEM, user, 6000))
    except Exception as exc:
        print(f"[llm] 深度報告產出失敗：{exc}")
        return {}
