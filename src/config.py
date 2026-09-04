"""設定載入：config.yaml + 環境變數。"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
TEMPLATE_DIR = ROOT / "templates"
DB_PATH = DATA_DIR / "market.db"

TPE = timezone(timedelta(hours=8))


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def now_tpe() -> datetime:
    """一律用台北時間，避免雲端主機跑在 UTC 時算錯日期。"""
    return datetime.now(TPE)


def today_str() -> str:
    return now_tpe().strftime("%Y-%m-%d")


# ── 環境變數（機密資訊絕不進 config.yaml） ──────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")

# 設為 "1" 時使用內建假資料，不呼叫任何外部 API（本地開發、CI 測試用）
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
