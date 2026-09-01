/* Bloom Anyway — admin panel JS */
(function () {
  "use strict";

  /* ---- in-page confirm dialogs (no browser popups) ---- */
  (function () {
    var dialog = document.getElementById("site-confirm");
    if (!dialog) {
      dialog = document.createElement("dialog");
      dialog.id = "site-confirm";
      dialog.className = "site-confirm";
      dialog.setAttribute("aria-labelledby", "site-confirm-title");
      dialog.innerHTML =
        '<div class="site-confirm__panel">' +
        '<h2 id="site-confirm-title" data-confirm-title>Are you sure?</h2>' +
        '<p class="site-confirm__body" data-confirm-body></p>' +
        '<div class="site-confirm__actions">' +
        '<button type="button" class="btn btn--secondary btn--sm" data-confirm-cancel>Cancel</button>' +
        '<button type="button" class="btn btn--danger btn--sm" data-confirm-ok>Confirm</button>' +
        "</div></div>";
      document.body.appendChild(dialog);
    }

    var titleEl = dialog.querySelector("[data-confirm-title]");
    var bodyEl = dialog.querySelector("[data-confirm-body]");
    var okBtn = dialog.querySelector("[data-confirm-ok]");
    var cancelBtn = dialog.querySelector("[data-confirm-cancel]");
    var pendingForm = null;

    function openDialog(form) {
      pendingForm = form;
      titleEl.textContent = form.getAttribute("data-confirm-title") || "Are you sure?";
      bodyEl.textContent = form.getAttribute("data-confirm") || "Please confirm to continue.";
      okBtn.textContent = form.getAttribute("data-confirm-ok") || "Confirm";
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
    }

    function closeDialog() {
      pendingForm = null;
      if (typeof dialog.close === "function") dialog.close();
      else dialog.removeAttribute("open");
    }

    function dismissConfirm() {
      closeDialog();
      document.dispatchEvent(new CustomEvent("site-confirm-dismiss"));
    }

    cancelBtn.addEventListener("click", dismissConfirm);
    dialog.addEventListener("cancel", function () {
      pendingForm = null;
      document.dispatchEvent(new CustomEvent("site-confirm-dismiss"));
    });
    okBtn.addEventListener("click", function () {
      var form = pendingForm;
      pendingForm = null;
      if (typeof dialog.close === "function") dialog.close();
      else dialog.removeAttribute("open");
      if (!form) return;
      // Native submit skips the confirm interceptor (no submit event).
      // Show the page loader manually since submit listeners will not run.
      var loader = document.getElementById("page-loader");
      if (loader) {
        loader.hidden = false;
        loader.setAttribute("aria-hidden", "false");
        document.documentElement.classList.add("is-page-loading");
      }
      HTMLFormElement.prototype.submit.call(form);
    });

    document.addEventListener("submit", function (e) {
      var form = e.target;
      if (!form || form.tagName !== "FORM") return;
      if (!form.hasAttribute("data-confirm")) return;
      if (form.dataset.confirmAccepted === "1") {
        delete form.dataset.confirmAccepted;
        return;
      }
      e.preventDefault();
      openDialog(form);
    });
  })();

  /* ---- product modules: title, note, and as much content as you like ---- */
  (function () {
    var root = document.querySelector("[data-studio-modules]");
    if (!root) return;
    var list = root.querySelector("[data-modules-list]");
    var addBtn = root.querySelector("[data-modules-add]");
    if (!list || !addBtn) return;
    var max = parseInt(root.getAttribute("data-modules-max") || "12", 10) || 12;
    var accept = root.getAttribute("data-modules-accept") || "";
    var maxMb = root.getAttribute("data-upload-max") || "";
    var canChunk = !!root.getAttribute("data-upload-begin");
    // Field suffixes that carry the module number. Text extracts and lessons
    // repeat, so they are renamed together rather than one input at a time.
    var parts = ["title", "desc", "file", "up", "text_title", "text_body",
                 "lesson_title", "lesson_desc"];

    function optionEl(value, label) {
      var o = document.createElement("option");
      o.value = String(value);
      o.textContent = label;
      return o;
    }

    // Renumber the "Lesson N" labels after a lesson is added or removed, and
    // keep each lesson's own uploader pointed at its current position so a file
    // still lands in the lesson it was dropped under.
    function refreshLessons(row) {
      var mod = moduleNumberOf(row);
      Array.prototype.slice.call(row.querySelectorAll("[data-lesson-row]"))
        .forEach(function (lr, i) {
          var pos = i + 1;
          var num = lr.querySelector("[data-lesson-num]");
          if (num) num.textContent = "Lesson " + pos;
          var up = lr.querySelector("[data-chunk-upload]");
          if (up) up.setAttribute("data-lesson", String(pos));
          // Files uploaded into this lesson a moment ago follow it if the
          // lesson moves before the form is saved.
          lr.querySelectorAll("[data-lesson-pin]").forEach(function (pin) {
            pin.value = String(pos);
          });
          var input = lr.querySelector("[data-chunk-input]");
          if (input) input.id = "mod" + mod + "_l" + pos + "_up";
          var lab = lr.querySelector("label[for^='mod']");
          if (lab) lab.setAttribute("for", "mod" + mod + "_l" + pos + "_up");
        });
    }

    // The uploader markup that server-rendered lessons already have, so a
    // just-added lesson can take files straight away (edit mode only — a brand
    // new product has no id to upload against yet).
    function lessonUploader(mod, pos) {
      return '<ul class="module-items" data-module-items hidden></ul>' +
        '<div class="field chunk-up" data-chunk-upload data-module="' + mod +
        '" data-lesson="' + pos + '">' +
        '<label for="mod' + mod + '_l' + pos + '_up">Add videos, documents or ' +
        'images to this lesson</label>' +
        '<input type="file" id="mod' + mod + '_l' + pos + '_up" accept="' +
        accept + '" multiple data-chunk-input>' +
        '<p class="field-help">Up to ' + maxMb + ' MB each.</p>' +
        '<ul class="chunk-up__list" data-chunk-list hidden></ul></div>';
    }

    function moduleNumberOf(row) {
      return Array.prototype.indexOf.call(
        list.querySelectorAll("[data-module-row]"), row) + 1;
    }

    function renumber() {
      var rows = list.querySelectorAll("[data-module-row]");
      rows.forEach(function (row, i) {
        var n = i + 1;
        row.querySelectorAll("label").forEach(function (lab) {
          lab.innerHTML = lab.innerHTML.replace(/Module\s+\d+/i, "Module " + n);
        });
        row.querySelectorAll("input, textarea").forEach(function (inp) {
          var name = inp.getAttribute("name") || "";
          var id = inp.getAttribute("id") || "";
          parts.forEach(function (part) {
            var re = new RegExp("^mod\\d+_" + part + "$");
            if (re.test(name)) inp.name = "mod" + n + "_" + part;
            if (re.test(id)) inp.id = "mod" + n + "_" + part;
          });
        });
        row.querySelectorAll("label[for]").forEach(function (lab) {
          var f = lab.getAttribute("for") || "";
          parts.forEach(function (part) {
            if (new RegExp("^mod\\d+_" + part + "$").test(f)) {
              lab.setAttribute("for", "mod" + n + "_" + part);
            }
          });
        });
        row.querySelectorAll("[data-chunk-upload], [data-lessons], "
          + "[data-lesson-add], [data-module-intro]").forEach(function (el) {
          el.setAttribute("data-module", String(n));
        });
      });
      addBtn.hidden = rows.length >= max;
    }

    function addSection(n) {
      // A brand-new module isn't in the saved plan yet, so uploading now can't
      // know which module it belongs to. Server-rendered modules get real
      // uploaders (under each lesson and the module intro); a just-added one
      // asks for a save first.
      if (canChunk) {
        return '<p class="field-help">Save changes, then upload files into this '
          + "module's lessons and intro.</p>";
      }
      return '<div class="field">' +
        '<label for="mod' + n + '_file">Files for this module</label>' +
        '<input type="file" id="mod' + n + '_file" name="mod' + n +
        '_file" accept="' + accept + '" multiple>' +
        '<p class="field-help">Add module files here; upload into each lesson after saving.</p>' +
        "</div>";
    }

    addBtn.addEventListener("click", function () {
      var rows = list.querySelectorAll("[data-module-row]");
      if (rows.length >= max) return;
      var n = rows.length + 1;
      var row = document.createElement("div");
      row.className = "studio-modules__row";
      row.setAttribute("data-module-row", "");
      row.innerHTML =
        '<div class="form-row">' +
        '<div class="field">' +
        '<label for="mod' + n + '_title">Module ' + n + " title</label>" +
        '<input type="text" id="mod' + n + '_title" name="mod' + n +
        '_title" maxlength="160" value="">' +
        "</div>" +
        '<div class="field" style="flex:2;">' +
        '<label for="mod' + n + '_desc">Short note</label>' +
        '<input type="text" id="mod' + n + '_desc" name="mod' + n +
        '_desc" maxlength="500" value="">' +
        "</div></div>" +
        '<div class="studio-lessons" data-lessons data-module="' + n + '">' +
        '<p class="module-sub-label">Lessons <span class="field-help" ' +
        'style="font-weight:400;">— optional subsections that unlock together ' +
        'with the module. Each lesson has its own description and files.</span></p>' +
        '<div data-lessons-list></div>' +
        '<button type="button" class="btn btn--quiet btn--sm" data-lesson-add ' +
        'data-module="' + n + '">Add a lesson</button></div>' +
        '<div class="studio-module-intro" data-module-intro data-module="' + n + '">' +
        '<p class="module-sub-label">Module intro <span class="field-help" ' +
        'style="font-weight:400;">— files and notes shown before the lessons ' +
        '(optional)</span></p>' +
        (canChunk ? '<ul class="module-items" data-module-items hidden></ul>' : "") +
        addSection(n) +
        '<div class="module-texts" data-text-blocks></div>' +
        '<button type="button" class="btn btn--quiet btn--sm" data-text-add>' +
        "Add a written extract</button></div>";
      list.appendChild(row);
      renumber();
    });

    /* Lessons: subsections within a module, added and removed on the spot. */
    list.addEventListener("click", function (e) {
      var lessonAdd = e.target.closest("[data-lesson-add]");
      if (lessonAdd) {
        var lrow = lessonAdd.closest("[data-module-row]");
        var holder = lrow && lrow.querySelector("[data-lessons-list]");
        if (!holder) return;
        var mod = moduleNumberOf(lrow);
        var n = holder.querySelectorAll("[data-lesson-row]").length + 1;
        var block = document.createElement("div");
        block.className = "studio-lesson";
        block.setAttribute("data-lesson-row", "");
        block.innerHTML =
          '<div class="studio-lesson__head">' +
          '<span class="studio-lessons__num" data-lesson-num>Lesson ' + n + '</span>' +
          '<button type="button" class="btn btn--quiet btn--sm" data-lesson-remove>Remove</button>' +
          '</div>' +
          '<input type="text" name="mod' + mod + '_lesson_title" maxlength="160" ' +
          'placeholder="Lesson title">' +
          '<textarea name="mod' + mod + '_lesson_desc" rows="3" maxlength="8000" ' +
          'placeholder="Description or text extract for this lesson (optional)."></textarea>' +
          (canChunk ? lessonUploader(mod, n)
            : '<p class="field-help">Save changes, then upload files into this lesson.</p>');
        holder.appendChild(block);
        refreshLessons(lrow);
        var field = block.querySelector("input");
        if (field) field.focus();
        return;
      }
      var lessonRemove = e.target.closest("[data-lesson-remove]");
      if (lessonRemove) {
        var lrow2 = lessonRemove.closest("[data-module-row]");
        var blk2 = lessonRemove.closest("[data-lesson-row]");
        if (blk2) blk2.remove();
        if (lrow2) refreshLessons(lrow2);
        return;
      }
    });

    /* Written extracts: typed straight in, any number per module. */
    list.addEventListener("click", function (e) {
      var add = e.target.closest("[data-text-add]");
      if (add) {
        var row = add.closest("[data-module-row]");
        var holder = row && row.querySelector("[data-text-blocks]");
        if (!holder) return;
        var n = Array.prototype.indexOf.call(
          list.querySelectorAll("[data-module-row]"), row) + 1;
        var block = document.createElement("div");
        block.className = "module-texts__block";
        block.innerHTML =
          '<input type="text" name="mod' + n + '_text_title" maxlength="160" ' +
          'placeholder="What this extract is called">' +
          '<textarea name="mod' + n + '_text_body" rows="5" ' +
          'placeholder="Write it here. Buyers read this on the page — no file needed."></textarea>' +
          '<button type="button" class="btn btn--quiet btn--sm" data-text-remove>Remove</button>';
        holder.appendChild(block);
        var field = block.querySelector("input");
        if (field) field.focus();
        return;
      }
      var rm = e.target.closest("[data-text-remove]");
      if (rm) {
        var blk = rm.closest("[data-text-block], .module-texts__block");
        if (blk) blk.remove();
      }
    });

    renumber();
  })();

  /* ---- a coach's week of 1:1 hours, set in one go ----
     One table shows the whole week at once: every weekday is a column and every
     hour a row, so nothing is hidden behind day tabs. The whole week saves with
     a single submit and unticking is how an hour is taken away. */
  (function () {
    var root = document.querySelector("[data-sg-week]");
    if (!root) return;
    var grid = root.querySelector("[data-week-grid]");
    if (!grid) return;

    var cells = Array.prototype.slice.call(grid.querySelectorAll("[data-week-slot]"));
    var totalOut = root.querySelector("[data-week-total]");

    function daySlots(day) {
      return cells.filter(function (c) { return c.getAttribute("data-week-slot") === String(day); });
    }
    function hourSlots(hour) {
      return cells.filter(function (c) { return c.getAttribute("data-week-hour") === String(hour); });
    }

    function setCell(cell, on) {
      if (cell.checked !== on) cell.checked = on;
      var label = cell.closest(".sg-cell");
      if (label) label.classList.toggle("is-on", cell.checked);
    }

    function syncDayCount(day) {
      var badge = root.querySelector('[data-week-count="' + day + '"]');
      if (!badge) return;
      var n = 0;
      daySlots(day).forEach(function (c) { if (c.checked) n += 1; });
      badge.textContent = String(n);
      badge.hidden = n === 0;
    }

    function syncTotals() {
      for (var day = 0; day < 7; day += 1) syncDayCount(day);
      if (totalOut) {
        var n = cells.filter(function (c) { return c.checked; }).length;
        totalOut.textContent = n + (n === 1 ? " hour open / week" : " hours open / week");
      }
    }

    function toggleGroup(members) {
      var allOn = members.length && members.every(function (c) { return c.checked; });
      members.forEach(function (c) { setCell(c, !allOn); });
      syncTotals();
    }

    // Reflect server-rendered checked state into the boxes, then count.
    cells.forEach(function (c) { setCell(c, c.checked); });
    syncTotals();

    // Drag to paint many cells: mousedown seeds the first cell, mouseover paints
    // the rest with the same on/off value, and the trailing click is swallowed so
    // nothing double-toggles. Keyboard (space) has no mousedown, so its click acts.
    var painting = false;
    var paintOn = true;
    var startCell = null;

    function cellFrom(target) {
      if (!target || !target.closest) return null;
      var label = target.closest(".sg-cell");
      return label ? label.querySelector("[data-week-slot]") : null;
    }

    grid.addEventListener("mousedown", function (e) {
      if (e.button !== 0) return;
      var cell = cellFrom(e.target);
      if (!cell) return;
      painting = true;
      startCell = cell;
      paintOn = !cell.checked;
      setCell(cell, paintOn);
      syncTotals();
    });
    grid.addEventListener("mouseover", function (e) {
      if (!painting) return;
      var cell = cellFrom(e.target);
      if (!cell) return;
      setCell(cell, paintOn);
      syncTotals();
    });
    document.addEventListener("mouseup", function () {
      painting = false;
      setTimeout(function () { startCell = null; }, 0);
    });
    grid.addEventListener("click", function (e) {
      var cell = cellFrom(e.target);
      if (!cell) return;
      e.preventDefault();        // we own the checked state
      if (startCell) return;     // this click belongs to a mouse interaction
      setCell(cell, !cell.checked);
      syncTotals();
    });

    // Column (weekday) and row (hour) headers fill or clear a whole line.
    grid.addEventListener("click", function (e) {
      var col = e.target.closest ? e.target.closest("[data-week-col]") : null;
      if (col) { toggleGroup(daySlots(col.getAttribute("data-week-col"))); return; }
      var row = e.target.closest ? e.target.closest("[data-week-row]") : null;
      if (row) { toggleGroup(hourSlots(row.getAttribute("data-week-row"))); }
    });

    // Keyboard toggles (space/enter) fire "change" without a mousedown.
    grid.addEventListener("change", function (e) {
      var cell = e.target;
      if (!cell || !cell.matches || !cell.matches("[data-week-slot]")) return;
      setCell(cell, cell.checked);
      syncTotals();
    });

    var clearAll = root.querySelector("[data-week-clear-all]");
    if (clearAll) {
      clearAll.addEventListener("click", function () {
        cells.forEach(function (c) { setCell(c, false); });
        syncTotals();
      });
    }
    var workweek = root.querySelector("[data-week-workweek]");
    if (workweek) {
      workweek.addEventListener("click", function () {
        cells.forEach(function (c) {
          var day = parseInt(c.getAttribute("data-week-slot"), 10);
          var hour = parseInt(c.getAttribute("data-week-hour"), 10);
          if (day >= 0 && day <= 4 && hour >= 9 && hour < 17) setCell(c, true);
        });
        syncTotals();
      });
    }
  })();

  /* ---- "Log a session" form: show the fields for the chosen kind ----
     Moved out of an inline <script> so the page's Content Security Policy
     (which forbids inline script) doesn't silently block it. */
  (function () {
    var sel = document.getElementById("sg-kind");
    if (!sel) return;
    function sync() {
      var kind = sel.value;
      document.querySelectorAll("[data-sg-for]").forEach(function (el) {
        var show = el.getAttribute("data-sg-for") === kind;
        el.hidden = !show;
        el.querySelectorAll("input, select").forEach(function (input) {
          if (input.name === "coach" || input.name === "member_email") {
            input.required = show;
          }
        });
      });
    }
    sel.addEventListener("change", sync);
    sync();
  })();

  /* ---- extracts written for one particular file ----
     These hang off a saved file rather than a module, so they also appear
     among the loose files outside the module list. Wired from the document
     rather than from one container for that reason. */
  (function () {
    if (!document.querySelector("[data-note-add]")) return;
    document.addEventListener("click", function (e) {
      var add = e.target.closest("[data-note-add]");
      if (add) {
        var parent = add.getAttribute("data-parent");
        var holder = document.querySelector(
          '[data-note-holder][data-parent="' + parent + '"]');
        if (!holder) return;
        var block = document.createElement("div");
        block.className = "module-notes__block";
        block.setAttribute("data-note-block", "");
        block.innerHTML =
          '<input type="text" form="product-form" maxlength="160" name="newnote_' +
          parent + '_title" placeholder="What this extract is called">' +
          '<textarea form="product-form" rows="5" name="newnote_' + parent +
          '_body" placeholder="Write it here. Buyers read this beside the file — ' +
          'no upload needed."></textarea>' +
          '<button type="button" class="btn btn--quiet btn--sm" data-note-drop>' +
          "Remove</button>";
        holder.appendChild(block);
        var field = block.querySelector("input");
        if (field) field.focus();
        return;
      }
      var drop = e.target.closest("[data-note-drop]");
      if (drop) {
        var blk = drop.closest("[data-note-block]");
        if (blk) blk.remove();
      }
    });
  })();

  /* ---- course files uploaded a slice at a time ----
     Cloudflare rejects a request body over ~100 MB, so a lesson video cannot
     arrive in one piece. The file is cut up here and reassembled on the disk. */
  (function () {
    var root = document.querySelector("[data-studio-modules]");
    var beginUrl = root && root.getAttribute("data-upload-begin");
    if (!beginUrl) return;
    var chunkTpl = root.getAttribute("data-upload-chunk") || "";
    var finishTpl = root.getAttribute("data-upload-finish") || "";
    var maxMb = parseInt(root.getAttribute("data-upload-max") || "0", 10) || 0;
    var csrf = (document.body && document.body.getAttribute("data-csrf")) || "";

    function readJson(resp) {
      return resp.json().catch(function () { return {}; })
        .then(function (body) { return { ok: resp.ok, body: body }; });
    }

    function listItem(name) {
      var li = document.createElement("li");
      li.className = "chunk-up__item";
      li.innerHTML = '<span class="chunk-up__name"></span>' +
        '<span class="chunk-up__bar"><i></i></span>' +
        '<span class="chunk-up__state">Starting</span>';
      li.querySelector(".chunk-up__name").textContent = name;
      return li;
    }

    function showAdded(box, info) {
      // Land the new file in the list of the lesson (or module intro) it was
      // uploaded into, not a shared module list.
      var block = box.closest("[data-lesson-row], [data-module-intro]")
        || box.closest("[data-module-row]");
      var items = block ? block.querySelector("[data-module-items]") : null;
      if (!items) return;
      var li = document.createElement("li");
      li.className = "module-items__row";
      li.innerHTML = '<span class="module-items__kind"></span>' +
        '<span class="module-items__name"></span>' +
        '<span class="module-items__size"></span>';
      li.querySelector(".module-items__kind").textContent = info.kind_label || "File";
      li.querySelector(".module-items__name").textContent = info.title || "";
      li.querySelector(".module-items__size").textContent = info.size || "";
      // A lesson added just now doesn't exist server-side, so the upload
      // couldn't file the piece under it and left it in the module. Saying
      // where it was dropped lets the form save finish the job: by then the
      // lesson has been written, and the file is pinned to it.
      var lesson = box.getAttribute("data-lesson") || "";
      if (info.asset_id && lesson) {
        var pin = document.createElement("input");
        pin.type = "hidden";
        pin.name = "asset_" + info.asset_id + "_lesson";
        pin.value = lesson;
        pin.setAttribute("data-lesson-pin", "");
        li.appendChild(pin);
      }
      items.hidden = false;
      items.appendChild(li);
    }

    function send(file, box, listEl) {
      var li = listItem(file.name);
      listEl.hidden = false;
      listEl.appendChild(li);
      var bar = li.querySelector(".chunk-up__bar i");
      var state = li.querySelector(".chunk-up__state");
      var moduleNumber = box.getAttribute("data-module") || "";
      // Fixed per uploader now (one under each lesson, one for the module intro).
      var lessonNumber = box.getAttribute("data-lesson") || "";

      if (maxMb && file.size > maxMb * 1024 * 1024) {
        li.classList.add("is-error");
        state.textContent = "Over " + maxMb + " MB";
        return Promise.resolve();
      }

      return fetch(beginUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrf,
          "X-Requested-With": "fetch"
        },
        body: JSON.stringify({ filename: file.name, size: file.size })
      }).then(readJson).then(function (res) {
        if (!res.ok) throw new Error(res.body.error || "Could not start that upload.");
        var id = res.body.upload_id;
        var step = res.body.chunk_bytes || 8 * 1024 * 1024;
        var sent = 0;

        function nextSlice() {
          if (sent >= file.size) return done();
          var slice = file.slice(sent, Math.min(sent + step, file.size));
          var fd = new FormData();
          fd.append("chunk", slice);
          return fetch(chunkTpl.replace("UPLOAD_ID", id), {
            method: "POST",
            headers: { "X-CSRFToken": csrf, "X-Requested-With": "fetch" },
            body: fd
          }).then(readJson).then(function (cr) {
            if (!cr.ok) throw new Error(cr.body.error || "That upload stalled.");
            sent = cr.body.received;
            var pct = Math.min(100, Math.round((100 * sent) / file.size));
            bar.style.width = pct + "%";
            state.textContent = pct + "%";
            return nextSlice();
          });
        }

        function done() {
          state.textContent = "Saving";
          return fetch(finishTpl.replace("UPLOAD_ID", id), {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-CSRFToken": csrf,
              "X-Requested-With": "fetch"
            },
            body: JSON.stringify({
              filename: file.name,
              module: moduleNumber ? parseInt(moduleNumber, 10) : null,
              lesson: lessonNumber ? parseInt(lessonNumber, 10) : null
            })
          }).then(readJson).then(function (fr) {
            if (!fr.ok) throw new Error(fr.body.error || "Could not save that file.");
            bar.style.width = "100%";
            li.classList.add("is-done");
            state.textContent = "Added";
            showAdded(box, fr.body);
          });
        }

        return nextSlice();
      }).catch(function (err) {
        li.classList.add("is-error");
        state.textContent = err.message || "Upload failed.";
      });
    }

    document.addEventListener("change", function (e) {
      var input = e.target.closest("[data-chunk-input]");
      if (!input) return;
      var box = input.closest("[data-chunk-upload]");
      var listEl = box && box.querySelector("[data-chunk-list]");
      if (!box || !listEl) return;
      var files = Array.prototype.slice.call(input.files || []);
      input.value = "";
      // One at a time: the server appends slices to a single part file, and
      // parallel uploads would interleave into each other.
      files.reduce(function (chain, file) {
        return chain.then(function () { return send(file, box, listEl); });
      }, Promise.resolve());
    });

    window.addEventListener("beforeunload", function (e) {
      if (!document.querySelector(".chunk-up__item:not(.is-done):not(.is-error)")) return;
      e.preventDefault();
      e.returnValue = "";
    });
  })();

  /* ---- CSP-safe auto-submit selects ---- */
  document.querySelectorAll("select[data-autosubmit]").forEach(function (sel) {
    sel.addEventListener("change", function () {
      if (sel.form) sel.form.submit();
    });
  });

  /* ---- collapsible long tables (show a few rows, expand on demand) ---- */
  document.querySelectorAll("table[data-collapsible]").forEach(function (table) {
    var limit = parseInt(table.getAttribute("data-collapsible"), 10) || 10;
    var body = table.tBodies[0];
    if (!body) return;
    var rows = Array.prototype.slice.call(body.rows);
    if (rows.length <= limit) return;

    var hidden = rows.slice(limit);
    var collapse = function () {
      hidden.forEach(function (r) { r.hidden = true; });
    };
    collapse();

    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn--secondary btn--sm show-all-btn";
    var setLabel = function (expanded) {
      btn.textContent = expanded
        ? "Show fewer"
        : "Show all " + rows.length;
    };
    setLabel(false);
    btn.addEventListener("click", function () {
      var expanded = hidden[0] && hidden[0].hidden;
      hidden.forEach(function (r) { r.hidden = !expanded; });
      setLabel(expanded);
    });
    table.insertAdjacentElement("afterend", btn);
  });

  /* ---- dashboard charts (Chart.js from CDN) ---- */
  var dataEl = document.getElementById("dashboard-data");
  if (dataEl && window.Chart) {
    var data = JSON.parse(dataEl.textContent);
    var plum = "#7A2E62";
    var gold = "#c79a41";

    var signupsCtx = document.getElementById("chart-signups");
    if (signupsCtx && data.signups) {
      new Chart(signupsCtx, {
        type: "line",
        data: {
          labels: data.signups.labels,
          datasets: [
            { label: "Accounts", data: data.signups.users, borderColor: plum, tension: 0.3, borderWidth: 2 }
          ]
        },
        options: { plugins: { legend: { display: false } } }
      });
    }

    var purchasesCtx = document.getElementById("chart-purchases");
    if (purchasesCtx && data.purchases) {
      var purchaseChart = new Chart(purchasesCtx, {
        type: "line",
        data: {
          labels: data.purchases.labels || [],
          datasets: [
            {
              label: "All products",
              data: data.purchases.all || [],
              borderColor: plum,
              backgroundColor: "rgba(122, 46, 98, 0.12)",
              tension: 0.3,
              fill: true,
              borderWidth: 2,
              pointRadius: 0,
            }
          ]
        },
        options: {
          plugins: { legend: { display: false } },
          scales: {
            x: {
              ticks: {
                maxTicksLimit: 8,
                callback: function (val, i) {
                  var lab = this.getLabelForValue(val);
                  if (!lab) return "";
                  // Show month-day for readability
                  return String(lab).slice(5);
                }
              }
            },
            y: {
              beginAtZero: true,
              ticks: { precision: 0 }
            }
          }
        }
      });

      var filters = document.getElementById("purchase-filters");
      if (filters) {
        filters.addEventListener("click", function (e) {
          var btn = e.target.closest("[data-product]");
          if (!btn) return;
          filters.querySelectorAll(".studio-purchase-filter").forEach(function (b) {
            b.classList.toggle("is-active", b === btn);
          });
          var key = btn.getAttribute("data-product") || "all";
          var series = key === "all"
            ? (data.purchases.all || [])
            : ((data.purchases.by_product || {})[key] || []);
          var label = key === "all" ? "All products" : (btn.textContent || "Product");
          purchaseChart.data.datasets[0].data = series;
          purchaseChart.data.datasets[0].label = label;
          purchaseChart.data.datasets[0].borderColor = key === "all" ? plum : gold;
          purchaseChart.data.datasets[0].backgroundColor =
            key === "all" ? "rgba(122, 46, 98, 0.12)" : "rgba(199, 154, 65, 0.15)";
          purchaseChart.update();
        });
      }
    }
  }
  /* ---- bulk select + remove on Studio list pages ---- */
  (function () {
    function itemsFor(form) {
      var id = (form && form.id) || "";
      if (!id) return [];
      var out = [];
      var nodes = document.querySelectorAll("input[data-bulk-item]");
      for (var i = 0; i < nodes.length; i++) {
        var el = nodes[i];
        if (el.disabled) continue;
        // Prefer explicit form= association (items live outside the <form>).
        if (el.getAttribute("form") === id || form.contains(el)) {
          out.push(el);
        }
      }
      return out;
    }

    function refresh(form) {
      if (!form) return;
      var items = itemsFor(form);
      var checked = items.filter(function (el) { return el.checked; });
      var n = checked.length;
      var submit = form.querySelector("[data-bulk-submit]");
      var countEl = form.querySelector("[data-bulk-count]");
      var all = form.querySelector("[data-bulk-all]");
      if (submit) {
        submit.disabled = n === 0;
        var base = submit.getAttribute("data-label")
          || submit.textContent.replace(/\s*\(\d+\)\s*$/, "").trim()
          || "Remove selected";
        submit.setAttribute("data-label", base);
        submit.textContent = n ? base + " (" + n + ")" : base;
      }
      if (countEl) {
        countEl.hidden = n === 0;
        countEl.textContent = n + " selected";
      }
      if (all) {
        all.checked = items.length > 0 && n === items.length;
        all.indeterminate = n > 0 && n < items.length;
      }
      var tmpl = form.getAttribute("data-bulk-confirm")
        || "Remove {n} selected item(s)? This cannot be undone.";
      form.setAttribute("data-confirm", tmpl.replace(/\{n\}/g, String(n || 0)));
    }

    function formFor(el) {
      if (!el) return null;
      var host = el.closest ? el.closest("form[data-bulk]") : null;
      if (host) return host;
      var fid = el.getAttribute("form");
      if (fid) return document.getElementById(fid);
      return null;
    }

    function setAll(form, on) {
      itemsFor(form).forEach(function (el) { el.checked = !!on; });
      refresh(form);
    }

    // One delegated listener — survives any number of bulk forms on the page.
    document.addEventListener("change", function (e) {
      var t = e.target;
      if (!t || !t.matches) return;
      if (t.matches("input[data-bulk-all]")) {
        var form = formFor(t);
        if (!form) return;
        setAll(form, t.checked);
        return;
      }
      if (t.matches("input[data-bulk-item]")) {
        refresh(formFor(t));
      }
    });

    document.querySelectorAll("form[data-bulk]").forEach(function (form) {
      refresh(form);
      form.addEventListener("submit", function (e) {
        refresh(form);
        var selected = itemsFor(form).filter(function (el) { return el.checked; });
        if (selected.length === 0) {
          e.preventDefault();
          e.stopImmediatePropagation();
          return;
        }
        // Backup: some browsers are flaky with form= association across tables.
        // Mirror checked values as hidden inputs inside the bulk form.
        form.querySelectorAll("input[data-bulk-mirror]").forEach(function (n) {
          n.parentNode.removeChild(n);
        });
        selected.forEach(function (el) {
          var name = el.getAttribute("name") || "ids";
          var hidden = document.createElement("input");
          hidden.type = "hidden";
          hidden.name = name;
          hidden.value = el.value;
          hidden.setAttribute("data-bulk-mirror", "1");
          form.appendChild(hidden);
        });
      });
    });

    // FAQ summaries: clicking the checkbox must not toggle <details>.
    document.querySelectorAll("input.faq-item__bulk").forEach(function (cb) {
      cb.addEventListener("click", function (e) { e.stopPropagation(); });
    });
  })();
})();

  (function () {
    document.querySelectorAll("[data-tz-picker]").forEach(function (root) {
      var hidden = root.querySelector('input[type="hidden"][name="timezone"]');
      var search = root.querySelector(".tz-picker__search");
      var list = root.querySelector(".tz-picker__list");
      var chosen = root.querySelector("[data-tz-chosen] strong");
      var empty = root.querySelector("[data-tz-empty]");
      if (!hidden || !search || !list) return;

      var opts = Array.prototype.slice.call(root.querySelectorAll(".tz-picker__opt"));
      var groups = Array.prototype.slice.call(root.querySelectorAll("[data-tz-group]"));

      function openList() {
        list.hidden = false;
        search.setAttribute("aria-expanded", "true");
        root.classList.add("is-open");
      }

      function closeList() {
        list.hidden = true;
        search.setAttribute("aria-expanded", "false");
        root.classList.remove("is-open");
      }

      function filter(q) {
        var needle = (q || "").trim().toLowerCase();
        var any = false;
        opts.forEach(function (btn) {
          var hay = btn.getAttribute("data-search") || "";
          var show = !needle || hay.indexOf(needle) !== -1;
          btn.hidden = !show;
          if (show) any = true;
        });
        groups.forEach(function (g) {
          var visible = g.querySelectorAll(".tz-picker__opt:not([hidden])");
          g.hidden = visible.length === 0;
        });
        if (empty) empty.hidden = any;
      }

      function selectOpt(btn) {
        if (!btn) return;
        opts.forEach(function (b) {
          b.classList.remove("is-selected");
          b.setAttribute("aria-selected", "false");
        });
        btn.classList.add("is-selected");
        btn.setAttribute("aria-selected", "true");
        hidden.value = btn.getAttribute("data-value") || "";
        if (chosen) chosen.textContent = btn.getAttribute("data-label") || hidden.value;
        search.value = "";
        filter("");
        closeList();
      }

      search.addEventListener("focus", function () {
        openList();
        filter(search.value);
      });
      search.addEventListener("input", function () {
        openList();
        filter(search.value);
      });
      search.addEventListener("keydown", function (e) {
        if (e.key === "Escape") {
          closeList();
          search.blur();
        } else if (e.key === "Enter") {
          e.preventDefault();
          var first = root.querySelector(".tz-picker__opt:not([hidden])");
          if (first) selectOpt(first);
        }
      });

      list.addEventListener("mousedown", function (e) {
        // Keep focus while clicking options (prevents blur-before-click).
        e.preventDefault();
      });
      list.addEventListener("click", function (e) {
        var btn = e.target && e.target.closest ? e.target.closest(".tz-picker__opt") : null;
        if (btn) selectOpt(btn);
      });

      document.addEventListener("click", function (e) {
        if (!root.contains(e.target)) closeList();
      });
    });
  })();

/* ---- content tip: preview the member's reading page while writing ---- */
(function () {
  var panel = document.querySelector("[data-tip-preview]");
  var toggle = document.querySelector("[data-tip-preview-toggle]");
  if (!panel || !toggle) return;

  var titleField = document.getElementById("title");
  var summaryField = document.getElementById("description");
  var bodyField = document.getElementById("body");
  var videoField = document.getElementById("video_file");
  var thumbField = document.getElementById("thumb_file");
  var dropVideo = document.querySelector('input[name="remove_video"]');
  var dropThumb = document.querySelector('input[name="remove_thumb"]');
  var freeBox = document.querySelector('input[name="free_access"]');
  var healingBox = document.querySelector('input[name="healing_access"]');
  var hadVideo = panel.getAttribute("data-has-video") === "1";
  var savedVideo = panel.getAttribute("data-video-src") || "";
  var savedCover = panel.getAttribute("data-cover-src") || "";

  var outCat = panel.querySelector("[data-tip-preview-cat]");
  var outTitle = panel.querySelector("[data-tip-preview-title]");
  var outRead = panel.querySelector("[data-tip-preview-read]");
  var outVideo = panel.querySelector("[data-tip-preview-video]");
  var outLede = panel.querySelector("[data-tip-preview-lede]");
  var outBody = panel.querySelector("[data-tip-preview-body]");
  var outPlayer = panel.querySelector("[data-tip-preview-player]");
  var outPlayerEl = panel.querySelector("[data-tip-preview-video-el]");
  var outCover = panel.querySelector("[data-tip-preview-cover]");

  // Object URLs for files that only exist in the browser so far.
  var localUrls = {};

  function pickedFileUrl(key, field) {
    var file = field && field.files && field.files[0];
    if (!file) {
      if (localUrls[key]) {
        URL.revokeObjectURL(localUrls[key].url);
        delete localUrls[key];
      }
      return "";
    }
    if (localUrls[key] && localUrls[key].file === file) return localUrls[key].url;
    if (localUrls[key]) URL.revokeObjectURL(localUrls[key].url);
    localUrls[key] = { file: file, url: URL.createObjectURL(file) };
    return localUrls[key].url;
  }

  /** Set (or clear) a URL attribute. Returns true when it changed. */
  function setSrc(el, attr, url) {
    if ((el.getAttribute(attr) || "") === url) return false;
    if (url) el.setAttribute(attr, url);
    else el.removeAttribute(attr);
    return true;
  }

  function keepsVideo() {
    if (videoField && videoField.files && videoField.files.length) return true;
    return hadVideo && !(dropVideo && dropVideo.checked);
  }

  function videoSrc() {
    return pickedFileUrl("video", videoField)
      || (keepsVideo() ? savedVideo : "");
  }

  function coverSrc() {
    return pickedFileUrl("cover", thumbField)
      || (dropThumb && dropThumb.checked ? "" : savedCover);
  }

  function paint() {
    var title = (titleField && titleField.value || "").trim();
    outTitle.textContent = title || "Untitled tip";

    var summary = (summaryField && summaryField.value || "").trim();
    outLede.textContent = summary;
    outLede.hidden = !summary;

    // Match the reading page: newlines become line breaks, nothing is markup.
    var text = (bodyField && bodyField.value) || "";
    outBody.textContent = "";
    if (text.trim()) {
      text.split("\n").forEach(function (line, i, all) {
        outBody.appendChild(document.createTextNode(line));
        if (i < all.length - 1) outBody.appendChild(document.createElement("br"));
      });
    } else {
      outBody.textContent = "Nothing written yet.";
    }

    var words = text.trim() ? text.trim().split(/\s+/).length : 0;
    var minutes = words ? Math.max(1, Math.round(words / 200)) : 0;
    outRead.textContent = minutes ? minutes + " min read" : "";
    outRead.hidden = !minutes;

    outVideo.hidden = !keepsVideo();

    // Same order as the reading page: the video if there is one, otherwise
    // the cover on its own.
    var vsrc = videoSrc();
    var csrc = coverSrc();
    setSrc(outPlayerEl, "poster", csrc);
    if (setSrc(outPlayerEl, "src", vsrc)) {
      if (!vsrc) outPlayerEl.pause();
      outPlayerEl.load();
    }
    outPlayer.hidden = !vsrc;
    setSrc(outCover, "src", csrc);
    outCover.hidden = !csrc || !!vsrc;

    outCat.textContent = freeBox && freeBox.checked
      ? "Free pick"
      : (healingBox && healingBox.checked ? "Healing tip" : "Creator tip");
  }

  toggle.addEventListener("click", function () {
    var opening = panel.hidden;
    if (opening) paint();
    else outPlayerEl.pause();
    panel.hidden = !opening;
    toggle.setAttribute("aria-expanded", opening ? "true" : "false");
    toggle.textContent = opening ? "Hide preview" : "Preview";
    if (opening) panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  });

  [titleField, summaryField, bodyField, videoField, thumbField,
   dropVideo, dropThumb, freeBox, healingBox]
    .forEach(function (field) {
      if (!field) return;
      ["input", "change"].forEach(function (evt) {
        field.addEventListener(evt, function () {
          if (!panel.hidden) paint();
        });
      });
    });
})();

/* ---- inbox reply: show the email as it's being written ---- */
(function () {
  var panel = document.querySelector("[data-reply-preview]");
  if (!panel) return;

  var fields = {};
  ["sender", "subject", "preview", "header", "title", "body"].forEach(function (key) {
    fields[key] = document.querySelector('[data-reply-field="' + key + '"]');
  });

  var out = {};
  ["from", "subject", "preview", "header", "title", "body"].forEach(function (key) {
    out[key] = panel.querySelector('[data-reply-out="' + key + '"]');
  });

  function value(key) {
    return (fields[key] && fields[key].value || "").trim();
  }

  /** Placeholder text that reads as absent rather than as content. */
  function orDash(el, text, fallback) {
    el.textContent = text || fallback;
    el.classList.toggle("is-empty", !text);
  }

  function paint() {
    var picked = fields.sender && fields.sender.selectedOptions[0];
    out.from.textContent = picked
      ? picked.getAttribute("data-name") + " <" + picked.getAttribute("data-email") + ">"
      : "";

    var subject = value("subject");
    orDash(out.subject, subject, "No subject yet");
    // Blank preview text falls back to the subject, same as the send does.
    orDash(out.preview, value("preview") || subject, "No preview text");
    orDash(out.header, value("header"), "Bloom Anyway");
    orDash(out.title, value("title"), "No title yet");

    // The template renders plain text, so newlines are the only formatting.
    var text = (fields.body && fields.body.value) || "";
    out.body.textContent = "";
    if (text.trim()) {
      out.body.classList.remove("is-empty");
      text.split("\n").forEach(function (line, i, all) {
        out.body.appendChild(document.createTextNode(line));
        if (i < all.length - 1) out.body.appendChild(document.createElement("br"));
      });
    } else {
      out.body.classList.add("is-empty");
      out.body.textContent = "Nothing written yet.";
    }
  }

  Object.keys(fields).forEach(function (key) {
    if (!fields[key]) return;
    ["input", "change"].forEach(function (evt) {
      fields[key].addEventListener(evt, paint);
    });
  });
  paint();
})();

/* ---- product editor: show the cover as it's being set up ---- */
(function () {
  var pane = document.querySelector("[data-cover-preview]");
  if (!pane) return;

  var frame = pane.querySelector(".lib-card__cover");
  var titleOut = pane.querySelector(".lib-card__cover-title");
  var kindOut = pane.querySelector(".lib-card__cover-kind");
  var photo = pane.querySelector("[data-cover-photo]");
  if (!frame) return;

  var title = document.getElementById("title");
  var type = document.getElementById("type");
  var track = document.getElementById("track");
  var useAccent = document.getElementById("use_accent");
  var accent = document.getElementById("accent");
  var file = document.getElementById("cover") || document.getElementById("cover_file");

  // Mirrors Product.type_pill(). Once a reading file is attached the cover
  // takes its kind from that file instead, so the server pins it for us.
  var KINDS = {
    course: "COURSE", workbook: "WORKBOOK", guide: "GUIDE",
    audio: "AUDIO GUIDE", template: "TEMPLATE", bundle: "BUNDLE"
  };
  var fixedKind = pane.getAttribute("data-cover-kind-fixed");

  /** Mirrors Product.cover_color(). */
  function colour() {
    if (useAccent && useAccent.checked && accent && accent.value) return accent.value;
    return (track && track.value === "healing") ? "#5A3158" : "#C4A574";
  }

  function paint() {
    frame.style.setProperty("--lib-cover", colour());
    if (titleOut) {
      var text = ((title && title.value) || "").trim();
      titleOut.textContent = text || "Untitled";
      titleOut.classList.toggle("is-empty", !text);
    }
    if (kindOut && !fixedKind) {
      var kind = ((type && type.value) || "").toLowerCase();
      kindOut.textContent = KINDS[kind] || (kind ? kind.toUpperCase() : "GUIDE");
    }
  }

  [title, type, track, useAccent, accent].forEach(function (el) {
    if (!el) return;
    ["input", "change"].forEach(function (evt) { el.addEventListener(evt, paint); });
  });

  if (file && photo && window.URL && URL.createObjectURL) {
    var made = "";
    file.addEventListener("change", function () {
      if (made) { URL.revokeObjectURL(made); made = ""; }
      var picked = file.files && file.files[0];
      if (!picked) {
        // Nothing chosen: fall back to the cover already saved, if any.
        var saved = photo.getAttribute("data-cover-saved");
        if (saved) { photo.src = saved; photo.hidden = false; }
        else { photo.removeAttribute("src"); photo.hidden = true; }
        return;
      }
      made = URL.createObjectURL(picked);
      photo.src = made;
      photo.hidden = false;
    });
  }

  paint();
})();
