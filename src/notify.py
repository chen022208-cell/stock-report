"""推播通知（Telegram Bot）。

用 Telegram 而不是 LINE Notify —— 後者已於 2025/3/31 終止服務。
LINE Messaging API 雖可替代，但免費額度有限；Telegram Bot 無則數上限且設定簡單。
"""
from __future__ import annotations

import requests

from .config import (DRY_RUN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
                     load_config)


def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[notify] 未設定 Telegram 環境變數，略過推播")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text[:4000],          # Telegram 單則上限 4096 字元
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=30,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        print(f"[notify] Telegram 推播失敗：{exc}")
        return False


def send_notification(title: str, body: str = "") -> bool:
    """推播摘要 + 網站連結。完整內容在網站上看，訊息只放重點。"""
    cfg = load_config()
    if not cfg["notify"].get("telegram_enabled"):
        return False

    parts = [f"<b>{title}</b>"]
    if body:
        parts.append(body[:1500])

    base_url = cfg["site"].get("base_url", "").rstrip("/")
    if base_url:
        parts.append(f"\n完整報告：{base_url}/")

    text = "\n\n".join(parts)

    if DRY_RUN:
        print("─" * 50)
        print("[notify] DRY_RUN 模擬推播內容：")
        print(text)
        print("─" * 50)
        return True

    return send_telegram(text)
