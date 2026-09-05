"""讀取「發布到網路」的 Google 試算表 CSV——這是 Google 表單回應的公開只讀出口，
不需要任何 API 金鑰或服務帳號：使用者在 Google Sheets 選「檔案→分享→發布到網路→
CSV」，產出的網址本來就設計成任何人都能直接 GET 到最新內容。

使用者提交研究文章走這條路徑，是為了讓完全沒有 GitHub 帳號的訪客也能提交
（表單本身用 iframe 隱藏送出、頁面不跳轉，見 submit.html）。
"""
from __future__ import annotations

import csv
import io

import requests

from ..config import DRY_RUN
from . import mock

TIMEOUT = 20
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}


def fetch_form_responses(csv_url: str) -> list[dict]:
    """回傳 [{"timestamp":, "title":, "body":}, ...]，依 Google 表單「回覆」試算表
    固定欄位順序（時間戳記、標題、內容）解析；抓不到或格式跑掉就回傳空清單，
    絕不讓這個外部依賴弄壞整條研究提交管線。"""
    if DRY_RUN:
        return mock.research_form_responses()
    if not csv_url:
        return []
    try:
        resp = requests.get(csv_url, headers=HEADERS, timeout=TIMEOUT)
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
        return []
