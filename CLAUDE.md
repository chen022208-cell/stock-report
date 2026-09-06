# 盤後快訊（stock-report）

台股報告網站。網站本體：https://chen022208-cell.github.io/stock-report

## 自動化架構

> 全部 8 支雲端 Routine 的完整規格（名稱／cron／model／allowed_tools／prompt 原文）
> 與「搬到另一個 Claude 帳號」的步驟，見 `ROUTINES.md`。

- **每日早報／盤後**：Claude Code Routine「台股每日早報」「台股每日盤後」驅動，
  `LLM_AGENT_MODE=1 python -m src.main auto morning|evening`，吃 Pro/Max 額度、
  不打計量 API。每月 12 號的績效回顧月報併在「台股每日盤後」裡多跑一次
  `python -m src.main monthly`。
  這兩個 Routine 用的雲端環境（"Default"，env_014xwk8aXGAakucHHKt3G6hW）一度因為
  Network access 設為「Trusted」（只放行套件來源，不含一般網站）擋掉
  `www.twse.com.tw` 等資料源（EGRESS_BLOCKED），2026-09-05 使用者把該環境的
  Network access 改掉之後已確認 `www.twse.com.tw`／`tpex.org.tw`／`discord.com`
  都連得到，兩個 Routine 已重新啟用，`report.yml` 的 `schedule` 已再次拿掉
  （只留 `workflow_dispatch` 當手動備援）。如果之後這兩個 Routine 又開始失敗、
  或看到 `EGRESS_BLOCKED`/`403`/`Tunnel connection failed` 這類錯誤，先去
  claude.ai → Settings → Claude Code → Cloud → Default 環境 → 齒輪設定 → Network
  access 檢查目前是不是又跳回 Trusted，不要一發現問題就急著恢復 GitHub Actions
  排程——先確認是不是網路設定被改回去。
  兩個 Routine 目前的 job_config 沒有預先掛載 git source，每次執行都是自己手動
  `git clone`，能動但每次多花幾秒，不算 bug。
- **每週深度週報**：Routine「台股週報：全球總經＋台股深度研究」，每週五，
  產出 `docs/weekly/<日期>.pdf`。不受網路政策影響（主要用 WebSearch/WebFetch
  做研究，讀本地 repo 檔案，不需要直連 twse.com.tw）。
  下次執行：2026-09-11。
- **每月深度月報**：Routine「台股深度月報：全球總經＋台股結構＋焦點個股」，
  每月 1 號，產出 `docs/monthly-deep/<年-月>.pdf`。同樣不受網路政策影響。
  下次執行：2026-10-01。
- **個股逐檔查證**：Routine「台股個股逐檔查證（每天 10 檔）」
  （trig_01E5mQG59kmfAewV1V79BoBY），cron `0 3 * * *`（每天 11:00 台灣時間）。
  `LLM_AGENT_MODE=1 python -m src.main verify-stocks`：取市值（最新月營收）
  最大、`stock_analysis` 還沒有或超過 180 天的個股，一次 10 檔，雲端 CCR
  session 對每一檔實際 WebSearch 鉅亨／Goodinfo／財報狗／官網／年報查證後
  才寫 `company_desc`＋SWOT，回覆一定要帶 `sources`（至少 1 個申報值以外的
  外部來源），查不到就不寫那一檔。有寫入才 commit push、沒有就安靜結束，
  失敗才 PushNotification。job_config 有預掛 git source。這條**取代**了已停用
  的全市場 SWOT 批次回填（process_stock_swot_batch）。
- **重要：`docs/weekly/index.html`／`docs/monthly-deep/index.html` 不要再手寫**。
  這兩頁本來是 Routine 每次執行時手動編輯 HTML（在檔案裡直接加一段 `.card`），
  2026-09-05 發現這樣做的版本忘記幫 nav 連結加 `../` 前綴（這兩頁在子目錄下），
  結果整排導覽列點什麼都 404。已經改成 `render.render_weekly_index()`／
  `render.render_monthly_deep_index()` 從 `docs/data/weekly_reports.json`／
  `docs/data/monthly_deep_reports.json` 讀資料、套用 `base.html` 樣板產生，
  `render_site()` 每次都會重繪。**新產出一份 PDF 時，只需要在對應的 JSON
  陣列「最後面」新增一筆 `{"filename":, "title":, "summary":, "date_label":}`
  （陣列是舊到新，render 那邊會自己反轉成新到舊），然後跑一次
  `python -m src.main site` 讓 index.html 重新產生——不要直接編輯 index.html，
  不然下次又會跟 nav 樣板的更新脱鉾。
- Discord／Telegram 通知：`src/notify.py` 會寫一份 `docs/_notify_payload.json`，
  `daily-notify.yml` 偵測到這個檔案變動時用 repo 的密鑰代為送出（發送時記得帶
  瀏覽器樣式 User-Agent，Discord/Cloudflare 會擋掉預設 UA，見 `send_discord()`）；
  週報/月報的新 PDF 則由 `deep-report-notify.yml` 偵測
  `docs/weekly/*.pdf`、`docs/monthly-deep/*.pdf` 新增直接送 Discord。
- **即時快訊監控**：`python -m src.main news`（`run_news_monitor()`）打
  `api.wallstreetcn.com` 的公開 JSON API（不是 wallstreetcn.com 網頁本身，
  那是前端渲染的 SPA，純 HTTP 抓不到內容）抓即時快訊，先用來源自己的
  `score`（重要度分數，≥2 算重要）粗篩，再用跟使用者研究提交同一套
  `_process_research_submission()` 分析＋驗證，只有「夠重要且真的跟現有
  題材/個股有關」才推播通知，避免每則國際新聞都推播造成通知疲勞。用
  `app_state` 表的 `news_monitor_last_id` 記錄檢查到哪一則，第一次執行只
  記錄基準不推播（避免一次性把歷史快訊全部分析一輪）。**排程：Routine
  「台股即時快訊監控」（trig_01HTffRTdsBaegyH5EiyRryw），cron `33 * * * *`
  每小時跑一次**，跟每日早報／盤後一樣是 CCR session 自己 clone repo、
  自己扮演 LLM 服務 `agent_llm_queue/`、只有真的產生變更才 commit push，
  失敗才 PushNotification。
- **盤中強勢股篩選（波段用）**：GitHub Actions 迴圈式（方案①），
  `.github/workflows/intraday.yml`。平日 08:45（Asia/Taipei）由外部 cron-job.org
  POST `repository_dispatch`（`event_type: intraday-loop`）觸發，一支長 job 內部
  每 ~55 秒跑一輪 `python -m src.main intraday`，用證交所 MIS 端點
  （`mis.twse.com.tw/stock/api/getStockInfo.jsp`，上市＋上櫃一次抓）拿近即時報價，
  跑多層漏斗（相對強度 vs 大盤／同業 → 昨量比（含盤中時間校正）→ 動能／突破
  20/60/120/252 日高）算加權分數分 A（≥80）／B（≥60）級。結果推到 **`intraday-data`
  分支**（force push，不留歷史），前端 `docs/intraday.html`（⚡ 盤中強勢，nav 有連結）
  每 45 秒 fetch `raw.githubusercontent.com/.../intraday-data/docs/data/intraday.json`。
  滾動參考值（新高、昨量）存 `data/intraday_hist.db`（不進 git，Actions cache 跨天保留），
  **需要幾個交易日暖機**新高／量比才準。`config.yaml` 的 `intraday:` 區塊調參數，
  公開頁 `display_delay_min: 15`（不轉發即時報價給第三方）。`intraday-ref` 子命令
  盤前更新參考值、`intraday --loop --until HH:MM --interval N` 跑迴圈。
- **盤中焦點股深度快報**：盤中篩選器拓到新 A 級強勢股時（`_update_signals` 寫
  `docs/data/intraday_new_signal.json` 並 push 到 intraday-data），webhook 觸發雲端
  Routine「台股盤中焦點股深度快報」（**trig_01HpFbfTcMzrwPn2LdBG3Smh**；webhook
  `25f1fb92-91cd-490b-8dfe-a6b574ba228c`，GitHub App、push 事件、**無法依分支/路徑
  過濾**；另有 cron `0 6 * * 1-5`＝台灣 14:00 當盤後掃尾）。Routine 跑
  `LLM_AGENT_MODE=1 python -m src.main intraday-report`（`run_intraday_deep_report`）：
  讀 intraday-data 的 `intraday_new_signal.json`，扣掉今日已產出的、每日上限
  **5 篇**（`deep_report_daily_cap`）、分數門檻（`deep_report_min_score`）後，對每檔
  用 `llm.intraday_flash_report` 走 `agent_llm_queue/`，CCR session 實際 WebSearch
  查證（鉅亨／Goodinfo／財報狗／官網），回覆一定帶 `sources`（≥1 個申報值以外
  外部來源），查不到就不寫那一檔。產出 `intraday_reports` 表 ＋
  `docs/analysis/<日期>-<代號>.html` ＋ `docs/analysis/index.html`（複用「一般對話
  股票分析同步存檔」那條路的資料夾）＋ `docs/_notify_intraday.json`（`daily-notify.yml`
  偵測到就發 Discord）。webhook 每次 push 都觸發、多數空跑（Routine 判斷沒有今日
  新訊號就安靜結束），所以 `intraday.yml` 迴圈把一般行情資料 push 壓到每 10 分鐘
  一次、只有 `intraday_new_signal.json` 變動才立即 push。**快報形式、非深度查證**，
  跟 `lookup.html` 的逐檔人工查證分析（`verify-stocks`）是兩回事。

## 個股技術圖表／查詢

- 每個出現股票代號的地方（評分頁、盤後報告技術面表格、籌碼頁、熱力圖展開列表、
  首頁焦點個股）都用 `data-stock-code`/`data-stock-name` 屬性標記，
  `docs/assets/stock-chart.js` 用事件委派監聽全站點擊，跳出彈窗即時抓
  TWSE STOCK_DAY 資料、純前端算 K 線/MA5/20/60/RSI 並畫圖——**這是瀏覽器直接對
  twse.com.tw 發 fetch，不經過任何後端或雲端 agent 環境，完全不受上面那個網路
  政策問題影響，永遠是即時真實資料**。
- **上櫃／興櫃沒有即時圖，走後端快照**：`tpex.org.tw` 完全沒送
  `Access-Control-Allow-Origin`、OPTIONS preflight 直接 403，瀏覽器端不可能
  直接抓。改由盤後 `snapshot_offmarket_history()` 抓好存成
  `docs/data/tpex_hist/<code>.json`，前端在 `stock_info` 的 `profile.market`
  是 tpex／esb 時直接讀快照（不要再打 TWSE，那是 36 次必定失敗的請求）。
  - 上櫃：`tradingStock` 端點，有真正的開高低收。
  - 興櫃：`emerging/historical` 只給得到**日均價**（議價／搓合市場沒有開收盤），
    K 棒是均價走勢；**但看盤講的股價是當日行情表的「成交」欄**，要另外用
    `tpex.fetch_esb_pricing()`（一支 bulk API）補進快照的 `latest` 欄位，
    彈窗標題價格用它。2026-09-05 曾經拿日均價 686.33 當 7686 的股價顯示，
    跟 TPEx 網站的成交價 802 對不起來被使用者抓到，不要再犯。
- 全市場個股查詢頁 `lookup.html`（`render.render_lookup_page()` 產出，讀
  `docs/data/heatmap.json` 建出 `docs/data/stock_index.json`，涵蓋當天全市場
  約 2000+ 檔個股）：純前端搜尋代號/名稱，點結果一樣跳出上面那個即時圖表。
  ⚠️ `stock_index.json` 混了 ETF／權證而且**沒有市場別欄位**，不要拿它當
  「全市場公司清單」用——那是 `main._all_market_codes()`（讀申報基本資料
  t187ap03 三個資料集）的工作，那份才有正確的 twse／tpex／esb 市場別。

### 個股資料三層（`docs/data/stock_info/<code>.json`，每檔一個小檔案）

`render.render_stock_info()` 產出，`stock-chart.js` 彈窗按需載入，畫面上**每一區都
標示資料來源**，重點是不要把「判讀」講得像「事實」：

1. **`profile` — 公司基本資料（事實）**：`company_profile` 表，來自
   `mops.fetch_company_profile()`（公開資訊觀測站 t05st03）。其中
   **`business`（主要經營業務）是「這家公司到底在做什麼」的唯一權威依據**。
2. **`rev` — 基本面（事實）**：`monthly_revenue` 表，來自
   `mops.fetch_monthly_revenue()`（政府開放資料，上市 `t187ap05_L`＋上櫃
   `mopsfin_t187ap05_O`＋**興櫃 `t187ap05_R`**，約 2333 檔月營收／YoY／MoM／
   累計），完全不經 LLM。三個資料集欄位名稱一致，同一個 parser 就能吃；
   之前少了興櫃那條，興櫃約 360 檔的基本面區塊全是空的。仍有 ~12 檔沒有
   月營收：金控／壽險（合併申報）、存託憑證（境外發行）、剛登錄還沒到第一次
   申報的興櫃——這是資料本身沒有，前端會明講而不是整段消失。
3. **`themes` — 判讀（事實層旁的歸類）**：本站題材知識庫的歸類（只有 ~156 檔
   被歸過類，**不硬把每檔都塞進題材**，沒歸類的前端就明寫「尚未歸入任何題材」）。
4. **`desc` / `swot` — 判讀（只在逐檔查證過的個股上出現）**：`stock_analysis` 表。
   **不是全市場都有**——只有：(a) 評分頁焦點股（`llm.stock_analysis_batch`，
   帶技術／籌碼／營收／新聞脈絡，每天盤後重產，寫進 `scores.json`）(b) 逐檔
   人工查證過的個股（目前 11 檔：1597 直得、2388 威盛、2413 環科、3094 聯傑、
   3324 雙鴻、3441 聯一光、3591 艾笛森、3714 富采、6168 宏齊、6224 聚鼎、
   6271 同欣電）。其餘個股彈窗只顯示第 1～3 層（基本資料＋月營收＋題材），
   前端 `swotHtml()` 在 `!info.desc` 時直接不渲染那一區。

`render_stock_info()` 的個股頁範圍＝`company_profile`（申報營業項目 t187ap03
全市場 2345 檔）＋有盤後快照的上櫃／興櫃。**題材成員只用來標註既有個股頁、
不會憑題材歸類就多生一頁**——否則〔國際〕總經題材裡的 SPY／NVDA／MSFT 這些
美股 ETF 也會被生成空的個股頁。`_all_market_codes()` 除了 t187ap03 三個資料集，
還會把月營收資料集裡多出來的代號補進來（t187ap03 偶爾跟月營收對不齊，例如
2867 三商美邦人壽、4150 優你康、5371 中強光電、7834 來毅），`sync_company_profiles`
再逐檔去 MOPS t05st03 補「主要經營業務」，不會因名單缺漏就永遠少一頁。

⚠️ 歷史教訓（2026-09-05 → 09-06）：先是想「全市場每檔都補 SWOT」，用
LLM_AGENT_MODE 一次灌 262 檔憑股票名稱瞎猜——把做**乳房重建軟組織填補**的
7686 捷立康寫成「PCB 廠」、做**光學玻璃／稜鏡**的 3441 聯一光寫成「LED 封裝廠」。
接著改成「以 `company_profile.business` 申報值為底＋標『（推論）』」，把全市場
2341 檔都補了一輪。**但 09-06 使用者仍判定這是「沒有證實的假資料」**：`company_desc`
雖錨在申報營業項目，SWOT 敘述裡還是塞了申報項目沒有、也沒逐檔查證的公司細節
（競爭對手、市占、合資關係、產品代號）。**最終決定：整批清空，`stock_analysis`
只留逐檔查證過的 11 檔＋評分頁焦點股每天重產的那批；`run_evening` 不再呼叫
`process_stock_swot_batch`（函式保留但停用）。個股資料頁上「公司在做什麼」只認
`company_profile.business` 申報值；要放判讀就得逐檔查證（WebSearch 鉅亨／
Goodinfo／財報狗／公司官網／財報），沒查證的寧可整區不顯示。**

## 使用者研究提交（提交研究／研究筆記頁）

靜態站沒有後端，「上傳文章」的路是 GitHub Issue：`submit.html` 讓使用者填標題/
內容後，前端 JS 開一個帶 `research-submission` 標籤（已在 repo 建立）的預填
GitHub 新增 Issue 分頁，使用者自己按「Submit new issue」即完成上傳（不用密鑰、
不用另外的帳號系統，因為使用者本來就是 repo owner）。

`python -m src.main research`（`run_research_intake()`）用 `gh issue list` 讀取
待處理的 Issue，交給 `llm.analyze_research_submission()` 分析。**驗證是這個功能
最重要的部分**：LLM 一定要標記 `verified`／`conflicting`／`unverified` 三種狀態，
只有明確 `verified` 且真的對應到既有題材，才用 `db.append_research_to_theme()`
累加寫進題材的 `theme_updates` 時間軸（不直接覆寫 `themes` 主表，之後寫深度報告
會自然讀到這筆）；`conflicting`／`unverified` 一律只存進 `research_notes` 表，
絕不動任何既有資料——寧可保守判定 unverified，不要因為內容「聽起來合理」就套用，
避免未經證實的來源污染整份報告的真實性。結果會留言在對應 Issue 上並關閉，
`research.html` 列出所有提交與其驗證狀態、有沒有真的套用。

**排程：Routine「台股使用者研究提交處理」（trig_01WCoQ9PpAkkwE6PgAWEzR9H），
cron `0 11 * * 1-5`（平日 11:00 UTC＝台灣 19:00，接在盤後報告之後）**。同樣是
CCR session 自己 clone repo、自己扮演 LLM 服務 `agent_llm_queue/`、沒有新提交
就安靜結束、只有失敗才 PushNotification。雲端環境不一定有 `gh` 登入，程式會
自動改走 Google 表單 CSV 那條路（不需要 gh），這不算失敗。

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
