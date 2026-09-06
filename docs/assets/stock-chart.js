/* 個股股價 K 線 + 技術指標，純前端即時抓 TWSE 公開資料計算，不用後端、不用 LLM。
 * 用 TradingView 的 lightweight-charts（v5，MIT）畫圖：日/週/月切換、可拖曳縮放、
 * 均線交叉與價量背離用 markers 標在圖上。
 * TWSE STOCK_DAY 這支端點對瀏覽器端 fetch 開放 CORS，上櫃（TPEx）目前測過不支援，
 * 遇到上櫃股會誠實顯示「此資料源不支援上櫃股即時圖表」，不會裝作有資料。
 */
(function () {
  "use strict";

  var cache = {};
  var MONTHS_BACK = 36;
  var LS_PREFIX = "sc_hist_v1_";

  // ── 個股資料（docs/data/stock_info/<code>.json，每檔一個小檔案）──
  // 內容刻意分三層並在畫面上標示來源，避免把「判讀」講得像「事實」：
  //   profile → 公開資訊觀測站申報值（公司到底在做什麼，事實）
  //   rev     → 政府開放資料的月營收（事實）
  //   themes / desc / swot → 本站題材歸類與 LLM 判讀（明確標為判讀）
  var infoCache = {};

  function assetBase() {
    var s = document.querySelector('script[src*="stock-chart.js"]');
    var src = s ? (s.getAttribute("src") || "") : "";
    var m = src.match(/^(.*?)assets\/stock-chart\.js/);
    return m ? m[1] : "";
  }

  function loadInfo(code) {
    if (!infoCache[code]) {
      infoCache[code] = fetch(assetBase() + "data/stock_info/" + code + ".json")
        .then(function (r) { return r.ok ? r.json() : null; })
        .catch(function () { return null; });
    }
    return infoCache[code];
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function fmtMoney(thousands) {
    if (thousands == null) return "-";
    var yi = thousands / 100000;           // 千元 → 億元
    if (Math.abs(yi) >= 1) return yi.toFixed(2) + " 億";
    return (thousands / 10000).toFixed(2) + " 千萬";
  }

  function pct(v) {
    if (v == null) return "-";
    return (v >= 0 ? "+" : "") + v.toFixed(2) + "%";
  }

  function profileHtml(info) {
    var p = info.profile || {};
    if (!p.business && !p.industry) return "";
    function row(label, val) {
      return val ? '<div class="sc-kv"><span>' + label + '</span><b>' + esc(val) + "</b></div>" : "";
    }
    var site = p.website
      ? '<div class="sc-kv"><span>公司網站</span><b><a href="' + esc(p.website)
        + '" target="_blank" rel="noopener">' + esc(p.website) + "</a></b></div>" : "";
    return '<div class="sc-sec">'
      + '<div class="sc-sec-h">公司基本資料 <span class="sc-src">公開資訊觀測站申報值</span></div>'
      + (p.business ? '<p class="sc-biz"><b>主要經營業務：</b>' + esc(p.business) + "</p>" : "")
      + '<div class="sc-kv-grid">'
      + row("公司全名", p.full_name) + row("產業類別", p.industry)
      + row("實收資本額", p.capital) + row("成立日期", p.founded)
      + row("掛牌日期", p.listed) + site
      + "</div></div>";
  }

  function revenueHtml(info) {
    var r = info.rev || {};
    if (!r.period || r.revenue == null) {
      // 每一檔都要有「基本面」區塊：沒有月營收申報資料時也明講，
      // 不要整段消失讓人以為漏了。金控／壽險／存託憑證是合併申報或
      // 境外發行、沒有單獨月營收，剛登錄的興櫃則是還沒到第一次申報。
      var mkt = (info.profile || {}).market;
      var why = mkt === "" || mkt == null
        ? "本站尚未取得此檔的月營收申報資料。"
        : "此類公司（金控／保險／存託憑證，或剛登錄興櫃者）沒有單獨的每月營收申報，"
          + "基本面請參閱其財報。";
      return '<div class="sc-sec">'
        + '<div class="sc-sec-h">基本面 · 月營收 <span class="sc-src">公開資訊觀測站申報值</span></div>'
        + '<p class="sc-biz">' + why + "</p></div>";
    }
    function cell(label, val, cls) {
      return '<div class="sc-fund-cell"><h5>' + label + '</h5><p class="' + (cls || "")
        + '">' + val + "</p></div>";
    }
    var yoyCls = r.yoy == null ? "" : (r.yoy >= 0 ? "up" : "down");
    var momCls = r.mom == null ? "" : (r.mom >= 0 ? "up" : "down");
    var cumCls = r.cum_yoy == null ? "" : (r.cum_yoy >= 0 ? "up" : "down");
    return '<div class="sc-sec">'
      + '<div class="sc-sec-h">基本面 · ' + esc(r.period) + ' 月營收 '
      + '<span class="sc-src">公開資訊觀測站申報值</span></div>'
      + '<div class="sc-fund-grid">'
      + cell("當月營收", fmtMoney(r.revenue))
      + cell("年增 YoY", pct(r.yoy), yoyCls)
      + cell("月增 MoM", pct(r.mom), momCls)
      + cell("今年累計", fmtMoney(r.cum_revenue))
      + cell("累計年增", pct(r.cum_yoy), cumCls)
      + "</div></div>";
  }

  function themesHtml(info) {
    var t = info.themes || [];
    // 每一檔都要有「題材」區塊：沒被本站題材知識庫歸類時也明講，
    // 不要硬把個股塞進題材（那正是先前資料出錯的原因）。
    if (!t.length) {
      return '<div class="sc-sec">'
        + '<div class="sc-sec-h">相關題材 <span class="sc-src">本站題材知識庫歸類</span></div>'
        + '<p class="sc-biz">本站題材知識庫尚未將此檔歸入任何題材。</p></div>';
    }
    return '<div class="sc-sec">'
      + '<div class="sc-sec-h">相關題材 <span class="sc-src">本站題材知識庫歸類</span></div>'
      + '<div class="sc-theme-tags">'
      + t.map(function (n) { return '<span class="sc-theme-tag">' + esc(n) + "</span>"; }).join("")
      + "</div></div>";
  }

  // 「同產業個股」：申報產業別分組，純事實。全市場只有一小部分個股被題材
  // 知識庫歸過類，沒歸類的個股在「相關題材」那區只會看到一句「尚未歸入」，
  // 這一區至少讓每一檔都有真的、可點的關聯個股，而且不需要任何推論。
  function peersHtml(info) {
    var p = info.industry_peers || {};
    if (!p.industry || !p.peers || !p.peers.length) return "";
    var reportLink = p.report_slug
      ? '<p class="sc-biz"><a class="sc-ind-link" href="' + assetBase() + 'industry/'
        + encodeURIComponent(p.report_slug) + '.html">📊 看「' + esc(p.industry)
        + '」產業深度分析 →</a></p>'
      : "";
    return '<div class="sc-sec">'
      + '<div class="sc-sec-h">同產業個股 · ' + esc(p.industry)
      + ' <span class="sc-src">公開資訊觀測站申報產業別</span></div>'
      + reportLink
      + '<p class="sc-biz">同一申報產業別共 ' + p.total + ' 檔，依最新月營收由大到小列出前 '
      + p.peers.length + ' 檔（點任一檔看該股 K 線與公司資料）：</p>'
      + '<div class="sc-theme-tags">'
      + p.peers.map(function (x) {
          return '<span class="sc-theme-tag sc-peer" data-stock-code="' + esc(x.code)
               + '" data-stock-name="' + esc(x.name) + '">' + esc(x.code) + ' ' + esc(x.name) + "</span>";
        }).join("")
      + "</div></div>";
  }

  function swotHtml(info) {
    if (!info.desc) return "";
    var sw = info.swot || {};
    function cell(title, val) {
      if (!val) return "";
      return '<div class="sc-swot-cell"><h5>' + title + '</h5><p>' + esc(val) + "</p></div>";
    }
    var grid = cell("優勢 S", sw.strengths) + cell("劣勢 W", sw.weaknesses)
             + cell("機會 O", sw.opportunities) + cell("威脅 T", sw.threats);
    var srcs = info.sources || [];
    var srcLine = srcs.length
      ? '<p class="sc-swot-updated">查證來源：'
        + srcs.map(function (s) {
            return /^https?:\/\//.test(s)
              ? '<a href="' + esc(s) + '" target="_blank" rel="noopener">' + esc(s) + "</a>"
              : esc(s);
          }).join("、")
        + "</p>"
      : "";
    var srcTag = srcs.length ? "已逐檔查證，SWOT 仍含推論" : "系統判讀，非官方說法";
    return '<div class="sc-sec sc-swot">'
      + '<div class="sc-sec-h">公司介紹與 SWOT <span class="sc-src sc-src-warn">' + srcTag + "</span></div>"
      + '<p class="sc-swot-desc">' + esc(info.desc) + "</p>"
      + (grid ? '<div class="sc-swot-grid">' + grid + "</div>" : "")
      + srcLine
      + (info.updated ? '<p class="sc-swot-updated">更新：' + esc(info.updated)
          + "　·　僅供研究參考，不構成投資建議</p>" : "")
      + "</div>";
  }

  function appendSwot(bodyEl, code) {
    loadInfo(code).then(function (info) {
      if (!info || !bodyEl || bodyEl.isConnected === false) return;
      var html = profileHtml(info) + revenueHtml(info) + themesHtml(info)
               + peersHtml(info) + swotHtml(info);
      if (html) bodyEl.insertAdjacentHTML("beforeend", html);
    });
  }

  function todayIso() {
    // 用瀏覽器本機日期，使用者主要都在台灣時區，不特別處理時區轉換。
    var d = new Date();
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0")
         + "-" + String(d.getDate()).padStart(2, "0");
  }

  function loadPersisted(code) {
    try {
      var raw = localStorage.getItem(LS_PREFIX + code);
      if (!raw) return null;
      var obj = JSON.parse(raw);
      return (obj && Array.isArray(obj.bars) && obj.bars.length) ? obj : null;
    } catch (e) { return null; }
  }

  function savePersisted(code, bars) {
    try {
      localStorage.setItem(LS_PREFIX + code, JSON.stringify({ bars: bars }));
    } catch (e) { /* 存不下去（例如私密模式擋掉 localStorage）就算了，不影響圖表本身顯示 */ }
  }

  function mergeBars(oldBars, newBars) {
    var byDate = {};
    oldBars.forEach(function (r) { byDate[r.date] = r; });
    newBars.forEach(function (r) { byDate[r.date] = r; }); // 同一天有新資料就覆蓋（收盤資料事後修正）
    var merged = Object.keys(byDate).map(function (d) { return byDate[d]; });
    merged.sort(function (a, b) { return a.date < b.date ? -1 : a.date > b.date ? 1 : 0; });
    return merged;
  }

  function parseNum(s) {
    if (s === undefined || s === null || s === "") return null;
    var n = parseFloat(String(s).replace(/,/g, ""));
    return isNaN(n) ? null : n;
  }

  function rocToIso(roc) {
    var parts = roc.split("/");
    return (parseInt(parts[0], 10) + 1911) + "-" + parts[1] + "-" + parts[2];
  }

  function fetchMonth(code, year, month) {
    var dateStr = "" + year + String(month).padStart(2, "0") + "01";
    var url = "https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date="
             + dateStr + "&stockNo=" + encodeURIComponent(code);
    return fetch(url).then(function (r) { return r.json(); }).then(function (j) {
      if (!j || j.stat !== "OK" || !j.data) return [];
      return j.data.map(function (row) {
        return {
          date: rocToIso(row[0]),
          volume: parseNum(row[1]),
          open: parseNum(row[3]), high: parseNum(row[4]),
          low: parseNum(row[5]), close: parseNum(row[6]),
        };
      }).filter(function (r) { return r.close !== null; });
    }).catch(function () { return []; });
  }

  function fetchHistory(code) {
    if (cache[code]) return Promise.resolve(cache[code]);

    var now = new Date();
    var persisted = loadPersisted(code);

    if (persisted) {
      var lastDate = persisted.bars[persisted.bars.length - 1].date;
      if (lastDate >= todayIso()) {
        // 今天已經補過了（或還沒開盤/假日沒有新資料），直接用本機存的，不用打網路。
        cache[code] = persisted.bars;
        return Promise.resolve(persisted.bars);
      }
      // 快取存在但不是今天的——只補抓「這個月」＋「上個月」（涵蓋跨月分界），
      // 不用重新抓 MONTHS_BACK 個月；平常收盤後只要 2 支 API 就能補到最新一天。
      // 遇到假日/非交易日，TWSE 本來就不會回傳那天的資料，快取自然不會多長一天，
      // 不用另外寫台股假日表去判斷。
      var prevY = now.getFullYear(), prevM = now.getMonth(); // getMonth() 是 0-based，正好是上個月數字
      if (prevM === 0) { prevM = 12; prevY -= 1; }
      return Promise.all([
        fetchMonth(code, now.getFullYear(), now.getMonth() + 1),
        fetchMonth(code, prevY, prevM),
      ]).then(function (results) {
        var merged = mergeBars(persisted.bars, [].concat.apply([], results));
        cache[code] = merged;
        savePersisted(code, merged);
        return merged;
      });
    }

    // 完全沒有本機快取（第一次查這檔股票）：抓齊 MONTHS_BACK 個月。
    var calls = [];
    for (var i = 0; i < MONTHS_BACK; i++) {
      var y = now.getFullYear(), m = now.getMonth() + 1 - i;
      while (m <= 0) { m += 12; y -= 1; }
      calls.push(fetchMonth(code, y, m));
    }
    return Promise.all(calls).then(function (results) {
      var all = [].concat.apply([], results);
      var seen = {};
      all = all.filter(function (r) {
        if (seen[r.date]) return false;
        seen[r.date] = true;
        return true;
      });
      all.sort(function (a, b) { return a.date < b.date ? -1 : a.date > b.date ? 1 : 0; });
      cache[code] = all;
      savePersisted(code, all);
      return all;
    });
  }

  // ── 週/月 K 棒聚合 ─────────────────────────────────────
  function isoWeekKey(dateStr) {
    var d = new Date(dateStr + "T00:00:00Z");
    var day = (d.getUTCDay() + 6) % 7; // 週一 = 0
    d.setUTCDate(d.getUTCDate() - day + 3);
    var firstThu = new Date(Date.UTC(d.getUTCFullYear(), 0, 4));
    var week = 1 + Math.round(((d - firstThu) / 86400000 - 3 + ((firstThu.getUTCDay() + 6) % 7)) / 7);
    return d.getUTCFullYear() + "-W" + String(week).padStart(2, "0");
  }

  function aggregate(daily, period) {
    if (period === "day") return daily;
    var groups = [], keyOf;
    if (period === "week") keyOf = function (r) { return isoWeekKey(r.date); };
    else keyOf = function (r) { return r.date.slice(0, 7); }; // YYYY-MM

    var byKey = {};
    daily.forEach(function (r) {
      var k = keyOf(r);
      if (!byKey[k]) { byKey[k] = []; groups.push(k); }
      byKey[k].push(r);
    });
    return groups.map(function (k) {
      var rows = byKey[k];
      return {
        date: rows[rows.length - 1].date,
        open: rows[0].open, close: rows[rows.length - 1].close,
        high: Math.max.apply(null, rows.map(function (r) { return r.high; })),
        low: Math.min.apply(null, rows.map(function (r) { return r.low; })),
        volume: rows.reduce(function (s, r) { return s + (r.volume || 0); }, 0),
      };
    });
  }

  // ── 技術指標 ───────────────────────────────────────────
  function sma(vals, period) {
    var out = new Array(vals.length).fill(null);
    var sum = 0;
    for (var i = 0; i < vals.length; i++) {
      sum += vals[i];
      if (i >= period) sum -= vals[i - period];
      if (i >= period - 1) out[i] = sum / period;
    }
    return out;
  }

  function computeRSI(closes, period) {
    period = period || 14;
    var rsi = new Array(closes.length).fill(null);
    var avgG = 0, avgL = 0;
    for (var i = 1; i < closes.length; i++) {
      var diff = closes[i] - closes[i - 1];
      var gain = diff > 0 ? diff : 0, loss = diff < 0 ? -diff : 0;
      if (i <= period) {
        avgG += gain; avgL += loss;
        if (i === period) { avgG /= period; avgL /= period; rsi[i] = avgL === 0 ? 100 : 100 - 100 / (1 + avgG / avgL); }
      } else {
        avgG = (avgG * (period - 1) + gain) / period;
        avgL = (avgL * (period - 1) + loss) / period;
        rsi[i] = avgL === 0 ? 100 : 100 - 100 / (1 + avgG / avgL);
      }
    }
    return rsi;
  }

  function gradeStock(hist, ma5, ma20, ma60, rsi5, rsi10) {
    var last = hist.length - 1;
    var notes = [], bull = 0, bear = 0;
    if (ma5[last] != null && ma20[last] != null && ma60[last] != null) {
      if (ma5[last] > ma20[last] && ma20[last] > ma60[last]) { notes.push("均線多頭排列"); bull += 2; }
      else if (ma5[last] < ma20[last] && ma20[last] < ma60[last]) { notes.push("均線空頭排列"); bear += 2; }
      else notes.push("均線糾結，方向未明");
      if (hist[last].close > ma60[last]) { notes.push("站上季線"); bull += 1; }
      else { notes.push("季線之下"); bear += 1; }
    } else {
      notes.push("歷史資料不足以計算均線");
    }
    var r5 = rsi5[last], r10 = rsi10[last];
    if (r5 != null) {
      if (r5 >= 70) { notes.push("RSI5 " + r5.toFixed(1) + " 進入超買區"); bear += 1; }
      else if (r5 <= 30) { notes.push("RSI5 " + r5.toFixed(1) + " 進入超賣區"); bull += 1; }
    }
    if (r5 != null && r10 != null) {
      if (r5 > r10) { notes.push("RSI5 站上 RSI10，短線動能轉強"); bull += 1; }
      else if (r5 < r10) { notes.push("RSI5 跌破 RSI10，短線動能轉弱"); bear += 1; }
    }
    var label = "區間整理";
    if (bear >= 2 && bear > bull) label = bull > 0 ? "過熱警訊" : "偏弱";
    else if (bull >= 2 && bull > bear) label = "多頭健康";
    return { label: label, notes: notes };
  }

  // ── 標記：均線黃金/死亡交叉、RSI5/RSI10 交叉與超買超賣、價量背離 ──
  function buildMarkers(bars, ma5, ma20, rsi5, rsi10) {
    var markers = [];
    for (var i = 1; i < bars.length; i++) {
      if (ma5[i] == null || ma20[i] == null || ma5[i - 1] == null || ma20[i - 1] == null) continue;
      if (ma5[i - 1] <= ma20[i - 1] && ma5[i] > ma20[i]) {
        markers.push({ time: bars[i].date, position: "belowBar", color: "#E15C4F",
                      shape: "arrowUp", size: 0.8, label: "黃金交叉" });
      } else if (ma5[i - 1] >= ma20[i - 1] && ma5[i] < ma20[i]) {
        markers.push({ time: bars[i].date, position: "aboveBar", color: "#4FB07A",
                      shape: "arrowDown", size: 0.8, label: "死亡交叉" });
      }
    }
    for (var j = 1; j < bars.length; j++) {
      if (rsi5[j] == null) continue;
      if (rsi5[j] >= 70 && (rsi5[j - 1] == null || rsi5[j - 1] < 70)) {
        markers.push({ time: bars[j].date, position: "aboveBar", color: "#E3AC4E",
                      shape: "circle", size: 0.6, label: "RSI5超買" });
      } else if (rsi5[j] <= 30 && (rsi5[j - 1] == null || rsi5[j - 1] > 30)) {
        markers.push({ time: bars[j].date, position: "belowBar", color: "#43BFAE",
                      shape: "circle", size: 0.6, label: "RSI5超賣" });
      }
    }
    for (var m = 1; m < bars.length; m++) {
      if (rsi5[m] == null || rsi10[m] == null || rsi5[m - 1] == null || rsi10[m - 1] == null) continue;
      if (rsi5[m - 1] <= rsi10[m - 1] && rsi5[m] > rsi10[m]) {
        markers.push({ time: bars[m].date, position: "belowBar", color: "#6C93F5",
                      shape: "arrowUp", size: 0.6, label: "RSI5/10黃金交叉" });
      } else if (rsi5[m - 1] >= rsi10[m - 1] && rsi5[m] < rsi10[m]) {
        markers.push({ time: bars[m].date, position: "aboveBar", color: "#B48CE8",
                      shape: "arrowDown", size: 0.6, label: "RSI5/10死亡交叉" });
      }
    }
    // 價量背離：找近 60 根裡的高點/低點，比較股價與成交量方向是否一致
    var lookback = Math.min(60, bars.length);
    var start = bars.length - lookback;
    for (var k = start + 3; k < bars.length - 1; k++) {
      var isHigh = bars[k].high >= bars[k - 1].high && bars[k].high >= bars[k - 2].high &&
                   bars[k].high >= bars[k + 1].high;
      var isLow = bars[k].low <= bars[k - 1].low && bars[k].low <= bars[k - 2].low &&
                  bars[k].low <= bars[k + 1].low;
      if (isHigh) {
        var prevHighIdx = findPrevPivotHigh(bars, k);
        if (prevHighIdx != null && bars[k].high > bars[prevHighIdx].high &&
            (bars[k].volume || 0) < (bars[prevHighIdx].volume || 0) * 0.8) {
          markers.push({ time: bars[k].date, position: "aboveBar", color: "#B48CE8",
                        shape: "arrowDown", size: 0.7, label: "頂背離（價創新高但量縮）" });
        }
      }
      if (isLow) {
        var prevLowIdx = findPrevPivotLow(bars, k);
        if (prevLowIdx != null && bars[k].low < bars[prevLowIdx].low &&
            (bars[k].volume || 0) < (bars[prevLowIdx].volume || 0) * 0.8) {
          markers.push({ time: bars[k].date, position: "belowBar", color: "#6C93F5",
                        shape: "arrowUp", size: 0.7, label: "底背離（價創新低但量縮）" });
        }
      }
    }
    markers.sort(function (a, b) { return a.time < b.time ? -1 : a.time > b.time ? 1 : 0; });
    return markers;
  }

  function findPrevPivotHigh(bars, idx) {
    for (var i = idx - 3; i >= Math.max(0, idx - 20); i--) {
      // i === 0 時 bars[i-1] 是 undefined，讀 .high 會整個彈窗炸掉
      // （TypeError: Cannot read properties of undefined）。剛掛牌的個股歷史短，
      // 樞紐點往回找很容易掃到第 0 根，findPrevPivotLow 本來就有這個防呆，
      // 這邊漏掉——7855 和運租車（2026-08-11 掛牌）就是這樣開不起來的。
      if (i <= 0) continue;
      if (bars[i].high >= bars[i - 1].high && bars[i].high >= bars[i + 1].high) return i;
    }
    return null;
  }
  function findPrevPivotLow(bars, idx) {
    for (var i = idx - 3; i >= Math.max(0, idx - 20); i--) {
      if (i <= 0) continue;
      if (bars[i].low <= bars[i - 1].low && bars[i].low <= bars[i + 1].low) return i;
    }
    return null;
  }

  function fmt(n) { return n == null ? "-" : n.toFixed(2); }

  // ── 圖表渲染（lightweight-charts v5，三個 pane：價格/成交量/RSI） ──
  // avgOnly = true（興櫃）時畫的是「日均價折線＋當日最高最低」，不是 K 棒：
  // 興櫃是議價／搓合市場，根本沒有開盤價與收盤價，硬畫 K 棒等於自己編一個
  // 不存在的實體（曾經拿前一日均價當開盤，畫出一根假的大紅 K）。
  function renderChart(container, infoEl, daily, period, avgOnly) {
    container.innerHTML = "";
    var bars = aggregate(daily, period);
    if (bars.length < 2) {
      container.innerHTML = '<p class="sc-empty">資料不足，無法繪圖。</p>';
      return null;
    }
    var closes = bars.map(function (r) { return r.close; });
    var ma5 = sma(closes, 5), ma20 = sma(closes, 20), ma60 = sma(closes, 60);
    var rsi5 = computeRSI(closes, 5), rsi10 = computeRSI(closes, 10);
    var barByTime = {};
    bars.forEach(function (r, i) { barByTime[r.date] = i; });

    var chart = LightweightCharts.createChart(container, {
      layout: { background: { color: "transparent" }, textColor: "#ABA398", fontSize: 11 },
      grid: { vertLines: { color: "#38332E" }, horzLines: { color: "#2A2725" } },
      timeScale: { timeVisible: false, borderColor: "#38332E" },
      rightPriceScale: { borderColor: "#38332E" },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      height: 460,
      autoSize: true,
    });

    var candle;
    if (avgOnly) {
      // 興櫃：只有日均價是真的，最高／最低另外用兩條細線標出當日成交區間
      var band = function (key, color) {
        var s = chart.addSeries(LightweightCharts.LineSeries, {
          color: color, lineWidth: 1, lineStyle: 2,
          priceLineVisible: false, lastValueVisible: false,
        }, 0);
        s.setData(bars.map(function (r) { return { time: r.date, value: r[key] }; }));
        return s;
      };
      band("high", "rgba(225,92,79,.45)");
      band("low", "rgba(79,176,122,.45)");
      candle = chart.addSeries(LightweightCharts.LineSeries, {
        color: "#E3AC4E", lineWidth: 2, priceLineVisible: true, lastValueVisible: true,
      }, 0);
      candle.setData(bars.map(function (r) {
        return { time: r.date, value: r.close };
      }));
    } else {
      candle = chart.addSeries(LightweightCharts.CandlestickSeries, {
        upColor: "#E15C4F", downColor: "#4FB07A",
        borderUpColor: "#E15C4F", borderDownColor: "#4FB07A",
        wickUpColor: "#E15C4F", wickDownColor: "#4FB07A",
      }, 0);
      candle.setData(bars.map(function (r) {
        return { time: r.date, open: r.open, high: r.high, low: r.low, close: r.close };
      }));
    }
    candle.priceScale().applyOptions({ autoScale: true });

    function maLine(vals, color) {
      var s = chart.addSeries(LightweightCharts.LineSeries, {
        color: color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
      }, 0);
      s.setData(bars.map(function (r, i) {
        return vals[i] == null ? null : { time: r.date, value: vals[i] };
      }).filter(Boolean));
      return s;
    }
    maLine(ma5, "#6C93F5"); maLine(ma20, "#E3AC4E"); maLine(ma60, "#B48CE8");

    var markers = buildMarkers(bars, ma5, ma20, rsi5, rsi10);
    var markerByTime = {};
    markers.forEach(function (mk) {
      (markerByTime[mk.time] = markerByTime[mk.time] || []).push(mk.label);
    });
    if (markers.length && LightweightCharts.createSeriesMarkers) {
      // 不放文字，只用形狀+顏色標點，避免密集的日線圖擠成一片看不清楚；
      // 對應說明改成 hover/點選時在資訊列顯示（見下面 crosshair 訂閱）。
      LightweightCharts.createSeriesMarkers(candle, markers.map(function (mk) {
        return { time: mk.time, position: mk.position, color: mk.color,
                 shape: mk.shape, size: mk.size };
      }));
    }

    var volume = chart.addSeries(LightweightCharts.HistogramSeries, {
      priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false,
    }, 1);
    volume.setData(bars.map(function (r, i) {
      // 興櫃沒有開盤價，量的紅綠改用「均價比前一日高／低」判斷才有意義
      var up = avgOnly
        ? (i > 0 ? r.close >= bars[i - 1].close : true)
        : r.close >= r.open;
      return { time: r.date, value: r.volume || 0,
              color: up ? "rgba(225,92,79,.55)" : "rgba(79,176,122,.55)" };
    }));
    volume.priceScale().applyOptions({ autoScale: true, scaleMargins: { top: 0.1, bottom: 0 } });

    var rsi5Series = chart.addSeries(LightweightCharts.LineSeries, {
      color: "#6C93F5", lineWidth: 1.4, priceLineVisible: false, lastValueVisible: true,
    }, 2);
    rsi5Series.setData(bars.map(function (r, i) {
      return rsi5[i] == null ? null : { time: r.date, value: rsi5[i] };
    }).filter(Boolean));
    rsi5Series.priceScale().applyOptions({ autoScale: true });

    var rsi10Series = chart.addSeries(LightweightCharts.LineSeries, {
      color: "#E3AC4E", lineWidth: 1.4, priceLineVisible: false, lastValueVisible: true,
    }, 2);
    rsi10Series.setData(bars.map(function (r, i) {
      return rsi10[i] == null ? null : { time: r.date, value: rsi10[i] };
    }).filter(Boolean));

    try {
      var panes = chart.panes();
      if (panes[0] && panes[0].setStretchFactor) panes[0].setStretchFactor(4);
      if (panes[1] && panes[1].setStretchFactor) panes[1].setStretchFactor(1.3);
      if (panes[2] && panes[2].setStretchFactor) panes[2].setStretchFactor(1.3);
    } catch (e) { /* 舊版沒有 panes()，忽略即可 */ }

    function showInfo(idx) {
      if (idx == null || !bars[idx]) { infoEl.innerHTML = ""; return; }
      var r = bars[idx];
      var chg = idx > 0 ? r.close - bars[idx - 1].close : 0;
      var pct = idx > 0 && bars[idx - 1].close ? chg / bars[idx - 1].close * 100 : 0;
      var cls = chg >= 0 ? "up" : "down";
      var extra = markerByTime[r.date]
        ? '<span class="sc-info-marker">' + markerByTime[r.date].join('、') + '</span>' : "";
      infoEl.innerHTML = '<span class="sc-info-date">' + r.date + '</span>'
        + (avgOnly ? "" : '<span>開 <b class="mono">' + fmt(r.open) + '</b></span>')
        + '<span>高 <b class="mono">' + fmt(r.high) + '</b></span>'
        + '<span>低 <b class="mono">' + fmt(r.low) + '</b></span>'
        + '<span>' + (avgOnly ? "均價" : "收") + ' <b class="mono ' + cls + '">' + fmt(r.close) + '</b></span>'
        + '<span>量 <b class="mono">' + Math.round((r.volume || 0) / 1000).toLocaleString() + '張</b></span>'
        + '<span class="mono ' + cls + '">' + (chg >= 0 ? "+" : "") + fmt(chg) + '（' + (pct >= 0 ? "+" : "") + pct.toFixed(2) + '%）</span>'
        + extra;
    }
    showInfo(bars.length - 1);
    chart.subscribeCrosshairMove(function (param) {
      if (!param || !param.time) { showInfo(bars.length - 1); return; }
      showInfo(barByTime[param.time]);
    });

    chart.timeScale().fitContent();
    return { chart: chart, bars: bars, ma5: ma5, ma20: ma20, ma60: ma60, rsi5: rsi5, rsi10: rsi10 };
  }

  function legend(avgOnly) {
    return '<div class="sc-legend">'
      + (avgOnly
          ? '<span><i class="sc-line" style="background:#E3AC4E"></i>日均價</span>'
            + '<span><i class="sc-line" style="background:rgba(225,92,79,.45)"></i>日最高</span>'
            + '<span><i class="sc-line" style="background:rgba(79,176,122,.45)"></i>日最低</span>'
          : '<span><i class="sc-dot" style="background:#E15C4F"></i>上漲</span>'
            + '<span><i class="sc-dot" style="background:#4FB07A"></i>下跌</span>')
      + '<span><i class="sc-line" style="background:#6C93F5"></i>MA5／RSI5</span>'
      + '<span><i class="sc-line" style="background:#E3AC4E"></i>MA20／RSI10</span>'
      + '<span><i class="sc-line" style="background:#B48CE8"></i>MA60</span>'
      + '<span>▲/▼ 標記：均線與 RSI5/10 黃金/死亡交叉、RSI 超買賣、量價背離</span>'
      + '</div>';
  }

  function ensureModal() {
    var m = document.getElementById("sc-modal");
    if (m) return m;
    m = document.createElement("div");
    m.id = "sc-modal";
    m.className = "sc-modal";
    m.innerHTML = '<div class="sc-modal-inner">'
      + '<a class="sc-fullpage" id="sc-fullpage" href="#">開啟完整頁面 ↗</a>'
      + '<button class="sc-close" aria-label="關閉">✕</button>'
      + '<div id="sc-modal-body"></div>'
      + '</div>';
    document.body.appendChild(m);
    m.addEventListener("click", function (e) { if (e.target === m) close(); });
    m.querySelector(".sc-close").addEventListener("click", close);
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") close(); });
    return m;
  }

  function close() {
    var m = document.getElementById("sc-modal");
    if (m) m.classList.remove("open");
  }

  // 把一組日 K（來源可能是 TWSE 即時，或本站對上櫃／興櫃做的每日快照）畫成
  // 完整彈窗內容。opts.source：資料來源說明；opts.avgPriceNote：興櫃均價提醒。
  function renderReport(body, code, name, daily, opts) {
    opts = opts || {};
    var last = daily[daily.length - 1];
    var prev = daily.length > 1 ? daily[daily.length - 2] : null;
    var chg = prev ? last.close - prev.close : 0;
    var chgPct = prev && prev.close ? (chg / prev.close * 100) : 0;
    var headPrice = last.close, headDate = last.date;
    var q = opts.latest;
    // 興櫃：看盤說的股價是「成交（最後成交價）」，不是日均價。
    // 舊的 TPEx 快照（avgPriceNote）收盤欄放的是日均價，一定要用當日行情表的
    // 「成交」蓋掉；Yahoo 快照的收盤本身就是成交價（實測 7686 = 802，跟 TPEx
    // 當日行情表一致），這時只有在當日行情表比最後一根 K 還新時才覆蓋。
    if (q && q.price && (opts.avgPriceNote || (q.date && q.date > last.date))) {
      headPrice = q.price; headDate = q.date || headDate;
      chg = q.change; chgPct = q.change_pct;
    }
    var upCls = chg >= 0 ? "up" : "down";

    body.innerHTML = '<h3 class="sc-title">' + code + ' ' + (name || "") + '</h3>'
      + '<div class="sc-price-row">'
      + '<span class="sc-price mono">' + fmt(headPrice) + '</span>'
      + '<span class="sc-chg mono ' + upCls + '">' + (chg >= 0 ? "▲" : "▼") + ' '
      + fmt(Math.abs(chg)) + '（' + (chgPct >= 0 ? "+" : "") + chgPct.toFixed(2) + '%）</span>'
      + '<span class="sc-date">' + headDate + '</span>'
      + '</div>'
      + (q ? '<div class="sc-quote-row">'
          + '<span>成交 <b class="mono">' + fmt(q.price) + '</b></span>'
          + '<span>日均價 <b class="mono">' + fmt(q.avg) + '</b></span>'
          + '<span>日最高 <b class="mono">' + fmt(q.high) + '</b></span>'
          + '<span>日最低 <b class="mono">' + fmt(q.low) + '</b></span>'
          + '<span>報買 <b class="mono">' + fmt(q.bid) + '</b></span>'
          + '<span>報賣 <b class="mono">' + fmt(q.ask) + '</b></span>'
          + '<span>前日均價 <b class="mono">' + fmt(q.prev_avg) + '</b></span>'
          + '</div>' : "")
      + (opts.avgPriceNote
          ? '<p class="sc-snapshot-note">興櫃是議價／搓合市場，沒有開盤收盤價。上方<b>成交</b>是最後成交價（跟 TPEx 當日行情表同一欄）；下方 K 線因為歷史資料只提供<b>日均價</b>，是以日均價當收盤、日最高最低為影線的均價走勢，兩者本來就會有落差。</p>'
          : "")
      + '<div class="sc-tf-tabs">'
      + '<button class="sc-tf active" data-tf="day">日</button>'
      + '<button class="sc-tf" data-tf="week">週</button>'
      + '<button class="sc-tf" data-tf="month">月</button>'
      + '<span class="sc-zoom-group">'
      + '<button class="sc-zoom" id="sc-zoom-out" aria-label="縮小" title="縮小">－</button>'
      + '<button class="sc-zoom" id="sc-zoom-fit" aria-label="還原" title="還原">⤢</button>'
      + '<button class="sc-zoom" id="sc-zoom-in" aria-label="放大" title="放大">＋</button>'
      + '</span>'
      + '</div>'
      + '<div class="sc-info-bar" id="sc-info-bar"></div>'
      + '<div id="sc-chart-container" class="sc-chart-container"></div>'
      + legend(!!opts.avgPriceNote)
      + '<div class="sc-grade" id="sc-grade"></div>'
      + '<p class="sc-disclaimer">'
      + (opts.source || "資料來源：TWSE 每日收盤行情，即時於瀏覽器端計算技術指標與標記")
      + '，僅供研究參考，不構成投資建議。拖曳游標可看當天'
      + (opts.avgPriceNote ? '均價與最高最低' : '開高低收') + '。</p>';

    var container = document.getElementById("sc-chart-container");
    var infoEl = document.getElementById("sc-info-bar");
    var gradeEl = document.getElementById("sc-grade");

    function renderGrade(state) {
      if (!state) { gradeEl.innerHTML = ""; return; }
      var g = gradeStock(state.bars, state.ma5, state.ma20, state.ma60, state.rsi5, state.rsi10);
      gradeEl.innerHTML = '<span class="tag sc-grade-tag">' + g.label + '</span>'
        + '<span class="sc-notes">' + g.notes.join('、') + '</span>';
    }

    var state = renderChart(container, infoEl, daily, "day", !!opts.avgPriceNote);
    renderGrade(state);

    function zoom(factor) {
      if (!state) return;
      var ts = state.chart.timeScale();
      var range = ts.getVisibleLogicalRange();
      if (!range) return;
      var center = (range.from + range.to) / 2;
      var half = (range.to - range.from) / 2 * factor;
      ts.setVisibleLogicalRange({ from: center - half, to: center + half });
    }
    document.getElementById("sc-zoom-in").addEventListener("click", function () { zoom(0.7); });
    document.getElementById("sc-zoom-out").addEventListener("click", function () { zoom(1 / 0.7); });
    document.getElementById("sc-zoom-fit").addEventListener("click", function () {
      if (state) state.chart.timeScale().fitContent();
    });

    body.querySelectorAll(".sc-tf").forEach(function (btn) {
      btn.addEventListener("click", function () {
        body.querySelectorAll(".sc-tf").forEach(function (b) { b.classList.remove("active"); });
        btn.classList.add("active");
        state = renderChart(container, infoEl, daily, btn.getAttribute("data-tf"), !!opts.avgPriceNote);
        renderGrade(state);
      });
    });

    appendSwot(body, code);
  }

  function showNoPrice(body, code, name) {
    body.innerHTML = '<h3 class="sc-title">' + code + ' ' + (name || "") + '</h3>'
      + '<p class="sc-empty">查無股價資料。這檔可能剛掛牌、或資料源暫時無回應。'
      + '以下為系統整理的公司介紹與 SWOT：</p>';
    appendSwot(body, code);
  }

  // 上櫃／興櫃日K快照的位置。全市場 1200+ 檔、每檔兩年約 48KB，一輪就 ~60MB
  // 而且每個交易日重寫，放進 main 的 git 歷史一年會長到好幾百 MB——所以比照
  // 盤中資料的做法推到 chart-data 分支（force push、不留歷史），這裡直接讀
  // raw.githubusercontent。讀不到時退回站內路徑，讓舊快照仍能顯示。
  var SNAP_CDN = "https://raw.githubusercontent.com/chen022208-cell/stock-report/"
               + "chart-data/docs/data/tpex_hist/";

  // 快照的 bars 為了縮小體積存成陣列 [d,o,h,l,c,v]（見 main._compact_bars），
  // 這裡展開回圖表用的物件。舊的物件格式照樣支援，換格式期間不會空窗。
  function expandBars(snap) {
    if (!snap || !Array.isArray(snap.bars)) return snap;
    snap.bars = snap.bars.map(function (b) {
      if (!Array.isArray(b)) return b;
      return { date: b[0], open: b[1], high: b[2], low: b[3], close: b[4], volume: b[5] };
    });
    return snap;
  }

  function fetchSnapshot(code) {
    return fetch(SNAP_CDN + code + ".json")
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; })
      .then(function (snap) {
        if (snap && snap.bars && snap.bars.length) return expandBars(snap);
        return fetch(assetBase() + "data/tpex_hist/" + code + ".json")
          .then(function (r) { return r.ok ? r.json() : null; })
          .catch(function () { return null; })
          .then(expandBars);
      });
  }

  // 上櫃／興櫃走本站快照（TWSE 端點沒有這些代號，硬打會是 36 個必定失敗的請求）
  function openFromSnapshot(body, code, name) {
    return fetchSnapshot(code)
      .then(function (snap) {
        // 只有 1 根也照畫（剛掛牌的個股）：圖表本身會顯示「資料不足」，
        // 但價格、公司基本資料與題材照樣看得到，比整頁「查無股價資料」有用。
        if (snap && snap.bars && snap.bars.length >= 1) {
          var esb = snap.market === "esb";
          // source==="yahoo" 的快照是真的開高低收（連興櫃都有），可以直接畫 K 棒。
          // 只有舊的 TPEx 興櫃快照才是「日均價當收盤」，那種要標註成均價走勢。
          var avgOnly = esb && snap.source !== "yahoo";
          var src = snap.source === "yahoo"
            ? "資料來源：Yahoo Finance 日線（開高低收）"
              + (esb ? "，興櫃成交價與 TPEx 當日行情表一致" : "")
            : (esb ? "資料來源：TPEx 興櫃當日行情表＋歷史行情"
                   : "資料來源：TPEx 上櫃每日成交資訊");
          renderReport(body, code, name || snap.name, snap.bars, {
            avgPriceNote: avgOnly,
            latest: snap.latest,
            source: src + "，本站盤後快照（非即時，更新於 "
              + String(snap.updated || "").slice(0, 10) + "）",
          });
        } else {
          showNoPrice(body, code, name);
        }
      })
      .catch(function () { showNoPrice(body, code, name); });
  }

  // 把「把某一檔畫進某個容器」抽出來，彈窗與獨立個股頁（stock.html）共用同一套
  // 流程，不會出現兩邊資料或版面不一致。body 可以是彈窗內容區，也可以是整頁的容器。
  function renderInto(body, code, name) {
    body.innerHTML = '<h3 class="sc-title">' + esc(code) + ' ' + esc(name || "") + '</h3>'
      + '<p class="sc-loading">讀取股價資料中…</p>';

    return loadInfo(code).then(function (info) {
      var market = info && info.profile ? info.profile.market : "";
      if (market === "tpex" || market === "esb") {
        return openFromSnapshot(body, code, name);
      }
      return fetchHistory(code).then(function (daily) {
        if (daily.length) {
          renderReport(body, code, name, daily, {});
          return;
        }
        return openFromSnapshot(body, code, name);
      });
    }).catch(function (exc) {
      body.innerHTML = '<h3 class="sc-title">' + esc(code) + ' ' + esc(name || "") + '</h3>'
        + '<p class="sc-empty">讀取失敗：' + esc(String(exc)) + '</p>';
      appendSwot(body, code);
    });
  }

  function open(code, name) {
    var m = ensureModal();
    var body = document.getElementById("sc-modal-body");
    m.classList.add("open");
    var link = document.getElementById("sc-fullpage");
    if (link) link.href = assetBase() + "stock.html?code=" + encodeURIComponent(code);
    return renderInto(body, code, name);
  }

  window.StockChart = { open: open, close: close, renderInto: renderInto };

  // 事件委派：任何有 data-stock-code 屬性的元素都自動可點擊開圖表，
  // 不用每個模板各自綁 onclick。
  document.addEventListener("click", function (e) {
    var el = e.target.closest ? e.target.closest("[data-stock-code]") : null;
    if (!el) return;
    e.preventDefault();
    var code = el.getAttribute("data-stock-code");
    // 已經在獨立個股頁上時，點同業／關聯個股應該是「換一檔」而不是疊一個彈窗
    if (document.body.getAttribute("data-stock-page") === "1") {
      location.href = "stock.html?code=" + encodeURIComponent(code);
      return;
    }
    open(code, el.getAttribute("data-stock-name") || "");
  });
})();
