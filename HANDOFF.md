# 專案交接文件：盤後快訊自動分析系統

> 這份文件是給 Claude Code 的完整背景說明。
> 使用方式：把 `stock-report` 資料夾用 Claude Code 開啟，然後把這份文件貼給它。

---

## 一、專案是什麼

一套每日自動產出台股／國際盤分析報告的系統。跑在 GitHub Actions 上，
產出靜態網站發佈到 GitHub Pages，並推播摘要到 Telegram。全部在免費額度內。

**使用者背景**：台灣的學生，有程式基礎，平常研究股票。這是個人研究工具，不是商用產品。

**核心價值主張**：不只是把數字播報出來，而是做出「分析師等級的判讀」——
判斷題材是真是假、公司是核心受惠者還是蹭題材、技術面位置健不健康。

---

## 二、目前完成度

程式碼已經寫完並在容器環境用假資料實測跑通。以下功能已驗證可運作：

| 功能 | 狀態 | 驗證方式 |
|---|---|---|
| 早報（國際盤） | ✅ 完成 | `DRY_RUN=1 python -m src.main morning` |
| 盤後報告（台股完整分析） | ✅ 完成 | `DRY_RUN=1 python -m src.main evening` |
| 題材知識庫累積更新 | ✅ 完成 | 模擬 4 天，同題材更新 4 次而非新建 4 筆 |
| 深度報告自動觸發 | ✅ 完成 | 第 3 天達門檻產出，第 4 天不重複 |
| 黑馬分流（不硬套題材） | ✅ 完成 | 孤立訊號走風險標記路徑 |
| 技術分析（均線/KD/RSI/MACD） | ✅ 完成 | 實際算出多頭健康／過熱警訊分級 |
| 題材退場機制 | ✅ 完成 | 超過 7 天無訊號自動轉休眠 |
| 事後驗證（超額報酬回測） | ✅ 完成 | 高信心度組 +11.43%、中信心度組 -1.27% |
| 自選股命中標記 | ✅ 完成 | 被掃到時置頂顯示 |
| 假日功課 | ✅ 完成 | `DRY_RUN=1 python -m src.main holiday` |
| 靜態網站產出 | ✅ 完成 | docs/ 下產出 index／archive／themes／reports／articles |
| GitHub Actions 排程 | ✅ 寫好未實測 | 需推上 GitHub 才能驗證 |
| Telegram 推播 | ✅ 寫好未實測 | 需真實 token 才能驗證 |

**未實作**：國際題材追蹤模組（ETF 資金流、投行評等調升降統計）—— 設計過但還沒寫。

---

## 三、系統架構

### 資料流

```
排程觸發（07:00 早報 / 18:00 盤後）
    ↓
開休市判斷（查 TWSE 行事曆，不用星期幾猜）
    ↓
資料擷取層（TWSE 台股 / Stooq 國際盤 / MOPS 法說會）
    ↓
第一層：強勢股掃描（漲幅 + 量能異常，不預設產業分類）
    ↓
第二層：LLM 題材聚類（Claude API）
    ├─ 有同族群呼應 → 題材分析流程
    └─ 孤立訊號 → 黑馬獨立調查（給風險標記，不給信心度）
    ↓
第三層：寫入題材知識庫（有就更新、沒有才新建）
    ↓
第四層：技術分析（只跑入選個股，省算力）
    ↓
判斷快照存檔（14/30 天後回頭驗證）
    ↓
渲染 HTML → docs/ → GitHub Pages
    ↓
Telegram 推播摘要 + 網站連結
```

### 檔案結構

```
config.yaml              使用者唯一需要改的檔案（自選股、門檻、參數）
setup.sh                 一鍵本機設定腳本
DEPLOY.md                部署教學（7 步驟）
README.md                專案說明

src/
  config.py              設定載入 + 時區（一律台北時間，避免雲端 UTC 算錯日期）
  market_calendar.py     開休市判斷、連假偵測、下一交易日
  db.py                  SQLite：themes / theme_updates / judgment_snapshots
                         / review_results / market_snapshots
  llm.py                 Claude API 封裝：題材聚類、每日評論、深度報告
  render.py              Jinja2 渲染 → docs/
  notify.py              Telegram 推播
  main.py                入口與分支（morning/evening/monthly/holiday/auto）
  fetchers/
    twse.py              台股（TWSE 開放 API，免申請）
    international.py     國際盤（Stooq，免 API key）
    mops.py              法說會行事曆（公開資訊觀測站）
    mock.py              DRY_RUN 假資料
  analysis/
    screener.py          強勢股掃描、黑馬判定、自選股比對
    technical.py         技術指標計算與評級
    review.py            事後驗證（超額報酬）

templates/               base / daily / article / index / archive / themes
docs/                    產出的網站（GitHub Pages 根目錄設這裡）
data/market.db           SQLite（跟著 git 走，等於自動備份）
.github/workflows/report.yml   排程設定
```

---

## 四、關鍵設計決策（改動前請先理解）

**盤後報告是 18:00 不是 13:30**
13:30 只是停止交易，三大法人、融資融券、期貨籌碼的官方統計都還沒落地。
18:00 留足緩衝確保資料完整。不要為了「即時」把它改早。

**月報是每月 12 日**
月營收依規定須在次月 10 日前公告（遇假日順延），12 日留兩天緩衝。
農曆年前後那個月可能順延超過兩天，需手動確認。

**GitHub Actions 的 cron 一律 UTC**
台北 07:00 = UTC 前一天 23:00；台北 18:00 = UTC 當日 10:00。
改排程時間時務必換算，這是最容易出錯的地方。

**黑馬不給信心度，只給風險標記**
黑馬的本質是資訊不對稱。套用跟題材股同一套信心度語言會誤導使用者。
所以改成量能倍數、公告佐證、法人動向三個客觀欄位 + 「觀察而非追蹤」的建議。

**台股紅漲綠跌**
CSS 變數 `--red` 用於上漲、`--green` 用於下跌。
不要套用美股的紅跌綠漲配色，台灣使用者會看反方向。

**判斷快照必須先存才能驗證**
系統每次說「值得留意」都先存進 `judgment_snapshots`，
14/30 天後用超額報酬（個股報酬 − 大盤報酬）回頭驗證。
沒有這個回饋迴路，系統只是播報器。

**資料源容錯不可移除**
每個 fetcher 都包 try/except，失敗回空值而非拋例外。
單一資料源掛掉不該讓整份報告開天窗，該區塊直接不顯示即可。

---

## 五、使用者接下來要做的事

### A. 本機驗證（10 分鐘）

```bash
cd stock-report
bash setup.sh                              # 環境檢查、裝套件、跑測試、git init
open docs/index.html                       # 看產出的網站（Windows 用 start）
```

確認版面符合預期。想調整就改 `config.yaml` 或 `templates/`。

### B. 申請憑證（15 分鐘，必須本人操作）

1. **Anthropic API key**：https://console.anthropic.com → API Keys → Create Key
   （API 是預付制，需先儲值。本專案一天 2-4 次呼叫，用量很小）
2. **Telegram Bot**：Telegram 搜尋 @BotFather → `/newbot` → 拿 token
   → 對 bot 傳一句話 → 開 `https://api.telegram.org/bot<TOKEN>/getUpdates` 拿 chat id
3. 兩者填進 `.env`

### C. 部署（15 分鐘）

照 `DEPLOY.md` 走，重點三步：
1. 推上 GitHub
2. Settings → Secrets and variables → Actions 新增三個 secret
3. Settings → Pages → Branch `main` / 資料夾 `/docs`
4. Settings → Actions → General → Workflow permissions 改成 **Read and write**
   （沒開這個，Actions 無法把報告 commit 回 repo，網站不會更新）

### D. 上線後（第一週）

先觀察，依實際結果調 `config.yaml`：
- 雜訊太多 → 提高 `screener.min_change_pct` 或 `min_volume_ratio`
- 題材太少 → 降低門檻
- API 花費想再省 → 提高 `theme_lifecycle.deep_dive_min_days`（深度報告最耗 token）

---

## 六、待辦事項（依建議順序）

### 優先度高

1. **實測真實 API**
   目前只用 mock 驗證過。接上真實 TWSE API 後，欄位名稱可能與預期不符，
   需要對照實際回傳調整 `src/fetchers/twse.py` 的欄位對應。
   TWSE 的欄位名歷年會變動，程式已做中英文雙重備援，但仍需實測。

2. **確認 Stooq 國際盤資料可用性**
   `src/fetchers/international.py` 用 Stooq 免費 CSV。
   若遇到擋爬蟲或資料延遲，替代方案是改用 `yfinance`（`pip install yfinance`），
   介面沿用現有的 dict 格式即可，不用改上層邏輯。

3. **法說會資料源驗證**
   `src/fetchers/mops.py` 的 API endpoint 需實測。
   若 TWSE 該資料集格式與預期不符，可改抓財報狗或 CMoney 的法說會摘要。

### 優先度中

4. **國際題材追蹤模組（尚未實作）**
   設計如下，需新增 `src/analysis/global_themes.py`：
   - 抓 Bloomberg/Reuters/WSJ 財經頭條（Google News RSS 免費）
   - 用 LLM 統計高頻關鍵字趨勢，判斷本週當紅題材
   - 資金流佐證：美股類股 ETF（XLK 科技、SMH 半導體）淨流入/流出
   - 機構態度：投行評等調升/調降家數，一致性越高信心度越高
   - 產出與台股題材相同格式，寫進同一個題材知識庫（`scope='intl'`）

5. **週報**
   目前尚未實作。設計上**不要獨立開排程**，應併入週一早報，
   因為台股週五 13:30 收盤但美股週五晚上才交易，週五發的週報會漏掉整個美股交易日。

6. **月報頁面**
   `run_monthly()` 目前只跑驗證邏輯並推播文字，沒有產出 HTML 頁面。
   需新增 `templates/monthly.html`，呈現月度題材增減、信心度分組成績單。

### 優先度低

7. **農曆年長假彙整報告**
   `is_last_day_before_reopen()` 已寫好判斷邏輯，但目前只是呼叫 `run_holiday()`。
   可以做一個專門的版型，把休市期間國際盤逐日變化彙整，避免開紅盤被跳空嚇到。

8. **視覺化**
   題材信心度變化折線圖、法人買賣超趨勢圖。
   建議用純 SVG 或輕量 JS，不要引入重型圖表庫（GitHub Pages 是靜態站）。

---

## 七、給 Claude Code 的指引

**先做的事**
1. 讀 `README.md` 和 `config.yaml` 了解全貌
2. 跑 `bash setup.sh` 確認環境正常
3. 跑 `DRY_RUN=1 python -m src.main evening` 看流程是否通

**改動時的原則**
- 所有新增的資料擷取都要包 try/except 並回傳空值，不可拋例外中斷流程
- 新增 LLM 呼叫時，務必同步在 `src/fetchers/mock.py` 加對應假資料，
  保持 `DRY_RUN=1` 能完整跑通（這是無網路無 key 時的唯一驗證手段）
- 時間相關的邏輯一律用 `src/config.py` 的 `now_tpe()` / `today_str()`，
  不要直接用 `datetime.now()`（雲端跑在 UTC 會算錯日期）
- 改 CSS 時遵守台股紅漲綠跌，變數定義在 `templates/base.html`

**測試方式**
```bash
DRY_RUN=1 python -m src.main evening    # 完整盤後流程
DRY_RUN=1 python -m src.main morning    # 早報
DRY_RUN=1 python -m src.main holiday    # 假日功課
DRY_RUN=1 python -m src.main site       # 只重建索引頁
```

模擬多日累積（測題材知識庫、深度報告觸發）：
覆寫 `src.main.today_str` 為固定日期後連續呼叫 `run_evening()`。

**不要做的事**
- 不要把 API key 寫進任何檔案，一律走環境變數
- 不要移除資料源的容錯處理
- 不要為了「即時」把盤後報告時間改早於 18:00
- 不要在沒有 mock 資料的情況下新增外部依賴

---

## 八、成本估算

| 項目 | 費用 |
|---|---|
| GitHub Actions | 免費（Public repo 無限；Private 每月 2000 分鐘，本專案用量遠低於此） |
| GitHub Pages | 免費 |
| TWSE / Stooq / MOPS 資料 | 免費，免申請 |
| Anthropic API | 預付制，每天 2-4 次呼叫，用量小 |
| Telegram Bot | 免費，無則數上限 |

唯一的持續成本是 Anthropic API。想壓低就調高 `deep_dive_min_days`。

---

## 九、免責

本系統為個人研究工具，產出內容僅供參考，不構成投資建議。
所有報告頁面底部已內建免責聲明，請勿移除。
