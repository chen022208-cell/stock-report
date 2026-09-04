# 部署教學

全程免費，不需要租主機。GitHub Actions 跑排程，GitHub Pages 當網站。

預估時間：30 分鐘。

---

## 步驟 0：本機環境（Windows）

Windows 通常沒有預裝 Python，`python` 只是 Microsoft Store 的轉址捷徑。先裝真的：

```bash
winget install --id Python.Python.3.12 --scope user
```

然後在專案資料夾建虛擬環境、裝套件（`setup.sh` 會自動做這些，但手動指令列在此備查）：

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m src.main evening   # 先設 DRY_RUN=1 跑假資料驗證版面
```

---

## 步驟 1：把專案放上 GitHub

```bash
cd stock-report
git init
git add .
git commit -m "初始版本"
```

到 GitHub 建一個新 repo（可以設 Public 或 Private，Public 的 Actions 額度是無限的，
Private 每月有免費分鐘數限制，這個專案的用量遠低於上限，兩者都夠用）。

```bash
git remote add origin https://github.com/你的帳號/stock-report.git
git branch -M main
git push -u origin main
```

---

## 步驟 2：申請 Anthropic API Key

1. 到 https://console.anthropic.com 註冊登入
2. 左側選單 **API Keys** → **Create Key**
3. 複製產生的 key（`sk-ant-` 開頭），**只會顯示一次**

⚠️ API 是預付制，需要先儲值。這個專案一天呼叫約 2-4 次，
每次幾千 token，用量很小，最低儲值額度可以撐很久。

---

## 步驟 3：建立推播管道（Discord Webhook，預設）

比 Telegram Bot 簡單很多，不用找 @BotFather、不用抓 chat id：

1. 在你的 Discord 伺服器裡，選一個頻道（或新建一個）
2. 頻道旁的齒輪圖示 → **編輯頻道** → **整合** → **Webhook** → **新增 Webhook**
3. 取個名字（例如「盤後快訊」），按 **複製 Webhook 網址**（`https://discord.com/api/webhooks/...` 格式）
4. 存起來，下一步要貼進 GitHub Secrets

**想改用或併用 Telegram？**

1. Telegram 搜尋 **@BotFather**（或直接開 https://t.me/BotFather ），傳送 `/newbot`
2. 依指示取名，完成後會給你一組 **token**（`123456:ABC-...` 格式）
3. 對你剛建立的 bot 傳任何一句訊息（重要，不先傳訊息拿不到 chat id）
4. 瀏覽器打開，把 `<TOKEN>` 換成你的 token：
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
5. 找到 `"chat":{"id":123456789` ——這串數字就是你的 **chat id**
6. 記得把 `config.yaml` 的 `notify.telegram_enabled` 改成 `true`

---

## 步驟 4：設定 GitHub Secrets

repo 頁面 → **Settings** → 左側 **Secrets and variables** → **Actions** → **New repository secret**

依序新增：

| Name | Secret |
|---|---|
| `ANTHROPIC_API_KEY` | 步驟 2 的 key |
| `DISCORD_WEBHOOK_URL` | 步驟 3 的 Webhook 網址（用 Discord 才需要） |
| `TELEGRAM_BOT_TOKEN` | 步驟 3 的 token（用 Telegram 才需要） |
| `TELEGRAM_CHAT_ID` | 步驟 3 的 chat id（用 Telegram 才需要） |

⚠️ 一定要用 Secrets，不要把 key 寫進程式碼推上 GitHub。
公開的 API key 會在幾分鐘內被掃到盜用。

---

## 步驟 5：開啟 GitHub Pages

repo 頁面 → **Settings** → 左側 **Pages**

- **Source** 選 `Deploy from a branch`
- **Branch** 選 `main`，資料夾選 **`/docs`**
- 按 **Save**

等 1-2 分鐘，網址會顯示在同一頁，格式是：
```
https://你的帳號.github.io/stock-report/
```

把這個網址填回 `config.yaml` 的 `site.base_url`，推播訊息才會附上連結：

```yaml
site:
  base_url: "https://你的帳號.github.io/stock-report"
```

```bash
git add config.yaml && git commit -m "填入網站網址" && git push
```

---

## 步驟 6：開啟 Actions 寫入權限

repo → **Settings** → **Actions** → **General** → 拉到最下面 **Workflow permissions**

選 **Read and write permissions** → **Save**

（沒開這個，Actions 沒辦法把產出的報告 commit 回 repo，網站就不會更新）

---

## 步驟 7：手動觸發測試

repo → **Actions** 分頁 → 左側選 **市場報告排程** → 右側 **Run workflow**

- 下拉選 `evening`
- 按綠色 **Run workflow**

等 1-3 分鐘。成功的話：
- Telegram 會收到推播
- repo 的 `docs/reports/` 會多一個檔案
- 網站會出現最新報告

失敗的話點進去看 log，最常見的原因是 Secrets 名稱打錯，或步驟 6 沒開權限。

---

## 排程時間對照

GitHub Actions 的 cron **一律是 UTC**，這是最常踩的坑。

| 台北時間 | UTC | workflow 裡的寫法 |
|---|---|---|
| 每日 07:00 | 前一天 23:00 | `0 23 * * 0-4` |
| 每日 18:00 | 當日 10:00 | `0 10 * * 1-5` |
| 每月 12 日 18:00 | 12 日 10:00 | `0 10 12 * *` |

要改時間就改 `.github/workflows/report.yml` 的 cron，記得換算。

⚠️ GitHub 的排程在尖峰時段可能延遲 5-15 分鐘，這是官方已知行為，不是設定錯誤。
對這套系統沒有影響（早盤 07:00 晚幾分鐘不影響判斷），但如果你之後要做盤中即時的東西，
就不該依賴 Actions 的 cron。

---

## 日常維護

**改自選股**：編輯 `config.yaml` 的 `watchlist` → push，下次排程生效。

**調整篩選門檻**：改 `config.yaml` 的 `screener`，太多雜訊就提高 `min_change_pct`。

**看資料庫**：
```bash
sqlite3 data/market.db "SELECT name, update_count, confidence FROM themes WHERE status='active';"
```

**備份**：`data/market.db` 跟著 git 走，每次排程都會 commit，等於自動備份。

---

## 常見問題

**Actions 沒有按時跑**
GitHub 會停用超過 60 天沒有 commit 活動的 repo 排程。系統每天自己 commit，
所以正常運作時不會遇到；但如果中間停用過一段時間，回來要手動觸發一次喚醒。

**某天報告缺了某個區塊**
資料源當天抓不到。程式有容錯，單一來源失敗不會讓整份報告開天窗，
該區塊會直接不顯示。看 Actions log 的 `[warn]` 訊息可以知道是哪個來源。

**Actions log 出現一堆 `[twse] ... 428 / 429`**
TWSE 對同一 IP 短時間內大量請求會限流。正常排程（一天兩次、每次數十個請求）
不會遇到；但如果你在本機反覆重跑真實流程測試，IP 可能被暫時擋，通常 5–15 分鐘自動解除。
程式對這幾個狀態碼已有退避重試，個股歷史抓不到就跳過該檔技術分析，不影響其他區塊。
`src/analysis/screener.py` 的強勢股掃描是兩段式的：先用零成本條件（漲幅＋成交金額）
把全市場約 1400 檔縮到數十檔，只對候選抓歷史算量能，避免對 TWSE 發出上千請求。

**國際盤資料來源**
早報的國際指數、總經、台積電 ADR 走 `yfinance`（Yahoo Finance）。
原本用的 Stooq 從 2026 起對程式抓取加了 JS 瀏覽器驗證，CSV 端點全掛，已改掉。
若 yfinance 之後也不穩，替代方案是 Alpha Vantage（免費 key，每日 25 次額度夠用）。

**想改成別的通知方式**
`src/notify.py` 現在同時支援 Discord Webhook（預設）與 Telegram Bot，
`config.yaml` 的 `notify.discord_enabled` / `telegram_enabled` 各自開關，兩個也可以同時開。
要換別的管道（例如 Slack）就照 `send_discord()` 的樣子加一個新函式。
LINE Notify 已於 2025/3/31 終止服務，不要再用。

**API 費用會不會失控**
每天 2-4 次呼叫。想再省的話，把 `config.yaml` 的 `deep_dive_min_days` 調高，
深度報告產出頻率就會降低（那是最耗 token 的部分）。
