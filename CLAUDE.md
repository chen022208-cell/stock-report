# 盤後快訊（stock-report）

台股報告網站。網站本體：https://chen022208-cell.github.io/stock-report

## 自動化架構

- **每日早報／盤後**：Claude Code Routine「台股每日早報」「台股每日盤後」驅動，
  `LLM_AGENT_MODE=1 python -m src.main auto morning|evening`，由該次 Routine 親自
  服務 `agent_llm_queue/` 產生內容（吃 Pro/Max 額度，不打計量 API），跑完自動
  commit + push。每月 12 號的績效回顧月報併在「台股每日盤後」裡多跑一次
  `python -m src.main monthly`。
- **每週深度週報**：Routine「台股週報：全球總經＋台股深度研究」，每週五，
  產出 `docs/weekly/<日期>.pdf`。
- **每月深度月報**：Routine「台股深度月報：全球總經＋台股結構＋焦點個股」，
  每月 1 號，產出 `docs/monthly-deep/<年-月>.pdf`。
- `.github/workflows/report.yml` 的 `schedule` 已拿掉（改由上面的 Routine 驅動，
  避免計量 API 費用），只留 `workflow_dispatch` 當手動備援。
- Discord／Telegram 通知：雲端 Routine 本機沒有 webhook 密鑰，由 `src/notify.py`
  寫一份 `docs/_notify_payload.json`，`daily-notify.yml` 偵測到這個檔案變動時
  用 repo 的密鑰代為送出；週報/月報的新 PDF 則由 `deep-report-notify.yml` 偵測
  `docs/weekly/*.pdf`、`docs/monthly-deep/*.pdf` 新增直接送 Discord。

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
