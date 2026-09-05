"""推播通知：Discord Webhook（預設）／Telegram Bot（可選）。

用 Discord Webhook 而不是 LINE Notify —— 後者已於 2025/3/31 終止服務。
Discord Webhook 比 Telegram Bot 更好設定：不用找 @BotFather、不用抓 chat id，
到頻道設定新增一個 Webhook、複製網址即可。兩種通知方式可以同時開，互不影響。
"""
from __future__ import annotations

import json
from pathlib import Path

import requests

from .config import (DISCORD_WEBHOOK_URL, DRY_RUN, TELEGRAM_BOT_TOKEN,
                     TELEGRAM_CHAT_ID, load_config)

NOTIFY_PAYLOAD_PATH = Path(__file__).resolve().parent.parent / "docs" / "_notify_payload.json"


def send_discord(text: str) -> bool:
    if not DISCORD_WEBHOOK_URL:
        print("[notify] 未設定 DISCORD_WEBHOOK_URL，略過 Discord 推播")
        return False
    try:
        resp = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": text[:2000]},   # Discord 單則上限 2000 字元
            # requests 預設 User-Agent（python-requests/x.x）常被 Discord/Cloudflare
            # 的機器人防護擋掉、回 403，換成瀏覽器樣式的 UA 才送得出去。
            headers={"User-Agent": "Mozilla/5.0 (compatible; stock-report-notify/1.0)"},
            timeout=30,
        )
        resp.raise_for_status()
        print(f"[notify] Discord 推播成功（status {resp.status_code}）")
        return True
    except Exception as exc:
        body = getattr(getattr(exc, "response", None), "text", "")
        print(f"[notify] Discord 推播失敗：{exc}"
              + (f"｜回應內容：{body[:300]}" if body else ""))
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
        print(f"[notify] Telegram 推播成功（status {resp.status_code}）")
        return True
    except Exception as exc:
        body = getattr(getattr(exc, "response", None), "text", "")
        print(f"[notify] Telegram 推播失敗：{exc}"
              + (f"｜回應內容：{body[:300]}" if body else ""))
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

    # 在雲端 agent（Pro 排程）跑報告時本機沒有 Discord/Telegram 密鑰，
    # 沒辦法在這裡直接送出去；先把內容寫成一份 JSON 隨 docs/ 一起 commit，
    # 有密鑰的 GitHub Actions 偵測到這個檔案變動時再幫忙把通知送出去。
    try:
        NOTIFY_PAYLOAD_PATH.write_text(
            json.dumps({"title": title, "body": body[:1500], "url": _site_base_url()},
                      ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"[notify] 寫入推播暫存檔失敗：{exc}")

    if not cfg.get("discord_enabled", True) and not cfg.get("telegram_enabled", False):
        print("[notify] Discord 與 Telegram 都在 config.yaml 裡關著，略過推播")
        return False

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
