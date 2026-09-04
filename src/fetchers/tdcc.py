"""集保結算所股權分散表（大戶持股），opendata.tdcc.com.tw，免費免申請、週更新。

集保代碼欄位是固定 6 碼、右邊補空白（"2330  "），要記得 strip。
持股分級 15 = 1,000 張以上（業界慣用的「千張大戶」門檻），17 = 合計（100%），
其餘 1–14 級距太細對「大戶集中度」這個用途沒意義，只取 15 級。
"""
from __future__ import annotations

import csv
import io

import requests

from ..config import DRY_RUN
from . import mock

URL = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
TIMEOUT = 45
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
BIG_HOLDER_TIER = "15"


def fetch_holder_concentration(codes: set[str] | None = None) -> dict[str, float]:
    """回傳 {代號: 千張大戶持股佔集保庫存比例%}。codes 給定時只保留這些代號，省記憶體。"""
    if DRY_RUN:
        return mock.holder_concentration()

    try:
        resp = requests.get(URL, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        resp.encoding = "utf-8-sig"
        reader = csv.DictReader(io.StringIO(resp.text))
        out: dict[str, float] = {}
        for row in reader:
            code = (row.get("證券代號") or "").strip()
            if not code or (codes is not None and code not in codes):
                continue
            if (row.get("持股分級") or "").strip() != BIG_HOLDER_TIER:
                continue
            try:
                out[code] = float(row.get("占集保庫存數比例%") or 0)
            except ValueError:
                continue
        return out
    except Exception as exc:
        print(f"[tdcc] 股權分散表擷取失敗：{exc}")
        return {}
