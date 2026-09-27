/* Bloom Anyway — the landing page builder.
 *
 * The canvas is the page itself, drawn by the same macro the public page
 * uses. This file never builds block markup: when something structural
 * changes it hands the block back to the server and swaps in what comes
 * back, so a block can't look one way while it's being built and another
 * once it's out.
 *
 * Typing is the exception — that stays here, because a round trip per
 * keystroke would be miserable. Text is read back as innerText, never
 * innerHTML, so what reaches the server is words rather than markup.
 */
(function () {
  "use strict";

  var root = document.querySelector("[data-lp-editor]");
  if (!root) return;

  var canvas = root.querySelector("[data-canvas]");
  var panel = root.querySelector("[data-panel]");
  var panelBody = root.querySelector("[data-panel-body]");
  var panelTitle = root.querySelector("[data-panel-title]");
  var stateEl = root.querySelector("[data-state]");
  var titleInput = root.querySelector('input[name="title"]');
  var slugInput = root.querySelector('input[name="slug"]');
  var saveBtn = root.querySelector("[data-save]");
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
  var dirty = root.getAttribute("data-dirty") === "yes";
  var openBlockId = "";

  /* ---- little helpers ---- */

  function post(url, body, asForm) {
    var headers = { "X-CSRFToken": csrf, "X-Requested-With": "fetch" };
    if (!asForm) headers["Content-Type"] = "application/json";
    return fetch(url, {
      method: "POST",
      headers: headers,
      body: asForm ? body : JSON.stringify(body)
    }).then(function (resp) {
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
    return canvas.querySelector('[data-slot-id="' + id + '"]');
  }

  function textOf(el) {
    // innerText, not innerHTML: the page keeps words, never markup.
    var value = el.innerText || "";
    return value.replace(/ /g, " ").replace(/\s+$/, "");
  }

  function markDirty() {
    dirty = true;
    if (stateEl) {
      stateEl.textContent = stateEl.getAttribute("data-live") === "yes"
        ? "Live · unsaved changes" : "Draft · unsaved changes";
      stateEl.classList.add("is-dirty");
    }
  }

  function markClean(status) {
    dirty = false;
    if (stateEl) {
      stateEl.textContent = status || "Saved";
      stateEl.classList.remove("is-dirty");
    }
  }

  /* ---- reading the canvas back into state ---- */

  function readSlot(slot) {
    var block = blockById(slot.getAttribute("data-slot-id"));
    if (!block) return;

    Array.prototype.forEach.call(slot.querySelectorAll("[data-f]"), function (el) {
      if (el.closest("[data-item]")) return;   // belongs to a repeating item
      var key = el.getAttribute("data-f");
      if (block.fields && Object.prototype.hasOwnProperty.call(block.fields, key)) {
        block.fields[key] = textOf(el);
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
        base[el.getAttribute("data-f")] = textOf(el);
      });
      next.push(base);
    });
    block.items = next;
  }

  function syncFromDom() {
    Array.prototype.forEach.call(canvas.querySelectorAll("[data-slot]"), readSlot);
    // The canvas is the order of record; state follows it.
    var ordered = [];
    Array.prototype.forEach.call(canvas.querySelectorAll("[data-slot]"), function (slot) {
      var b = blockById(slot.getAttribute("data-slot-id"));
      if (b) ordered.push(b);
    });
    if (ordered.length === blocks.length) blocks = ordered;
  }

  /* ---- asking the server to redraw ---- */

  function redraw(block) {
    var slot = slotFor(block.id);
    return post(renderUrl, { block: block }).then(function (res) {
      if (!res.ok || !res.data.html) return;
      var holder = document.createElement("div");
      holder.innerHTML = res.data.html;
      var fresh = holder.firstElementChild;
      if (!fresh || !slot) return;
      slot.replaceWith(fresh);
      if (openBlockId === block.id) openPanel(block.id);
    });
  }

  function addBlock(type, afterId) {
    syncFromDom();
    return post(renderUrl, { type: type }).then(function (res) {
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
      fresh.scrollIntoView({ behavior: "smooth", block: "center" });
    });
  }

  /* ---- the side panel: everything that isn't typed on the page ---- */

  function fieldRow(labelText, control) {
    var wrap = document.createElement("label");
    wrap.className = "lp-ed__field";
    var span = document.createElement("span");
    span.textContent = labelText;
    wrap.appendChild(span);
    wrap.appendChild(control);
    return wrap;
  }

  function buildPanel(block) {
    var def = defs[block.type] || { fields: [] };
    panelBody.innerHTML = "";
    panelTitle.textContent = def.label || block.type;

    (def.fields || []).forEach(function (spec) {
      var key = spec.key;
      if (spec.kind === "line" || spec.kind === "rich") return;  // typed on the page

      if (spec.kind === "choice") {
        var select = document.createElement("select");
        (spec.options || []).forEach(function (opt) {
          var o = document.createElement("option");
          o.value = opt;
          o.textContent = opt.charAt(0).toUpperCase() + opt.slice(1);
          if (block.fields[key] === opt) o.selected = true;
          select.appendChild(o);
        });
        select.addEventListener("change", function () {
          syncFromDom();
          block.fields[key] = select.value;
          markDirty();
          redraw(block);
        });
        panelBody.appendChild(fieldRow(spec.label, select));
        return;
      }

      var input = document.createElement("input");
      input.type = "text";
      input.value = block.fields[key] || "";
      input.placeholder = spec.kind === "image"
        ? "https://… or upload below" : "/courses or https://…";
      input.addEventListener("change", function () {
        syncFromDom();
        block.fields[key] = input.value.trim();
        markDirty();
        redraw(block);
      });
      panelBody.appendChild(fieldRow(spec.label, input));

      if (spec.kind === "image") {
        var pick = document.createElement("button");
        pick.type = "button";
        pick.className = "btn btn--secondary btn--sm";
        pick.textContent = "Upload a picture";
        pick.addEventListener("click", function () { chooseImage(block, key); });
        var row = document.createElement("div");
        row.className = "lp-ed__field lp-ed__field--btn";
        row.appendChild(pick);
        panelBody.appendChild(row);
      }
    });

    var note = document.createElement("p");
    note.className = "field-help";
    note.textContent = "Words are changed on the page itself — click them.";
    panelBody.appendChild(note);
  }

  function openPanel(id) {
    var block = blockById(id);
    if (!block) return;
    openBlockId = id;
    buildPanel(block);
    panel.hidden = false;
    Array.prototype.forEach.call(canvas.querySelectorAll("[data-slot]"), function (s) {
      s.classList.toggle("is-open", s.getAttribute("data-slot-id") === id);
    });
  }

  function closePanel() {
    openBlockId = "";
    panel.hidden = true;
    Array.prototype.forEach.call(canvas.querySelectorAll(".is-open"), function (s) {
      s.classList.remove("is-open");
    });
  }

  /* ---- pictures ---- */

  var filePicker = null;

  function chooseImage(block, key) {
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
          window.alert(res.data.error || "That picture didn't go up.");
          return;
        }
        block.fields[key] = res.data.url;
        markDirty();
        redraw(block);
      });
    };
    filePicker.click();
  }

  /* ---- one listener for the whole canvas, so redraws need no rewiring ---- */

  root.addEventListener("click", function (e) {
    var target = e.target;

    if (target.closest("[data-panel-close]")) { closePanel(); return; }

    var add = target.closest("[data-add]");
    if (add) { addBlock(add.getAttribute("data-add")); return; }

    var slot = target.closest("[data-slot]");
    if (!slot) return;
    var id = slot.getAttribute("data-slot-id");
    var block = blockById(id);
    if (!block) return;

    if (target.closest("[data-settings]")) {
      if (openBlockId === id) closePanel(); else openPanel(id);
      return;
    }
    if (target.closest("[data-up]") || target.closest("[data-down]")) {
      var up = !!target.closest("[data-up]");
      syncFromDom();
      var i = indexOfId(id);
      var j = up ? i - 1 : i + 1;
      if (i < 0 || j < 0 || j >= blocks.length) return;
      blocks.splice(j, 0, blocks.splice(i, 1)[0]);
      var sibling = up ? slot.previousElementSibling : slot.nextElementSibling;
      if (sibling) {
        if (up) sibling.before(slot); else sibling.after(slot);
      }
      markDirty();
      slot.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    if (target.closest("[data-dupe]")) {
      syncFromDom();
      var copy = JSON.parse(JSON.stringify(block));
      copy.id = "b" + Math.random().toString(16).slice(2, 14);
      post(renderUrl, { block: copy }).then(function (res) {
        if (!res.ok || !res.data.html) return;
        var holder = document.createElement("div");
        holder.innerHTML = res.data.html;
        var fresh = holder.firstElementChild;
        if (!fresh) return;
        blocks.splice(indexOfId(id) + 1, 0, res.data.block);
        slot.insertAdjacentElement("afterend", fresh);
        markDirty();
      });
      return;
    }
    if (target.closest("[data-remove]")) {
      if (!window.confirm("Take this block off the page?")) return;
      syncFromDom();
      var at = indexOfId(id);
      if (at >= 0) blocks.splice(at, 1);
      if (openBlockId === id) closePanel();
      slot.remove();
      markDirty();
      return;
    }
    if (target.closest("[data-img-pick]")) {
      var picker = target.closest("[data-img]");
      if (picker) chooseImage(block, picker.getAttribute("data-img"));
      return;
    }
    if (target.closest("[data-img-clear]")) {
      var holder2 = target.closest("[data-img]");
      if (!holder2) return;
      syncFromDom();
      block.fields[holder2.getAttribute("data-img")] = "";
      markDirty();
      redraw(block);
      return;
    }
    if (target.closest("[data-item-add]")) {
      var def = defs[block.type] || {};
      var max = def.item_max || 0;
      syncFromDom();
      block.items = block.items || [];
      if (max && block.items.length >= max) {
        window.alert("That's as many as this block takes.");
        return;
      }
      block.items.push(JSON.parse(JSON.stringify(def.item_defaults || {})));
      markDirty();
      redraw(block);
      return;
    }
    if (target.closest("[data-item-drop]")) {
      var row = target.closest("[data-item]");
      if (!row) return;
      var rows = Array.prototype.slice.call(
        row.parentElement.querySelectorAll("[data-item]"));
      var at2 = rows.indexOf(row);
      syncFromDom();
      if (at2 >= 0 && block.items) block.items.splice(at2, 1);
      markDirty();
      redraw(block);
      return;
    }
  });

  /* ---- typing ---- */

  root.addEventListener("input", function (e) {
    if (e.target.closest && e.target.closest("[data-f]")) markDirty();
  });
  [titleInput, slugInput].forEach(function (el) {
    if (el) el.addEventListener("input", markDirty);
  });

  // Paste as words. Without this, pasting from a document brings its markup
  // with it, and the editor would send back something it can't store.
  root.addEventListener("paste", function (e) {
    var field = e.target.closest && e.target.closest("[data-f]");
    if (!field) return;
    e.preventDefault();
    var text = (e.clipboardData || window.clipboardData).getData("text/plain");
    document.execCommand("insertText", false, text);
  });

  /* ---- dragging a block to a new place ---- */

  var dragging = null;
  root.addEventListener("mousedown", function (e) {
    var grip = e.target.closest && e.target.closest("[data-drag]");
    if (!grip) return;
    var slot = grip.closest("[data-slot]");
    if (slot) slot.setAttribute("draggable", "true");
  });
  root.addEventListener("dragstart", function (e) {
    var slot = e.target.closest && e.target.closest("[data-slot]");
    if (!slot) return;
    dragging = slot;
    slot.classList.add("is-dragging");
    try { e.dataTransfer.setData("text/plain", slot.getAttribute("data-slot-id")); } catch (err) {}
    e.dataTransfer.effectAllowed = "move";
  });
  root.addEventListener("dragover", function (e) {
    if (!dragging) return;
    e.preventDefault();
    var over = e.target.closest && e.target.closest("[data-slot]");
    if (!over || over === dragging) return;
    var box = over.getBoundingClientRect();
    var after = (e.clientY - box.top) > box.height / 2;
    if (after) over.after(dragging); else over.before(dragging);
  });
  root.addEventListener("drop", function (e) { if (dragging) e.preventDefault(); });
  root.addEventListener("dragend", function () {
    if (!dragging) return;
    dragging.classList.remove("is-dragging");
    dragging.removeAttribute("draggable");
    dragging = null;
    syncFromDom();
    markDirty();
  });

  /* ---- saving ---- */

  function save() {
    syncFromDom();
    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = "Saving…"; }
    return post(saveUrl, {
      blocks: blocks,
      title: titleInput ? titleInput.value : undefined,
      slug: slugInput ? slugInput.value : undefined
    }).then(function (res) {
      if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = "Save changes"; }
      if (!res.ok) {
        window.alert((res.data && res.data.error) || "That didn't save. Try again.");
        return false;
      }
      // The server may have tidied the address (or given us a free one).
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
      save().then(function (ok) {
        if (ok) publishForm.submit();
      });
    });
  }

  window.addEventListener("beforeunload", function (e) {
    if (!dirty) return;
    e.preventDefault();
    e.returnValue = "";
  });

  // Ctrl/Cmd+S saves, because everybody tries it.
  document.addEventListener("keydown", function (e) {
    if ((e.metaKey || e.ctrlKey) && (e.key === "s" || e.key === "S")) {
      e.preventDefault();
      save();
    }
  });
})();
