/* 個股股價 K 線 + 技術指標，純前端即時抓 TWSE 公開資料計算，不用後端、不用 LLM。
 * 用 TradingView 的 lightweight-charts（v5，MIT）畫圖：日/週/月切換、可拖曳縮放、
 * 均線交叉與價量背離用 markers 標在圖上。
 * TWSE STOCK_DAY 這支端點對瀏覽器端 fetch 開放 CORS，上櫃（TPEx）目前測過不支援，
 * 遇到上櫃股會誠實顯示「此資料源不支援上櫃股即時圖表」，不會裝作有資料。
 */
(function () {
  "use strict";

  var cache = {};
  var MONTHS_BACK = 15;

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

  function gradeStock(hist, ma5, ma20, ma60, rsi) {
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
    var r = rsi[last];
    if (r != null) {
      if (r >= 70) { notes.push("RSI " + r.toFixed(1) + " 進入超買區"); bear += 1; }
      else if (r <= 30) { notes.push("RSI " + r.toFixed(1) + " 進入超賣區"); bull += 1; }
    }
    var label = "區間整理";
    if (bear >= 2 && bear > bull) label = bull > 0 ? "過熱警訊" : "偏弱";
    else if (bull >= 2 && bull > bear) label = "多頭健康";
    return { label: label, notes: notes };
  }

  // ── 標記：均線黃金/死亡交叉、RSI 超買超賣、價量背離 ──────
  function buildMarkers(bars, ma5, ma20, rsi) {
    var markers = [];
    for (var i = 1; i < bars.length; i++) {
      if (ma5[i] == null || ma20[i] == null || ma5[i - 1] == null || ma20[i - 1] == null) continue;
      if (ma5[i - 1] <= ma20[i - 1] && ma5[i] > ma20[i]) {
        markers.push({ time: bars[i].date, position: "belowBar", color: "#E15C4F",
                      shape: "arrowUp", text: "黃金交叉" });
      } else if (ma5[i - 1] >= ma20[i - 1] && ma5[i] < ma20[i]) {
        markers.push({ time: bars[i].date, position: "aboveBar", color: "#4FB07A",
                      shape: "arrowDown", text: "死亡交叉" });
      }
    }
    for (var j = 1; j < bars.length; j++) {
      if (rsi[j] == null) continue;
      if (rsi[j] >= 70 && (rsi[j - 1] == null || rsi[j - 1] < 70)) {
        markers.push({ time: bars[j].date, position: "aboveBar", color: "#E3AC4E",
                      shape: "circle", text: "RSI超買" });
      } else if (rsi[j] <= 30 && (rsi[j - 1] == null || rsi[j - 1] > 30)) {
        markers.push({ time: bars[j].date, position: "belowBar", color: "#43BFAE",
                      shape: "circle", text: "RSI超賣" });
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
                        shape: "arrowDown", text: "頂背離" });
        }
      }
      if (isLow) {
        var prevLowIdx = findPrevPivotLow(bars, k);
        if (prevLowIdx != null && bars[k].low < bars[prevLowIdx].low &&
            (bars[k].volume || 0) < (bars[prevLowIdx].volume || 0) * 0.8) {
          markers.push({ time: bars[k].date, position: "belowBar", color: "#6C93F5",
                        shape: "arrowUp", text: "底背離" });
        }
      }
    }
    markers.sort(function (a, b) { return a.time < b.time ? -1 : a.time > b.time ? 1 : 0; });
    return markers;
  }

  function findPrevPivotHigh(bars, idx) {
    for (var i = idx - 3; i >= Math.max(0, idx - 20); i--) {
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
  function renderChart(container, daily, period) {
    container.innerHTML = "";
    var bars = aggregate(daily, period);
    if (bars.length < 2) {
      container.innerHTML = '<p class="sc-empty">資料不足，無法繪圖。</p>';
      return null;
    }
    var closes = bars.map(function (r) { return r.close; });
    var ma5 = sma(closes, 5), ma20 = sma(closes, 20), ma60 = sma(closes, 60);
    var rsi = computeRSI(closes, 14);

    var chart = LightweightCharts.createChart(container, {
      layout: { background: { color: "transparent" }, textColor: "#ABA398" },
      grid: { vertLines: { color: "#38332E" }, horzLines: { color: "#38332E" } },
      timeScale: { timeVisible: false, borderColor: "#38332E" },
      rightPriceScale: { borderColor: "#38332E" },
      height: 380,
      autoSize: true,
    });

    var candle = chart.addSeries(LightweightCharts.CandlestickSeries, {
      upColor: "#E15C4F", downColor: "#4FB07A",
      borderUpColor: "#E15C4F", borderDownColor: "#4FB07A",
      wickUpColor: "#E15C4F", wickDownColor: "#4FB07A",
    }, 0);
    candle.setData(bars.map(function (r) {
      return { time: r.date, open: r.open, high: r.high, low: r.low, close: r.close };
    }));

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

    var markers = buildMarkers(bars, ma5, ma20, rsi);
    if (markers.length && LightweightCharts.createSeriesMarkers) {
      LightweightCharts.createSeriesMarkers(candle, markers);
    }

    var volume = chart.addSeries(LightweightCharts.HistogramSeries, {
      priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false,
    }, 1);
    volume.setData(bars.map(function (r) {
      return { time: r.date, value: r.volume || 0,
              color: r.close >= r.open ? "rgba(225,92,79,.55)" : "rgba(79,176,122,.55)" };
    }));

    var rsiSeries = chart.addSeries(LightweightCharts.LineSeries, {
      color: "#43BFAE", lineWidth: 1.4, priceLineVisible: false, lastValueVisible: true,
    }, 2);
    rsiSeries.setData(bars.map(function (r, i) {
      return rsi[i] == null ? null : { time: r.date, value: rsi[i] };
    }).filter(Boolean));

    try {
      var panes = chart.panes();
      if (panes[0] && panes[0].setStretchFactor) panes[0].setStretchFactor(3);
      if (panes[1] && panes[1].setStretchFactor) panes[1].setStretchFactor(1);
      if (panes[2] && panes[2].setStretchFactor) panes[2].setStretchFactor(1);
    } catch (e) { /* 舊版沒有 panes()，忽略即可 */ }

    chart.timeScale().fitContent();
    return { chart: chart, bars: bars, ma5: ma5, ma20: ma20, ma60: ma60, rsi: rsi };
  }

  function legend() {
    return '<div class="sc-legend">'
      + '<span><i class="sc-dot" style="background:#E15C4F"></i>上漲</span>'
      + '<span><i class="sc-dot" style="background:#4FB07A"></i>下跌</span>'
      + '<span><i class="sc-line" style="background:#6C93F5"></i>MA5</span>'
      + '<span><i class="sc-line" style="background:#E3AC4E"></i>MA20</span>'
      + '<span><i class="sc-line" style="background:#B48CE8"></i>MA60</span>'
      + '<span>▲/▼ 標記：黃金/死亡交叉、RSI 超買賣、量價背離</span>'
      + '</div>';
  }

  function ensureModal() {
    var m = document.getElementById("sc-modal");
    if (m) return m;
    m = document.createElement("div");
    m.id = "sc-modal";
    m.className = "sc-modal";
    m.innerHTML = '<div class="sc-modal-inner">'
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

  function open(code, name) {
    var m = ensureModal();
    var body = document.getElementById("sc-modal-body");
    body.innerHTML = '<h3 class="sc-title">' + code + ' ' + (name || "") + '</h3>'
      + '<p class="sc-loading">讀取即時股價資料中…</p>';
    m.classList.add("open");

    fetchHistory(code).then(function (daily) {
      if (!daily.length) {
        body.innerHTML = '<h3 class="sc-title">' + code + ' ' + (name || "") + '</h3>'
          + '<p class="sc-empty">查無資料。若為上櫃（TPEx）個股，該資料源目前不支援瀏覽器端即時查詢，'
          + '暫時只能看本站每日報告裡系統另外算好的技術面資訊。</p>';
        return;
      }
      var last = daily[daily.length - 1];
      var prev = daily.length > 1 ? daily[daily.length - 2] : null;
      var chg = prev ? last.close - prev.close : 0;
      var chgPct = prev && prev.close ? (chg / prev.close * 100) : 0;
      var upCls = chg >= 0 ? "up" : "down";

      body.innerHTML = '<h3 class="sc-title">' + code + ' ' + (name || "") + '</h3>'
        + '<div class="sc-price-row">'
        + '<span class="sc-price mono">' + fmt(last.close) + '</span>'
        + '<span class="sc-chg mono ' + upCls + '">' + (chg >= 0 ? "▲" : "▼") + ' '
        + fmt(Math.abs(chg)) + '（' + (chgPct >= 0 ? "+" : "") + chgPct.toFixed(2) + '%）</span>'
        + '<span class="sc-date">' + last.date + '</span>'
        + '</div>'
        + '<div class="sc-tf-tabs">'
        + '<button class="sc-tf active" data-tf="day">日</button>'
        + '<button class="sc-tf" data-tf="week">週</button>'
        + '<button class="sc-tf" data-tf="month">月</button>'
        + '</div>'
        + '<div id="sc-chart-container" class="sc-chart-container"></div>'
        + legend()
        + '<div class="sc-grade" id="sc-grade"></div>'
        + '<p class="sc-disclaimer">資料來源：TWSE 每日收盤行情，即時於瀏覽器端計算技術指標與標記，僅供研究參考，不構成投資建議。</p>';

      var container = document.getElementById("sc-chart-container");
      var gradeEl = document.getElementById("sc-grade");

      function renderGrade(state) {
        if (!state) { gradeEl.innerHTML = ""; return; }
        var g = gradeStock(state.bars, state.ma5, state.ma20, state.ma60, state.rsi);
        gradeEl.innerHTML = '<span class="tag sc-grade-tag">' + g.label + '</span>'
          + '<span class="sc-notes">' + g.notes.join('、') + '</span>';
      }

      var state = renderChart(container, daily, "day");
      renderGrade(state);

      body.querySelectorAll(".sc-tf").forEach(function (btn) {
        btn.addEventListener("click", function () {
          body.querySelectorAll(".sc-tf").forEach(function (b) { b.classList.remove("active"); });
          btn.classList.add("active");
          state = renderChart(container, daily, btn.getAttribute("data-tf"));
          renderGrade(state);
        });
      });
    }).catch(function (exc) {
      body.innerHTML = '<h3 class="sc-title">' + code + ' ' + (name || "") + '</h3>'
        + '<p class="sc-empty">讀取失敗：' + String(exc) + '</p>';
    });
  }

  window.StockChart = { open: open, close: close };

  // 事件委派：任何有 data-stock-code 屬性的元素都自動可點擊開圖表，
  // 不用每個模板各自綁 onclick。
  document.addEventListener("click", function (e) {
    var el = e.target.closest ? e.target.closest("[data-stock-code]") : null;
    if (!el) return;
    e.preventDefault();
    open(el.getAttribute("data-stock-code"), el.getAttribute("data-stock-name") || "");
  });
})();
