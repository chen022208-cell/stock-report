# 盤後快訊（stock-report）

台股報告網站。網站本體：https://chen022208-cell.github.io/stock-report

## 自動化架構

- **每日早報／盤後／每月12號績效回顧月報**：走 `.github/workflows/report.yml` 的
  GitHub Actions `schedule`（`ANTHROPIC_API_KEY` 計量 API）。**曾經試過改成用
  Claude Code Routine（LLM_AGENT_MODE=1，吃 Pro/Max 額度）取代，Routine 也建好了
  （「台股每日早報」「台股每日盤後」），但那個雲端環境（env_014xwk8aXGAakucHHKt3G6hW）
  的網路政策會擋掉 `www.twse.com.tw`、`tpex.org.tw`、`finance.yahoo.com`、
  `discord.com` 等外部網域（回 `EGRESS_BLOCKED`），導致抓不到大盤/個股即時資料，
  報告會產出空殼內容——這兩個 Routine 目前是 disabled 狀態，report.yml 的
  schedule 已經恢復。** 如果之後使用者把該 Claude Code 環境的網路白名單開放給
  這些網域，可以重新 enable 那兩個 Routine 並拿掉 report.yml 的 schedule；在那之前
  不要再關掉 report.yml 的 schedule。
  `src/llm.py` 的 `LLM_AGENT_MODE=1` 機制本身沒問題（本機測試過完整跑通），卡點
  純粹是那個雲端環境的網路政策，不是程式邏輯。
- **每週深度週報**：Routine「台股週報：全球總經＋台股深度研究」，每週五，
  產出 `docs/weekly/<日期>.pdf`。不受網路政策影響（主要用 WebSearch/WebFetch
  做研究，讀本地 repo 檔案，不需要直連 twse.com.tw）。
  下次執行：2026-09-11。
- **每月深度月報**：Routine「台股深度月報：全球總經＋台股結構＋焦點個股」，
  每月 1 號，產出 `docs/monthly-deep/<年-月>.pdf`。同樣不受網路政策影響。
  下次執行：2026-10-01。
- Discord／Telegram 通知：`src/notify.py` 會寫一份 `docs/_notify_payload.json`，
  `daily-notify.yml` 偵測到這個檔案變動時用 repo 的密鑰代為送出（發送時記得帶
  瀏覽器樣式 User-Agent，Discord/Cloudflare 會擋掉預設 UA，見 `send_discord()`）；
  週報/月報的新 PDF 則由 `deep-report-notify.yml` 偵測
  `docs/weekly/*.pdf`、`docs/monthly-deep/*.pdf` 新增直接送 Discord。

## 個股技術圖表／查詢

- 每個出現股票代號的地方（評分頁、盤後報告技術面表格、籌碼頁、熱力圖展開列表、
  首頁焦點個股）都用 `data-stock-code`/`data-stock-name` 屬性標記，
  `docs/assets/stock-chart.js` 用事件委派監聽全站點擊，跳出彈窗即時抓
  TWSE STOCK_DAY 資料、純前端算 K 線/MA5/20/60/RSI 並畫圖——**這是瀏覽器直接對
  twse.com.tw 發 fetch，不經過任何後端或雲端 agent 環境，完全不受上面那個網路
  政策問題影響，永遠是即時真實資料**。上櫃（TPEx）個股目前測過該端點不支援
  瀏覽器端 CORS，圖表會誠實顯示查無資料，不會生成假圖。
- 全市場個股查詢頁 `lookup.html`（`render.render_lookup_page()` 產出，讀
  `docs/data/heatmap.json` 建出 `docs/data/stock_index.json`，涵蓋當天全市場
  約 2000+ 檔個股）：純前端搜尋代號/名稱，點結果一樣跳出上面那個即時圖表；
  已經在評分頁被系統選中、有完整 SWOT 分析的個股額外標記「⭐ 完整分析」連結。
  評分頁的公司介紹／SWOT／漲跌原因（LLM 產出）目前仍只有每天系統挑出的
  前 8～12 檔熱門股才有，若要擴大到全市場需要額外的計量 API 費用，尚未實作、
  待使用者決定要不要做。

## 重要：一般對話中的股票分析也要同步存回網站

如果使用者在跟你的對話（不是上面那幾個排程 Routine）裡問股票分析、要你查某檔股票
或某個題材，**除了在對話裡回覆，也要把這份分析同步整理成頁面存進這個網站**，讓網站
資料跟對話同步，而不是只留在聊天記錄裡。原則：

- 存放位置：`docs/analysis/<日期>-<股票代號或主題slug>.html`（例如
  `docs/analysis/2026-09-10-2330.html`），沒有這個資料夾就建立，並比照
  `docs/weekly/index.html`、`docs/monthly-deep/index.html` 的做法維護一份
  `docs/analysis/index.html` 清單頁（複用現成 `<head>` 版型，清單只新增不覆蓋）。
- 內容格式沿用其他報告的原則：分析文章形式、明確區分已驗證事實與推論、不用投資建議
  語氣、數據附來源。
- 完成後一樣要 `git pull --rebase origin main` → commit（訊息簡短說明分析了什麼）
  → push（被拒就重新 rebase 再推，最多 3 次）。
- 目前 `docs/index.html` 首頁的導覽列還沒加上這個新分類的連結，先不要動首頁，除非
  使用者另外要求——避免跟每日自動化搶著改同一個檔案。
- 這是「順手存檔」，不是每次隨口聊天都要生成頁面：使用者明確要求分析、或這是一次
  有實質內容的股票/題材研究時才存；單純問「今天大盤怎樣」這種已經在首頁看得到的
  資訊不需要重複產出頁面。

## 注意：這個 repo 可能同時被多個 Claude Code session 操作

使用者習慣同時在終端機（本機 Claude Code）跟手機／網頁版 claude.ai 開啟同一個
repo 的排程 Routine session 對話，兩邊有時候會拿到一樣的指示、各自做出類似或
重疊的功能（例如個股圖表功能就曾經被兩邊分別實作過一次）。動手改動前，先
`git fetch && git log --oneline origin/main -10` 看一下遠端是不是已經有其他
session 剛推上來的相關改動，避免重工或衝突；真的衝突時優先保留「已經驗證真的
能動」的那個版本。
