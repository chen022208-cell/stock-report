"""讀取「發布到網路」的 Google 試算表 CSV——這是 Google 表單回應的公開只讀出口，
不需要任何 API 金鑰或服務帳號：使用者在 Google Sheets 選「檔案→分享→發布到網路→
CSV」，產出的網址本來就設計成任何人都能直接 GET 到最新內容。

使用者提交研究文章走這條路徑，是為了讓完全沒有 GitHub 帳號的訪客也能提交
（表單本身用 iframe 隱藏送出、頁面不跳轉，見 submit.html）。
"""
from __future__ import annotations

import csv
import io
import time

import requests

from ..config import DRY_RUN
from . import mock

TIMEOUT = 20
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}


class FormFetchError(RuntimeError):
    """連不到／讀不到表單 CSV。

    刻意跟「表單是空的」分開：兩者以前都回傳 `[]`，呼叫端無從分辨，結果就是
    雲端環境被網路政策擋住時（`Tunnel connection failed: 403`），整條管線照樣
    exit 0、Routine 照樣 SUCCEEDED，使用者送了表單卻永遠等不到報告，而且沒有
    任何人會知道。2026-09-07 換 Claude 帳號後實際踩到（新帳號的雲端環境
    network access 是預設的 Trusted，擋掉 docs.google.com）。
    """


def fetch_form_responses(csv_url: str) -> list[dict]:
    """回傳 [{"timestamp":, "title":, "body":}, ...]，依 Google 表單「回覆」試算表
    固定欄位順序（時間戳記、標題、內容）解析。

    沒設網址、或試算表本身沒有任何回覆 → 回傳 `[]`（正常的「沒有東西」）。
    連線／HTTP 失敗 → 丟 `FormFetchError`（異常的「讀不到」），由呼叫端決定
    要不要讓整輪標記成失敗。"""
    if DRY_RUN:
        return mock.research_form_responses()
    if not csv_url:
        return []
    try:
        # ⚠️ 一定要破快取。Google 這個「發布到網路」的網址回的是
        # `Cache-Control: private, max-age=300`——最多 5 分鐘的舊資料。
        # 表單送出後用 API trigger 秒觸發 Routine 時，若讀到快取版本就會看不到
        # 「剛剛那一筆」，等於觸發了卻什麼都沒處理，秒觸發形同虛設。
        # 加隨機 query 參數＋no-cache 標頭強制取得最新內容。
        bust = f"{'&' if '?' in csv_url else '?'}_cb={int(time.time())}"
        resp = requests.get(csv_url + bust,
                            headers={**HEADERS, "Cache-Control": "no-cache",
                                     "Pragma": "no-cache"},
                            timeout=TIMEOUT)
        resp.raise_for_status()
        reader = csv.reader(io.StringIO(resp.text))
        rows = list(reader)
        if len(rows) < 2:
            return []
        out = []
        for row in rows[1:]:
            if len(row) < 3:
                continue
            out.append({"timestamp": row[0].strip(), "title": row[1].strip(), "body": row[2].strip()})
        return out
    except Exception as exc:
        print(f"[google_sheet] 讀取表單回應失敗：{exc}")
        raise FormFetchError(str(exc)) from exc
