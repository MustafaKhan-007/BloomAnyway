/* Bloom Anyway — the landing page builder.
 *
 * The canvas is the page itself, drawn by the same macro the public page
 * uses. This file never builds block markup: when something structural
 * changes it hands the block back to the server and swaps in what comes
 * back, so a block can't look one way while it's being built and another
 * once it's out.
 *
 * Typing and formatting are the exception — those stay here, because a round
 * trip per keystroke would be miserable. What gets sent back is innerHTML,
 * and the server cleans it against a small allow-list of marks before it is
 * stored and again before it is drawn.
 */
(function () {
  "use strict";

  var root = document.querySelector("[data-lp-editor]");
  if (!root) return;

  var canvas = root.querySelector("[data-canvas]");
  var panel = root.querySelector("[data-panel]");
  var panelBody = root.querySelector("[data-panel-body]");
  var panelTitle = root.querySelector("[data-panel-title]");
  var layerList = root.querySelector("[data-layers]");
  var picker = root.querySelector("[data-picker]");
  var formatBar = root.querySelector("[data-format]");
  var stateEl = root.querySelector("[data-state]");
  var titleInput = root.querySelector('input[name="title"]');
  var slugInput = root.querySelector('input[name="slug"]');
  var saveBtn = root.querySelector("[data-save]");
  var undoBtn = root.querySelector("[data-undo]");
  var redoBtn = root.querySelector("[data-redo]");
  var publishForm = root.querySelector("[data-publish-form]");
  var previewLink = root.querySelector("[data-preview]");
  var csrf = (document.body && document.body.getAttribute("data-csrf")) || "";

  var saveUrl = root.getAttribute("data-save-url");
  var renderUrl = root.getAttribute("data-render-url");
  var uploadUrl = root.getAttribute("data-upload-url");

  function readJsonTag(sel, fallback) {
    var el = document.querySelector(sel);
    if (!el) return fallback;
    try { return JSON.parse(el.textContent || ""); } catch (e) { return fallback; }
  }

  var blocks = readJsonTag("[data-lp-blocks]", []);
  var defs = readJsonTag("[data-lp-defs]", {});
  var pageState = readJsonTag("[data-lp-page]", { settings: {}, page_fields: [] });
  var settings = pageState.settings || {};
  var pageFields = pageState.page_fields || [];

  var dirty = false;
  var selectedId = "";
  var showingPage = false;
  var addAfterId = "";      // where the picker will insert
  var busy = false;

  /* ---- little helpers ---- */

  function post(url, body, asForm) {
    var headers = { "X-CSRFToken": csrf, "X-Requested-With": "fetch" };
    if (!asForm) headers["Content-Type"] = "application/json";
    return fetch(url, { method: "POST", headers: headers,
                        body: asForm ? body : JSON.stringify(body) })
      .then(function (resp) {
        return resp.json().catch(function () { return {}; })
          .then(function (data) { return { ok: resp.ok, data: data }; });
      });
  }

  function blockById(id) {
    for (var i = 0; i < blocks.length; i++) {
      if (blocks[i].id === id) return blocks[i];
    }
    return null;
  }

  function indexOfId(id) {
    for (var i = 0; i < blocks.length; i++) {
      if (blocks[i].id === id) return i;
    }
    return -1;
  }

  function slotFor(id) {
    return canvas.querySelector('[data-slot-id="' + CSS.escape(id) + '"]');
  }

  function newId() {
    return "b" + Math.random().toString(16).slice(2, 14);
  }

  function markDirty() {
    dirty = true;
    if (!stateEl) return;
    stateEl.textContent = stateEl.getAttribute("data-live") === "yes"
      ? "Live · unsaved" : "Draft · unsaved";
    stateEl.classList.add("is-dirty");
  }

  function markClean(status) {
    dirty = false;
    if (!stateEl) return;
    stateEl.textContent = status || "Saved";
    stateEl.classList.remove("is-dirty");
  }

  /* ---- undo ----
     A snapshot of the blocks before anything that changes their shape. Text
     typing is left to the browser's own undo inside the field. */

  var history = [];
  var future = [];

  function snapshot() {
    syncFromDom();
    history.push(JSON.stringify(blocks));
    if (history.length > 40) history.shift();
    future.length = 0;
    refreshUndoButtons();
  }

  function refreshUndoButtons() {
    if (undoBtn) undoBtn.disabled = history.length === 0;
    if (redoBtn) redoBtn.disabled = future.length === 0;
  }

  function restore(json) {
    blocks = JSON.parse(json);
    return redrawAll().then(function () { markDirty(); });
  }

  function undo() {
    if (!history.length) return;
    syncFromDom();
    future.push(JSON.stringify(blocks));
    restore(history.pop()).then(refreshUndoButtons);
  }

  function redo() {
    if (!future.length) return;
    syncFromDom();
    history.push(JSON.stringify(blocks));
    restore(future.pop()).then(refreshUndoButtons);
  }

  /* ---- reading the canvas back into state ---- */

  function fieldValue(el) {
    // innerHTML: the marks are the point. The server decides which survive.
    return (el.innerHTML || "")
      .replace(/ /g, " ")
      .replace(/<br\s*\/?>\s*$/i, "")
      .trim();
  }

  function readSlot(slot) {
    var block = blockById(slot.getAttribute("data-slot-id"));
    if (!block) return;

    Array.prototype.forEach.call(slot.querySelectorAll("[data-f]"), function (el) {
      if (el.closest("[data-item]")) return;   // belongs to a repeating item
      var key = el.getAttribute("data-f");
      if (block.fields && Object.prototype.hasOwnProperty.call(block.fields, key)) {
        block.fields[key] = fieldValue(el);
      }
    });

    if (!block.items) return;
    var rows = slot.querySelectorAll("[data-item]");
    var next = [];
    Array.prototype.forEach.call(rows, function (row, i) {
      var base = {};
      var existing = block.items[i];
      if (existing) {
        for (var k in existing) {
          if (Object.prototype.hasOwnProperty.call(existing, k)) base[k] = existing[k];
        }
      }
      Array.prototype.forEach.call(row.querySelectorAll("[data-f]"), function (el) {
        base[el.getAttribute("data-f")] = fieldValue(el);
      });
      next.push(base);
    });
    block.items = next;
  }

  function syncFromDom() {
    if (!canvas) return;
    Array.prototype.forEach.call(canvas.querySelectorAll("[data-slot]"), readSlot);
    var ordered = [];
    Array.prototype.forEach.call(canvas.querySelectorAll("[data-slot]"), function (slot) {
      var b = blockById(slot.getAttribute("data-slot-id"));
      if (b) ordered.push(b);
    });
    if (ordered.length === blocks.length) blocks = ordered;
  }

  /* ---- asking the server to redraw ---- */

  function fetchSlot(block) {
    return post(renderUrl, { block: block }).then(function (res) {
      if (!res.ok || !res.data.html) return null;
      var holder = document.createElement("div");
      holder.innerHTML = res.data.html;
      return holder.firstElementChild;
    });
  }

  function redraw(block) {
    var slot = slotFor(block.id);
    return fetchSlot(block).then(function (fresh) {
      if (!fresh || !slot) return;
      slot.replaceWith(fresh);
      if (selectedId === block.id) paintSelection();
      renderLayers();
    });
  }

  function redrawAll() {
    var jobs = blocks.map(function (b) { return fetchSlot(b); });
    return Promise.all(jobs).then(function (nodes) {
      canvas.innerHTML = "";
      nodes.forEach(function (n) { if (n) canvas.appendChild(n); });
      paintSelection();
      renderLayers();
    });
  }

  /* ---- the list of blocks down the left ---- */

  function blockSummary(block) {
    var f = block.fields || {};
    var raw = f.heading || f.eyebrow || f.quote || f.title || f.caption || "";
    var tmp = document.createElement("div");
    tmp.innerHTML = raw;
    var text = (tmp.textContent || "").trim();
    return text.length > 34 ? text.slice(0, 34) + "…" : text;
  }

  function renderLayers() {
    if (!layerList) return;
    // Rebuilding the list mid-drag would destroy the row being dragged.
    if (dragging) return;
    layerList.innerHTML = "";
    blocks.forEach(function (block, i) {
      var def = defs[block.type] || {};
      var li = document.createElement("li");
      li.className = "lp-ed__layer" + (block.id === selectedId ? " is-on" : "");
      li.setAttribute("data-layer", block.id);
      li.setAttribute("draggable", "true");
      li.innerHTML =
        '<span class="lp-ed__layer-icon" aria-hidden="true"></span>' +
        '<span class="lp-ed__layer-text">' +
        '<span class="lp-ed__layer-name"></span>' +
        '<span class="lp-ed__layer-sub"></span></span>' +
        '<span class="lp-ed__layer-tools">' +
        '<button type="button" data-layer-up title="Move up" aria-label="Move up">&uarr;</button>' +
        '<button type="button" data-layer-down title="Move down" aria-label="Move down">&darr;</button>' +
        '<button type="button" data-layer-drop title="Delete" aria-label="Delete">&times;</button>' +
        "</span>";
      li.querySelector(".lp-ed__layer-icon").textContent = def.icon || "▦";
      li.querySelector(".lp-ed__layer-name").textContent = def.label || block.type;
      li.querySelector(".lp-ed__layer-sub").textContent = blockSummary(block);
      if (i === 0) li.classList.add("is-first");
      if (i === blocks.length - 1) li.classList.add("is-last");
      layerList.appendChild(li);
    });
  }

  function paintSelection() {
    Array.prototype.forEach.call(canvas.querySelectorAll("[data-slot]"), function (s) {
      s.classList.toggle("is-on", s.getAttribute("data-slot-id") === selectedId);
    });
    Array.prototype.forEach.call(root.querySelectorAll("[data-layer]"), function (l) {
      l.classList.toggle("is-on", l.getAttribute("data-layer") === selectedId);
    });
  }

  /* ---- the side panel ---- */

  function fieldRow(labelText, control) {
    var wrap = document.createElement("label");
    wrap.className = "lp-ed__field";
    var span = document.createElement("span");
    span.textContent = labelText;
    wrap.appendChild(span);
    wrap.appendChild(control);
    return wrap;
  }

  function niceOption(value) {
    return value.charAt(0).toUpperCase() + value.slice(1).replace(/_/g, " ");
  }

  function controlsFor(specs, bag, onChange, container) {
    specs.forEach(function (spec) {
      var key = spec.key;
      if (spec.kind === "line" || spec.kind === "rich") {
        if (!/description/.test(key)) return;   // typed on the page itself
      }

      if (spec.kind === "choice") {
        var pills = document.createElement("div");
        pills.className = "lp-ed__pills";
        (spec.options || []).forEach(function (opt) {
          var b = document.createElement("button");
          b.type = "button";
          b.className = "lp-ed__pill" + (bag[key] === opt ? " is-on" : "");
          b.setAttribute("aria-pressed", bag[key] === opt ? "true" : "false");
          b.textContent = niceOption(opt);
          b.addEventListener("click", function () {
            bag[key] = opt;
            // Move the highlight here and now. Redrawing the block doesn't
            // rebuild this panel, so without this the pill you pressed did
            // the thing and the old one went on looking like the answer.
            Array.prototype.forEach.call(pills.children, function (other) {
              var on = other === b;
              other.classList.toggle("is-on", on);
              other.setAttribute("aria-pressed", on ? "true" : "false");
            });
            onChange();
          });
          pills.appendChild(b);
        });
        container.appendChild(fieldRow(spec.label, pills));
        return;
      }

      var input = document.createElement("input");
      input.type = "text";
      input.value = bag[key] || "";
      input.placeholder = spec.kind === "image"
        ? "https://… or upload below"
        : (spec.kind === "url" ? "/courses or https://…" : "");
      input.addEventListener("change", function () {
        bag[key] = input.value.trim();
        onChange();
      });
      container.appendChild(fieldRow(spec.label, input));

      if (spec.kind === "image") {
        var row = document.createElement("div");
        row.className = "lp-ed__field lp-ed__field--btn";
        var pick = document.createElement("button");
        pick.type = "button";
        pick.className = "btn btn--secondary btn--sm";
        pick.textContent = bag[key] ? "Replace picture" : "Upload a picture";
        pick.addEventListener("click", function () {
          chooseImage(function (url) { bag[key] = url; onChange(); });
        });
        row.appendChild(pick);
        if (bag[key]) {
          var clear = document.createElement("button");
          clear.type = "button";
          clear.className = "btn btn--quiet btn--sm";
          clear.textContent = "Remove";
          clear.addEventListener("click", function () { bag[key] = ""; onChange(); });
          row.appendChild(clear);
        }
        container.appendChild(row);
      }
    });
  }

  function buildBlockPanel(block) {
    var def = defs[block.type] || { fields: [] };
    panelBody.innerHTML = "";
    panelTitle.textContent = def.label || block.type;

    controlsFor(def.fields || [], block.fields, function () {
      snapshot();
      markDirty();
      redraw(block);
    }, panelBody);

    var note = document.createElement("p");
    note.className = "field-help";
    note.textContent = "Words are changed on the page itself — click them, "
      + "and select any of them to make them bold or a link.";
    panelBody.appendChild(note);

    var actions = document.createElement("div");
    actions.className = "lp-ed__panel-actions";
    [["Duplicate", function () { duplicate(block.id); }],
     ["Delete", function () { removeBlock(block.id); }]].forEach(function (pair) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "btn btn--quiet btn--sm";
      b.textContent = pair[0];
      b.addEventListener("click", pair[1]);
      actions.appendChild(b);
    });
    panelBody.appendChild(actions);
  }

  function buildPagePanel() {
    panelBody.innerHTML = "";
    panelTitle.textContent = "Whole page";
    controlsFor(pageFields, settings, function () {
      markDirty();
      applyPageSettings();
      buildPagePanel();
    }, panelBody);
    var note = document.createElement("p");
    note.className = "field-help";
    note.textContent = "These apply to every block on the page.";
    panelBody.appendChild(note);
  }

  function applyPageSettings() {
    var lp = canvas;
    if (!lp) return;
    lp.className = lp.className.replace(/\blp-(accent|font|page)--\S+/g, "").trim();
    lp.classList.add("lp");
    lp.classList.add("lp-accent--" + (settings.accent || "plum"));
    lp.classList.add("lp-font--" + (settings.font || "brand"));
    lp.classList.add("lp-page--" + (settings.width || "normal"));
  }

  function selectBlock(id) {
    selectedId = id;
    showingPage = false;
    var block = blockById(id);
    if (!block) return;
    buildBlockPanel(block);
    paintSelection();
  }

  function selectPage() {
    selectedId = "";
    showingPage = true;
    buildPagePanel();
    paintSelection();
  }

  function clearSelection() {
    selectedId = "";
    showingPage = false;
    panelTitle.textContent = "Nothing selected";
    panelBody.innerHTML =
      '<p class="field-help">Click any block on the page to change how it looks.</p>';
    paintSelection();
  }

  /* ---- pictures ---- */

  var filePicker = null;

  function chooseImage(done) {
    if (!filePicker) {
      filePicker = document.createElement("input");
      filePicker.type = "file";
      filePicker.accept = "image/*";
      filePicker.style.display = "none";
      document.body.appendChild(filePicker);
    }
    filePicker.value = "";
    filePicker.onchange = function () {
      var file = filePicker.files && filePicker.files[0];
      if (!file) return;
      var fd = new FormData();
      fd.append("image", file);
      syncFromDom();
      post(uploadUrl, fd, true).then(function (res) {
        if (!res.ok || !res.data.url) {
          window.alert((res.data && res.data.error) || "That picture didn't go up.");
          return;
        }
        done(res.data.url);
      });
    };
    filePicker.click();
  }

  /* ---- structural edits ---- */

  function addBlock(type, afterId) {
    if (busy) return;
    busy = true;
    snapshot();
    post(renderUrl, { type: type }).then(function (res) {
      busy = false;
      if (!res.ok || !res.data.html) return;
      var holder = document.createElement("div");
      holder.innerHTML = res.data.html;
      var fresh = holder.firstElementChild;
      if (!fresh) return;
      var at = afterId ? indexOfId(afterId) + 1 : blocks.length;
      blocks.splice(at, 0, res.data.block);
      var afterSlot = afterId ? slotFor(afterId) : null;
      if (afterSlot) afterSlot.insertAdjacentElement("afterend", fresh);
      else canvas.appendChild(fresh);
      markDirty();
      renderLayers();
      selectBlock(res.data.block.id);
      fresh.scrollIntoView({ behavior: "smooth", block: "center" });
    });
  }

  function duplicate(id) {
    var block = blockById(id);
    if (!block) return;
    snapshot();
    var copy = JSON.parse(JSON.stringify(block));
    copy.id = newId();
    post(renderUrl, { block: copy }).then(function (res) {
      if (!res.ok || !res.data.html) return;
      var holder = document.createElement("div");
      holder.innerHTML = res.data.html;
      var fresh = holder.firstElementChild;
      if (!fresh) return;
      blocks.splice(indexOfId(id) + 1, 0, res.data.block);
      var slot = slotFor(id);
      if (slot) slot.insertAdjacentElement("afterend", fresh);
      markDirty();
      renderLayers();
      selectBlock(res.data.block.id);
    });
  }

  function removeBlock(id) {
    var block = blockById(id);
    if (!block) return;
    var def = defs[block.type] || {};
    if (!window.confirm("Take the " + (def.label || "block").toLowerCase()
                        + " off the page?")) return;
    snapshot();
    var at = indexOfId(id);
    if (at >= 0) blocks.splice(at, 1);
    var slot = slotFor(id);
    if (slot) slot.remove();
    if (selectedId === id) clearSelection();
    markDirty();
    renderLayers();
  }

  function move(id, up) {
    snapshot();
    var i = indexOfId(id);
    var j = up ? i - 1 : i + 1;
    if (i < 0 || j < 0 || j >= blocks.length) return;
    blocks.splice(j, 0, blocks.splice(i, 1)[0]);
    var slot = slotFor(id);
    var sibling = up ? slot.previousElementSibling : slot.nextElementSibling;
    if (sibling) { if (up) sibling.before(slot); else sibling.after(slot); }
    markDirty();
    renderLayers();
    slot.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  /* ---- the block picker ---- */

  function openPicker(afterId) {
    addAfterId = afterId || "";
    picker.hidden = false;
    var first = picker.querySelector("[data-add]");
    if (first) first.focus();
  }

  function closePicker() { picker.hidden = true; }

  /* ---- formatting ---- */

  var activeField = null;

  function fieldOf(node) {
    if (!node) return null;
    var el = node.nodeType === 1 ? node : node.parentElement;
    return el ? el.closest("[data-f]") : null;
  }

  function showFormatBar() {
    var sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) { formatBar.hidden = true; return; }
    var field = fieldOf(sel.anchorNode);
    if (!field || !root.contains(field)) { formatBar.hidden = true; return; }
    activeField = field;
    var rect = sel.getRangeAt(0).getBoundingClientRect();
    if (!rect.width && !rect.height) { formatBar.hidden = true; return; }
    formatBar.hidden = false;
    var barW = formatBar.offsetWidth || 420;
    var left = Math.max(8, Math.min(
      window.innerWidth - barW - 8, rect.left + rect.width / 2 - barW / 2));
    var top = rect.top - formatBar.offsetHeight - 10;
    if (top < 8) top = rect.bottom + 10;
    formatBar.style.left = left + "px";
    formatBar.style.top = (top + window.scrollY) + "px";
    // A heading takes marks but not lists; say so by dimming what won't apply.
    var rich = field.hasAttribute("data-rich");
    Array.prototype.forEach.call(
      formatBar.querySelectorAll('[data-cmd$="List"]'), function (b) {
        b.disabled = !rich;
      });
  }

  // Pressing a button in the bar must not take the selection with it. The
  // browser moves focus on mousedown, which collapses what was selected, and
  // by the time the click arrives there is nothing left to embolden.
  if (formatBar) {
    formatBar.addEventListener("mousedown", function (e) { e.preventDefault(); });
  }

  function runCmd(cmd) {
    if (!activeField) return;
    document.execCommand(cmd, false, null);
    markDirty();
    window.setTimeout(showFormatBar, 0);
  }

  function wrapSelection(className) {
    var sel = window.getSelection();
    if (!sel || !sel.rangeCount || sel.isCollapsed || !activeField) return;
    var range = sel.getRangeAt(0);
    // Peel off any colour/size span already on this run, so the swatches
    // replace each other rather than nesting six deep.
    var existing = fieldOf(sel.anchorNode);
    var frag = range.extractContents();
    var plain = document.createElement("div");
    plain.appendChild(frag);
    Array.prototype.forEach.call(plain.querySelectorAll("span"), function (s) {
      if (/\blp-t--/.test(s.className)) {
        while (s.firstChild) s.parentNode.insertBefore(s.firstChild, s);
        s.remove();
      }
    });
    var node;
    if (className) {
      node = document.createElement("span");
      node.className = className;
      while (plain.firstChild) node.appendChild(plain.firstChild);
    } else {
      node = document.createDocumentFragment();
      while (plain.firstChild) node.appendChild(plain.firstChild);
    }
    range.insertNode(node);
    sel.removeAllRanges();
    if (existing) existing.normalize();
    markDirty();
    formatBar.hidden = true;
  }

  function addLink() {
    var sel = window.getSelection();
    if (!sel || sel.isCollapsed || !activeField) return;
    // Hold the range: a prompt takes the selection away with it.
    var saved = sel.getRangeAt(0).cloneRange();
    var url = window.prompt("Where should this link go?\n\nA path like /courses, "
                            + "or a full address like https://…");
    if (url === null) return;
    url = url.trim();
    activeField.focus();
    sel.removeAllRanges();
    sel.addRange(saved);
    if (!url) { document.execCommand("unlink", false, null); }
    else { document.execCommand("createLink", false, url); }
    markDirty();
    formatBar.hidden = true;
  }

  /* ---- one listener for the whole editor, so redraws need no rewiring ---- */

  root.addEventListener("click", function (e) {
    var t = e.target;

    if (t.closest("[data-panel-close]")) { clearSelection(); return; }
    if (t.closest("[data-picker-close]") || t === picker) { closePicker(); return; }
    if (t.closest("[data-page-settings]")) { selectPage(); return; }
    if (t.closest("[data-undo]")) { undo(); return; }
    if (t.closest("[data-redo]")) { redo(); return; }

    var addOpen = t.closest("[data-add-open]");
    if (addOpen) {
      openPicker(addOpen.hasAttribute("data-at-end") ? "" : selectedId);
      return;
    }
    var addHere = t.closest("[data-add-here]");
    if (addHere) {
      var hereSlot = addHere.closest("[data-slot]");
      openPicker(hereSlot ? hereSlot.getAttribute("data-slot-id") : "");
      return;
    }
    var pick = t.closest("[data-add]");
    if (pick) {
      closePicker();
      addBlock(pick.getAttribute("data-add"), addAfterId);
      return;
    }

    // formatting bar
    var cmd = t.closest("[data-cmd]");
    if (cmd) { e.preventDefault(); runCmd(cmd.getAttribute("data-cmd")); return; }
    var colour = t.closest("[data-colour]");
    if (colour) { e.preventDefault(); wrapSelection(colour.getAttribute("data-colour")); return; }
    if (t.closest("[data-link]")) { e.preventDefault(); addLink(); return; }
    if (t.closest("[data-unlink]")) { e.preventDefault(); runCmd("unlink"); return; }
    if (t.closest("[data-clear]")) { e.preventDefault(); runCmd("removeFormat"); return; }

    // the list down the left
    var layer = t.closest("[data-layer]");
    if (layer) {
      var lid = layer.getAttribute("data-layer");
      if (t.closest("[data-layer-up]")) { move(lid, true); return; }
      if (t.closest("[data-layer-down]")) { move(lid, false); return; }
      if (t.closest("[data-layer-drop]")) { removeBlock(lid); return; }
      selectBlock(lid);
      var target = slotFor(lid);
      if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }

    var slot = t.closest("[data-slot]");
    if (!slot) return;
    var id = slot.getAttribute("data-slot-id");
    var block = blockById(id);
    if (!block) return;

    if (t.closest("[data-settings]")) { selectBlock(id); return; }
    if (t.closest("[data-up]")) { move(id, true); return; }
    if (t.closest("[data-down]")) { move(id, false); return; }
    if (t.closest("[data-dupe]")) { duplicate(id); return; }
    if (t.closest("[data-remove]")) { removeBlock(id); return; }

    if (t.closest("[data-img-pick]")) {
      var holder = t.closest("[data-img]");
      var item = t.closest("[data-item]");
      if (!holder) return;
      var key = holder.getAttribute("data-img");
      chooseImage(function (url) {
        snapshot();
        if (item) {
          var rows = Array.prototype.slice.call(
            item.parentElement.querySelectorAll("[data-item]"));
          var at = rows.indexOf(item);
          if (block.items && block.items[at]) block.items[at][key] = url;
        } else {
          block.fields[key] = url;
        }
        markDirty();
        redraw(block);
      });
      return;
    }
    if (t.closest("[data-img-clear]")) {
      var holder2 = t.closest("[data-img]");
      var item2 = t.closest("[data-item]");
      if (!holder2) return;
      snapshot();
      var key2 = holder2.getAttribute("data-img");
      if (item2) {
        var rows2 = Array.prototype.slice.call(
          item2.parentElement.querySelectorAll("[data-item]"));
        var at2 = rows2.indexOf(item2);
        if (block.items && block.items[at2]) block.items[at2][key2] = "";
      } else {
        block.fields[key2] = "";
      }
      markDirty();
      redraw(block);
      return;
    }

    if (t.closest("[data-item-add]")) {
      var def = defs[block.type] || {};
      snapshot();
      block.items = block.items || [];
      if (def.item_max && block.items.length >= def.item_max) {
        window.alert("That's as many as this block takes.");
        return;
      }
      block.items.push(JSON.parse(JSON.stringify(def.item_defaults || {})));
      markDirty();
      redraw(block);
      return;
    }
    var itemBtn = t.closest("[data-item-drop], [data-item-up], [data-item-down], [data-item-dupe]");
    if (itemBtn) {
      var row = t.closest("[data-item]");
      if (!row || !block.items) return;
      var all = Array.prototype.slice.call(
        row.parentElement.querySelectorAll("[data-item]"));
      var at3 = all.indexOf(row);
      if (at3 < 0) return;
      snapshot();
      if (itemBtn.hasAttribute("data-item-drop")) {
        block.items.splice(at3, 1);
      } else if (itemBtn.hasAttribute("data-item-dupe")) {
        block.items.splice(at3 + 1, 0,
                           JSON.parse(JSON.stringify(block.items[at3])));
      } else {
        var to = itemBtn.hasAttribute("data-item-up") ? at3 - 1 : at3 + 1;
        if (to < 0 || to >= block.items.length) return;
        block.items.splice(to, 0, block.items.splice(at3, 1)[0]);
      }
      markDirty();
      redraw(block);
      return;
    }

    // Clicking anywhere else on a block selects it.
    selectBlock(id);
  });

  /* ---- typing ---- */

  root.addEventListener("input", function (e) {
    if (e.target.closest && e.target.closest("[data-f]")) {
      markDirty();
      renderLayers();
    }
  });
  [titleInput, slugInput].forEach(function (el) {
    if (el) el.addEventListener("input", markDirty);
  });

  // Paste as words. Without this, pasting from a document brings its markup
  // with it, and most of that is not markup we keep.
  root.addEventListener("paste", function (e) {
    var field = e.target.closest && e.target.closest("[data-f]");
    if (!field) return;
    e.preventDefault();
    var text = (e.clipboardData || window.clipboardData).getData("text/plain");
    document.execCommand("insertText", false, text);
  });

  // setTimeout, not requestAnimationFrame: a frame callback never runs while
  // the tab is in the background, which would leave the bar showing the last
  // selection when somebody came back to it. The nought is only to let the
  // selection settle before it is measured.
  document.addEventListener("selectionchange", function () {
    if (!formatBar) return;
    window.setTimeout(showFormatBar, 0);
  });
  root.addEventListener("keydown", function (e) {
    var field = e.target.closest && e.target.closest("[data-f]");
    if (!field) return;
    // A heading is one line; Enter in one would make a paragraph we then
    // have to throw away, so it just moves on instead.
    if (e.key === "Enter" && !field.hasAttribute("data-rich") && !e.shiftKey) {
      e.preventDefault();
      field.blur();
    }
  });

  /* ---- dragging, on the canvas and in the list ----
     A block can't simply be draggable="true" all the time: a draggable
     ancestor stops you selecting the text inside it, and the whole point of
     this canvas is that the words are editable. So the attribute goes on
     when the grip is pressed and comes off the moment the drag is over. */

  var dragging = null;

  function endDragArming() {
    Array.prototype.forEach.call(
      canvas.querySelectorAll('[data-slot][draggable="true"]'),
      function (s) { s.removeAttribute("draggable"); });
  }

  root.addEventListener("mousedown", function (e) {
    var grip = e.target.closest && e.target.closest("[data-drag]");
    endDragArming();
    if (!grip) return;
    var slot = grip.closest("[data-slot]");
    if (slot) slot.setAttribute("draggable", "true");
  });
  // A press that never became a drag must not leave the block armed.
  document.addEventListener("mouseup", function () {
    window.setTimeout(function () { if (!dragging) endDragArming(); }, 0);
  });

  root.addEventListener("dragstart", function (e) {
    var layer = e.target.closest && e.target.closest("[data-layer]");
    var slot = e.target.closest && e.target.closest('[data-slot][draggable="true"]');
    dragging = layer || slot;
    if (!dragging) return;
    dragging.classList.add("is-dragging");
    root.classList.add("is-dragging-block");
    try { e.dataTransfer.setData("text/plain", "block"); } catch (err) {}
    e.dataTransfer.effectAllowed = "move";
  });

  root.addEventListener("dragover", function (e) {
    if (!dragging) return;
    // Always allow the drop while a block is in hand, or the pointer shows
    // "no" over the very gaps you are trying to drop into.
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";

    // Near the top or bottom of the window, keep the page moving — a page
    // is taller than a screen and otherwise a block can't reach the end.
    var edge = 90;
    if (e.clientY < edge) window.scrollBy(0, -18);
    else if (e.clientY > window.innerHeight - edge) window.scrollBy(0, 18);

    var sel = dragging.hasAttribute("data-layer") ? "[data-layer]" : "[data-slot]";
    var over = e.target.closest && e.target.closest(sel);
    if (!over || over === dragging) return;
    var box = over.getBoundingClientRect();
    var after = (e.clientY - box.top) > box.height / 2;
    if (after) over.after(dragging); else over.before(dragging);
  });

  root.addEventListener("drop", function (e) { if (dragging) e.preventDefault(); });

  root.addEventListener("dragend", function () {
    if (!dragging) return;
    dragging.classList.remove("is-dragging");
    root.classList.remove("is-dragging-block");
    var wasLayer = dragging.hasAttribute("data-layer");
    dragging = null;
    endDragArming();
    if (wasLayer) {
      // The list is the order of record now; put the canvas in step.
      var order = Array.prototype.map.call(
        root.querySelectorAll("[data-layer]"),
        function (l) { return l.getAttribute("data-layer"); });
      syncFromDom();
      blocks.sort(function (a, b) {
        return order.indexOf(a.id) - order.indexOf(b.id);
      });
      order.forEach(function (id) {
        var s = slotFor(id);
        if (s) canvas.appendChild(s);
      });
    } else {
      syncFromDom();
    }
    renderLayers();
    markDirty();
  });

  /* ---- saving ---- */

  function save() {
    syncFromDom();
    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = "Saving…"; }
    return post(saveUrl, {
      blocks: blocks,
      settings: settings,
      title: titleInput ? titleInput.value : undefined,
      slug: slugInput ? slugInput.value : undefined
    }).then(function (res) {
      if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = "Save changes"; }
      if (!res.ok) {
        window.alert((res.data && res.data.error) || "That didn't save. Try again.");
        return false;
      }
      if (slugInput && res.data.slug) slugInput.value = res.data.slug;
      if (previewLink && res.data.slug) {
        previewLink.href = "/p/" + res.data.slug + "?preview=1";
      }
      markClean(res.data.status);
      return true;
    });
  }

  if (saveBtn) saveBtn.addEventListener("click", save);

  // Publishing sends what was saved, so save first or the press is a lie.
  if (publishForm) {
    publishForm.addEventListener("submit", function (e) {
      if (!dirty) return;
      e.preventDefault();
      save().then(function (ok) { if (ok) publishForm.submit(); });
    });
  }

  window.addEventListener("beforeunload", function (e) {
    if (!dirty) return;
    e.preventDefault();
    e.returnValue = "";
  });

  document.addEventListener("keydown", function (e) {
    var meta = e.metaKey || e.ctrlKey;
    if (!meta) {
      if (e.key === "Escape") { closePicker(); formatBar.hidden = true; }
      return;
    }
    var k = (e.key || "").toLowerCase();
    if (k === "s") { e.preventDefault(); save(); return; }
    if (k === "z") {
      var inField = document.activeElement
        && document.activeElement.closest
        && document.activeElement.closest("[data-f]");
      if (inField) return;          // let the browser undo the typing
      e.preventDefault();
      if (e.shiftKey) redo(); else undo();
    }
  });

  /* ---- go ---- */
  renderLayers();
  applyPageSettings();
  refreshUndoButtons();
})();
