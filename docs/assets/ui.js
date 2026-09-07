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

  /* ---- 5. 盤中即時覆寫 ---- */
  var LIVE_SRC = "https://raw.githubusercontent.com/chen022208-cell/stock-report/"
               + "intraday-data/docs/data/intraday.json";
  var LIVE_LOCAL = REL + "data/intraday.json";
  var fmtInt = function (n) { return Math.round(n).toLocaleString("en-US"); };

  function paintLive(d) {
    if (!d || !d.taiex) return;
    var open = ["open", "pre_open", "closing"].indexOf(d.market_status) >= 0;
    doc.body.classList.toggle("mkt-live", open);
    if (!open) return;
    var t = d.taiex, pct = t.change_pct;
    var pts = (t.value != null && t.prev_close != null) ? (t.value - t.prev_close) : null;
    doc.querySelectorAll('[data-live="taiex"]').forEach(function (el) {
      if (t.value != null) el.textContent = fmtInt(t.value);
    });
    doc.querySelectorAll('[data-live="taiex-chg"]').forEach(function (el) {
      if (pct == null) return;
      var up = pct >= 0;
      el.className = (el.dataset.liveBase || "") + " " + (up ? "up" : "down");
      el.textContent = (up ? "▲ " : "▼ ")
        + (pts != null ? Math.abs(pts).toFixed(2) + "（" : "")
        + (up ? "+" : "") + pct.toFixed(2) + "%" + (pts != null ? "）" : "");
    });
    doc.querySelectorAll('[data-live="as-of"]').forEach(function (el) {
      el.textContent = "盤中 " + (t.trade_time || d.as_of || "");
    });
  }

  function pullLive() {
    var bust = "?_=" + Date.now();
    fetch(LIVE_SRC + bust, { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw 0; return r.json(); })
      .catch(function () { return fetch(LIVE_LOCAL + bust, { cache: "no-store" }).then(function (r) { return r.json(); }); })
      .then(paintLive)
      .catch(function () {});
  }
  // 只在有 [data-live] 標記的頁面才輪詢，省流量
  if (doc.querySelector("[data-live]")) {
    pullLive();
    setInterval(pullLive, 40000);
  }
})();
