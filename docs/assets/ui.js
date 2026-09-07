/* 全站互動層——漸進增強，關掉 JS 頁面仍可讀。
 *
 *  1. 分段控制 / pill 切換
 *     <div class="seg" data-group="range"><button data-value="1m">1月</button>…</div>
 *     - 點擊 → 該顆加 .on、同組其他移除
 *     - 若有祖先或文件內 [data-panes="range"]:not([data-group])，依 data-value
 *       切換其中 .pane[data-value] 的顯示（找不到相符就顯示 data-value="*"）
 *     - 每次切換派發 CustomEvent('uichange',{detail:{group,value}}) 到 document
 *
 *  2. 側邊抽屜選單：#menuBtn / #navFab 開，#drClose / backdrop / Esc 關
 *
 *  3. <details class="expand"> 展開時把內容捲進視窗
 *  4. .datescrub 選中那顆自動置中
 *
 *  5. 盤中即時：交易時段自動抓 intraday-data 分支的 intraday.json，
 *     - body 加 class "mkt-live"（顯示品牌旁的即時點、.live-badge）
 *     - 覆寫 [data-live="taiex"]（值）與 [data-live="taiex-chg"]（漲跌）
 *     每 40 秒更新一次；收盤 / 抓不到就靜默不動，維持靜態的盤後數字。
 */
(function () {
  "use strict";
  var doc = document;
  var REL = window.__rel || "";

  /* ---- 1. seg / chips 切換 ---- */
  function panesFor(group) {
    var sel = "[data-panes='" + group + "']:not([data-group])";
    return doc.querySelector(sel) || doc.querySelector("[data-panes='" + group + "']");
  }
  function applyPanes(container, value) {
    if (!container) return;
    var panes = container.querySelectorAll(".pane");
    var exact = Array.prototype.some.call(panes, function (p) { return p.dataset.value === value; });
    panes.forEach(function (pane) {
      pane.hidden = !(pane.dataset.value === value || (!exact && pane.dataset.value === "*"));
    });
  }
  function wireGroup(group) {
    var buttons = doc.querySelectorAll(
      ".seg[data-group='" + group + "'] > button, .chips[data-group='" + group + "'] > button");
    if (!buttons.length) return;
    var container = panesFor(group);
    buttons.forEach(function (btn) {
      btn.addEventListener("click", function () {
        if (btn.disabled) return;
        if (btn.dataset.action) {            // 動作鈕：只派發事件，不改 .on 狀態
          doc.dispatchEvent(new CustomEvent("uichange", {
            detail: { group: group, value: btn.dataset.value || "", action: btn.dataset.action }
          }));
          return;
        }
        if (btn.classList.contains("on")) return;
        buttons.forEach(function (b) { if (!b.dataset.action) b.classList.toggle("on", b === btn); });
        var value = btn.dataset.value || btn.textContent.trim();
        applyPanes(container, value);
        doc.dispatchEvent(new CustomEvent("uichange", { detail: { group: group, value: value } }));
      });
    });
    var on = Array.prototype.find.call(buttons, function (b) { return b.classList.contains("on"); })
             || buttons[0];
    applyPanes(container, on.dataset.value || on.textContent.trim());
  }
  var groups = new Set();
  doc.querySelectorAll(".seg[data-group], .chips[data-group]").forEach(function (el) {
    groups.add(el.dataset.group);
  });
  groups.forEach(wireGroup);

  /* ---- 2. 側邊抽屜 ---- */
  var drawer = doc.getElementById("navDrawer");
  var backdrop = doc.getElementById("navBackdrop");
  if (drawer && backdrop) {
    var openD = function () { drawer.classList.add("open"); backdrop.classList.add("open"); };
    var closeD = function () { drawer.classList.remove("open"); backdrop.classList.remove("open"); };
    ["menuBtn", "navFab"].forEach(function (id) {
      var b = doc.getElementById(id);
      if (b) b.addEventListener("click", function () {
        drawer.classList.contains("open") ? closeD() : openD();
      });
    });
    var dc = doc.getElementById("drClose");
    if (dc) dc.addEventListener("click", closeD);
    backdrop.addEventListener("click", closeD);
    doc.addEventListener("keydown", function (e) { if (e.key === "Escape") closeD(); });
    drawer.addEventListener("click", function (e) { if (e.target.closest("a")) closeD(); });
  }

  /* ---- 3. expand 展開捲進視窗 ---- */
  doc.querySelectorAll("details.expand").forEach(function (d) {
    d.addEventListener("toggle", function () {
      if (d.open) {
        var body = d.querySelector(".expand-body");
        if (body && body.getBoundingClientRect().bottom > window.innerHeight) {
          d.scrollIntoView({ behavior: "smooth", block: "nearest" });
        }
      }
    });
  });

  /* ---- 4. datescrub 選中置中 ---- */
  doc.querySelectorAll(".datescrub").forEach(function (scrub) {
    var on = scrub.querySelector("button.on");
    if (on) on.scrollIntoView({ inline: "center", block: "nearest" });
    scrub.addEventListener("click", function (e) {
      var btn = e.target.closest("button");
      if (!btn) return;
      scrub.querySelectorAll("button").forEach(function (b) { b.classList.toggle("on", b === btn); });
      btn.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
    });
  });

  /* ---- 5. 報價：全站共用的一份 ----------------------------------------
   * 2026-09-08 合併：原本 intraday.html、_intraday_strip.html、ui.js 各有一份
   * 讀 intraday.json 的實作，只有 intraday.html 那份做了收盤價校正，所以熱力圖／
   * 評分／籌碼上的盤中條整晚掛著錯的數字（1516 川飛顯示 +9.76%，實際收盤 +2.79%）。
   * 全部收斂成 window.TWQuote，任何頁面都吃同一套規則。
   *
   * 三個必須一起看的事實：
   *  1. intraday.json 的 market_status 是**產檔當下**算的。收盤後檔案凍在 13:29:52，
   *     那個欄位會一直寫著 "open"——不能拿它判斷「現在是不是盤中」，要用 as_of 算年齡。
   *  2. 迴圈最後一次推送常停在 13:29，13:30 收盤集合競價往往成交在別的價位。
   *     薄量股差很多，所以資料一過期就要用 STOCK_DAY 的官方收盤價校正。
   *  3. 逐筆即時只能靠 Cloudflare Worker 中繼（MIS 對瀏覽器擋 CORS）。
   */
  var LIVE_SRC = "https://raw.githubusercontent.com/chen022208-cell/stock-report/"
               + "intraday-data/docs/data/intraday.json";
  var LIVE_LOCAL = REL + "data/intraday.json";
  var QUOTE_PROXY = "https://twse-quote.chen022208.workers.dev/";
  var STALE_MIN = 5;          // 資料超過幾分鐘就不算即時
  var LIVE_MS = 3000;         // 逐筆輪詢間隔
  var LIVE_MAX = 60;          // 跟 Worker 單次上限一致
  var fmtInt = function (n) { return Math.round(n).toLocaleString("en-US"); };

  function ageMin(asOf) {
    // as_of 是台北時間、沒有時區標記。用 +08:00 明確解析，不要讓瀏覽器
    // 依本地時區猜——不同時區的使用者會算出完全不同的年齡。
    var m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})/.exec(String(asOf || ""));
    if (!m) return Infinity;
    var t = Date.parse(m[1] + "-" + m[2] + "-" + m[3] + "T" + m[4] + ":" + m[5] + ":" + m[6] + "+08:00");
    return isFinite(t) ? (Date.now() - t) / 60000 : Infinity;
  }
  function isLive(d) {
    return !!d && d.market_status === "open" && ageMin(d.as_of) <= STALE_MIN;
  }
  function statusLabel(d) {
    var base = { open: "盤中", pre_open: "開盤前", closing: "尾盤", closed: "已收盤" };
    if (!d) return "";
    if (isLive(d)) return base[d.market_status] || d.market_status;
    var a = ageMin(d.as_of);
    if (d.market_status === "open" && isFinite(a)) {
      return "已收盤（資料 " + Math.round(a) + " 分鐘前）";
    }
    return base[d.market_status] || d.market_status || "";
  }

  function num(v) {
    var f = parseFloat(String(v == null ? "" : v).replace(/,/g, ""));
    return isFinite(f) ? f : null;
  }
  /* 把價／漲跌幅寫進已渲染的節點。各頁的 DOM 長得不一樣，用一串候選選擇器
     涵蓋：先找明確標記的 [data-q-pct]，再退回各頁既有的 class，最後才是表格欄位。 */
  function write(node, price, pct, why) {
    if (!node || pct == null) return false;
    var txt = (pct > 0 ? "+" : "") + pct.toFixed(2) + "%";
    var cls = pct >= 0 ? "up" : "down";
    var changed = false;
    var pctEl = node.querySelector("[data-q-pct]") || node.querySelector(".iv-chg")
             || node.querySelector(".ivs-chip .p") || node.querySelector(".p");
    if (pctEl && pctEl.textContent.trim() !== txt) {
      var base = pctEl.dataset.qBase || (pctEl.className || "").replace(/\b(up|down)\b/g, "").trim();
      pctEl.dataset.qBase = base;
      pctEl.className = (base + " " + cls).trim();
      pctEl.textContent = txt;
      pctEl.title = why || "";
      changed = true;
    }
    var priceEl = node.querySelector("[data-q-price]");
    if (!priceEl && node.tagName === "TR") {
      var td = node.querySelectorAll("td");
      if (td.length >= 2) priceEl = td[1];
    }
    if (priceEl && price != null && priceEl.textContent.trim() !== String(price)) {
      priceEl.textContent = price; priceEl.title = why || ""; changed = true;
    }
    return changed;
  }

  /* ── 官方收盤價校正（STOCK_DAY，唯一對瀏覽器開放 CORS 的證交所端點）── */
  var dayCache = {};
  function rocDate(iso) {
    var p2 = String(iso || "").slice(0, 10).split("-");
    return p2.length === 3 ? (Number(p2[0]) - 1911) + "/" + p2[1] + "/" + p2[2] : null;
  }
  function officialClose(code, iso) {
    var k = code + "@" + String(iso).slice(0, 10);
    if (dayCache[k] !== undefined) return Promise.resolve(dayCache[k]);
    var ymd = String(iso || "").slice(0, 10).replace(/-/g, ""), want = rocDate(iso);
    if (!/^\d{4}$/.test(code) || !ymd || !want) return Promise.resolve(null);
    return fetch("https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date="
                 + ymd + "&stockNo=" + code)
      .then(function (r) { return r.json(); })
      .then(function (j) {
        var row = (j.data || []).filter(function (x) { return x[0] === want; })[0], out = null;
        if (row) {
          var close = num(row[6]), chg = num(String(row[7]).replace(/[+X]/g, ""));
          if (close != null && chg != null && close - chg > 0) {
            out = { close: close, pct: chg / (close - chg) * 100 };
          }
        }
        dayCache[k] = out; return out;
      })
      .catch(function () { dayCache[k] = null; return null; });
  }
  /* nodes：[{el, code}]；一次最多 4 個併發，避免一口氣打幾十個請求 */
  function correctCloses(nodes, iso, onDone) {
    var q = (nodes || []).filter(function (n) { return n && n.code; });
    var i = 0, fixed = 0;
    function next() {
      if (i >= q.length) return Promise.resolve();
      var item = q[i++];
      return officialClose(item.code, iso).then(function (o) {
        if (o && write(item.el, o.close, o.pct, "已校正為證交所官方收盤價")) fixed++;
        return next();
      });
    }
    return Promise.all([next(), next(), next(), next()]).then(function () {
      if (onDone) onDone(fixed);
      return fixed;
    });
  }

  /* ── 逐筆即時（Worker 中繼 MIS）── */
  function exPrefix(ex) { return ex === "tse" ? "tse" : ex === "otc" ? "otc" : null; }
  function pollQuotes(nodes, onStatus) {
    // nodes：[{el, code, ex}]
    var list = (nodes || []).filter(function (n) { return n && /^\d{4}$/.test(n.code); });
    var noEx = !list.some(function (n) { return n.ex; });
    var cap = noEx ? Math.floor(LIVE_MAX / 2) : LIVE_MAX;
    var chans = [], byCode = {}, n = 0;
    for (var i = 0; i < list.length && n < cap; i++) {
      var it = list[i], pre = exPrefix(it.ex);
      if (!byCode[it.code]) {
        if (pre) chans.push(pre + "_" + it.code + ".tw");
        else if (noEx) chans.push("tse_" + it.code + ".tw", "otc_" + it.code + ".tw");
        else continue;                       // 有 ex 但是興櫃／未知 → MIS 查不到
        byCode[it.code] = []; n++;
      }
      byCode[it.code].push(it.el);
    }
    if (!chans.length) return Promise.resolve(0);
    return fetch(QUOTE_PROXY + "?ex_ch=" + encodeURIComponent(chans.join("|")), { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (j.closed) { if (onStatus) onStatus(0, "已收盤"); return 0; }
        var hit = 0, at = "";
        (j.msgArray || []).forEach(function (a) {
          var price = num(a.z);
          if (price == null || price <= 0) price = num(a.pz);   // 無成交退回參考價
          var prev = num(a.y);
          if (price == null || prev == null || prev <= 0) return;
          var pct = (price - prev) / prev * 100;
          (byCode[a.c] || []).forEach(function (el) {
            write(el, price, pct, "逐筆即時 " + (a.t || ""));
          });
          if (a.t) at = a.t;
          hit++;
        });
        if (onStatus) onStatus(hit, hit ? ("即時 " + hit + " 檔" + (at ? " · " + at : "")) : "即時報價無回應");
        return hit;
      })
      .catch(function () { if (onStatus) onStatus(0, "即時報價連線失敗"); return 0; });
  }
  /* getTargets 每次呼叫要回傳當下畫面上的 [{el, code, ex}]（名單會隨排行切換而變） */
  function startLive(key, getTargets, onStatus) {
    stopLive(key);
    var tick = function () { pollQuotes(getTargets(), onStatus); };
    tick();
    liveTimers[key] = setInterval(tick, LIVE_MS);
  }
  var liveTimers = {};
  function stopLive(key) {
    if (liveTimers[key]) { clearInterval(liveTimers[key]); delete liveTimers[key]; }
  }

  function fetchIntraday() {
    var bust = "?_=" + Date.now();
    return fetch(LIVE_SRC + bust, { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw 0; return r.json(); })
      .catch(function () {
        return fetch(LIVE_LOCAL + bust, { cache: "no-store" }).then(function (r) { return r.json(); });
      });
  }

  /* 證交所官方大盤收盤（FMTQIK）。跟 STOCK_DAY 同一個 host，實測同樣對瀏覽器
   * 開放 CORS，而且回得到當日官方收盤——這是「盤後 Routine 沒跑時，首頁大盤數字
   * 停在上一個交易日」的解法：不必等 Routine，前端自己就能補上正確的指數。
   * 回 {date:"YYYY-MM-DD", close, change, pct, turnover} 或 null。 */
  var idxCache;
  function officialIndex() {
    if (idxCache !== undefined) return Promise.resolve(idxCache);
    var d = new Date(Date.now() + 8 * 3600 * 1000);   // 用台北時間決定要抓哪一個月
    var ym = d.getUTCFullYear() + String(d.getUTCMonth() + 1).padStart(2, "0") + "01";
    return fetch("https://www.twse.com.tw/exchangeReport/FMTQIK?response=json&date=" + ym)
      .then(function (r) { return r.json(); })
      .then(function (j) {
        var rows = j.data || [], row = rows[rows.length - 1], out = null;
        if (row) {
          var p2 = String(row[0]).split("/");           // 民國 115/09/07
          var close = num(row[4]), chg = num(row[5]);
          if (p2.length === 3 && close != null) {
            var prev = (chg != null) ? close - chg : null;
            out = {
              date: (Number(p2[0]) + 1911) + "-" + p2[1] + "-" + p2[2],
              close: close, change: chg,
              pct: (prev && prev > 0 && chg != null) ? chg / prev * 100 : null,
              turnover: num(row[2])
            };
          }
        }
        idxCache = out; return out;
      })
      .catch(function () { idxCache = null; return null; });
  }

  window.TWQuote = {
    ageMin: ageMin, isLive: isLive, statusLabel: statusLabel,
    write: write, correctCloses: correctCloses,
    startLive: startLive, stopLive: stopLive, pollQuotes: pollQuotes,
    fetchIntraday: fetchIntraday, officialIndex: officialIndex, num: num,
    STALE_MIN: STALE_MIN, LIVE_MS: LIVE_MS
  };

  /* ---- 6. 首頁等頁面的 [data-live] 大盤覆寫 ---- */
  function paintIndexNums(value, pct, pts, asOfText) {
    doc.querySelectorAll('[data-live="taiex"]').forEach(function (el) {
      if (value != null) el.textContent = fmtInt(value);
    });
    doc.querySelectorAll('[data-live="taiex-chg"]').forEach(function (el) {
      if (pct == null) return;
      var up = pct >= 0;
      el.className = (el.dataset.liveBase || "") + " " + (up ? "up" : "down");
      el.textContent = (up ? "▲ " : "▼ ")
        + (pts != null ? Math.abs(pts).toFixed(2) + "（" : "")
        + (up ? "+" : "") + pct.toFixed(2) + "%" + (pts != null ? "）" : "");
    });
    if (asOfText != null) {
      doc.querySelectorAll('[data-live="as-of"]').forEach(function (el) {
        el.textContent = asOfText;
      });
    }
  }

  /* 盤後 Routine 沒跑時，靜態的大盤數字會停在上一個交易日。這裡拿證交所
     官方收盤補上——只覆蓋指數本身，並明講「其他數字仍是哪一天的」，
     不要讓人誤以為整張卡都更新了。 */
  function patchStaleIndex() {
    var card = doc.querySelector("[data-report-date]");
    if (!card) return;
    var pageDate = card.dataset.reportDate || "";
    officialIndex().then(function (o) {
      if (!o || !o.date || (pageDate && o.date <= pageDate)) return;
      paintIndexNums(o.close, o.pct, o.change, o.date.slice(5) + " 收盤");
      var note = doc.querySelector('[data-live="note"]');
      if (note) {
        note.textContent = "指數已更新為證交所 " + o.date + " 官方收盤"
          + (pageDate ? ("；同卡片其他數字仍是 " + pageDate + " 盤後報告的值（盤後排程尚未產出新的一份）") : "")
          + "。";
      }
    });
  }

  function paintLive(d) {
    if (!d || !d.taiex) return;
    // ⚠️ 用 isLive() 而不是 d.market_status——收盤後那個欄位永遠是 "open"，
    // 直接信它會把凍住的 13:29 指數一直當成現在的盤中值在畫。
    var live = isLive(d);
    doc.body.classList.toggle("mkt-live", live);
    if (!live) { patchStaleIndex(); return; }
    var t = d.taiex;
    var pts = (t.value != null && t.prev_close != null) ? (t.value - t.prev_close) : null;
    paintIndexNums(t.value, t.change_pct, pts, "盤中 " + (t.trade_time || d.as_of || ""));
  }
  if (doc.querySelector("[data-live]")) {
    var pull = function () { fetchIntraday().then(paintLive).catch(function () {}); };
    pull();
    setInterval(pull, 40000);
  }
})();
