/* Bloom Anyway — public site JS (vanilla, no dependencies) */
(function () {
  "use strict";

  var reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---- scroll-triggered reveal ---- */
  var revealEls = document.querySelectorAll(".reveal");
  if (revealEls.length && !reducedMotion && "IntersectionObserver" in window) {
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("visible");
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0.15 });
    revealEls.forEach(function (el) { observer.observe(el); });
  } else {
    revealEls.forEach(function (el) { el.classList.add("visible"); });
  }

  /* ---- mobile nav drawer (slides in from left, overlays page) ---- */
  var toggle = document.querySelector(".nav-toggle");
  var drawer = document.getElementById("nav-drawer");
  var backdrop = document.getElementById("nav-backdrop");
  if (toggle && drawer) {
    var focusables = function () {
      return drawer.querySelectorAll("a[href], button:not([disabled])");
    };
    var setOpen = function (open) {
      drawer.classList.toggle("open", open);
      drawer.setAttribute("aria-hidden", open ? "false" : "true");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      document.body.classList.toggle("nav-open", open);
      if (backdrop) {
        if (open) backdrop.removeAttribute("hidden");
        else backdrop.setAttribute("hidden", "");
      }
    };
    var close = function () {
      setOpen(false);
      toggle.focus();
    };
    toggle.addEventListener("click", function () {
      var open = !drawer.classList.contains("open");
      setOpen(open);
      if (open) {
        var first = focusables()[0];
        if (first) first.focus();
      }
    });
    if (backdrop) backdrop.addEventListener("click", close);
    drawer.addEventListener("click", function (e) {
      if (e.target === drawer) close();
    });
    document.addEventListener("keydown", function (e) {
      if (!drawer.classList.contains("open")) return;
      if (e.key === "Escape") { close(); return; }
      if (e.key !== "Tab") return;
      var items = focusables();
      if (!items.length) return;
      var first = items[0];
      var last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        toggle.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        toggle.focus();
      } else if (!e.shiftKey && document.activeElement === toggle) {
        e.preventDefault();
        first.focus();
      }
    });
  }

  /* ---- a picture that won't load leaves a mark, not its own alt text ---- */
  // Error events don't bubble, so this listens on the way down. Swapping the
  // image for the placeholder it would have had keeps the circle a circle
  // instead of a ring of cut-off words.
  document.addEventListener("error", function (e) {
    var img = e.target;
    if (!img || img.tagName !== "IMG") return;
    var mark = img.getAttribute("data-photo-fallback");
    if (mark === null || !img.parentNode) return;
    var base = (img.className || "").split(/\s+/)[0];
    var stand = document.createElement("span");
    stand.className = base ? base + " " + base + "--ph" : "";
    stand.setAttribute("aria-hidden", "true");
    stand.textContent = mark;
    img.parentNode.replaceChild(stand, img);
  }, true);

  /* ---- password show/hide toggles ---- */
  document.querySelectorAll(".password-toggle").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var input = document.getElementById(btn.dataset.toggles);
      if (!input) return;
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.textContent = show ? "Hide" : "Show";
      btn.setAttribute("aria-pressed", show ? "true" : "false");
      btn.setAttribute("aria-label", show ? "Hide password" : "Show password");
    });
  });

  /* Clear auth passwords when navigating back to login/register */
  if (document.querySelector(".auth-card")) {
    var clearAuthPasswords = function () {
      document.querySelectorAll(".auth-card input[type='password']").forEach(function (input) {
        input.value = "";
      });
    };
    clearAuthPasswords();
    window.addEventListener("pageshow", clearAuthPasswords);
  }

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
      if (form.hasAttribute("data-require-sure")) {
        var sure = form.querySelector("[data-sure-check]");
        if (!sure || !sure.checked) {
          if (sure) sure.focus();
          return;
        }
      }
      openDialog(form);
    });

    document.querySelectorAll("form[data-require-sure]").forEach(function (form) {
      var sure = form.querySelector("[data-sure-check]");
      var submit = form.querySelector("[data-sure-submit]");
      if (!sure || !submit) return;
      function sync() {
        submit.disabled = !sure.checked;
      }
      sure.addEventListener("change", sync);
      sync();
    });
  })();

  /* ---- avatar crop / resize before upload ---- */
  (function () {
    var input = document.querySelector("[data-avatar-crop]");
    var dialog = document.getElementById("avatar-crop");
    if (!input || !dialog) return;

    // Keep the modal at the document root so stacking/CSP ancestors can't hide it.
    if (dialog.parentElement !== document.body) {
      document.body.appendChild(dialog);
    }

    var canModal = typeof dialog.showModal === "function";
    var stage = dialog.querySelector("[data-crop-stage]");
    var img = dialog.querySelector("[data-crop-image]");
    var zoom = dialog.querySelector("[data-crop-zoom]");
    var applyBtn = dialog.querySelector("[data-crop-apply]");
    var cancelBtn = dialog.querySelector("[data-crop-cancel]");
    var help = dialog.querySelector("[data-crop-help]");
    var pick = input.closest(".avatar-edit") &&
               input.closest(".avatar-edit").querySelector(".avatar");
    if (!stage || !img || !zoom || !applyBtn || !cancelBtn) return;

    var objectUrl = null;
    var natural = { w: 0, h: 0 };
    var state = { scale: 1, x: 0, y: 0, dragging: false, lastX: 0, lastY: 0 };
    var pendingFile = null;
    var pendingIsGif = false;

    function isImageFile(file) {
      if (!file) return false;
      if (file.type && file.type.indexOf("image/") === 0) return true;
      return /\.(jpe?g|png|gif|webp|bmp|heic|heif)$/i.test(file.name || "");
    }

    function isGifFile(file) {
      if (!file) return false;
      var type = (file.type || "").toLowerCase();
      if (type === "image/gif" || type.indexOf("gif") !== -1) return true;
      return /\.gif$/i.test(file.name || "");
    }

    function stageSize() {
      return Math.min(stage.clientWidth, stage.clientHeight) || 280;
    }

    function fitScale() {
      var s = stageSize();
      if (!natural.w || !natural.h) return 1;
      return Math.max(s / natural.w, s / natural.h);
    }

    function render() {
      if (!natural.w || !natural.h) return;
      var s = stageSize();
      var min = fitScale();
      var scale = Math.max(min, state.scale || min);
      state.scale = scale;
      var w = natural.w * scale;
      var h = natural.h * scale;
      var maxX = Math.max(0, (w - s) / 2);
      var maxY = Math.max(0, (h - s) / 2);
      state.x = Math.max(-maxX, Math.min(maxX, state.x));
      state.y = Math.max(-maxY, Math.min(maxY, state.y));
      img.style.width = w + "px";
      img.style.height = h + "px";
      img.style.transform = "translate(calc(-50% + " + state.x + "px), calc(-50% + " + state.y + "px))";
      var zoomPct = Math.round((scale / min) * 100);
      zoom.value = String(Math.max(100, Math.min(300, zoomPct)));
    }

    function setHelp(text) {
      if (help) help.textContent = text;
    }

    function openDialog() {
      if (canModal) {
        if (!dialog.open) dialog.showModal();
      } else {
        dialog.setAttribute("open", "");
        dialog.classList.add("avatar-crop--fallback");
      }
    }

    function closeDialog() {
      if (canModal) {
        if (dialog.open) dialog.close();
      } else {
        dialog.removeAttribute("open");
        dialog.classList.remove("avatar-crop--fallback");
      }
    }

    function revokePreview() {
      if (objectUrl) {
        URL.revokeObjectURL(objectUrl);
        objectUrl = null;
      }
    }

    function onImageReady() {
      natural.w = img.naturalWidth || 0;
      natural.h = img.naturalHeight || 0;
      if (!natural.w || !natural.h) {
        setHelp("That image couldn't be read. Try a JPG or PNG.");
        return;
      }
      state.scale = fitScale();
      state.x = 0;
      state.y = 0;
      if (pendingIsGif) {
        setHelp("Animated GIFs keep their motion. Preview below, then use this GIF — it plays on your profile and in Settings.");
      } else {
        setHelp("Drag to reposition. Use the slider to zoom. The circle is what people will see.");
      }
      render();
      requestAnimationFrame(render);
    }

    function loadFile(file) {
      pendingFile = file;
      pendingIsGif = isGifFile(file);
      setHelp("Loading your picture…");
      openDialog();
      revokePreview();
      img.onload = onImageReady;
      img.onerror = function () {
        setHelp("That image couldn't be previewed. Try a JPG, PNG, or GIF.");
      };

      try {
        objectUrl = URL.createObjectURL(file);
        img.src = objectUrl;
      } catch (err) {
        var reader = new FileReader();
        reader.onload = function () {
          img.src = String(reader.result || "");
        };
        reader.onerror = function () {
          setHelp("That image couldn't be read. Try a JPG, PNG, or GIF.");
        };
        reader.readAsDataURL(file);
      }
    }

    input.addEventListener("change", function () {
      var file = input.files && input.files[0];
      if (!file) return;
      if (!isImageFile(file)) {
        input.value = "";
        pendingFile = null;
        pendingIsGif = false;
        window.alert("Please choose an image file (JPG, PNG, WEBP, or GIF).");
        return;
      }
      loadFile(file);
    });

    zoom.addEventListener("input", function () {
      var min = fitScale();
      state.scale = min * (Math.max(100, parseInt(zoom.value, 10) || 100) / 100);
      render();
    });

    stage.addEventListener("pointerdown", function (e) {
      if (!natural.w) return;
      state.dragging = true;
      state.lastX = e.clientX;
      state.lastY = e.clientY;
      try { stage.setPointerCapture(e.pointerId); } catch (err) {}
    });
    stage.addEventListener("pointermove", function (e) {
      if (!state.dragging) return;
      state.x += e.clientX - state.lastX;
      state.y += e.clientY - state.lastY;
      state.lastX = e.clientX;
      state.lastY = e.clientY;
      render();
    });
    function endDrag() { state.dragging = false; }
    stage.addEventListener("pointerup", endDrag);
    stage.addEventListener("pointercancel", endDrag);

    function closeCrop(clearInput) {
      closeDialog();
      revokePreview();
      img.removeAttribute("src");
      natural.w = 0;
      natural.h = 0;
      if (clearInput) {
        pendingFile = null;
        pendingIsGif = false;
        input.value = "";
      }
    }

    cancelBtn.addEventListener("click", function (e) {
      e.preventDefault();
      closeCrop(true);
    });
    dialog.addEventListener("cancel", function (e) {
      e.preventDefault();
      closeCrop(true);
    });

    applyBtn.addEventListener("click", function (e) {
      e.preventDefault();
      if (!natural.w || !natural.h) return;

      function finishPreview(url) {
        if (!pick) return;
        if (pick.tagName === "IMG") {
          pick.src = url;
        } else {
          pick.style.backgroundImage = "url('" + url + "')";
          pick.textContent = "";
        }
      }

      var file = pendingFile || (input.files && input.files[0]);
      var keepGif = pendingIsGif || isGifFile(file);

      // Never canvas-flatten GIFs — that kills the animation.
      if (keepGif && file) {
        try {
          var dtGif = new DataTransfer();
          dtGif.items.add(file);
          input.files = dtGif.files;
        } catch (err) {}
        finishPreview(URL.createObjectURL(file));
        var removeGif = document.querySelector("input[name='remove_avatar']");
        if (removeGif) removeGif.checked = false;
        closeCrop(false);
        return;
      }

      var s = stageSize();
      var min = fitScale();
      var scale = Math.max(min, state.scale);
      var out = 400;
      var canvas = document.createElement("canvas");
      canvas.width = out;
      canvas.height = out;
      var ctx = canvas.getContext("2d");
      if (!ctx) return;
      var srcSize = s / scale;
      var srcX = (natural.w / 2) - (srcSize / 2) - (state.x / scale);
      var srcY = (natural.h / 2) - (srcSize / 2) - (state.y / scale);
      ctx.fillStyle = "#fff";
      ctx.fillRect(0, 0, out, out);
      try {
        ctx.drawImage(img, srcX, srcY, srcSize, srcSize, 0, 0, out, out);
      } catch (err) {
        closeCrop(false);
        return;
      }
      canvas.toBlob(function (blob) {
        if (!blob) {
          closeCrop(false);
          return;
        }
        try {
          var cropped = new File([blob], "avatar.jpg", { type: "image/jpeg" });
          var dt = new DataTransfer();
          dt.items.add(cropped);
          input.files = dt.files;
        } catch (err) {}
        finishPreview(URL.createObjectURL(blob));
        var remove = document.querySelector("input[name='remove_avatar']");
        if (remove) remove.checked = false;
        closeCrop(false);
      }, "image/jpeg", 0.92);
    });

    window.addEventListener("resize", function () {
      if ((dialog.open || dialog.hasAttribute("open")) && natural.w) render();
    });
  })();

  /* ---- CSP-safe auto-submit selects ---- */
  document.querySelectorAll("select[data-autosubmit]").forEach(function (sel) {
    sel.addEventListener("change", function () {
      if (sel.form) sel.form.submit();
    });
  });

  /* ---- CSP-safe: block context menu on protected media ---- */
  document.querySelectorAll("[data-no-contextmenu]").forEach(function (el) {
    el.addEventListener("contextmenu", function (e) { e.preventDefault(); });
  });

  /* ---- marketplace listing form: show location box for services ---- */
  var listingForm = document.getElementById("listing-form");
  if (listingForm) {
    var locBox = listingForm.querySelector("[data-location-box]");
    var locInput = listingForm.querySelector("#location");
    var syncKind = function () {
      var picked = listingForm.querySelector('input[name="kind"]:checked');
      var isService = !!(picked && picked.value === "service");
      listingForm.classList.toggle("is-service", isService);
      listingForm.classList.toggle("is-product", !isService);
      if (locBox) {
        if (isService) locBox.removeAttribute("hidden");
        else locBox.setAttribute("hidden", "");
      }
      if (locInput) {
        locInput.required = isService;
        if (!isService) locInput.value = locInput.value; // keep typed text if they toggle back
      }
    };
    listingForm.querySelectorAll('input[name="kind"]').forEach(function (r) {
      r.addEventListener("change", syncKind);
      // also catch clicks on the visible label chip
      var label = r.closest("label");
      if (label) label.addEventListener("click", function () {
        // let the radio update, then sync on next tick
        setTimeout(syncKind, 0);
      });
    });
    syncKind();

    var max = parseInt(listingForm.getAttribute("data-tag-max") || "24", 10);
    var boxes = listingForm.querySelectorAll('input[name="tags"]');
    var countEl = listingForm.querySelector("[data-tag-count]");
    var syncTags = function () {
      var n = 0;
      boxes.forEach(function (b) { if (b.checked) n++; });
      if (countEl) countEl.textContent = n + " / " + max + " selected";
      boxes.forEach(function (b) {
        if (!b.checked) b.disabled = n >= max;
      });
    };
    boxes.forEach(function (b) { b.addEventListener("change", syncTags); });
    syncTags();
  }

  /* ---- notification bell: click-outside, Escape, mark-as-read ---- */
  document.querySelectorAll("details.note-bell").forEach(function (bell) {
    var marked = false;

    function clearUnreadUi() {
      var count = bell.querySelector(".note-bell__count");
      if (count) count.remove();
      bell.querySelectorAll(".note-bell__item.is-unread").forEach(function (el) {
        el.classList.remove("is-unread");
      });
      var markBtn = bell.querySelector("[data-mark-read]");
      if (markBtn) markBtn.remove();
      var summary = bell.querySelector(".note-bell__btn");
      if (summary) summary.setAttribute("aria-label", "Notifications");
      document.querySelectorAll(".myspace-tabs__dot").forEach(function (dot) {
        dot.remove();
      });
    }

    function markRead() {
      if (marked) return;
      var url = bell.getAttribute("data-mark-read-url");
      var csrf = bell.getAttribute("data-csrf");
      if (!url || !csrf) return;
      marked = true;
      clearUnreadUi();
      fetch(url, {
        method: "POST",
        headers: {
          "X-CSRFToken": csrf,
          "X-Requested-With": "fetch",
          "Accept": "application/json"
        },
        credentials: "same-origin"
      }).catch(function () {
        marked = false;
      });
    }

    bell.addEventListener("toggle", function () {
      if (bell.open) markRead();
    });

    var markBtn = bell.querySelector("[data-mark-read]");
    if (markBtn) {
      markBtn.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        markRead();
      });
    }

    document.addEventListener("click", function (e) {
      if (!bell.open) return;
      if (bell.contains(e.target)) return;
      bell.removeAttribute("open");
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && bell.open) {
        bell.removeAttribute("open");
      }
    });
  });

  /* ---- reply textarea: full width, grow with lines (no horizontal resize) ---- */
  function autosizeTextarea(el) {
    el.style.height = "auto";
    el.style.height = el.scrollHeight + "px";
  }
  document.querySelectorAll(".comment-form--reply textarea").forEach(function (ta) {
    autosizeTextarea(ta);
    ta.addEventListener("input", function () { autosizeTextarea(ta); });
  });
  document.querySelectorAll("details.reply-toggle").forEach(function (d) {
    d.addEventListener("toggle", function () {
      if (!d.open) return;
      var ta = d.querySelector("textarea");
      if (ta) {
        autosizeTextarea(ta);
        ta.focus();
      }
    });
  });

  /* ---- Showcase listing gallery: thumbnails swap the hero image ---- */
  document.querySelectorAll("[data-listing-gallery]").forEach(function (gallery) {
    var hero = gallery.querySelector("#listing-hero") ||
               gallery.querySelector(".listing-detail__hero");
    if (!hero) return;
    gallery.querySelectorAll("[data-listing-thumb]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var src = btn.getAttribute("data-src");
        if (!src) return;
        hero.src = src;
        gallery.querySelectorAll("[data-listing-thumb]").forEach(function (other) {
          other.classList.toggle("is-active", other === btn);
          other.setAttribute("aria-pressed", other === btn ? "true" : "false");
        });
      });
    });
  });

  /* ---- @username mention autocomplete ---- */
  (function setupMentions() {
    var suggestUrl = document.body.getAttribute("data-mention-suggest");
    if (!suggestUrl) return;

    var menu = document.createElement("div");
    menu.className = "mention-menu";
    menu.hidden = true;
    menu.setAttribute("role", "listbox");
    document.body.appendChild(menu);

    var active = null;
    var items = [];
    var highlight = 0;
    var tokenStart = -1;
    var debounce = null;
    var reqId = 0;

    function hide() {
      menu.hidden = true;
      menu.innerHTML = "";
      items = [];
    }

    function placeMenu(textarea) {
      if (!textarea) return;
      var rect = textarea.getBoundingClientRect();
      var width = Math.min(300, Math.max(200, rect.width));
      var left = Math.min(
        Math.max(8, rect.left),
        Math.max(8, window.innerWidth - width - 8)
      );
      menu.style.position = "fixed";
      menu.style.left = left + "px";
      menu.style.top = (rect.bottom + 6) + "px";
      menu.style.minWidth = width + "px";
      menu.style.zIndex = "200";
    }

    function applyChoice(username) {
      if (!active || tokenStart < 0) return;
      var val = active.value;
      var caret = active.selectionStart;
      var before = val.slice(0, tokenStart);
      var after = val.slice(caret);
      active.value = before + "@" + username + " " + after;
      var pos = before.length + username.length + 2;
      active.focus();
      active.setSelectionRange(pos, pos);
      hide();
      active = null;
    }

    function render() {
      menu.innerHTML = "";
      if (!items.length || !active) {
        hide();
        return;
      }
      items.forEach(function (row, i) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "mention-menu__item" + (i === highlight ? " is-active" : "");
        btn.setAttribute("role", "option");
        var handle = document.createElement("strong");
        handle.textContent = "@" + row.username;
        btn.appendChild(handle);
        if (row.name) {
          var name = document.createElement("span");
          name.textContent = row.name;
          btn.appendChild(name);
        }
        btn.addEventListener("mousedown", function (e) {
          e.preventDefault();
          applyChoice(row.username);
        });
        menu.appendChild(btn);
      });
      menu.hidden = false;
      placeMenu(active);
    }

    function fetchSuggestions(q) {
      var myReq = ++reqId;
      var url = suggestUrl + (suggestUrl.indexOf("?") >= 0 ? "&" : "?") +
                "q=" + encodeURIComponent(q);
      fetch(url, {
        headers: { "Accept": "application/json" },
        credentials: "same-origin",
        redirect: "follow"
      })
        .then(function (r) {
          var ct = (r.headers.get("content-type") || "").toLowerCase();
          if (!r.ok || ct.indexOf("application/json") === -1) return [];
          return r.json();
        })
        .then(function (data) {
          if (myReq !== reqId) return;
          items = Array.isArray(data) ? data : [];
          highlight = 0;
          render();
        })
        .catch(function () {
          if (myReq === reqId) hide();
        });
    }

    function mentionQuery(textarea) {
      var caret = typeof textarea.selectionStart === "number"
        ? textarea.selectionStart
        : textarea.value.length;
      var upto = textarea.value.slice(0, caret);
      // Allow bare "@" (empty query) and partial handles; require a boundary
      // before @ so emails like name@host are ignored.
      var match = upto.match(/(?:^|[^\w@])@([a-zA-Z0-9_]{0,30})$/);
      if (!match) return null;
      var handle = match[1] || "";
      return {
        q: handle,
        tokenStart: caret - handle.length - 1
      };
    }

    function onInput(textarea) {
      active = textarea;
      var hit = mentionQuery(textarea);
      if (!hit) {
        hide();
        return;
      }
      tokenStart = hit.tokenStart;
      clearTimeout(debounce);
      debounce = setTimeout(function () { fetchSuggestions(hit.q); }, 80);
    }

    function isMentionField(el) {
      return el && el.tagName === "TEXTAREA" && el.hasAttribute("data-mentions");
    }

    document.addEventListener("input", function (e) {
      if (isMentionField(e.target)) onInput(e.target);
    });
    document.addEventListener("keydown", function (e) {
      if (!isMentionField(e.target)) return;
      if (menu.hidden || !items.length) return;
      if (e.key === "ArrowDown") {
        e.preventDefault();
        highlight = (highlight + 1) % items.length;
        render();
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        highlight = (highlight - 1 + items.length) % items.length;
        render();
      } else if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        applyChoice(items[highlight].username);
      } else if (e.key === "Escape") {
        hide();
      }
    });
    document.addEventListener("blur", function (e) {
      if (isMentionField(e.target)) setTimeout(hide, 180);
    }, true);

    window.addEventListener("scroll", function () {
      if (!menu.hidden && active) placeMenu(active);
    }, true);
    window.addEventListener("resize", function () {
      if (!menu.hidden && active) placeMenu(active);
    });
  })();

  /* ---- site feedback dialog (stars / complaint / error) ---- */
  (function () {
    var dialog = document.getElementById("feedback-dialog");
    if (!dialog) return;
    var kindInput = dialog.querySelector("[data-feedback-kind-input]");
    var starsBox = dialog.querySelector("[data-feedback-stars]");
    var tabs = dialog.querySelectorAll("[data-feedback-tab]");

    function setKind(kind) {
      if (kindInput) kindInput.value = kind;
      tabs.forEach(function (btn) {
        btn.classList.toggle("is-active", btn.getAttribute("data-feedback-tab") === kind);
      });
      if (starsBox) {
        if (kind === "feedback") starsBox.removeAttribute("hidden");
        else starsBox.setAttribute("hidden", "");
      }
    }

    function openDialog(pref) {
      setKind(pref || "feedback");
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
    }

    function closeDialog() {
      if (typeof dialog.close === "function") dialog.close();
      else dialog.removeAttribute("open");
    }

    document.querySelectorAll("[data-feedback-open]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        openDialog(btn.getAttribute("data-feedback-pref") || "feedback");
      });
    });
    dialog.querySelectorAll("[data-feedback-close]").forEach(function (btn) {
      btn.addEventListener("click", closeDialog);
    });
    tabs.forEach(function (btn) {
      btn.addEventListener("click", function () {
        setKind(btn.getAttribute("data-feedback-tab") || "feedback");
      });
    });
    setKind("feedback");
  })();

  // Journal: prompt picks the question heading + flip the notebook (oldest → today)
  (function () {
    var list = document.getElementById("journal-prompt-ideas");
    var keyInput = document.getElementById("journal-prompt-key");
    var labelEl = document.getElementById("journal-prompt-label");
    var body = document.getElementById("journal-body");
    var book = document.querySelector("[data-jn-book]");

    if (list) {
      list.addEventListener("click", function (e) {
        var btn = e.target.closest(".jn-prompt-list__btn, .ms-prompt-list__btn");
        if (!btn || !list.contains(btn)) return;
        if (book && book.classList.contains("is-on-past")) return;
        var key = btn.getAttribute("data-prompt-key") || "";
        var label = btn.getAttribute("data-prompt-label") || btn.textContent.trim();
        if (keyInput) keyInput.value = key;
        if (labelEl) {
          var free = key === "free" || !label;
          labelEl.hidden = free;
          labelEl.textContent = label;
          if (!free) {
            labelEl.classList.add("is-swapped");
            window.setTimeout(function () {
              labelEl.classList.remove("is-swapped");
            }, 450);
          } else {
            labelEl.classList.remove("is-swapped");
          }
        }
        if (body) {
          body.setAttribute("aria-label", label || "Journal note");
          body.focus();
        }
        list.querySelectorAll(".jn-prompt-list__btn, .ms-prompt-list__btn").forEach(function (b) {
          b.classList.toggle("is-active", b === btn);
        });
      });
    }

    if (!book) return;
    var sheets = Array.prototype.slice.call(book.querySelectorAll("[data-jn-sheet]"));
    if (!sheets.length) return;
    var backBtn = book.querySelector("[data-jn-back]");
    var fwdBtn = book.querySelector("[data-jn-fwd]");
    var counter = book.querySelector("[data-jn-counter]");
    // Pages are oldest → newest/today; start on the newest (last) page.
    var index = sheets.length - 1;
    var animating = false;
    var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    if (sheets.length < 2) {
      book.classList.add("jn-book--solo");
    }

    function updateChrome() {
      var total = sheets.length;
      if (counter) {
        counter.textContent =
          total <= 1 ? "Today" : "Page " + (index + 1) + " of " + total;
      }
      if (backBtn) backBtn.disabled = index <= 0 || animating;
      if (fwdBtn) fwdBtn.disabled = index >= total - 1 || animating;
      var onPast = sheets[index] && sheets[index].getAttribute("data-writable") !== "1";
      book.classList.toggle("is-on-past", !!onPast);
    }

    function show(nextIndex, dir) {
      if (animating || nextIndex === index || nextIndex < 0 || nextIndex >= sheets.length) {
        return;
      }
      var current = sheets[index];
      var next = sheets[nextIndex];
      var leaveClass = dir === "back" ? "is-leave-back" : "is-leave-fwd";
      var enterClass = dir === "back" ? "is-enter-back" : "is-enter-fwd";

      if (reduceMotion) {
        current.hidden = true;
        current.classList.remove(
          "is-current", "is-leave-back", "is-leave-fwd", "is-enter-back", "is-enter-fwd"
        );
        next.hidden = false;
        next.classList.add("is-current");
        index = nextIndex;
        updateChrome();
        return;
      }

      animating = true;
      updateChrome();
      var stage = book.querySelector("[data-jn-stage]") || book;
      var h = Math.max(current.offsetHeight || 0, next.offsetHeight || 0, 520);
      stage.style.minHeight = h + "px";
      current.classList.remove("is-enter-back", "is-enter-fwd");
      current.classList.add(leaveClass);
      next.hidden = false;
      next.classList.add(enterClass, "is-current");

      window.setTimeout(function () {
        current.hidden = true;
        current.classList.remove("is-current", leaveClass);
        next.classList.remove(enterClass);
        index = nextIndex;
        animating = false;
        stage.style.minHeight = "";
        updateChrome();
      }, 560);
    }

    if (backBtn) {
      backBtn.addEventListener("click", function () {
        show(index - 1, "back");
      });
    }
    if (fwdBtn) {
      fwdBtn.addEventListener("click", function () {
        show(index + 1, "fwd");
      });
    }

    // Ensure only the start page is visible.
    sheets.forEach(function (sheet, i) {
      var on = i === index;
      sheet.hidden = !on;
      sheet.classList.toggle("is-current", on);
      sheet.classList.remove(
        "is-leave-back", "is-leave-fwd", "is-enter-back", "is-enter-fwd"
      );
    });
    updateChrome();
  })();

  /* ---- My space library filters ---- */
  (function () {
    var bar = document.querySelector("[data-lib-filters]");
    if (!bar) return;
    var cards = Array.prototype.slice.call(document.querySelectorAll(".lib-card[data-lib-bucket]"));
    var empty = document.querySelector("[data-lib-empty]");
    var setFilter = function (filter) {
      var shown = 0;
      cards.forEach(function (card) {
        var match = filter === "all" || card.getAttribute("data-lib-bucket") === filter;
        card.hidden = !match;
        if (match) shown += 1;
      });
      bar.querySelectorAll("[data-lib-filter]").forEach(function (btn) {
        var on = btn.getAttribute("data-lib-filter") === filter;
        btn.classList.toggle("is-active", on);
        btn.setAttribute("aria-selected", on ? "true" : "false");
      });
      if (empty) empty.hidden = shown > 0;
    };
    bar.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-lib-filter]");
      if (!btn || !bar.contains(btn)) return;
      e.preventDefault();
      setFilter(btn.getAttribute("data-lib-filter") || "all");
    });
    document.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-lib-empty] [data-lib-filter]");
      if (!btn) return;
      e.preventDefault();
      setFilter(btn.getAttribute("data-lib-filter") || "all");
    });
  })();

  /* ---- Handheld-only interactions (phones / tablets ≤899px) ---- */
  (function () {
    var hand = window.matchMedia("(max-width: 899px)");
    var isHand = function () { return hand.matches; };

    /* Community cards: tap expands details; Join button navigates */
    document.querySelectorAll("[data-comm-card]").forEach(function (card) {
      card.addEventListener("click", function (e) {
        if (!isHand()) return;
        if (e.target.closest("a, button")) return;
        card.classList.toggle("is-expanded");
      });
      card.addEventListener("keydown", function (e) {
        if (!isHand()) return;
        if (e.key === "Enter" || e.key === " ") {
          if (e.target.closest("a, button")) return;
          e.preventDefault();
          card.classList.toggle("is-expanded");
        }
      });
    });

    /* Category welcome: expand when community title is tapped */
    var title = document.getElementById("fc-title");
    var welcome = document.getElementById("fc-welcome");
    if (title && welcome) {
      title.setAttribute("role", "button");
      title.setAttribute("aria-controls", "fc-welcome");
      title.setAttribute("aria-expanded", "false");
      var toggleWelcome = function () {
        if (!isHand()) return;
        var open = welcome.classList.toggle("is-open");
        title.setAttribute("aria-expanded", open ? "true" : "false");
      };
      title.addEventListener("click", toggleWelcome);
      title.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          toggleWelcome();
        }
      });
      hand.addEventListener("change", function () {
        if (!hand.matches) {
          welcome.classList.remove("is-open");
          title.setAttribute("aria-expanded", "false");
        }
      });
    }

    /* Content Hub: reveal weekly draw only after Submit your reel */
    var rotw = document.querySelector("[data-rotw-panel]");
    if (rotw) {
      document.querySelectorAll("[data-reveal-rotw]").forEach(function (el) {
        el.addEventListener("click", function () {
          if (!isHand()) return;
          rotw.classList.add("is-revealed");
        });
      });
      if (window.location.hash === "#reel-review" && isHand()) {
        rotw.classList.add("is-revealed");
      }
    }
  })();

  /* ---- client-side upload size guard (beats Cloudflare's blank 413) ---- */
  (function () {
    document.querySelectorAll("form[data-max-upload-mb]").forEach(function (form) {
      var maxMb = parseFloat(form.getAttribute("data-max-upload-mb") || "0");
      if (!(maxMb > 0)) return;
      var inputName = form.getAttribute("data-max-upload-input") || "";
      var input = inputName
        ? form.querySelector('input[type="file"][name="' + inputName + '"]')
        : form.querySelector('input[type="file"]');
      if (!input) return;
      var errEl = form.querySelector("[data-upload-error]");
      var maxBytes = Math.floor(maxMb * 1024 * 1024);

      function showErr(msg) {
        if (!errEl) {
          window.alert(msg);
          return;
        }
        errEl.hidden = !msg;
        errEl.textContent = msg || "";
      }

      input.addEventListener("change", function () {
        var file = input.files && input.files[0];
        if (!file) {
          showErr("");
          return;
        }
        if (file.size > maxBytes) {
          showErr(
            "That file is about " +
              (file.size / (1024 * 1024)).toFixed(0) +
              " MB. Please use a video under " +
              maxMb +
              " MB (compress or trim it first)."
          );
          input.value = "";
        } else {
          showErr("");
        }
      });

      form.addEventListener("submit", function (e) {
        var file = input.files && input.files[0];
        if (file && file.size > maxBytes) {
          e.preventDefault();
          showErr(
            "That file is too large (max " + maxMb + " MB). Compress or trim it, then try again."
          );
          input.focus();
        }
      });
    });
  })();

  /* ---- 1:1 booking slot calendar ---- */
  (function () {
    var root = document.querySelector("[data-slot-cal]");
    if (!root) return;
    var jsonEl = root.querySelector("[data-slot-cal-json]");
    var hidden = document.getElementById("slot_utc");
    var grid = root.querySelector("[data-slot-cal-grid]");
    var monthEl = root.querySelector("[data-slot-cal-month]");
    var prevBtn = root.querySelector("[data-slot-cal-prev]");
    var nextBtn = root.querySelector("[data-slot-cal-next]");
    var timesWrap = root.querySelector("[data-slot-cal-times]");
    var timesList = root.querySelector("[data-slot-cal-time-list]");
    var dayLabel = root.querySelector("[data-slot-cal-day-label]");
    var chosenEl = root.querySelector("[data-slot-cal-chosen]");
    if (!jsonEl || !hidden || !grid || !monthEl) return;

    var slots = [];
    try {
      slots = JSON.parse(jsonEl.textContent || "[]") || [];
    } catch (err) {
      slots = [];
    }
    if (!slots.length) return;

    var byDate = {};
    slots.forEach(function (s) {
      var key = s.date_key;
      if (!key) return;
      if (!byDate[key]) byDate[key] = [];
      byDate[key].push(s);
    });
    var openKeys = Object.keys(byDate).sort();
    if (!openKeys.length) return;

    function parseKey(key) {
      var parts = key.split("-");
      return new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
    }
    function fmtKey(d) {
      var m = d.getMonth() + 1;
      var day = d.getDate();
      return (
        d.getFullYear() +
        "-" +
        (m < 10 ? "0" : "") +
        m +
        "-" +
        (day < 10 ? "0" : "") +
        day
      );
    }

    var firstOpen = parseKey(openKeys[0]);
    var lastOpen = parseKey(openKeys[openKeys.length - 1]);
    var view = new Date(firstOpen.getFullYear(), firstOpen.getMonth(), 1);
    var selectedDate = null;
    var todayKey = fmtKey(new Date());
    var monthNames = [
      "January", "February", "March", "April", "May", "June",
      "July", "August", "September", "October", "November", "December",
    ];

    function clearSelection() {
      hidden.value = "";
      if (chosenEl) {
        chosenEl.hidden = true;
        chosenEl.textContent = "";
      }
    }

    function showTimes(dateKey) {
      selectedDate = dateKey;
      var list = byDate[dateKey] || [];
      if (!timesWrap || !timesList) return;
      timesList.innerHTML = "";
      if (!list.length) {
        timesWrap.hidden = true;
        return;
      }
      if (dayLabel) {
        dayLabel.textContent =
          "Times on " + (list[0].day_label || dateKey);
      }
      list.forEach(function (s) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "slot-cal__time";
        btn.setAttribute("role", "option");
        btn.textContent = s.time_label || s.label;
        btn.dataset.utc = s.utc;
        btn.addEventListener("click", function () {
          hidden.value = s.utc || "";
          timesList.querySelectorAll(".slot-cal__time").forEach(function (b) {
            b.classList.toggle("is-selected", b === btn);
          });
          if (chosenEl) {
            chosenEl.hidden = false;
            chosenEl.innerHTML =
              "Selected: <strong>" + (s.label || s.utc) + "</strong>";
          }
        });
        timesList.appendChild(btn);
      });
      timesWrap.hidden = false;
      clearSelection();
    }

    function render() {
      monthEl.textContent =
        monthNames[view.getMonth()] + " " + view.getFullYear();
      if (prevBtn) {
        prevBtn.disabled =
          view.getFullYear() < firstOpen.getFullYear() ||
          (view.getFullYear() === firstOpen.getFullYear() &&
            view.getMonth() <= firstOpen.getMonth());
      }
      if (nextBtn) {
        nextBtn.disabled =
          view.getFullYear() > lastOpen.getFullYear() ||
          (view.getFullYear() === lastOpen.getFullYear() &&
            view.getMonth() >= lastOpen.getMonth());
      }

      grid.innerHTML = "";
      var start = new Date(view.getFullYear(), view.getMonth(), 1);
      var startPad = (start.getDay() + 6) % 7; // Mon=0
      var cursor = new Date(view.getFullYear(), view.getMonth(), 1 - startPad);
      for (var i = 0; i < 42; i++) {
        var key = fmtKey(cursor);
        var inMonth = cursor.getMonth() === view.getMonth();
        var open = !!byDate[key];
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "slot-cal__day";
        btn.textContent = String(cursor.getDate());
        if (!inMonth) btn.classList.add("is-muted");
        if (key === todayKey) btn.classList.add("is-today");
        if (open) {
          btn.classList.add("is-open");
          btn.setAttribute("aria-label", "Available " + key);
          if (selectedDate === key) btn.classList.add("is-selected");
          btn.addEventListener(
            "click",
            (function (dk) {
              return function () {
                selectedDate = dk;
                render();
                showTimes(dk);
              };
            })(key)
          );
        } else {
          btn.disabled = true;
          btn.setAttribute("aria-disabled", "true");
        }
        grid.appendChild(btn);
        cursor.setDate(cursor.getDate() + 1);
      }
    }

    if (prevBtn) {
      prevBtn.addEventListener("click", function () {
        view = new Date(view.getFullYear(), view.getMonth() - 1, 1);
        render();
      });
    }
    if (nextBtn) {
      nextBtn.addEventListener("click", function () {
        view = new Date(view.getFullYear(), view.getMonth() + 1, 1);
        render();
      });
    }

    render();
    // Auto-open the first available day for quicker booking.
    showTimes(openKeys[0]);
    render();
  })();
})();
