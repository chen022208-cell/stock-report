"""推播通知：Discord Webhook（預設）／Telegram Bot（可選）。

用 Discord Webhook 而不是 LINE Notify —— 後者已於 2025/3/31 終止服務。
Discord Webhook 比 Telegram Bot 更好設定：不用找 @BotFather、不用抓 chat id，
到頻道設定新增一個 Webhook、複製網址即可。兩種通知方式可以同時開，互不影響。
"""
from __future__ import annotations

import requests

from .config import (DISCORD_WEBHOOK_URL, DRY_RUN, TELEGRAM_BOT_TOKEN,
                     TELEGRAM_CHAT_ID, load_config)


def send_discord(text: str) -> bool:
    if not DISCORD_WEBHOOK_URL:
        print("[notify] 未設定 DISCORD_WEBHOOK_URL，略過 Discord 推播")
        return False
    try:
        resp = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": text[:2000]},   # Discord 單則上限 2000 字元
            timeout=30,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        print(f"[notify] Discord 推播失敗：{exc}")
        return False


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
    """推播摘要 + 網站連結。完整內容在網站上看，訊息只放重點。

    Discord 與 Telegram 都開的話兩邊都送；哪個環境變數缺就自動略過那個管道，
    不影響另一個，也不讓整個流程失敗。
    """
    cfg = load_config()["notify"]

    def build(bold_open: str, bold_close: str) -> str:
        parts = [f"{bold_open}{title}{bold_close}"]
        if body:
            parts.append(body[:1500])
        url = _site_base_url()
        if url:
            parts.append(f"\n完整報告：{url}/")
        return "\n\n".join(parts)

    if DRY_RUN:
        print("─" * 50)
        print("[notify] DRY_RUN 模擬推播內容：")
        print(build("<b>", "</b>"))
        print("─" * 50)
        return True

    sent = False
    if cfg.get("discord_enabled", True):
        # Discord 用 Markdown（**粗體**），不是 HTML
        sent = send_discord(build("**", "**")) or sent
    if cfg.get("telegram_enabled", False):
        sent = send_telegram(build("<b>", "</b>")) or sent
    return sent


def _site_base_url() -> str:
    cfg = load_config()["site"]
    return cfg.get("base_url", "").rstrip("/")
