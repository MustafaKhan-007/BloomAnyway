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
  var previewUrl = root.getAttribute("data-preview-url");
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
    scheduleAutosave();
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

  // Settings travel with the blocks. Changing the accent colour and then
  // pressing undo used to undo the last thing you did to a *block*, which
  // is a strange answer to "put that back".
  function state() {
    syncFromDom();
    return JSON.stringify({ blocks: blocks, settings: settings });
  }

  function snapshot() {
    history.push(state());
    if (history.length > 40) history.shift();
    future.length = 0;
    refreshUndoButtons();
  }

  function refreshUndoButtons() {
    if (undoBtn) undoBtn.disabled = history.length === 0;
    if (redoBtn) redoBtn.disabled = future.length === 0;
  }

  function restore(json) {
    var was = JSON.parse(json);
    blocks = was.blocks || [];
    settings = was.settings || settings;
    return redrawAll().then(function () {
      applyPageSettings();
      if (showingPage) buildPagePanel();
      markDirty();
    });
  }

  function undo() {
    if (!history.length) return;
    future.push(state());
    restore(history.pop()).then(refreshUndoButtons);
  }

  function redo() {
    if (!future.length) return;
    history.push(state());
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

  /* Every repeating row is stamped with where it started in the block's own
     list. Reading a row back used to pair DOM position with state position,
     which is the same thing right up until a row is dragged somewhere else
     — and then the words moved with the card while its picture and its link
     stayed behind, because those aren't typed on the page and were being
     taken from whatever used to be in that slot. */
  function stampItems(node) {
    if (!node) return node;
    Array.prototype.forEach.call(node.querySelectorAll("[data-item]"),
      function (row, i) { row.setAttribute("data-item-i", String(i)); });
    return node;
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
    Array.prototype.forEach.call(rows, function (row) {
      var base = {};
      var from = parseInt(row.getAttribute("data-item-i"), 10);
      var existing = block.items[isNaN(from) ? next.length : from];
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
    // Back in step with the list that was just rebuilt, so a second drag
    // before any redraw reads the right rows.
    stampItems(slot);
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
      return stampItems(holder.firstElementChild);
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
    // A line break is a space once the marks are gone. Reading textContent
    // straight off gave "an audience.You need a plan" — two sentences run
    // together at exactly the place the heading was split.
    tmp.innerHTML = String(raw).replace(/<br\s*\/?>/gi, " ");
    var text = (tmp.textContent || "").replace(/\s+/g, " ").trim();
    return text.length > 40 ? text.slice(0, 40).trim() + "…" : text;
  }

  /* Which slot is top and which is bottom, so the canvas hides the arrows
     that would do nothing — the list on the left has always done this, and
     a disabled-looking arrow in one place and a live one in the other for
     the same block reads as a bug. */
  function markEnds() {
    var slots = canvas.querySelectorAll("[data-slot]");
    Array.prototype.forEach.call(slots, function (s, i) {
      s.classList.toggle("is-first", i === 0);
      s.classList.toggle("is-last", i === slots.length - 1);
    });
  }

  function renderLayers() {
    markEnds();
    if (!layerList) return;
    // Rebuilding the list mid-drag would destroy the row being dragged.
    if (drag) return;
    layerList.innerHTML = "";
    blocks.forEach(function (block, i) {
      var def = defs[block.type] || {};
      var off = (block.fields || {}).visible === "hide";
      var li = document.createElement("li");
      li.className = "lp-ed__layer" + (block.id === selectedId ? " is-on" : "")
        + (off ? " is-off" : "");
      li.setAttribute("data-layer", block.id);
      li.setAttribute("draggable", "true");
      li.innerHTML =
        '<span class="lp-ed__layer-icon" aria-hidden="true"></span>' +
        '<span class="lp-ed__layer-text">' +
        '<span class="lp-ed__layer-name"></span>' +
        '<span class="lp-ed__layer-sub"></span></span>' +
        '<span class="lp-ed__layer-tools">' +
        '<button type="button" data-layer-hide aria-label="Show or hide"></button>' +
        '<button type="button" data-layer-up title="Move up" aria-label="Move up">&uarr;</button>' +
        '<button type="button" data-layer-down title="Move down" aria-label="Move down">&darr;</button>' +
        '<button type="button" data-layer-drop title="Delete" aria-label="Delete">&times;</button>' +
        "</span>";
      li.querySelector(".lp-ed__layer-icon").textContent = def.icon || "▦";
      li.querySelector(".lp-ed__layer-name").textContent = def.label || block.type;
      li.querySelector(".lp-ed__layer-sub").textContent =
        off ? "Hidden — " + blockSummary(block) : blockSummary(block);
      var eye = li.querySelector("[data-layer-hide]");
      eye.textContent = off ? "🚫" : "👁";
      eye.title = off ? "Show this block again" : "Hide this block from visitors";
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

  /* ``before`` is called just ahead of every write, ``onChange`` just
     after. Two hooks rather than one because undo needs the page as it was
     a moment ago: taking the snapshot in onChange recorded the change that
     had already happened, so pressing undo after picking a colour put back
     the colour you had just picked. */
  function controlsFor(specs, bag, onChange, container, before) {
    specs.forEach(function (spec) {
      var key = spec.key;
      function edit(write) {
        if (before) before();
        write();
        onChange(key);
      }
      if (spec.kind === "line" || spec.kind === "rich") {
        if (!/description/.test(key)) return;   // typed on the page itself
      }

      // Where the block sits across the page, and how much of it it
      // takes. The number moves while the slider does; the change is
      // taken once it is let go, so sliding from twelve to four is one
      // step to undo rather than eight.
      if (spec.kind === "span") {
        var top = spec.max || 12;
        if (key === "col_start") {
          var wide = parseInt(bag.col_span, 10);
          top = 13 - (isNaN(wide) ? 12 : Math.min(12, Math.max(1, wide)));
        }
        var slider = document.createElement("span");
        slider.className = "lp-ed__slider";
        var range = document.createElement("input");
        range.type = "range";
        range.min = String(spec.min || 1);
        range.max = String(Math.max(spec.min || 1, top));
        range.value = String(Math.min(top, parseInt(bag[key], 10) || 1));
        range.setAttribute("aria-label", spec.label);
        var out = document.createElement("output");
        function say() {
          out.textContent = key === "col_span"
            ? range.value + " of 12" : "column " + range.value;
        }
        say();
        range.addEventListener("input", say);
        range.addEventListener("change", function () {
          edit(function () { bag[key] = range.value; });
        });
        slider.appendChild(range);
        slider.appendChild(out);
        container.appendChild(fieldRow(spec.label, slider));
        if (key === "col_start" && top === 1) {
          range.disabled = true;
          var only = document.createElement("p");
          only.className = "field-help lp-ed__hint";
          only.textContent = "A block the full width of the page has only "
            + "one place to be. Make it narrower to move it across.";
          container.appendChild(only);
        }
        return;
      }

      // A colour of her own, past the six the theme offers. Two controls
      // for one value: the swatch to pick with, the box to paste a brand
      // hex into — and an empty box means "whatever the theme says", which
      // a colour well on its own has no way of expressing.
      if (spec.kind === "color") {
        var well = document.createElement("span");
        well.className = "lp-ed__colour";
        var swatch = document.createElement("input");
        swatch.type = "color";
        swatch.value = bag[key] || "#ffffff";
        swatch.setAttribute("aria-label", spec.label);
        var hex = document.createElement("input");
        hex.type = "text";
        hex.className = "lp-ed__hex";
        hex.placeholder = "theme";
        hex.maxLength = 7;
        hex.value = bag[key] || "";
        hex.setAttribute("aria-label", spec.label + " as a hex code");
        var clear = document.createElement("button");
        clear.type = "button";
        clear.className = "lp-ed__colour-clear";
        clear.textContent = "×";
        clear.title = "Back to the theme colour";
        function setColour(value) {
          edit(function () { bag[key] = value; });
          hex.value = value;
          if (value) swatch.value = value;
          clear.hidden = !value;
        }
        swatch.addEventListener("input", function () { setColour(swatch.value); });
        hex.addEventListener("change", function () {
          var typed = hex.value.trim().toLowerCase();
          if (typed && typed.charAt(0) !== "#") typed = "#" + typed;
          setColour(/^#(?:[0-9a-f]{3}|[0-9a-f]{6})$/.test(typed) ? typed : "");
        });
        clear.addEventListener("click", function () { setColour(""); });
        clear.hidden = !bag[key];
        well.appendChild(swatch);
        well.appendChild(hex);
        well.appendChild(clear);
        container.appendChild(fieldRow(spec.label, well));
        if (key === "text_color") {
          var note = document.createElement("p");
          note.className = "field-help lp-ed__hint";
          note.textContent = "Leave this empty and the words follow the "
            + "background on their own — light on a dark colour, dark on a "
            + "light one.";
          container.appendChild(note);
        }
        return;
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
            // Move the highlight here and now. Redrawing the block doesn't
            // rebuild this panel, so without this the pill you pressed did
            // the thing and the old one went on looking like the answer.
            Array.prototype.forEach.call(pills.children, function (other) {
              var on = other === b;
              other.classList.toggle("is-on", on);
              other.setAttribute("aria-pressed", on ? "true" : "false");
            });
            edit(function () { bag[key] = opt; });
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
        : (spec.kind === "url" ? "/courses or https://…"
           : (spec.kind === "anchor" ? "pricing" : ""));
      input.addEventListener("change", function () {
        edit(function () { bag[key] = input.value.trim(); });
      });
      container.appendChild(fieldRow(spec.label, input));

      if (spec.kind === "anchor") {
        var tip = document.createElement("p");
        tip.className = "field-help lp-ed__hint";
        tip.textContent = bag[key]
          ? 'Link a button to "#' + bag[key] + '" and it jumps here.'
          : "Name this section and a button anywhere on the page can jump "
            + "straight to it.";
        container.appendChild(tip);
      }

      if (spec.kind === "image") {
        var row = document.createElement("div");
        row.className = "lp-ed__field lp-ed__field--btn";
        var pick = document.createElement("button");
        pick.type = "button";
        pick.className = "btn btn--secondary btn--sm";
        pick.textContent = bag[key] ? "Replace picture" : "Upload a picture";
        pick.addEventListener("click", function () {
          chooseImage(function (url) { edit(function () { bag[key] = url; }); });
        });
        row.appendChild(pick);
        if (bag[key]) {
          var drop = document.createElement("button");
          drop.type = "button";
          drop.className = "btn btn--quiet btn--sm";
          drop.textContent = "Remove";
          drop.addEventListener("click", function () {
            edit(function () { bag[key] = ""; });
          });
          row.appendChild(drop);
        }
        container.appendChild(row);
      }
    });
  }

  function buildBlockPanel(block) {
    var def = defs[block.type] || { fields: [] };
    panelBody.innerHTML = "";
    panelTitle.textContent = def.label || block.type;

    controlsFor(def.fields || [], block.fields, function (key) {
      markDirty();
      redraw(block);
      // Narrower means fewer places it can start, so the other slider has
      // to be redrawn against the new range.
      if (key === "col_span") buildBlockPanel(block);
    }, panelBody, snapshot);

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
    }, panelBody, snapshot);
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
    var i = indexOfId(id);
    var j = up ? i - 1 : i + 1;
    // Checked before the snapshot, not after: pressing "up" on the top
    // block used to push a step onto the undo stack and then do nothing,
    // so undo had to be pressed twice to get anywhere.
    if (i < 0 || j < 0 || j >= blocks.length) return;
    snapshot();
    blocks.splice(j, 0, blocks.splice(i, 1)[0]);
    var slot = slotFor(id);
    var sibling = up ? slot.previousElementSibling : slot.nextElementSibling;
    if (sibling) { if (up) sibling.before(slot); else sibling.after(slot); }
    markDirty();
    renderLayers();
    slot.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  /* A block taken off the page without being thrown away. Keeping a
     section for next month used to mean deleting it and writing it again,
     so people kept a second copy of the whole page instead. */
  function toggleHidden(id) {
    var block = blockById(id);
    if (!block) return;
    snapshot();
    block.fields.visible = block.fields.visible === "hide" ? "show" : "hide";
    markDirty();
    redraw(block);
  }

  /* ---- Preview ----
     The canvas, redrawn by the server out of the same macro the public
     page uses with the editing hooks off. Not a likeness of the page: the
     page. Which is why it is asked for rather than made here — anything
     the browser assembled itself could be wrong in exactly the way a
     preview must never be. */

  var previewing = false;

  function setPreview(on) {
    var pane = root.querySelector("[data-preview-canvas]");
    var button = root.querySelector("[data-preview-toggle]");
    if (!pane) return;
    if (!on) {
      previewing = false;
      pane.hidden = true;
      pane.innerHTML = "";
      canvas.hidden = false;
      root.classList.remove("is-previewing");
      if (button) {
        button.textContent = "Preview";
        button.setAttribute("aria-pressed", "false");
      }
      fitCanvas();
      return;
    }
    syncFromDom();
    post(previewUrl, { blocks: blocks, settings: settings })
      .then(function (res) {
        if (!res.ok || !res.data.html) {
          window.alert("Couldn't draw the preview just now.");
          return;
        }
        previewing = true;
        pane.innerHTML = res.data.html;
        pane.hidden = false;
        canvas.hidden = true;
        root.classList.add("is-previewing");
        fitCanvas();
        if (button) {
          button.textContent = "Back to editing";
          button.setAttribute("aria-pressed", "true");
        }
      });
  }

  /* ---- what a visitor's screen does to this ----
     The canvas is the page, so narrowing the canvas is the honest way to
     ask the question: no second rendering that could disagree with the
     first, and the words stay editable at every width. */

  //: What each button means, in real screen pixels. The canvas is laid
  //: out at these widths and shrunk to fit, so the page's own rules about
  //: what stacks and what sits side by side answer the same way they will
  //: for whoever opens it.
  var DEVICE_WIDTH = { wide: 1280, tablet: 820, phone: 390 };
  var frame = root.querySelector("[data-device-frame]");
  var scaler = root.querySelector("[data-scaler]");
  var device = "wide";
  var zoom = "fit";

  function fitCanvas() {
    if (!frame || !scaler) return;
    var target = DEVICE_WIDTH[device] || DEVICE_WIDTH.wide;
    var room = frame.clientWidth;
    var k = zoom === "fit" && room ? Math.min(1, room / target) : 1;
    scaler.style.setProperty("--lp-w", target + "px");
    scaler.style.setProperty("--lp-k", String(k));
    // The transform draws the canvas smaller but leaves its box the size
    // it was, so the frame is told what the picture actually comes to or
    // there is a screen of nothing under the page.
    frame.style.height = Math.ceil(scaler.offsetHeight * k) + "px";
    frame.style.overflowX = k < 1 ? "hidden" : "auto";
  }

  if (window.ResizeObserver && scaler) {
    new window.ResizeObserver(function () { fitCanvas(); }).observe(scaler);
  }
  window.addEventListener("resize", fitCanvas);

  function setDevice(name) {
    device = DEVICE_WIDTH[name] ? name : "wide";
    if (frame) frame.setAttribute("data-device", device);
    Array.prototype.forEach.call(
      root.querySelectorAll("[data-set-device]"), function (b) {
        var on = b.getAttribute("data-set-device") === device;
        b.classList.toggle("is-on", on);
        b.setAttribute("aria-pressed", on ? "true" : "false");
      });
    fitCanvas();
    try { window.localStorage.setItem("lp-device", device); } catch (err) {}
  }

  function setZoom(name) {
    zoom = name === "full" ? "full" : "fit";
    Array.prototype.forEach.call(
      root.querySelectorAll("[data-set-zoom]"), function (b) {
        var on = b.getAttribute("data-set-zoom") === zoom;
        b.classList.toggle("is-on", on);
        b.setAttribute("aria-pressed", on ? "true" : "false");
      });
    fitCanvas();
    try { window.localStorage.setItem("lp-zoom", zoom); } catch (err) {}
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
    var pickDevice = t.closest("[data-set-device]");
    if (pickDevice) {
      setDevice(pickDevice.getAttribute("data-set-device"));
      return;
    }
    var pickZoom = t.closest("[data-set-zoom]");
    if (pickZoom) { setZoom(pickZoom.getAttribute("data-set-zoom")); return; }
    if (t.closest("[data-preview-toggle]")) { setPreview(!previewing); return; }
    // Nothing on the canvas is editable while the page is being previewed,
    // because what is on the canvas then is not the editor's markup.
    if (previewing && t.closest("[data-preview-canvas]")) return;

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
      if (t.closest("[data-layer-hide]")) { toggleHidden(lid); return; }
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
    if (t.closest("[data-hide]")) { toggleHidden(id); return; }
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

  /* ---- dragging ----

     Three things can be picked up, and all three go through here: a row in
     the list on the left, a block on the canvas, and one repeating item —
     a card, a number, a question — inside a block.

     Nothing is draggable="true" sitting still. A draggable ancestor stops
     you selecting the words inside it, and the words are the whole point of
     this canvas, so the attribute goes on when a grip is pressed and comes
     off the moment the drag is done with. The list on the left has no words
     to select, so its rows are draggable anywhere.

     Small things move live as you pass them, which reads as the thing
     itself moving. A block on the canvas is often taller than the screen,
     so shuffling it live would haul the page around under the pointer —
     those get a line showing where it will land instead. */

  var drag = null;
  var dropLine = null;

  function disarm() {
    Array.prototype.forEach.call(
      root.querySelectorAll('[data-slot][draggable="true"],'
                            + ' [data-item][draggable="true"]'),
      function (el) { el.removeAttribute("draggable"); });
  }

  root.addEventListener("mousedown", function (e) {
    var t = e.target;
    disarm();
    if (!t.closest) return;
    var itemGrip = t.closest("[data-item-drag]");
    if (itemGrip) {
      var row = itemGrip.closest("[data-item]");
      if (row) row.setAttribute("draggable", "true");
      return;
    }
    var grip = t.closest("[data-drag]");
    if (!grip) return;
    var slot = grip.closest("[data-slot]");
    if (slot) slot.setAttribute("draggable", "true");
  });

  // A press that never became a drag must not leave anything armed.
  document.addEventListener("mouseup", function () {
    window.setTimeout(function () { if (!drag) disarm(); }, 0);
  });

  // Down a column, or across a row? Cards sit three abreast and questions
  // sit one under another, and which it is decides whether the pointer's
  // left-of-centre or its above-centre means "in front of this one".
  function laidOutInRows(container, sel) {
    if (!container) return false;
    var kids = [];
    Array.prototype.forEach.call(container.children, function (el) {
      if (el.matches && el.matches(sel)) kids.push(el);
    });
    if (kids.length < 2) return false;
    var a = kids[0].getBoundingClientRect();
    var b = kids[1].getBoundingClientRect();
    return b.left > a.left + 1
      && Math.abs(b.top - a.top) < Math.max(8, a.height / 2);
  }

  function theLine() {
    if (!dropLine) {
      dropLine = document.createElement("div");
      dropLine.className = "lp-ed__dropline";
      dropLine.setAttribute("aria-hidden", "true");
    }
    return dropLine;
  }

  function clearLine() {
    if (dropLine && dropLine.parentNode) dropLine.remove();
  }

  /* ---- where on the page, not just how far down it ----

     The canvas is twelve columns wide. Dragging a block left or right
     picks which of them it starts at, and it keeps whatever width it
     already has, so a block can be put beside another one rather than
     only above or below it. The line that shows where it will land is
     drawn at that column and that width — it is the block's footprint,
     not a full-width rule, so what you are shown is what you get. */

  var COLUMNS = 12;

  function gridUnit() {
    var box = canvas.getBoundingClientRect();
    return box.width / COLUMNS;
  }

  function spanOf(block) {
    var n = parseInt((block.fields || {}).col_span, 10);
    return isNaN(n) ? COLUMNS : Math.min(COLUMNS, Math.max(1, n));
  }

  function startOf(block) {
    var n = parseInt((block.fields || {}).col_start, 10);
    var start = isNaN(n) ? 1 : Math.min(COLUMNS, Math.max(1, n));
    return Math.min(start, COLUMNS + 1 - spanOf(block));
  }

  // Which column the pointer is over, as a start for a block this wide.
  function columnUnderPointer(clientX, span) {
    var box = canvas.getBoundingClientRect();
    var unit = box.width / COLUMNS;
    // The pointer holds the middle of the block, which is where a hand
    // expects to be holding it.
    var left = clientX - box.left - (span * unit) / 2;
    var start = Math.round(left / unit) + 1;
    return Math.min(COLUMNS + 1 - span, Math.max(1, start));
  }

  function placeBlock(block, start, span) {
    block.fields.col_start = String(start);
    block.fields.col_span = String(span);
    var slot = slotFor(block.id);
    if (slot) {
      slot.style.gridColumn = (start === 1 && span === COLUMNS)
        ? "" : start + "/span " + span;
    }
  }

  /* ---- pulling a block narrower ----
     Not an HTML5 drag: that gesture reports where the pointer went, and
     what this needs is where it is, continuously, so the block can follow
     the hand a column at a time. */

  var sizing = null;

  root.addEventListener("pointerdown", function (e) {
    var grab = e.target.closest && e.target.closest("[data-grab-width]");
    if (!grab) return;
    var slot = grab.closest("[data-slot]");
    var block = slot && blockById(slot.getAttribute("data-slot-id"));
    if (!block) return;
    e.preventDefault();
    snapshot();
    sizing = { block: block, start: startOf(block), was: spanOf(block) };
    grab.setPointerCapture(e.pointerId);
    root.classList.add("is-sizing-block");
  });

  root.addEventListener("pointermove", function (e) {
    if (!sizing) return;
    var box = canvas.getBoundingClientRect();
    var edge = (e.clientX - box.left) / gridUnit();
    var span = Math.round(edge) - (sizing.start - 1);
    span = Math.min(COLUMNS + 1 - sizing.start, Math.max(1, span));
    if (span === spanOf(sizing.block)) return;
    placeBlock(sizing.block, sizing.start, span);
  });

  function endSizing() {
    if (!sizing) return;
    var was = sizing.was, block = sizing.block;
    sizing = null;
    root.classList.remove("is-sizing-block");
    if (spanOf(block) === was) {
      history.pop();               // nothing moved; don't litter undo
      refreshUndoButtons();
      return;
    }
    markDirty();
    renderLayers();
  }

  root.addEventListener("pointerup", endSizing);
  root.addEventListener("pointercancel", endSizing);

  root.addEventListener("dragstart", function (e) {
    var t = e.target;
    if (!t.closest) return;
    var found = null;
    // Nearest first: an item lives inside a slot, so an armed item wins
    // over the slot around it.
    var layer = t.closest("[data-layer]");
    var item = t.closest('[data-item][draggable="true"]');
    var slot = t.closest('[data-slot][draggable="true"]');
    if (layer) {
      found = { node: layer, kind: "layer", sel: "[data-layer]", home: layerList };
    } else if (item) {
      found = { node: item, kind: "item", sel: "[data-item]",
                home: item.parentElement };
    } else if (slot) {
      var held = blockById(slot.getAttribute("data-slot-id"));
      found = { node: slot, kind: "block", sel: "[data-slot]", home: canvas,
                span: held ? spanOf(held) : COLUMNS,
                column: held ? startOf(held) : 1 };
    }
    if (!found) return;
    // Take the page as it stands first, so undo puts the drag back.
    snapshot();
    found.rows = found.kind === "item"
      ? laidOutInRows(found.home, found.sel) : false;
    drag = found;
    drag.node.classList.add("is-dragging");
    root.classList.add("is-dragging-block");
    try { e.dataTransfer.setData("text/plain", "block"); } catch (err) {}
    e.dataTransfer.effectAllowed = "move";
  });

  root.addEventListener("dragover", function (e) {
    if (!drag) return;
    // Always allow the drop while something is in hand, or the pointer
    // shows "no" over the very gaps you are trying to drop into.
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";

    // Near the top or bottom of the window, keep the page moving — a page
    // is taller than a screen and otherwise a block can't reach the end.
    var edge = 90;
    if (e.clientY < edge) window.scrollBy(0, -18);
    else if (e.clientY > window.innerHeight - edge) window.scrollBy(0, 18);

    var over = e.target.closest && e.target.closest(drag.sel);
    if (!over || over === drag.node) return;
    // A card belongs to its own block, and a block to the canvas. Without
    // this a card could be dropped into the cards of a different block,
    // where the state behind it has nowhere to go.
    if (drag.home && over.parentElement !== drag.home) return;

    var box = over.getBoundingClientRect();
    var after = drag.rows
      ? (e.clientX - box.left) > box.width / 2
      : (e.clientY - box.top) > box.height / 2;

    if (drag.kind === "block") {
      if (after) over.after(theLine()); else over.before(theLine());
      // Sideways as well as down: the column the pointer is over is the
      // column the block will start at. The line is drawn at that column
      // and at the block's own width, so it is the block's footprint
      // rather than a rule across the page.
      drag.column = columnUnderPointer(e.clientX, drag.span);
      dropLine.style.gridColumn = drag.column + "/span " + drag.span;
      dropLine.classList.toggle("is-part", drag.span < COLUMNS);
      return;
    }
    if (after) over.after(drag.node); else over.before(drag.node);
  });

  root.addEventListener("drop", function (e) {
    if (drag) e.preventDefault();
  });

  // The move is made here rather than on drop, and dragend is the reason:
  // it always fires. A drop does not — let go a shade outside the canvas,
  // or over something the browser reads as no man's land, and the whole
  // gesture ends with dragend alone. Doing the work there means the block
  // goes where the line said it would every time, which is the promise the
  // line makes the moment it appears.
  root.addEventListener("dragend", function () {
    if (!drag) return;
    var done = drag;
    drag = null;
    if (done.kind === "block" && dropLine && dropLine.parentNode) {
      dropLine.replaceWith(done.node);
      var landed = blockById(done.node.getAttribute("data-slot-id"));
      if (landed && done.column) placeBlock(landed, done.column, done.span);
    }
    clearLine();
    done.node.classList.remove("is-dragging");
    root.classList.remove("is-dragging-block");
    disarm();

    if (done.kind === "layer") {
      // The list is the order of record; put the canvas in step behind it.
      var order = Array.prototype.map.call(
        layerList.querySelectorAll("[data-layer]"),
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
      // Blocks and items are both read straight back off the canvas: the
      // block order from the slots, and each block's items from the stamps
      // its rows carry with them.
      syncFromDom();
    }
    renderLayers();
    markDirty();
  });

  /* ---- saving ---- */

  function save(quiet) {
    syncFromDom();
    if (saveBtn && !quiet) {
      saveBtn.disabled = true;
      saveBtn.textContent = "Saving…";
    }
    return post(saveUrl, {
      blocks: blocks,
      settings: settings,
      title: titleInput ? titleInput.value : undefined,
      slug: slugInput ? slugInput.value : undefined
    }).then(function (res) {
      if (saveBtn && !quiet) {
        saveBtn.disabled = false;
        saveBtn.textContent = "Save changes";
      }
      if (!res.ok) {
        if (!quiet) {
          window.alert((res.data && res.data.error)
                       || "That didn't save. Try again.");
        }
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

  /* ---- saving on its own ----
     Only ever to the draft, which no visitor can reach, so the worst an
     autosave can do here is keep a half-written sentence somebody was
     going to finish anyway. Against that: a builder is where an afternoon
     goes, and a closed tab that takes it with it is the thing people
     never quite forgive.

     Quiet when it fails. A page left dirty is saved by the next keystroke,
     or by the Save button, or refused again at Publish — none of which
     needs a dialog in the middle of writing. */
  var autosaveTimer = null;

  function scheduleAutosave() {
    if (autosaveTimer) window.clearTimeout(autosaveTimer);
    // Two and a half seconds after the last change, so it waits for a
    // pause rather than firing between two keystrokes. The number is
    // written here rather than in a variable above because markDirty can
    // reach this function before the top of the file has finished running.
    autosaveTimer = window.setTimeout(function () {
      autosaveTimer = null;
      if (!dirty || busy || drag) return;
      save(true);
    }, 2500);
  }

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
    // Alt and an arrow moves whatever is selected. Dragging is quicker once
    // you know it is there, but it is also the one way of reordering a
    // keyboard can't do, and nudging one block past another is fiddlier
    // with a mouse than it looks.
    if (e.altKey && selectedId
        && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
      e.preventDefault();
      move(selectedId, e.key === "ArrowUp");
      return;
    }
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
  Array.prototype.forEach.call(canvas.querySelectorAll("[data-slot]"), stampItems);
  renderLayers();
  applyPageSettings();
  refreshUndoButtons();
  var lastDevice = "wide", lastZoom = "fit";
  try {
    lastDevice = window.localStorage.getItem("lp-device") || "wide";
    lastZoom = window.localStorage.getItem("lp-zoom") || "fit";
  } catch (err) {}
  setZoom(lastZoom);
  setDevice(lastDevice);
  fitCanvas();
})();
