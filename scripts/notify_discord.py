"""把一個 JSON 檔裡的 text 欄位送到 Discord webhook。

為什麼需要這支獨立小工具：盤中迴圈（intraday.yml）只推 `intraday-data` 分支，
不碰 main——而 `daily-notify.yml` 是靠「main 有 push」才會啟動的。所以盤中選股
訊號沒有辦法走既有那條通知路徑，必須由 workflow 自己拿 repo secret 直接送。

用法：python scripts/notify_discord.py docs/data/intraday_alert.json
讀不到檔、沒有 webhook、送失敗都只印訊息並以 0 結束——通知失敗不該讓整支盤中
迴圈中斷。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else ""
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook:
        print("[notify] 沒有 DISCORD_WEBHOOK_URL，略過")
        return 0
    if not path or not os.path.exists(path):
        print(f"[notify] 找不到 {path}，略過")
        return 0
    try:
        text = json.load(open(path, encoding="utf-8")).get("text", "")
    except Exception as exc:
        print(f"[notify] 讀取 {path} 失敗：{exc}")
        return 0
    if not text.strip():
        print("[notify] 內容是空的，略過")
        return 0
    try:
        req = urllib.request.Request(
            webhook, data=json.dumps({"content": text[:2000]}).encode("utf-8"),
            # Discord／Cloudflare 會擋掉 python-urllib 的預設 UA（見 CLAUDE.md）
            headers={"Content-Type": "application/json",
                     "User-Agent": "Mozilla/5.0 (compatible; stock-report-notify/1.0)"})
        urllib.request.urlopen(req, timeout=30)
        print("[notify] Discord 推播成功")
    except Exception as exc:
        detail = ""
        if hasattr(exc, "read"):
            try:
                detail = "｜" + exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
        print(f"[notify] Discord 推播失敗（不影響盤中迴圈）：{exc}{detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
