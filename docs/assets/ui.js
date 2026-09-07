/* 全站互動層——漸進增強，關掉 JS 頁面仍可讀。
 *
 * 提供的模式（HTML 只要加 class ＋ data 屬性，不用寫 JS）：
 *
 *  1. 分段控制 / pill 切換
 *     <div class="seg" data-group="range">
 *       <button data-value="1m">1月</button>
 *       <button data-value="6m" class="on">半年</button>
 *     </div>
 *     - 點擊 → 該顆加 .on、同組其他移除
 *     - 若某個祖先有 [data-panes="range"]，會依 data-value 切換其中
 *       .pane[data-value="..."] 的顯示（找不到完全相符就顯示 data-value="*"）
 *     - 每次切換派發 CustomEvent('uichange', {detail:{group, value}}) 到 document，
 *       其他頁面腳本可以自己接來重畫圖表。
 *
 *  2. 浮動選單鈕（base.html already wired）：#navFab 開 / 關 #navSheet
 *
 *  3. <details class="expand"> 原生可用；這裡只補「點外面不會關」以外的小事：
 *     開啟時把內容捲進視窗（手機上展開長表格很有用）。
 *
 *  4. .datescrub 選中的那顆自動置中。
 */
(function () {
  "use strict";
  var doc = document;

  /* ---- 1. seg / chips 切換 ---- */
  function panesFor(el, group) {
    var p = el.closest("[data-panes='" + group + "']") ||
            doc.querySelector("[data-panes='" + group + "']");
    return p;
  }
  function applyPanes(container, value) {
    if (!container) return;
    var panes = container.querySelectorAll(".pane");
    var exact = false;
    panes.forEach(function (pane) {
      if (pane.dataset.value === value) exact = true;
    });
    panes.forEach(function (pane) {
      var show = pane.dataset.value === value || (!exact && pane.dataset.value === "*");
      pane.hidden = !show;
    });
  }
  function wireGroup(group) {
    var buttons = doc.querySelectorAll(
      ".seg[data-group='" + group + "'] > button, .chips[data-group='" + group + "'] > button");
    if (!buttons.length) return;
    var container = null;
    buttons.forEach(function (btn) {
      btn.addEventListener("click", function () {
        if (btn.disabled || btn.classList.contains("on")) return;
        buttons.forEach(function (b) { b.classList.toggle("on", b === btn); });
        var value = btn.dataset.value || btn.textContent.trim();
        container = container || panesFor(btn, group);
        applyPanes(container, value);
        doc.dispatchEvent(new CustomEvent("uichange", {
          detail: { group: group, value: value }
        }));
      });
    });
    // 初始：依已有 .on 的那顆同步一次面板
    var on = Array.prototype.find.call(buttons, function (b) {
      return b.classList.contains("on");
    }) || buttons[0];
    applyPanes(panesFor(on, group), on.dataset.value || on.textContent.trim());
  }
  var groups = new Set();
  doc.querySelectorAll(".seg[data-group], .chips[data-group]").forEach(function (el) {
    groups.add(el.dataset.group);
  });
  groups.forEach(wireGroup);

  /* ---- 2. 浮動選單 ---- */
  var fab = doc.getElementById("navFab");
  var sheet = doc.getElementById("navSheet");
  var backdrop = doc.getElementById("navBackdrop");
  if (fab && sheet && backdrop) {
    var open = function () { sheet.classList.add("open"); backdrop.classList.add("open"); };
    var close = function () { sheet.classList.remove("open"); backdrop.classList.remove("open"); };
    fab.addEventListener("click", function () {
      sheet.classList.contains("open") ? close() : open();
    });
    backdrop.addEventListener("click", close);
    doc.addEventListener("keydown", function (e) { if (e.key === "Escape") close(); });
    // 點選單裡的連結後把它收起來（連結本來就會導頁，收起只是視覺上乾淨）
    sheet.addEventListener("click", function (e) {
      if (e.target.closest("a")) close();
    });
  }

  /* ---- 3. expand 展開時捲進視窗 ---- */
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
      scrub.querySelectorAll("button").forEach(function (b) {
        b.classList.toggle("on", b === btn);
      });
      btn.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
    });
  });
})();
