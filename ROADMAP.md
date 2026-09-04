# v2 擴充藍圖：產業鏈 / 籌碼 / 評分 / 處置股 / 國際總經

> 這份文件是給 Claude Code 的設計說明，接續 `HANDOFF.md`。
> 目標：把現有「盤後快訊」從「每日報告產生器」擴充成一個**個人用的產業／個股研究資料庫 + 前端**，
> 參考對象是使用者提供的某商業 App（產業題材、供應鏈定位、產業熱力圖、AI 五面向評分、
> 處置股預警、主動式 ETF 追蹤、籌碼動向）。
>
> **硬性要求**：現有的當日報告 / 週報 / 月報全部保留，新模組是「加上去」，不是取代。

## 現況（2026-09-05 更新）

- ✅ **Phase 1 完成並部署**：上櫃併入掃描、產業熱力圖（`heatmap.html`）、
  籌碼儀表板（`chips.html`：法人/資券/強勢股）
- ✅ **Phase 2 完成並部署**：處置股預警（`disposition.html`，用 TWSE 官方
  `notetrans`「注意累計次數」公告，沒有重寫整套處置規則引擎）、五面向評分四個免費軸
  （`scores.html`）、大戶持股（集保 `opendata.tdcc.com.tw`，併入 chips.html）、
  盤後評論的輸入補上評分前段個股 + 處置概況 + 國際題材，daily.html 新增「今日值得關注」表格
- ⏭️ **主動式 ETF 每日持股**：找不到公開 API（TWSE openapi 只有月報表，沒有逐日 PCF），擱置
- ⏭️ **近期新上市櫃**：`t187ap03_L` 的上市日期欄位查無 2026 年紀錄，資料集可能不適用，擱置
- ⬜ **Phase 3、4 未開始**：產業鏈結構（供應鏈圖）、新聞面評分、FRED 總經、跨市場產業對照

---

## 一、設計原則（沿用 HANDOFF，再加三條）

1. 每個 fetcher 包 try/except，失敗回空值，不拋例外中斷流程。
2. 每個 LLM 呼叫都要在 `src/fetchers/mock.py` 有對應假資料，`DRY_RUN=1` 能完整跑通。
3. 時間一律 `config.now_tpe()` / `today_str()`；台股紅漲綠跌。
4. **（新）能用規則算的就不要用 LLM。** 五面向評分裡有四項是純公式，只有「新聞面」需要 LLM，且要能關掉。
5. **（新）資料落地優先。** 先把每日行情 / 法人 / 資券存進 DB，之後所有分析都查 DB，不重複打外部 API（也解決 TWSE 限流問題）。
6. **（新）產出兩份：HTML（給人看）+ JSON（給未來的 App / 前端吃）。** render 階段兩個都吐，放在 `docs/` 底下同源，App 直接讀 `docs/data/*.json`，不需要架伺服器。

---

## 二、模組拆解（對照 App 截圖）

每個模組標註：**資料源**、**是否免費**、**要不要 LLM**、**難度**、**現況**。
資料源狀態：`✅實測` = 2026-09 這次驗證過可用；`🔍待驗` = 設計上可行但還沒打過。

### M1. 產業鏈分析（上中下游 / 角色定位 / 龍頭 / 關聯度）

App 畫面：一個題材 → 上游供應 / 本題材 / 下游應用；公司依角色分群（矽智財授權 / 客製 ASIC 設計 / 晶圓代工），
標「產業龍頭」「高關聯度」，還有「差異比較」表。

- **沒有免費的權威供應鏈圖資料集**（財報狗 / CMoney 有，要錢）。做法：
  - **LLM 建、DB 存、逐步修正**：題材被辨識出來時，請 LLM 產出結構化供應鏈物件（上/中/下游各段、各段的上市櫃公司、誰是龍頭誰是邊緣），寫進知識庫。之後每次該題材有新訊號就補充/修正。等於把現有的 `write_deep_dive` 從「一篇文章」升級成「一個結構 + 一篇文章」。
  - **關聯度**：純算 —— 個股報酬對「題材成分股等權籃子」報酬的 20/60 日相關係數。免費。
  - **龍頭**：啟發式 —— 該分段內市值最大 + 關聯度最高 + 在題材訊號中最早/最常出現。或直接讓 LLM 判。
  - **公司標籤**（有股票期貨 / 營收新高 / 外資買超）：全部可算：
    - 有股票期貨：TAIFEX 個股期貨標的清單，季更新，🔍待驗（`https://www.taifex.com.tw/cht/2/stockLists`）
    - 營收新高：月營收資料集比對過去 12 個月最大值，✅實測（`openapi/v1/opendata/t187ap05_L` 有 `產業別` `營業收入-當月營收`）
    - 外資買超：T86，✅實測（RWD `/fund/T86`）
- 免費：資料本身要 LLM 建一次（每題材約 1 次呼叫）。難度：**高**（結構設計 + prompt + 修正邏輯）。
- 現況：`themes` / `theme_updates` 已有基礎，缺 `supply_chains` 表與 render。

### M2. 個別產業國際分析與總經

App 畫面：同一題材「其他市場同主題」（日本 Fabless IC / 美國 客製 ASIC / 韓國晶圓代工）；跨台美日韓上下游。

- **美股**：yfinance 全覆蓋，✅實測。
- **日股 / 韓股**：yfinance 用後綴（東京 `.T`、韓國 `.KS`/`.KQ`），大型股覆蓋堪用，🔍待驗覆蓋率。
- **總經數據**（CPI / PMI / 就業 / Fed funds / 殖利率曲線）：**FRED API**（免費，需免費 key），🔍待驗。殖利率、DXY、原油、黃金、VIX 已用 yfinance 做掉。
- **跨市場同題材對應**：LLM curated，存進 `supply_chains` 的 `peers_by_market` 欄位。
- 免費（FRED 要註冊拿 key，但不收錢）。要 LLM（低頻，週級）。難度：**中**。
- 現況：`src/analysis/global_themes.py` 已有國際題材雛形（Google News RSS + 類股 ETF），往這裡長。

### M3. 產業熱力圖（treemap，單日/單週/單月）

App 畫面：子產業方格圖，紅綠代表漲跌幅，大小代表權重。

- 需要**每檔股票的產業分類**：月營收資料集 `t187ap05_L` 每列有 `產業別`（實測看到「水泥工業」），月更新即可。TPEx 另有一份。✅實測（分類欄位存在）。
- 有了分類就把當日/週/月報酬**依產業彙總**（市值加權或等權），`src/viz.py` 加一個 treemap 產生器（純 SVG）。
- App 有更細的子產業（碳化矽基板、氣冷散熱）——那其實是「題材」層級，用知識庫的題材當第二種分群模式。
- 免費、**不用 LLM**。難度：**中**（treemap 版面 + 分類維護）。
- 現況：無。`viz.py` 目前只有 sparkline / 左右橫條。

### M4. AI 五面向評分（題材 / 基本 / 技術 / 籌碼 / 新聞，0–100 + 排行）

- **技術面**：`technical.py` 的評級轉 0–100。免費、無 LLM。**現成**。
- **籌碼面**：法人買賣超（T86）+ 資券變化 + 大戶持股集中度，加權混一個分數。免費、無 LLM。
- **基本面**：月營收 YoY/MoM + 是否創新高 +（季報 EPS/毛利，TWSE `t163sb*` 系列，🔍待驗，季更新）。免費、無 LLM。
- **題材面**：這檔在不在 active 題材裡、題材信心度、核心 or 邊緣（用關聯度）。免費、無 LLM。
- **新聞面**：對「該個股」的 Google News RSS 標題做情緒分類。**唯一需要 LLM 的一項**，而且可以：只對前 N 名跑、每天一次、批次一次呼叫多檔、結果快取。
- 綜合分 = 五項加權（權重放 `config.yaml` 讓使用者調）。
- 關鍵：**4/5 是免費規則分**，新聞面可關。全市場評分成本可控。
- 難度：**中**（各軸的分數函數要調校）。
- 現況：技術面現成，其餘要接 M7 的籌碼資料 + 基本面資料。

### M5. 處置股提前預警（還差幾次 / 觸價條件 / 出關日）

App 畫面：處置中 25 / 已達標 3 / 高風險 8 / 接近 7 / 觀察 29；每檔顯示「再 N 次」「明天不跌破 X 就加重處置」「第 2 次處置措施」「出關日」。

- TWSE / TPEx 每日公告**注意股票**與**處置股票**：
  - 注意：`https://www.twse.com.tw/rwd/zh/announcement/notice`（🔍待驗）
  - 處置：`https://www.twse.com.tw/rwd/zh/announcement/punish`（🔍待驗）
- 「還差幾次」要**重寫一份注意標準判斷**：TWSE《公布或通知注意交易資訊暨處置作業要點》有 9 條量化條件（連續/累積 N 個營業日的漲跌幅、週轉率、本益比、成交量、集中度…），規則公開。用已落地的 `stock_daily` 就能逐日回推每檔「近 6 / 30 個營業日內已觸發幾次、還差幾次」。
- 100% 可算、**不用 LLM**、免費。難度：**中高**（規則引擎 ~200 行，要對照官方要點逐條實作 + 驗證）。
- 現況：無。價值高（避免踩進處置股被鎖流動性）。

### M6. 主動式 ETF 每日追蹤（新增 / 加碼 / 減碼 / 移出）

App 畫面：每檔主動式 ETF（00xxxA）當日持股變動家數。

- 主動式 ETF 每日公告持股（PCF / 申購買回清單）。TWSE 有 `ETF/etfPcf` 類端點，各投信官網也有。🔍待驗。
- 存每日持股快照，與前一日 diff → 新增/移出；權重升降 → 加碼/減碼。
- 免費、**不用 LLM**。難度：**中**（要確認每檔 ETF 的 PCF 來源格式）。
- 現況：無。需要 `etf_holdings` 表。

### M7. 籌碼動向儀表板（法人 / 資券 / 強勢股 / 大戶股）

- **法人**：T86（個股）+ BFI82U（大盤），✅實測。**現成**。
- **資券**：TWSE `rwd/zh/marginTrading/MI_MARGN`（大盤融資融券餘額）、`TWT93U`（個股），🔍待驗。免費。
- **大戶股**：集保結算所股權分散表（週更新），`https://www.tdcc.com.tw/smWeb/QryStockAjax.do`，🔍待驗。給「千張大戶持股比例趨勢」。
- **強勢股**：`screener.py` 已有。**現成**。
- 免費、**不用 LLM**。難度：**中**。
- 現況：法人 + 強勢股現成，缺資券 / 大戶的 fetcher + 儲存 + 儀表板頁。

---

## 三、資料庫設計

現有 5 張表（`themes` / `theme_updates` / `judgment_snapshots` / `review_results` / `market_snapshots`）全部保留。新增：

| 表 | 用途 | 更新頻率 | 來源 |
|---|---|---|---|
| `stock_daily` | 個股每日 OHLCV + 漲跌 + 成交值（**取代重複抓歷史**） | 每交易日 | STOCK_DAY_ALL + TPEx |
| `industry_map` | 股票代號 → 產業別 / 子產業 | 每月 | `t187ap05_L` |
| `revenue_monthly` | 月營收（當月 / 去年同月 / YoY / MoM / 累計） | 每月 10–12 日 | `t187ap05_L` |
| `institutional_daily` | 個股三大法人買賣超（股數） | 每交易日 | `T86` |
| `margin_daily` | 個股 / 大盤融資融券餘額與增減 | 每交易日 | `MI_MARGN` / `TWT93U` |
| `holder_distribution` | 集保股權分散（各級距張數、千張大戶%） | 每週 | 集保 |
| `disposition_status` | 每日注意/處置狀態 + 計算後的「還差幾次 / 出關日」 | 每交易日 | notice / punish + 規則引擎 |
| `etf_holdings` | 主動式 ETF 每日持股快照 | 每交易日 | ETF PCF |
| `supply_chains` | 題材 → 結構化供應鏈 JSON（上中下游段、各段公司、龍頭、跨市場對應） | LLM 產出 / 修正 | LLM |
| `stock_scores` | 個股五面向分數 + 綜合分 | 每交易日 | 各模組彙整 |
| `macro_series` | 國際總經時間序列（CPI/PMI/利率…） | 依序列 | FRED |

**儲存策略**：
- `market.db` 只放「判斷 / 知識 / 評分」這種筆數少、git diff 有意義的表。
- **另開 `data/prices.db`** 放 `stock_daily` / `institutional_daily` / `margin_daily` / `etf_holdings` 這種大量時間序列。
- `prices.db` 成長很快（~1900 檔 × 250 日/年 ≈ 47 萬列/年，SQLite 撐得住幾百萬列沒問題），但**不要 git commit**——改用 GitHub Actions 的 cache 或 Release asset 保存，避免 repo 膨脹。
- 真的大到 SQLite 吃力再換 Postgres，schema 不用重寫。

---

## 四、分階段路線圖（依「價值 / 成本 / 相依」排序）

### Phase 1 — 資料地基（全免費、零 LLM、先做）
- `src/db.py` 拆出 `prices.db`，建 `stock_daily` / `institutional_daily` / `margin_daily`
- 盤後流程改成「先落地當日全市場行情 + 法人 + 資券」，技術分析與掃描改查 DB
- `industry_map` + `revenue_monthly` 從月營收資料集建立
- **上櫃（TPEx）納入**：`src/fetchers/tpex.py`，介面比照 `twse.py`
- 新頁：**產業熱力圖**（`viz.py` 加 treemap）
- 新頁：**籌碼儀表板**（法人 / 資券 / 強勢股，大戶股留 Phase 2）

### Phase 2 — 規則型分析（免費、零 LLM）
- **處置股預警**：`src/analysis/disposition.py` 規則引擎 + `disposition_status` 表 + 專頁
- **五面向評分**的四個免費軸（技術 / 籌碼 / 基本 / 題材）+ `stock_scores` 表 + 排行頁
- **主動式 ETF 追蹤**（先驗 PCF 來源）+ `etf_holdings` 表 + 專頁
- **大戶持股**（集保）併進籌碼儀表板

### Phase 3 — LLM 輔助（小額成本）
- **產業鏈結構**：`supply_chains` 表 + prompt + 修正邏輯；深度報告改吃這個結構
- **新聞面評分**：個股新聞情緒（前 N 名、每日批次、快取）→ 補上五面向的第五軸
- **跨市場同題材對應**（日 / 韓 / 美 analogues）寫進 `supply_chains`

### Phase 4 — 國際總經深化
- `src/fetchers/fred.py`（免費 key）→ `macro_series`
- 個別產業的國際傳導分析（該產業的美 / 日 / 韓 對照與展望，週級 LLM）
- 總經儀表板頁

### 隨時可做（不阻塞）
- 現有站台加 `manifest.json` + service worker → 變成可安裝的 PWA（「未來的 App」最省力路線）
- render 階段同步吐 `docs/data/*.json`（每個模組一份），未來原生 App 直接讀

---

## 五、成本影響（Anthropic API；跑在 GitHub Actions 時才有）

| 階段 | 每月增量 | 說明 |
|---|---|---|
| Phase 1–2 | **+US$0** | 全部規則型，只是多用 Actions 分鐘數（仍在免費額度）+ DB 變大 |
| Phase 3 | **+US$2–5** | 供應鏈結構每題材建一次（~$0.05）；新聞面前 ~50 檔批次每日 1–2 次呼叫 |
| Phase 4 | **+US$1** | FRED 免費；國際傳導分析週級低頻 |
| 全部做完（部署版） | 約 **US$10–15/月** | |
| 全部做完（LLM 部分改成你在 Claude 裡手動跑） | 規則型自動化 **$0**，LLM 部分走 Pro | |

---

## 六、web → App 的架構建議

現在：靜態 HTML → GitHub Pages。維持這個，但：

1. **render 同時輸出 JSON**：`docs/data/heatmap.json`、`docs/data/scores.json`、`docs/data/disposition.json`…
   HTML 頁面和未來的 App 讀同一份資料。
2. **PWA 化**：加 `docs/manifest.json` + 一支簡單 service worker，手機「加入主畫面」就有 App 體感，離線可看最近一份。
3. 之後若要做原生 App：後端就是這些 JSON 檔（放在 Pages / 或 GitHub Release），App 純讀取，仍然零伺服器成本。
4. 若之後真的需要互動查詢（任意個股即時查）→ 那一步才需要一個小 API 服務（Cloudflare Workers 免費額度夠），或回到「在 Claude 裡問」。

---

## 七、待驗證清單（下一個 session 動手前先打這些端點）

- [ ] TPEx open API / RWD：上櫃個股當日行情、歷史、法人、資券
- [ ] TWSE `announcement/notice`、`announcement/punish`（注意 / 處置清單）
- [ ] TWSE `marginTrading/MI_MARGN`、`TWT93U`（資券）
- [ ] 集保 `smWeb/QryStockAjax.do`（股權分散）
- [ ] 主動式 ETF PCF / 每日持股來源（TWSE 或各投信）
- [ ] TAIFEX 個股期貨標的清單
- [ ] TWSE `t163sb*`（季報財務數據）
- [ ] FRED API（註冊免費 key，測 CPI / PMI / DGS10 等序列）
- [ ] yfinance 對日股（`.T`）韓股（`.KS`/`.KQ`）的覆蓋率抽樣

驗證方式比照這次：寫 `scratchpad/probe*.py`，看實際回傳欄位再定 schema。
