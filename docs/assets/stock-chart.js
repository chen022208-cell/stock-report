/* 個股股價 K 線 + 技術指標，純前端即時抓 TWSE 公開資料計算，不用後端、不用 LLM。
 * TWSE STOCK_DAY 這支端點對瀏覽器端 fetch 開放 CORS，上櫃（TPEx）目前測過不支援，
 * 遇到上櫃股會誠實顯示「此資料源不支援上櫃股即時圖表」，不會裝作有資料。
 */
(function () {
  "use strict";

  var cache = {};

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

  function fetchHistory(code, monthsBack) {
    monthsBack = monthsBack || 6;
    if (cache[code]) return Promise.resolve(cache[code]);
    var now = new Date();
    var calls = [];
    for (var i = 0; i < monthsBack; i++) {
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

  function fmt(n) { return n == null ? "-" : n.toFixed(2); }

  function buildSVG(hist, ma5, ma20, ma60, rsi) {
    var w = 760, hMain = 260, hRsi = 90, padL = 44, padR = 10, padTop = 10;
    var n = hist.length;
    if (n < 2) return '<p class="sc-empty">資料不足，無法繪圖。</p>';
    var closes = hist.map(function (r) { return r.close; });
    var highs = hist.map(function (r) { return r.high; });
    var lows = hist.map(function (r) { return r.low; });
    var maxP = Math.max.apply(null, highs.filter(function(v){return v!=null;}));
    var minP = Math.min.apply(null, lows.filter(function(v){return v!=null;}));
    var pad = (maxP - minP) * 0.06 || 1;
    maxP += pad; minP -= pad;
    var xw = (w - padL - padR) / n;
    function xAt(i) { return padL + i * xw + xw / 2; }
    function yAt(p) { return padTop + (maxP - p) / (maxP - minP) * (hMain - padTop - 10); }

    var parts = [];
    parts.push('<svg viewBox="0 0 ' + w + ' ' + (hMain + hRsi + 30) + '" class="sc-svg" role="img" aria-label="K線圖">');

    // 網格與 y 軸刻度（大盤價格）
    for (var g = 0; g <= 4; g++) {
      var yy = padTop + g * (hMain - padTop - 10) / 4;
      var val = maxP - g * (maxP - minP) / 4;
      parts.push('<line x1="' + padL + '" y1="' + yy + '" x2="' + (w - padR) + '" y2="' + yy + '" class="sc-grid"/>');
      parts.push('<text x="2" y="' + (yy + 4) + '" class="sc-axis">' + val.toFixed(1) + '</text>');
    }

    // K 線
    for (var i = 0; i < n; i++) {
      var r = hist[i];
      if (r.open == null || r.close == null) continue;
      var up = r.close >= r.open;
      var color = up ? "var(--red)" : "var(--green)";
      var x = xAt(i);
      parts.push('<line x1="' + x + '" y1="' + yAt(r.high) + '" x2="' + x + '" y2="' + yAt(r.low) + '" stroke="' + color + '" stroke-width="1"/>');
      var yo = yAt(r.open), yc = yAt(r.close);
      var top = Math.min(yo, yc), h = Math.max(Math.abs(yc - yo), 1);
      parts.push('<rect x="' + (x - xw * 0.32) + '" y="' + top + '" width="' + (xw * 0.64) + '" height="' + h + '" fill="' + color + '"/>');
    }

    // MA 線
    function maPath(ma, color) {
      var d = "", started = false;
      for (var i = 0; i < n; i++) {
        if (ma[i] == null) continue;
        var cmd = started ? "L" : "M";
        d += cmd + xAt(i) + "," + yAt(ma[i]) + " ";
        started = true;
      }
      return '<path d="' + d + '" fill="none" stroke="' + color + '" stroke-width="1.4"/>';
    }
    parts.push(maPath(ma5, "var(--c-market)"));
    parts.push(maPath(ma20, "var(--gold)"));
    parts.push(maPath(ma60, "var(--c-theme)"));

    // RSI 副圖
    var rsiTop = hMain + 16;
    var rsiH = hRsi - 10;
    function ry(v) { return rsiTop + (100 - v) / 100 * rsiH; }
    parts.push('<line x1="' + padL + '" y1="' + ry(70) + '" x2="' + (w - padR) + '" y2="' + ry(70) + '" class="sc-grid-dash"/>');
    parts.push('<line x1="' + padL + '" y1="' + ry(30) + '" x2="' + (w - padR) + '" y2="' + ry(30) + '" class="sc-grid-dash"/>');
    parts.push('<text x="2" y="' + (ry(70) + 4) + '" class="sc-axis">70</text>');
    parts.push('<text x="2" y="' + (ry(30) + 4) + '" class="sc-axis">30</text>');
    var rd = "", started2 = false;
    for (var j = 0; j < n; j++) {
      if (rsi[j] == null) continue;
      var cmd2 = started2 ? "L" : "M";
      rd += cmd2 + xAt(j) + "," + ry(rsi[j]) + " ";
      started2 = true;
    }
    parts.push('<path d="' + rd + '" fill="none" stroke="var(--c-score)" stroke-width="1.4"/>');
    parts.push('<text x="' + padL + '" y="' + (rsiTop - 4) + '" class="sc-axis">RSI(14)</text>');

    parts.push("</svg>");
    return parts.join("");
  }

  function legend() {
    return '<div class="sc-legend">'
      + '<span><i class="sc-dot" style="background:var(--red)"></i>上漲</span>'
      + '<span><i class="sc-dot" style="background:var(--green)"></i>下跌</span>'
      + '<span><i class="sc-line" style="background:var(--c-market)"></i>MA5</span>'
      + '<span><i class="sc-line" style="background:var(--gold)"></i>MA20</span>'
      + '<span><i class="sc-line" style="background:var(--c-theme)"></i>MA60</span>'
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
    m.addEventListener("click", function (e) {
      if (e.target === m) close();
    });
    m.querySelector(".sc-close").addEventListener("click", close);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") close();
    });
    return m;
  }

  function close() {
    var m = document.getElementById("sc-modal");
    if (m) m.classList.remove("open");
  }

  function isLikelyTwseListed(code) {
    // 沒有可靠的前端市場別清單；先嘗試 TWSE，抓不到資料再提示可能是上櫃。
    return true;
  }

  function open(code, name) {
    var m = ensureModal();
    var body = document.getElementById("sc-modal-body");
    body.innerHTML = '<h3 class="sc-title">' + code + ' ' + (name || "") + '</h3>'
      + '<p class="sc-loading">讀取即時股價資料中…</p>';
    m.classList.add("open");

    fetchHistory(code).then(function (hist) {
      if (!hist.length) {
        body.innerHTML = '<h3 class="sc-title">' + code + ' ' + (name || "") + '</h3>'
          + '<p class="sc-empty">查無資料。若為上櫃（TPEx）個股，該資料源目前不支援瀏覽器端即時查詢，'
          + '暫時只能看本站每日報告裡系統另外算好的技術面資訊。</p>';
        return;
      }
      var closes = hist.map(function (r) { return r.close; });
      var ma5 = sma(closes, 5), ma20 = sma(closes, 20), ma60 = sma(closes, 60);
      var rsi = computeRSI(closes, 14);
      var g = gradeStock(hist, ma5, ma20, ma60, rsi);
      var last = hist[hist.length - 1];
      var prev = hist.length > 1 ? hist[hist.length - 2] : null;
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
        + buildSVG(hist, ma5, ma20, ma60, rsi)
        + legend()
        + '<div class="sc-grade"><span class="tag sc-grade-tag">' + g.label + '</span>'
        + '<span class="sc-notes">' + g.notes.join('、') + '</span></div>'
        + '<p class="sc-disclaimer">資料來源：TWSE 每日收盤行情，即時於瀏覽器端計算技術指標，僅供研究參考，不構成投資建議。</p>';
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
