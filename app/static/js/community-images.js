/* Bloom Anyway — community image compose previews + a zoomable in-page viewer. */
(function () {
  "use strict";

  /* ----------------------------- compose previews ----------------------------
     The "Add photos" label opens the (visually hidden) file input. We keep our
     own list of the chosen files so each can be removed, and so picking again
     adds to the set rather than replacing it, then write them back onto the
     input so they upload with the form. */
  function bytesLabel(n) {
    if (!n && n !== 0) return "";
    if (n < 1024 * 1024) return (n / 1024).toFixed(0) + " KB";
    return (n / (1024 * 1024)).toFixed(1) + " MB";
  }

  function initPicker(input) {
    if (input.dataset.ciPick === "1") return;
    input.dataset.ciPick = "1";
    var picker = input.closest("[data-community-picker]");
    if (!picker) return;
    var previews = picker.querySelector("[data-community-previews]");
    var max = parseInt(input.dataset.max || "4", 10) || 4;
    var chosen = [];

    function write() {
      if (chosen.length > max) chosen = chosen.slice(0, max);
      try {
        var dt = new DataTransfer();
        chosen.forEach(function (f) { dt.items.add(f); });
        input.files = dt.files;
      } catch (e) { /* older browsers: leave the native selection as-is */ }
      render();
    }

    function render() {
      if (!previews) return;
      previews.innerHTML = "";
      chosen.forEach(function (file, idx) {
        var url = URL.createObjectURL(file);
        var item = document.createElement("div");
        item.className = "ci-preview";

        var img = document.createElement("img");
        img.className = "ci-preview__img";
        img.src = url;
        img.alt = file.name || "Selected image";
        img.addEventListener("load", function () { URL.revokeObjectURL(url); });

        var rm = document.createElement("button");
        rm.type = "button";
        rm.className = "ci-preview__remove";
        rm.setAttribute("aria-label", "Remove " + (file.name || "image"));
        rm.innerHTML = "&times;";
        rm.addEventListener("click", function () {
          chosen.splice(idx, 1);
          write();
        });

        var cap = document.createElement("span");
        cap.className = "ci-preview__size";
        cap.textContent = bytesLabel(file.size);

        item.appendChild(img);
        item.appendChild(rm);
        item.appendChild(cap);
        previews.appendChild(item);
      });
      var over = picker.querySelector("[data-ci-note]");
      if (over) over.remove();
      if (chosen.length >= max) {
        var note = document.createElement("p");
        note.className = "field-help";
        note.setAttribute("data-ci-note", "1");
        note.style.margin = "6px 0 0";
        note.textContent = "That's the most you can add (" + max + ").";
        picker.appendChild(note);
      }
    }

    input.addEventListener("change", function () {
      var picked = Array.prototype.slice.call(input.files || []).filter(function (f) {
        return /^image\//.test(f.type);
      });
      if (!picked.length) return;
      chosen = chosen.concat(picked).slice(0, max);
      write();
    });
  }

  function initPickers(root) {
    (root || document).querySelectorAll("input.community-image-input")
      .forEach(initPicker);
  }

  /* ------------------------------- zoom viewer -------------------------------
     Clicking an attached image opens it in a panel over the page — dimmed, but
     framed and well short of the edges so it's clearly still Bloom Anyway —
     with zoom in/out (buttons or wheel) and drag-to-pan once zoomed. */
  var box = null;
  var stageImg = null;
  var scale = 1;
  var tx = 0;
  var ty = 0;
  var MIN = 1;
  var MAX = 6;
  var drag = null;

  function apply() {
    if (!stageImg) return;
    stageImg.style.transform =
      "translate(" + tx + "px," + ty + "px) scale(" + scale + ")";
    stageImg.style.cursor = scale > 1 ? "grab" : "auto";
  }

  function setZoom(next) {
    scale = Math.max(MIN, Math.min(MAX, next));
    if (scale === 1) { tx = 0; ty = 0; }
    apply();
  }

  function close() {
    if (!box) return;
    box.hidden = true;
    document.body.classList.remove("ci-lightbox-open");
    if (stageImg) stageImg.removeAttribute("src");
  }

  function ensureBox() {
    if (box) return box;
    box = document.createElement("div");
    box.className = "ci-lightbox";
    box.hidden = true;
    box.innerHTML =
      '<div class="ci-lightbox__backdrop" data-close></div>' +
      '<div class="ci-lightbox__panel" role="dialog" aria-modal="true" aria-label="Image viewer">' +
      '  <div class="ci-lightbox__stage" data-stage>' +
      '    <img class="ci-lightbox__img" alt="">' +
      '  </div>' +
      '  <div class="ci-lightbox__bar">' +
      '    <button type="button" class="ci-lightbox__btn" data-zoom-out aria-label="Zoom out">&minus;</button>' +
      '    <button type="button" class="ci-lightbox__btn" data-zoom-in aria-label="Zoom in">+</button>' +
      '    <button type="button" class="ci-lightbox__btn" data-reset>Reset</button>' +
      '    <button type="button" class="ci-lightbox__btn ci-lightbox__close" data-close>Close &times;</button>' +
      '  </div>' +
      '</div>';
    document.body.appendChild(box);
    stageImg = box.querySelector(".ci-lightbox__img");
    var stage = box.querySelector("[data-stage]");

    box.addEventListener("click", function (e) {
      if (e.target.closest("[data-close]")) { close(); return; }
      if (e.target.closest("[data-zoom-in]")) { setZoom(scale + 0.5); return; }
      if (e.target.closest("[data-zoom-out]")) { setZoom(scale - 0.5); return; }
      if (e.target.closest("[data-reset]")) { setZoom(1); return; }
    });

    stage.addEventListener("wheel", function (e) {
      e.preventDefault();
      setZoom(scale + (e.deltaY < 0 ? 0.3 : -0.3));
    }, { passive: false });

    // Double-click toggles between fit and 2x.
    stage.addEventListener("dblclick", function () {
      setZoom(scale > 1 ? 1 : 2);
    });

    // Drag to pan when zoomed in.
    stage.addEventListener("pointerdown", function (e) {
      if (scale <= 1) return;
      drag = { x: e.clientX, y: e.clientY, tx: tx, ty: ty };
      stageImg.style.cursor = "grabbing";
      stage.setPointerCapture(e.pointerId);
    });
    stage.addEventListener("pointermove", function (e) {
      if (!drag) return;
      tx = drag.tx + (e.clientX - drag.x);
      ty = drag.ty + (e.clientY - drag.y);
      apply();
    });
    function endDrag() { drag = null; if (stageImg) apply(); }
    stage.addEventListener("pointerup", endDrag);
    stage.addEventListener("pointercancel", endDrag);

    return box;
  }

  function open(src, alt) {
    ensureBox();
    scale = 1; tx = 0; ty = 0;
    stageImg.src = src;
    stageImg.alt = alt || "Attached image";
    apply();
    box.hidden = false;
    document.body.classList.add("ci-lightbox-open");
  }

  document.addEventListener("click", function (e) {
    var link = e.target.closest("a.forum-images__item, a[data-lightbox]");
    if (!link) return;
    e.preventDefault();
    var img = link.querySelector("img");
    open(link.getAttribute("href"), img ? img.alt : "");
  });

  document.addEventListener("keydown", function (e) {
    if (box && !box.hidden) {
      if (e.key === "Escape") close();
      else if (e.key === "+" || e.key === "=") setZoom(scale + 0.5);
      else if (e.key === "-") setZoom(scale - 0.5);
    }
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { initPickers(); });
  } else {
    initPickers();
  }
})();
