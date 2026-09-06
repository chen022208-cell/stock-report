# 雲端 Routine 清單與搬移手冊

這份文件記錄 `chen022208-cell/stock-report` 目前掛在 **Claude 帳號** 下的所有雲端 Routine（Claude Code Cloud / CCR），以及要把它們搬到「另一個 Claude 帳號」時的完整步驟。

最後更新：2026-09-06（新增 3.5b 主題點播深度報告，共 9 支）

---

## 0. 先搞清楚什麼吃 Claude 額度、什麼不吃

| 元件 | 跑在哪 | 吃 Claude 額度？ | 搬帳號要動？ |
|---|---|---|---|
| **9 支雲端 Routine**（下面列表） | Anthropic 雲端 CCR session | **是**（Pro/Max 訂閱額度） | **要全部重建** |
| GitHub Actions workflows（`report.yml`、`intraday.yml`、`daily-notify.yml`、`deep-report-notify.yml`、`pages-build-deployment`…） | GitHub 自己的 runner | 否 | 不用動 |
| cron-job.org「stock-report 盤中迴圈啟動」 | cron-job.org | 否 | 不用動 |
| GitHub Pages、GitHub Secrets（`DISCORD_WEBHOOK_URL`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`、`ANTHROPIC_API_KEY`）、repo 本身 | GitHub | 否 | 不用動 |
| 前端即時圖表（瀏覽器直接打 twse.com.tw） | 使用者瀏覽器 | 否 | 不用動 |

**結論：** 搬帳號 = 在新帳號重建這 8 支 Routine（＋ 1 個 webhook），然後把舊帳號那 8 支關掉。GitHub / cron-job.org 那側完全不用改。

---

## 1. 搬移前提（重要）

新帳號的雲端環境必須能 **push 到 `chen022208-cell/stock-report`**。目前這些 Routine 的做法是 CCR session 內 `git clone`（公開 repo 免驗證）→ 改檔 → `git push`。push 需要寫入權限：

- 如果新帳號 = 同一個人（chen022208@gmail.com）的另一個 Claude 訂閱 → 綁的還是同一個 GitHub，通常直接可用。
- 如果新帳號 = 別人的帳號 → 那個人要先被加成 repo 的 collaborator（Settings → Collaborators），而且他的 Claude Code 雲端環境要接上他自己的 GitHub（claude.ai → Settings → Claude Code → Cloud / 環境設定 → GitHub 連線）。
- 新帳號的「Default」雲端環境 **network access 不要設成 Trusted**，否則會擋掉 `www.twse.com.tw` / `tpex.org.tw` / `discord.com`（見 CLAUDE.md 的說明）。

`environment_id` 每個帳號不一樣。舊帳號是 `env_014xwk8aXGAakucHHKt3G6hW`；新帳號重建時用 `/schedule` 會列出新帳號自己的環境 id。

`mcp_connections`（`Claude_Code_Remote` / connector_uuid `bf7c680d-…`）是平台自動加的，重建時**不用手動填**。

---

## 2. 搬移步驟

1. 用新帳號登入 Claude Code（CLI `claude` 或桌面 App）。
2. 對照下面第 3 節，逐一重建 8 支 Routine。兩種做法：
   - **`/schedule` 指令**：跟 Claude 說「建立一支排程 cloud agent」，把「名稱 / cron / 模型 / git repo / allowed_tools / prompt（整段照貼）」給它。
   - **網頁**：https://claude.ai/code/routines → 逐支手動建。
3. 重建「台股盤中焦點股深度快報」後，**記下它的新 routine id**，再建 webhook（第 4 節）。
4. 8 支都建好、`/schedule` 各按一次「Run now」測試沒問題後，去舊帳號把 8 支 **停用或刪除**：
   - 網頁 https://claude.ai/code/routines → 每支點進去 → Disable（或 Delete）。
   - API 只能 disable（`enabled:false`），不能 delete。
   - **一定要關**，否則兩個帳號會同時跑、同時 push、互相 rebase 衝突，而且舊帳號還是在燒額度。
5. cron-job.org、GitHub 那側都不用動。

> cron 時間全部是 **UTC**。下面每支都附了台灣時間（UTC+8）對照。

---

## 3. 8 支 Routine 完整規格

共通設定：
- **git source**：`https://github.com/chen022208-cell/stock-report`
- **persist_session**：false
- **model**：除非註明，用 `claude-sonnet-5`（「每日早報 / 盤後」原本沒指定，等同帳號預設即可）

---

### 3.1 台股每日早報

- **cron**：`0 23 * * 0-4` （UTC 週日–週四 23:00 ＝ **台灣 週一–週五 07:00**）
- **model**：帳號預設（原本未指定）
- **allowed_tools**：`preset:default, Task, Bash, Glob, Grep, Read, Edit, MultiEdit, Write, NotebookEdit, WebFetch, TodoWrite, WebSearch, BashOutput, KillBash, Skill, Tmux, Monitor, SendUserFile, REPL`
- **prompt**：

```
你正在為「盤後快訊」台股報告網站（chen022208-cell/stock-report，已 clone 到本機）跑每日早報。這支自動化原本由 GitHub Actions 排程直接打 Anthropic 計量 API（`ANTHROPIC_API_KEY`）產生報告內容，因為使用者不想要額外計費，已改成由你（吃 Pro/Max 額度）親自扮演 LLM 的角色來產生內容，GitHub Actions 那邊的自動排程已經拿掉，只保留手動備援。

【執行步驟】
1. `cd` 到 repo（工作目錄應該已經是 clone 好的 stock-report），執行 `git pull --rebase origin main`。
2. 確保 Python 套件已安裝：`pip install -r requirements.txt`（若已裝過會很快跳過）。
3. 用 Bash 工具、**背景執行**（run_in_background: true）啟動：
   `LLM_AGENT_MODE=1 python -m src.main auto morning`
   這個環境沒有 `FRED_API_KEY`，程式會自動優雅降級（美國總經數據區塊留空），不會噴錯，不用處理。
4. 這支程式在需要 LLM 回覆的地方，會把請求寫成 `agent_llm_queue/<uuid>.request.json`（內容是 `{"system": ..., "user": ..., "max_tokens": ...}`），然後卡住等待對應的 `agent_llm_queue/<uuid>.response.txt` 出現。你必須在它跑的期間，**用一個輪詢迴圈**（例如每隔幾秒 `ls agent_llm_queue/*.request.json` 一次，用 Monitor 工具的 until-loop 或簡單的 Bash 迴圈都可以）去偵測新出現的 `.request.json` 檔案：
   - 讀取該檔案的 `system` 與 `user` 欄位
   - **你自己扮演那個 LLM**：把 `system` 當成你的角色設定、`user` 當成使用者輸入，產生一段回覆內容——輸出格式必須完全符合 `system` 提示裡要求的格式（很多步驟會要求輸出純 JSON，若是這樣就只輸出 JSON，不要加 markdown code fence、不要加額外說明文字），因為下游程式碼會直接解析這段文字
   - 把你產生的回覆文字寫進對應的 `agent_llm_queue/<同一個 uuid>.response.txt`
   - 繼續輪詢，直到背景的 `python -m src.main` 程序執行完畢（用 BashOutput 或類似方式確認程序已結束、沒有殘留的 `.request.json` 未回覆）
   - 單一請求最多等 20 分鐘會逾時失敗，所以你的輪詢間隔不要太長（建議 5-10 秒一次）
5. 確認背景程序成功結束（exit code 0）後：
   - `git add docs data`
   - 若 `git diff --staged --quiet` 顯示沒有變更就跳過 commit
   - 否則 `git commit -m "報告更新 $(date -u +'%Y-%m-%d %H:%M UTC')"`，然後 `git push`；若被拒絕（遠端有新 commit）就 `git pull --rebase origin main` 後重推，最多重試 3 次
6. 如果今天是台股休市日，`src.main auto morning` 內部本來就會自動偵測並略過（看程式輸出即可判斷），這是正常行為不是錯誤。

【重要】
- 這是每天都會發生的例行任務，跑順的話（main.py 正常結束、有 commit 就 push 成功）**不需要通知使用者**，安靜結束就好。
- 只有在下列情況才需要用 PushNotification 通知使用者：main.py 執行失敗（非「今日休市」的正常略過）、輪詢過程中有 `.request.json` 超過合理時間沒被你處理到、或 git push 重試 3 次後依然失敗。
- 絕對不要去動 `docs/weekly/`、`docs/monthly-deep/` 底下的檔案，那是另外兩個獨立的 Routine 在維護。
```

---

### 3.2 台股每日盤後（每月 12 號多跑一次績效回顧月報）

- **cron**：`0 10 * * 1-5` （UTC 平日 10:00 ＝ **台灣平日 18:00**）
- **model**：帳號預設（原本未指定）
- **allowed_tools**：同 3.1
- **prompt**：

```
你正在為「盤後快訊」台股報告網站（chen022208-cell/stock-report，已 clone 到本機）跑每日盤後報告（每月 12 號另外加跑月度績效回顧）。這支自動化原本由 GitHub Actions 排程直接打 Anthropic 計量 API（`ANTHROPIC_API_KEY`）產生報告內容，因為使用者不想要額外計費，已改成由你（吃 Pro/Max 額度）親自扮演 LLM 的角色來產生內容，GitHub Actions 那邊的自動排程已經拿掉，只保留手動備援。

【執行步驟】
1. `cd` 到 repo（工作目錄應該已經是 clone 好的 stock-report），執行 `git pull --rebase origin main`。
2. 確保 Python 套件已安裝：`pip install -r requirements.txt`。
3. 用 Bash 工具、**背景執行**（run_in_background: true）啟動：
   `LLM_AGENT_MODE=1 python -m src.main auto evening`
   這個環境沒有 `FRED_API_KEY`，程式會自動優雅降級（美國總經數據區塊留空），不會噴錯，不用處理。
4. 這支程式在需要 LLM 回覆的地方，會把請求寫成 `agent_llm_queue/<uuid>.request.json`（內容是 `{"system": ..., "user": ..., "max_tokens": ...}`），然後卡住等待對應的 `agent_llm_queue/<uuid>.response.txt` 出現。你必須在它跑的期間，**用一個輪詢迴圈**（例如每隔幾秒 `ls agent_llm_queue/*.request.json` 一次，用 Monitor 工具的 until-loop 或簡單的 Bash 迴圈都可以）去偵測新出現的 `.request.json` 檔案：
   - 讀取該檔案的 `system` 與 `user` 欄位
   - **你自己扮演那個 LLM**：把 `system` 當成你的角色設定、`user` 當成使用者輸入，產生一段回覆內容——輸出格式必須完全符合 `system` 提示裡要求的格式（很多步驟會要求輸出純 JSON，若是這樣就只輸出 JSON，不要加 markdown code fence、不要加額外說明文字），因為下游程式碼會直接解析這段文字
   - 把你產生的回覆文字寫進對應的 `agent_llm_queue/<同一個 uuid>.response.txt`
   - 繼續輪詢，直到背景的 `python -m src.main` 程序執行完畢
   - 單一請求最多等 20 分鐘會逾時失敗，輪詢間隔建議 5-10 秒一次
5. 確認背景程序成功結束後：
   - `git add docs data`
   - 若沒有變更就跳過 commit，否則 `git commit -m "報告更新 $(date -u +'%Y-%m-%d %H:%M UTC')"` 並 `git push`（被拒就 `git pull --rebase origin main` 後重推，最多 3 次）
6. **檢查今天日期**：`date +%d`，如果是 `12`，額外再跑一次完整流程（背景執行 `LLM_AGENT_MODE=1 python -m src.main monthly`、同樣輪詢服務 `agent_llm_queue/`、跑完後 `git add docs data` 並 commit push）——這是每月一次的績效回顧月報，跟盤後報告是兩個獨立的產出。
7. 如果今天是台股休市日，`src.main auto evening` 內部本來就會自動偵測並略過（可能會另外觸發假期彙整 `run_holiday`），這是正常行為。

【重要】
- 這是每天都會發生的例行任務，跑順的話不需要通知使用者，安靜結束就好。
- 只有在下列情況才需要用 PushNotification 通知使用者：main.py 或 monthly 執行失敗（非「今日休市」的正常略過）、輪詢過程中有 `.request.json` 超過合理時間沒被你處理到、或 git push 重試 3 次後依然失敗。
- 絕對不要去動 `docs/weekly/`、`docs/monthly-deep/` 底下的檔案，那是另外兩個獨立的 Routine 在維護（跟這裡的每月 12 號績效回顧月報是不同的東西，不要搞混）。
```

---

### 3.3 台股即時快訊監控

- **cron**：`33 * * * *` （每小時 33 分，UTC＝台灣同樣每小時 33 分）
- **model**：`claude-sonnet-5`
- **allowed_tools**：同 3.1
- **prompt**：

```
你正在為「盤後快訊」台股報告網站（chen022208-cell/stock-report）跑「即時快訊監控」。這是每小時一次的輕量任務：抓華爾街見聞（wallstreetcn）的即時快訊，只有「夠重要且真的跟站上現有題材／個股有關」的才推播通知並寫進資料。跑順的話安靜結束、不要通知使用者。

【執行步驟】
1. 工作目錄若還沒有 repo，就 `git clone https://github.com/chen022208-cell/stock-report` 再 `cd stock-report`；已存在就 `cd` 進去執行 `git pull --rebase origin main`。
2. `pip install -r requirements.txt`（裝過會很快跳過）。
3. 用 Bash 工具、背景執行（run_in_background: true）啟動：
   `LLM_AGENT_MODE=1 python -m src.main news`
   - 這支程式打 api.wallstreetcn.com 的公開 JSON API 抓即時快訊，用來源自己的 score（≥2 算重要）粗篩，再用跟使用者研究提交同一套 _process_research_submission() 分析＋驗證流程判斷是否跟現有題材／個股相關。
   - 用 app_state 表的 news_monitor_last_id 記錄檢查到哪一則；第一次執行只記錄基準、不推播，這是正常行為。
4. 這支程式在需要 LLM 回覆的地方，會把請求寫成 `agent_llm_queue/<uuid>.request.json`（內容是 `{"system": ..., "user": ..., "max_tokens": ...}`），然後卡住等待對應的 `agent_llm_queue/<uuid>.response.txt` 出現。你必須在背景程序執行期間，用一個輪詢迴圈（每 5-10 秒 `ls agent_llm_queue/*.request.json` 一次，用 Monitor 的 until-loop 或簡單 Bash 迴圈都可以）偵測新出現的 `.request.json`：
   - 讀取該檔的 `system` 與 `user` 欄位
   - 你自己扮演那個 LLM：把 `system` 當角色設定、`user` 當使用者輸入，產生回覆，格式完全符合 `system` 要求（要求純 JSON 就只輸出 JSON，不要加 markdown code fence、不要加額外說明），因為下游程式會直接解析
   - 把回覆寫進對應的 `agent_llm_queue/<同一個 uuid>.response.txt`
   - 繼續輪詢直到背景的 `python -m src.main` 程序結束（用 BashOutput 確認已結束、沒有殘留未回覆的 `.request.json`）
   - 單一請求最多等 20 分鐘會逾時，所以輪詢間隔不要太長
5. 確認背景程序成功結束後：
   - `git add docs data`
   - 若 `git diff --staged --quiet` 顯示沒變更就跳過 commit（大多數小時都是這種情況）
   - 否則 `git commit -m "即時快訊更新 $(date -u +'%Y-%m-%d %H:%M UTC')"` 並 `git push`；被拒就 `git pull --rebase origin main` 後重推，最多 3 次
   - docs/_notify_payload.json 若有變動，repo 的 GitHub Actions（daily-notify.yml）會偵測到並用 repo 密鑰代送 Discord／Telegram，你不用自己送通知

【重要】
- 這是每小時的例行任務，大多數時候沒有夠重要的新快訊、程式不會產生任何 git 變更，這時安靜結束就好，不要通知使用者。
- 只有下列情況才用 PushNotification 通知使用者：`python -m src.main news` 執行失敗（有 traceback、非正常結束）、輪詢過程中有 `.request.json` 超過合理時間沒被你處理到、或 git push 重試 3 次後依然失敗。
- 絕對不要去動 `docs/weekly/`、`docs/monthly-deep/` 底下的檔案，那是另外兩個獨立的 Routine 在維護。
```

---

### 3.4 台股使用者研究提交處理

- **cron**：`0 11 * * 1-5` （UTC 平日 11:00 ＝ **台灣平日 19:00**）
- **model**：`claude-sonnet-5`
- **allowed_tools**：同 3.1
- **prompt**：

```
你正在為「盤後快訊」台股報告網站（chen022208-cell/stock-report）跑「使用者研究提交處理」。每個交易日盤後報告之後跑一次，把使用者透過 GitHub Issue（research-submission 標籤）或 Google 表單提交的研究內容，做分析＋驗證後整理進網站。跑順且沒有新提交的話安靜結束、不要通知使用者。

【執行步驟】
1. 工作目錄若還沒有 repo，就 `git clone https://github.com/chen022208-cell/stock-report` 再 `cd stock-report`；已存在就 `cd` 進去執行 `git pull --rebase origin main`。
2. `pip install -r requirements.txt`（裝過會很快跳過）。
3. 檢查 `gh auth status`：能用就用（會讓程式能讀 GitHub Issue）；不能用也沒關係，Google 表單那條路（config.yaml 的 google_sheet_csv_url，公開 CSV）不需要 gh。
4. 用 Bash 工具、背景執行（run_in_background: true）啟動：
   `LLM_AGENT_MODE=1 python -m src.main research`
   - 這支程式（run_research_intake）用 `gh issue list` 讀待處理的 Issue、並讀 config.yaml 裡的 google_sheet_csv_url 讀表單回應。
   - 驗證是重點：LLM 一定要標記 verified／conflicting／unverified，只有明確 verified 且真的對應到既有題材，才用 append_research_to_theme 累加寫進題材的 theme_updates 時間軸；conflicting／unverified 一律只存進 research_notes 表，絕不動任何既有資料。寧可保守判 unverified，不要因為內容「聽起來合理」就套用。
5. 這支程式在需要 LLM 回覆的地方，會把請求寫成 `agent_llm_queue/<uuid>.request.json`（內容是 `{"system": ..., "user": ..., "max_tokens": ...}`），然後卡住等待對應的 `agent_llm_queue/<uuid>.response.txt` 出現。你必須在背景程序執行期間，用一個輪詢迴圈（每 5-10 秒 `ls agent_llm_queue/*.request.json` 一次）偵測新出現的 `.request.json`：
   - 讀取該檔的 `system` 與 `user` 欄位
   - 你自己扮演那個 LLM：把 `system` 當角色設定、`user` 當使用者輸入，產生回覆，格式完全符合 `system` 要求（要求純 JSON 就只輸出 JSON，不要加 markdown code fence、不要加額外說明），因為下游程式會直接解析
   - 把回覆寫進對應的 `agent_llm_queue/<同一個 uuid>.response.txt`
   - 繼續輪詢直到背景的 `python -m src.main` 程序結束（用 BashOutput 確認已結束、沒有殘留未回覆的 `.request.json`）
   - 單一請求最多等 20 分鐘會逾時，所以輪詢間隔不要太長
6. 程式會把處理結果留言在對應的 GitHub Issue 並關閉它（若走 gh 那條路）。
7. 確認背景程序成功結束後：
   - `git add docs data`
   - 若 `git diff --staged --quiet` 顯示沒變更就跳過 commit
   - 否則 `git commit -m "研究提交處理 $(date -u +'%Y-%m-%d %H:%M UTC')"` 並 `git push`；被拒就 `git pull --rebase origin main` 後重推，最多 3 次

【重要】
- 大多數日子沒有新提交，程式不會有任何 git 變更，這時安靜結束、不要通知使用者。
- 只有下列情況才用 PushNotification：`python -m src.main research` 執行失敗（有 traceback、非正常結束）、輪詢中 `.request.json` 長時間沒被你處理到、或 git push 重試 3 次後仍失敗。gh 不可用本身不算失敗（還有 Google 表單那條路），不需要通知。
- 絕對不要去動 `docs/weekly/`、`docs/monthly-deep/` 底下的檔案，那是另外兩個獨立的 Routine 在維護。
```

---

### 3.5 台股個股逐檔查證（每天 10 檔）

- **cron**：`0 3 * * *` （UTC 每天 03:00 ＝ **台灣每天 11:00**）
- **model**：`claude-sonnet-5`
- **allowed_tools**：`Bash, BashOutput, KillBash, Read, Write, Edit, Glob, Grep, WebSearch, WebFetch, TodoWrite`
- **prompt**：

```
你正在為「盤後快訊」台股報告網站（chen022208-cell/stock-report）跑「個股逐檔查證」。每天取市值（最新月營收）最大、還沒查證過或已超過 180 天的個股，一次 10 檔，每一檔都要實際上網查證後才把公司介紹＋SWOT 寫進網站。跑順且沒有新寫入的話安靜結束、不要通知使用者。

【最重要的鐵則】
公司「在做什麼」只能根據：(1) 程式附上的公開資訊觀測站申報「主要經營業務」，(2) 你這次實際 WebSearch 查到並看過的資料。絕對不可以用股票名稱或產業分類去推測（歷史事故：把做乳房重建軟組織填補的 7686 捷立康寫成 PCB 廠）。查不到可靠外部來源、或查完仍沒把握的個股，就不要寫，直接不要出現在回覆 JSON 裡。少寫沒關係，寫沒查證過的東西不行。

【執行步驟】
1. 工作目錄若還沒有 repo 就 `git clone https://github.com/chen022208-cell/stock-report` 再 `cd stock-report`；已存在就 `cd` 進去 `git fetch origin && git checkout main && git pull --rebase origin main`。
2. `pip install -r requirements.txt`（裝過會很快跳過）。
3. 用 Bash 背景執行（run_in_background: true）：`LLM_AGENT_MODE=1 python -m src.main verify-stocks`
   - 這支程式（run_verify_stocks）會挑出今天要查證的一批（約 10 檔，config.yaml 的 stock_verify.daily_count），把要問 LLM 的內容寫成一個 `agent_llm_queue/<uuid>.request.json`（`{system, user, max_tokens}`），然後卡住等 `agent_llm_queue/<uuid>.response.txt` 出現。
4. 在背景程序執行期間，用輪詢迴圈（每 5-10 秒 `ls agent_llm_queue/*.request.json`）偵測那個 request 檔：
   - 讀出 `system`（角色與輸出格式規定）與 `user`（這批股票，每檔附：股票代號、名稱、產業類別、主要經營業務申報值、最新月營收與年增率、本站題材歸類）。
   - 對 user 裡的每一檔股票，用 WebSearch／WebFetch 查證，至少看過下列其中兩類來源：鉅亨網 cnyes.com、Goodinfo goodinfo.tw、財報狗 statementdog.com、公司官網、公開資訊觀測站年報／法人說明會簡報、經濟日報 money.udn.com／工商時報 ctee.com.tw 的個股新聞。確認：主要產品與營收占比、主要客戶或終端應用、主要競爭對手、近年有沒有重大併購／轉投資／轉型。
   - 依 `system` 要求產生純 JSON（不要 markdown code fence、不要多餘說明），key 是股票代號，每檔 `{company_desc, swot:{strengths,weaknesses,opportunities,threats}, sources:[...]}`：
     · company_desc：2～4 句，講這家公司實際在做什麼＋關鍵事實（主力產品、重要客戶或應用、重大併購），每個具體細節都要是這次查到的，主業不得與申報「主要經營業務」矛盾。
     · swot：各 1～2 句，用查到的事實與營收數字支撐；純推理、查不到直接佐證的判斷標「（推論）」。
     · sources：必填，列出你這次實際看過並用來下筆的來源（網址或明確出處），至少 1 個是「公開資訊觀測站申報值」以外的外部來源。
     · 查不到或沒把握的個股，直接不要放進 JSON。
   - 把 JSON 寫進 `agent_llm_queue/<同一個 uuid>.response.txt`。
   - 用 BashOutput 確認背景的 `python -m src.main verify-stocks` 程序已結束、`agent_llm_queue/` 沒有殘留未回覆的 `.request.json`。單一請求 20 分鐘逾時，輪詢間隔別太長。
5. 程式收到回覆後會：只寫入 sources 有申報值以外來源的個股 → `db.upsert_stock_analysis`（帶 sources）→ 重繪 `docs/data/stock_analysis.json` 與 `docs/data/stock_info/*.json`。終端會印出 `[verify] 本批查證寫入 N / 10 檔`。
6. `git status -sb`。若 `data/market.db`、`docs/data/stock_analysis.json` 或 `docs/data/stock_info/` 有變動：
   `git add -A && git commit -m "個股逐檔查證 $(date -u +%Y-%m-%d)（N 檔，附來源）"`（N 用步驟 5 印出的數字）→ `git pull --rebase origin main` → `git push`。被拒就重新 rebase 再推，最多 3 次。這個 repo 會同時被每日盤後等其他排程改，衝突正常。
7. 若程式印出「本批查證寫入 0 檔」或 git 沒有任何變動：安靜結束，不要 commit、不要通知使用者。
8. 只有在失敗時（程式逾時、git push 連續 3 次失敗、程式報錯）才 PushNotification，訊息帶上關鍵錯誤行。

注意：這條排程只做「逐檔查證版」的個股分析，跟已停用的全市場 SWOT 批次回填不同。品質重於數量，寧可某檔跳過，也不要放沒查證的內容。
```

---

### 3.5b 台股主題點播深度報告

> 提交研究頁的第三種模式（🎯 點播深度主題）。使用者只給一個題目、不貼文章，
> 系統自己上網查證後寫一份報告。跟 3.4「使用者研究提交處理」是**兩條不同的
> 流程**：3.4 是驗證使用者貼進來的內容（保守、寧可判 unverified），這一支是
> 系統自己產出內容（查證責任全在產出端，沒有外部來源就不產出）。

- **cron**：`30 11 * * 1-5` （UTC 平日 11:30 ＝ **台灣平日 19:30**，接在 3.4 之後）
- **model**：`claude-sonnet-5`
- **allowed_tools**：`Bash, BashOutput, KillBash, Read, Write, Edit, Glob, Grep, WebSearch, WebFetch, TodoWrite`
- **prompt**：

```
你正在為「盤後快訊」台股報告網站（chen022208-cell/stock-report）跑「主題點播深度報告」。
使用者在提交研究頁點播一個主題（GitHub Issue，標籤 topic-request，標題以「[主題點播]」開頭），
你要實際上網查證後產出一份深度研究報告存回網站。沒有待處理的點播就安靜結束、不要通知使用者。

【鐵則】
個股「在做什麼」只能根據你這次實際 WebSearch 查到的資料，絕對不可以用股票名稱或產業分類
推測（歷史事故：把做乳房重建軟組織填補的 7686 捷立康寫成 PCB 廠）。sources 必填，至少 2 個
外部來源；查不到可靠來源的主題就不要寫，程式會自動在 Issue 上說明並關閉，這是正常行為。
明確區分「已查證的事實」與「推論」，推論一律標「（推論）」。不要用投資建議語氣。

【執行步驟】
1. 工作目錄若還沒有 repo 就 `git clone https://github.com/chen022208-cell/stock-report` 再 `cd stock-report`；
   已存在就 `cd` 進去 `git fetch origin && git checkout main && git pull --rebase origin main`。
2. `pip install -r requirements.txt`（裝過會很快跳過）。
3. 確認 `gh auth status` 可用（這一支一定要 gh：點播是靠 GitHub Issue 傳進來的）。
4. 用 Bash 背景執行（run_in_background: true）：`LLM_AGENT_MODE=1 python -m src.main topic-requests`
   - 程式（run_topic_requests）會用 `gh issue list --label topic-request` 讀待處理的點播，
     對每一則把要問 LLM 的內容寫成 `agent_llm_queue/<uuid>.request.json`，然後卡住等回覆。
5. 在背景程序執行期間，輪詢（每 5-10 秒 `ls agent_llm_queue/*.request.json`）偵測 request 檔：
   - 讀出 `system`（角色與輸出格式規定）與 `user`（使用者點播的主題＋站內既有題材清單）。
   - 針對這個主題用 WebSearch／WebFetch 實際查證：鉅亨網 cnyes.com、Goodinfo、財報狗
     statementdog.com、公司官網、公開資訊觀測站、經濟日報 money.udn.com、工商時報 ctee.com.tw。
     確認：這個題材是不是真的在發生、有哪些台廠實際參與、有沒有營收或訂單佐證。
   - 依 `system` 要求輸出純 JSON（不要 markdown fence）：
     {title, summary, sections:[{heading, body}], stocks:[{code,name,note}], risks, sources:[...]}
     · stocks 只放你查證過、確定跟這個主題有關的台股，查不到就給空陣列，不要湊。
     · sources 必填，至少 2 個你實際看過的外部來源。
     · 如果查證後認為證據不足、或這根本不是一個成立的題材，就誠實寫出來，不要硬湊一篇。
   - 寫進 `agent_llm_queue/<同一個 uuid>.response.txt`。用 BashOutput 確認程序已結束、
     沒有殘留未回覆的 request。單一請求 20 分鐘逾時。
6. 程式會：寫進 topic_reports 表 ＋ 產出 docs/analysis/<日期>-<主題slug>.html ＋ 更新
   docs/analysis/index.html ＋ 在對應 Issue 留言附上網址並關閉。終端會印
   `[topic] 產出 N 篇主題報告`。
7. `git status -sb`。若 `data/market.db`、`docs/analysis/` 有變動：
   `git add -A && git commit -m "主題點播報告 $(date -u +%Y-%m-%d)（N 篇）"` →
   `git pull --rebase origin main` → `git push`。被拒就重新 rebase 再推，最多 3 次。
8. 若「沒有待處理的主題點播」或 git 沒有任何變動：安靜結束，不要 commit、不要通知使用者。
9. 只有失敗時（程式逾時、git push 連 3 次失敗、程式報錯）才 PushNotification。
```

**前置設定（一次性）**：GitHub repo 要有 `topic-request` 這個 Issue 標籤
（Issues → Labels → New label），否則前端帶 `labels=topic-request` 開新 Issue 時
標籤會被忽略，程式就撈不到。

---

### 3.6 台股週報：全球總經＋台股深度研究

- **cron**：`0 12 * * 5` （UTC 週五 12:00 ＝ **台灣週五 20:00**）
- **model**：`claude-sonnet-5`
- **allowed_tools**：`Bash, Read, Write, Edit, Glob, Grep, WebSearch, WebFetch`
- **prompt**：

```
你正在為「盤後快訊」台股報告網站（chen022208-cell/stock-report，已 clone 到本機）產出每週深度週報。這個網站平常有一支獨立的 Python 自動化（main.py）每個交易日跑早報/盤後報告，你這次的任務跟那個完全分開、不要去動它產出的檔案。

【任務】用繁體中文寫一份本週（今天是週五收盤後）的深度研究報告，分兩大部分：
1. 全球總體經濟：本週主要央行動態、利率/通膨數據、地緣政治事件、原物料與美股/美元/美債走勢，對台股/科技供應鏈的傳導路徑。用 WebSearch/WebFetch 做即時研究，不要用你訓練資料裡的舊記憶當成本週事實。
2. 台股市場深度研究：本週大盤走勢、資金流向（外資/投信/自營）、產業輪動、值得留意的結構性變化。可以先讀 data/market.db（sqlite3，market_snapshots 表）跟 docs/data/*.json（heatmap.json、chips.json、scores.json），以及本週的 docs/reports/*.html 幾份報告，了解這支自動化系統這週已經抓到什麼即時訊號，你的週報要在這個基礎上做更完整的綜合分析，不是重複列數字。

寫作要求：分析文章形式、有論述脈絡，不是條列摘要；明確區分「已驗證的事實」與「你的推論」；完全不要用投資建議的語氣（不是推薦買賣，是資訊整理）；重要數據盡量註明來源。

【時效性把關——這一段一定要做，之前出過錯】
先跑 `python -m src.main weekly-facts`，它會印出「站內已落地、每一筆都帶日期」的事實：
區間內每個交易日的加權指數收盤、外資／投信買賣超（沒資料的那天會明寫「（尚無資料）」，
不要當成 0）、區間內有新訊號的題材（含 last_signal_date）、以及有查證來源的個股。

- 凡是你要寫成「本週發生」的事，都必須能對應到這個區間內的日期。
- WebSearch 查到但**無法確認發生日期**的事件，要嘛在文中標明實際日期，要嘛不要寫成本週新聞。
- 實際踩過的錯誤：把「大立光重新站回 5,000 元大關」當成本週事件寫進報告，
  但那是好幾週前的事。搜尋結果的排序與語氣不代表時間新舊，一定要自己查日期。
- 法人買賣超請直接引用 weekly-facts 印出來的數字，不要自己另外加總（那份已經
  排除掉「當天資料尚未落地」的日子，自己加會重複計算）。

【產出格式】要輸出 PDF。步驟：
1. 檢查 sandbox 有沒有 python 套件可以產生正確顯示繁體中文的 PDF（例如已安裝 weasyprint、或可以 pip install weasyprint，或系統有 chromium/wkhtmltopdf 可以把 HTML 轉 PDF）。優先順序：能正確顯示中文字（不是方塊亂碼）最重要。
2. 用你選定的工具鏈，把報告內容排版成一份清楚易讀的 PDF（標題、章節、段落，適當留白，不需要花俏設計）。
3. 產出後務必驗證：用 pdftotext（沒有就 pip install 或 apt install poppler-utils）把 PDF 轉回文字，確認繁體中文可以正確讀出來，不是亂碼或空白。如果怎麼試都無法正確產生可讀的中文 PDF，才退而求其次改產出一份排版整齊的獨立 HTML 檔（.html，一樣放同個路徑但副檔名改 .html），並在最後的執行摘要裡誠實說明為什麼沒有做出 PDF。

【檔案位置】
- 今天日期用 `date +%Y-%m-%d` 取得（純日期，例如 2026-09-11）。報告存成 docs/weekly/<日期>.pdf（或退而求其次的 .html）。
- 讀取並更新 docs/weekly/index.html：這是週報列表頁，第一次執行時裡面只有佔位文字，把它換成一個清單頁——保留跟首頁一致的版型/CSS（直接複用檔案裡現成的 <head> 那段，不要整個重寫），內容是「本週報告」連結卡片，之後每次執行都要在清單最上面新增一筆（保留之前所有週的記錄，不要覆蓋掉歷史），每筆包含日期、連結、還有這份報告的一句話重點摘要。

【Git 操作】
- 開始前先 `git pull --rebase origin main`（這支自動化系統的機器人可能剛好也在同時間 commit，用 rebase 避免衝突）。
- 只 commit 你新增/修改的檔案（docs/weekly/ 底下的東西），絕對不要動 docs/index.html、docs/archive.html、docs/themes.html、docs/reports/、docs/heatmap.html、docs/scores.html、docs/chips.html、docs/disposition.html、data/market.db，或任何其他既有頁面/資料庫檔案。
- commit message 用繁體中文，簡短說明「週報更新」＋日期。
- push 到 main。如果 push 被拒絕（代表遠端又有新 commit），重新 `git pull --rebase origin main` 再 push，最多重試 3 次。

【驗證】完成後確認：新的 PDF/HTML 檔案存在且非空、docs/weekly/index.html 確實新增了這週的項目、git push 真的成功（用 `git log origin/main -1` 或類似方式確認）。在最後給我一份簡短的執行摘要：做了什麼、PDF 是否成功、push 是否成功、遇到什麼問題。
```

---

### 3.7 台股深度月報：全球總經＋台股結構＋焦點個股

- **cron**：`0 0 1 * *` （UTC 每月 1 號 00:00 ＝ **台灣每月 1 號 08:00**）
- **model**：`claude-sonnet-5`
- **allowed_tools**：`Bash, Read, Write, Edit, Glob, Grep, WebSearch, WebFetch`
- **prompt**：

```
你正在為「盤後快訊」台股報告網站（chen022208-cell/stock-report，已 clone 到本機）產出每月深度月報。這個網站平常有一支獨立的 Python 自動化（main.py）每個交易日跑早報/盤後報告、每月12號跑一份事後績效回顧月報，你這次的任務跟那兩個完全分開、不要去動它們產出的檔案——這是「另一份」全新的深度研究報告，不是績效回顧。

【任務】用繁體中文寫一份上個月的深度研究報告，分三大部分：
1. 全球總體經濟：上個月主要央行動態、利率/通膨趨勢、地緣政治事件、原物料與美股/美元/美債走勢，對台股/科技供應鏈的傳導路徑。用 WebSearch/WebFetch 做即時研究，不要用你訓練資料裡的舊記憶當成上個月的事實，要查證真實發生的事件。
2. 台股市場結構研究：上個月大盤走勢、資金流向（外資/投信/自營）、產業輪動、跟前幾個月相比出現的結構性變化（不是逐日流水帳，是月度視角的趨勢判讀）。可以先讀 data/market.db（sqlite3，market_snapshots、themes 表）、docs/data/*.json、以及上個月累積的 docs/reports/*.html 報告，了解這支自動化系統整個月已經抓到什麼訊號，你的月報要在這個基礎上做更完整的綜合判讀。
3. 焦點個股深度分析：只挑上個月表現最突出、成長最好的少數幾檔個股（不是全部，只挑真的亮眼的，大約3-6檔），針對這幾檔做深入的個股分析——公司在做什麼生意、上月為什麼漲、有沒有基本面（營收/法人籌碼）支撐、後續觀察重點。判斷依據可以參考 docs/reports/*.html 裡系統標記過的評分/題材/技術面資料，並用 WebSearch 補上更完整的公司背景與最新消息。

寫作要求：分析文章形式、有論述脈絡，不是條列摘要；明確區分「已驗證的事實」與「你的推論」；完全不要用投資建議的語氣（不是推薦買賣，是資訊整理）；重要數據盡量註明來源；焦點個股務必是根據上月實際數據挑出來的少數標的，不要為了湊數硬寫沒有明顯亮點的股票。

【時效性把關】先跑 `python -m src.main monthly-facts`，它會印出站內已落地、每一筆
都帶日期的事實（逐日收盤、法人買賣超、題材訊號日期、有查證來源的個股）。凡是要寫成
「上個月發生」的事，都要能對應到區間內的日期；WebSearch 查到但無法確認日期的事件，
要嘛標明日期、要嘛不要寫成上月新聞。法人買賣超直接引用該指令印出的數字，不要自己加總。

【產出格式】要輸出 PDF。步驟：
1. 檢查 sandbox 有沒有 python 套件可以產生正確顯示繁體中文的 PDF（例如已安裝 weasyprint、或可以 pip install weasyprint，或系統有 chromium/wkhtmltopdf 可以把 HTML 轉 PDF）。優先順序：能正確顯示中文字（不是方塊亂碼）最重要。
2. 用你選定的工具鏈，把報告內容排版成一份清楚易讀的 PDF（標題、章節、段落，適當留白，不需要花俏設計）。
3. 產出後務必驗證：用 pdftotext（沒有就 pip install 或 apt install poppler-utils）把 PDF 轉回文字，確認繁體中文可以正確讀出來，不是亂碼或空白。如果怎麼試都無法正確產生可讀的中文 PDF，才退而求其次改產出一份排版整齊的獨立 HTML 檔（.html，一樣放同個路徑但副檔名改 .html），並在最後的執行摘要裡誠實說明為什麼沒有做出 PDF。

【檔案位置】
- 用 `date +%Y-%m` 取得上個月（如果今天是每月1號，代表要處理的是剛結束的上個月，例如今天是2026-10-01，取得的月份字串要是 2026-09，用 date -d 'last month' +%Y-%m 或等效方式計算，不要直接用今天的年月）。報告存成 docs/monthly-deep/<年-月>.pdf（或退而求其次的 .html）。
- 讀取並更新 docs/monthly-deep/index.html：這是深度月報列表頁，第一次執行時裡面只有佔位文字，把它換成一個清單頁——保留跟首頁一致的版型/CSS（直接複用檔案裡現成的 <head> 那段，不要整個重寫），內容是「本月報告」連結卡片，之後每次執行都要在清單最上面新增一筆（保留之前所有月份的記錄，不要覆蓋掉歷史），每筆包含年月、連結、還有這份報告的一句話重點摘要（可以提到這次挑了哪幾檔焦點個股）。

【Git 操作】
- 開始前先 `git pull --rebase origin main`（這支自動化系統的機器人可能剛好也在同時間 commit，用 rebase 避免衝突）。
- 只 commit 你新增/修改的檔案（docs/monthly-deep/ 底下的東西），絕對不要動 docs/index.html、docs/archive.html、docs/themes.html、docs/reports/、docs/heatmap.html、docs/scores.html、docs/chips.html、docs/disposition.html、data/market.db，或任何其他既有頁面/資料庫檔案。
- commit message 用繁體中文，簡短說明「深度月報更新」＋年月。
- push 到 main。如果 push 被拒絕（代表遠端又有新 commit），重新 `git pull --rebase origin main` 再 push，最多重試 3 次。

【驗證】完成後確認：新的 PDF/HTML 檔案存在且非空、docs/monthly-deep/index.html 確實新增了這個月的項目、git push 真的成功（用 `git log origin/main -1` 或類似方式確認）。在最後給我一份簡短的執行摘要：做了什麼、挑了哪幾檔焦點個股、PDF 是否成功、push 是否成功、遇到什麼問題。
```

---

### 3.8 台股盤中焦點股深度快報

> 這支除了 cron 之外，還綁了一個 **webhook**（見第 4 節）。cron 是收盤後掃尾用。

- **cron**：`0 6 * * 1-5` （UTC 平日 06:00 ＝ **台灣平日 14:00**，收盤後掃尾）
- **model**：`claude-sonnet-5`
- **allowed_tools**：`Bash, BashOutput, KillBash, Read, Write, Edit, Glob, Grep, WebSearch, WebFetch, TodoWrite`
- **prompt**：

```
你正在為「盤後快訊」台股報告網站（chen022208-cell/stock-report）跑「盤中焦點股深度快報」。盤中篩選器拓到新的 A 級強勢股時（寫進 intraday-data 分支的 intraday_new_signal.json），會 webhook 觸發這個 Routine。你要對這幾檔逐檔上網查證後寫一份快報。沒有新標的、或今日已達上限就安靜結束，不要通知使用者。

【鐵則】公司「在做什麼」只能根據程式附上的申報主要經營業務 ＋ 你這次 WebSearch 查到的，絕對不可以用股票名稱或產業分類推測（歷史事故：把做乳房重建軟組織的 7686 捷立康寫成 PCB 廠）。查不到可靠外部來源就不要寫那一檔。

【執行步驟】
1. 工作目錄若還沒有 repo 就 `git clone https://github.com/chen022208-cell/stock-report` 再 `cd stock-report`；已存在就 `cd` 進去 `git fetch origin && git checkout main && git pull --rebase origin main`。
2. `pip install -r requirements.txt`（裝過會很快跳過）。
3. 用 Bash 背景執行（run_in_background: true）：`LLM_AGENT_MODE=1 python -m src.main intraday-report`
   - 這支程式（run_intraday_deep_report）會去拓 intraday-data 分支上的 intraday_new_signal.json，對比今日已產出的快報（去重）、每日上限 5 篇、分數門檻後，把要問 LLM 的內容寫成一個 `agent_llm_queue/<uuid>.request.json`（`{system, user, max_tokens}`），然後卡住等 `agent_llm_queue/<uuid>.response.txt` 出現。若沒有新標的會直接印「沒有今日的新訊號」或「已達上限」並結束，這時你直接跳到步驟 6。
4. 在背景程序執行期間，輪詢（每 5-10 秒 `ls agent_llm_queue/*.request.json`）偵測那個 request 檔：
   - 讀出 `system`（角色與輸出格式規定）與 `user`（這批股票，每檔附：代號、名稱、產業、申報主要經營業務、觸發當下的量價訊號）。
   - 對每一檔股票用 WebSearch／WebFetch 查證（鉅亨網 cnyes.com、Goodinfo goodinfo.tw、財報狗 statementdog.com、公司官網、經濟日報 money.udn.com／工商時報 ctee.com.tw）：確認主力產品、主要客戶或終端應用、今天有沒有明確的個股催化事件。
   - 依 `system` 要求產生純 JSON（不要 markdown fence），key 是股票代號，每檔 `{headline, company_desc, swot:{strengths,weaknesses,opportunities,threats}, sources:[...]}`：
     · headline：一句話這檔今天盤中在動什麼；查不到明確催化就寫「盤面帶動，暫無明確個股消息」，不要編。
     · company_desc：2～3 句，這家公司實際在做什麼➕關鍵事實。
     · swot：各 1～2 句，推理標「（推論）」。
     · sources：實際看過的來源，至少 1 個是申報值以外的外部來源。查不到就不要放進 JSON。
   - 寫進 `agent_llm_queue/<同一個 uuid>.response.txt`。用 BashOutput 確認背景程序已結束、沒有殘留未回覆的 request。
5. 程式會：寫進 intraday_reports 表➕ 產出 docs/analysis/<日期>-<代號>.html ➕ 更新 docs/analysis/index.html ➕ 寫 docs/_notify_intraday.json。終端會印 `[intraday-report] 產出 N 篇`。
6. `git status -sb`。若 `data/market.db`、`docs/analysis/`、`docs/_notify_intraday.json` 有變動：`git add -A && git commit -m "盤中焦點股快報 $(TZ=Asia/Taipei date +%Y-%m-%d\ %H:%M)（N 篇）"` → `git pull --rebase origin main` → `git push`。被拒就重新 rebase 再推，最多 3 次。_notify_intraday.json 一進 main，另一支 workflow（daily-notify.yml）會自動發 Discord。
7. 若程式印「產出 0 篇」、「沒有今日的新訊號」、「這批訊號已處理過」或 git 沒任何變動：安靜結束，不要 commit、不要通知使用者（webhook 每分鐘都可能觸發，大多數時候本來就是空跑）。
8. 只有失敗時（程式逾時、git push 連 3 次失敗、程式報錯）才 PushNotification。

這條 Routine 既有 webhook 觸發（盤中新 A 級訊號），也有一個每日盤後的 cron 当掃尾（把收盤前最後一波新訊號補齊）。兩種情境執行邏輯完全一樣。
```

---

## 4. Webhook（只有 3.8 有）

「台股盤中焦點股深度快報」除了 cron，還綁了一個 GitHub push webhook，讓盤中篩選器一 push 新訊號就觸發它。

舊帳號的設定值：

| 欄位 | 值 |
|---|---|
| hook_type | `app` |
| source | `github` |
| scope_id | `chen022208-cell/stock-report`（回傳會正規化成 `github.com/chen022208-cell/stock-report`） |
| events | `["push"]` |
| routine_trigger_id | 3.8 那支 Routine 的 id（舊帳號是 `trig_01HpFbfTcMzrwPn2LdBG3Smh`） |
| 舊帳號產生的 webhook id | `25f1fb92-91cd-490b-8dfe-a6b574ba228c`（僅供參考，新帳號會產生新的） |

**新帳號重建方式**：目前只能透過 API。在新帳號的 Claude Code session 裡（要能用 `RemoteTrigger` 工具）呼叫：

```
RemoteTrigger action=create_webhook_trigger body={
  "routine_trigger_id": "<新帳號 3.8 那支的 id>",
  "hook_type": "app",
  "source": "github",
  "scope_id": "chen022208-cell/stock-report",
  "events": ["push"]
}
```

注意：**這個 webhook 無法依分支/路徑過濾**，任何 push 到 repo 都會觸發 3.8。多數是空跑（程式判斷沒有今日新訊號就結束）。`intraday.yml` 迴圈已經把一般行情資料 push 壓到每 10 分鐘一次來降低空跑量，只有 `intraday_new_signal.json` 變動才立即 push。

如果新帳號沒有 `RemoteTrigger` 工具、也沒有 webhook 建立管道，就先**只靠 cron**（`0 6 * * 1-5` 收盤後掃尾）跑 3.8，latency 變成「當天收盤後才出快報」，功能不會壞、只是不即時。

---

## 5. 搬完的驗收清單

- [ ] 新帳號 8 支 Routine 都建好、`enabled: true`
- [ ] 每支 `/schedule` → Run now 各測一次，看 https://claude.ai/code/routines 的 run log 沒報錯（早報/盤後遇到休市日會正常略過）
- [ ] 3.8 的 webhook 建好，或已接受「只靠 cron」
- [ ] 新帳號雲端環境能 push 到 repo（看某支 Run now 有沒有成功 commit）
- [ ] 新帳號 Default 環境 network access 不是 Trusted
- [ ] **舊帳號 8 支全部 Disable/Delete**（https://claude.ai/code/routines）
- [ ] `CLAUDE.md` 裡寫死的舊 trigger id 更新成新的（`trig_01E5mQG59kmfAewV1V79BoBY`、`trig_01HTffRTdsBaegyH5EiyRryw`、`trig_01WCoQ9PpAkkwE6PgAWEzR9H`、`trig_01HpFbfTcMzrwPn2LdBG3Smh`）

---

## 6. 附：cron 時刻總表（UTC → 台灣）

| Routine | cron (UTC) | 台灣時間 |
|---|---|---|
| 每日早報 | `0 23 * * 0-4` | 平日 07:00 |
| 每日盤後 | `0 10 * * 1-5` | 平日 18:00 |
| 即時快訊監控 | `33 * * * *` | 每小時 :33 |
| 使用者研究提交處理 | `0 11 * * 1-5` | 平日 19:00 |
| 主題點播深度報告 | `30 11 * * 1-5` | 平日 19:30 |
| 個股逐檔查證 | `0 3 * * *` | 每天 11:00 |
| 週報 | `0 12 * * 5` | 週五 20:00 |
| 深度月報 | `0 0 1 * *` | 每月 1 號 08:00 |
| 盤中焦點股深度快報（掃尾 cron） | `0 6 * * 1-5` | 平日 14:00 |
