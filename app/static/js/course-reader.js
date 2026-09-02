/* Bloom Anyway — on-site course reader (PDF one-page, H5P, media). */
(function () {
  "use strict";

  var root = document.querySelector(".course-reader-page");
  if (!root) return;

  var kind = root.getAttribute("data-asset-kind") || "";
  var fileUrl = root.getAttribute("data-file-url") || "";
  var progressUrl = root.getAttribute("data-progress-url") || "";
  var bookmarkUrl = root.getAttribute("data-bookmark-url") || "";
  var startPage = parseInt(root.getAttribute("data-start-page") || "1", 10) || 1;
  var startPercent = parseInt(root.getAttribute("data-start-percent") || "0", 10) || 0;
  var csrf = (document.body && document.body.getAttribute("data-csrf")) || "";

  var pill = document.getElementById("reader-progress-label");
  var pageInput = document.getElementById("reader-page");
  var totalEl = document.getElementById("reader-total");
  var chipPage = document.getElementById("reader-chip-page");
  var chipTotal = document.getElementById("reader-chip-total");
  var statusEl = document.getElementById("reader-pdf-status");
  var canvas = document.getElementById("reader-pdf-canvas");
  var toc = document.getElementById("reader-toc");
  var tocLoading = document.getElementById("reader-toc-loading");
  var tocDrawer = document.getElementById("reader-toc-drawer");
  var searchInput = document.getElementById("reader-search");
  var bookmarkBtn = document.getElementById("reader-bookmark-btn");
  var appearanceBtn = document.getElementById("reader-appearance-btn");
  var appearancePanel = document.getElementById("reader-appearance-panel");

  /* Handheld: pages list starts collapsed; desktop stays open */
  if (tocDrawer) {
    var handMq = window.matchMedia("(max-width: 899px)");
    var syncTocDrawer = function () {
      tocDrawer.open = !handMq.matches;
    };
    syncTocDrawer();
    if (handMq.addEventListener) handMq.addEventListener("change", syncTocDrawer);
    else if (handMq.addListener) handMq.addListener(syncTocDrawer);
  }

  var bookmarks = [];
  try {
    bookmarks = JSON.parse(root.getAttribute("data-bookmarks") || "[]") || [];
  } catch (e) {
    bookmarks = [];
  }

  var state = {
    page: Math.max(1, startPage),
    total: 0,
    percent: startPercent,
    saving: false,
    pdf: null,
    renderToken: 0,
    pageText: {},
    go: null,
  };

  var PREF_KEY = "ba-reader-prefs";

  function setPill() {
    if (!pill) return;
    pill.textContent = state.percent + "% complete";
  }

  function syncPageUi() {
    if (pageInput) pageInput.value = String(state.page);
    if (chipPage) chipPage.textContent = String(state.page);
    if (totalEl) totalEl.textContent = state.total ? String(state.total) : "—";
    if (chipTotal) chipTotal.textContent = state.total ? String(state.total) : "—";
    highlightToc();
    syncBookmarkBtn();
  }

  function computePercent() {
    if (state.total > 0) {
      state.percent = Math.max(
        0,
        Math.min(100, Math.round((100 * state.page) / state.total))
      );
    }
    setPill();
  }

  function saveProgress(opts) {
    if (!progressUrl || !csrf) return;
    var body = {
      page: state.page,
      total: state.total,
      percent: state.percent,
    };
    if (opts && typeof opts.percent === "number") {
      body.percent = opts.percent;
      state.percent = opts.percent;
      setPill();
    }
    fetch(progressUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrf,
        "X-Requested-With": "fetch",
      },
      body: JSON.stringify(body),
      credentials: "same-origin",
      keepalive: true,
    }).catch(function () {});
  }

  function saveSoon() {
    if (state.saving) return;
    state.saving = true;
    window.setTimeout(function () {
      state.saving = false;
      saveProgress();
    }, 250);
  }

  window.addEventListener("pagehide", function () {
    saveProgress();
  });
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") saveProgress();
  });

  function buildToc() {
    if (!toc) return;
    if (tocLoading) tocLoading.remove();
    toc.innerHTML = "";
    if (!state.total) {
      toc.innerHTML = "<p class=\"field-help\">No pages yet.</p>";
      return;
    }
    var frag = document.createDocumentFragment();
    for (var i = 1; i <= state.total; i++) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "reader__toc-item";
      btn.setAttribute("data-page", String(i));
      btn.textContent = "Page " + i;
      if (bookmarks.indexOf(i) >= 0) {
        btn.classList.add("is-bookmarked");
      }
      frag.appendChild(btn);
    }
    toc.appendChild(frag);
    highlightToc();
  }

  function highlightToc() {
    if (!toc) return;
    toc.querySelectorAll(".reader__toc-item").forEach(function (el) {
      var p = parseInt(el.getAttribute("data-page"), 10);
      el.classList.toggle("is-active", p === state.page);
      el.classList.toggle("is-bookmarked", bookmarks.indexOf(p) >= 0);
    });
  }

  if (toc) {
    toc.addEventListener("click", function (e) {
      var btn = e.target.closest(".reader__toc-item");
      if (!btn || !state.go) return;
      state.go(parseInt(btn.getAttribute("data-page"), 10) || 1);
    });
  }

  if (searchInput) {
    searchInput.addEventListener("input", function () {
      var q = (searchInput.value || "").trim().toLowerCase();
      if (!toc) return;
      // Page-number filter only (not PDF body text — that made "2" match every page with a "2" in it).
      toc.querySelectorAll(".reader__toc-item").forEach(function (el) {
        var p = parseInt(el.getAttribute("data-page"), 10);
        var pageStr = String(p);
        var show = !q || pageStr.indexOf(q) >= 0 || ("page " + pageStr).indexOf(q) >= 0;
        el.hidden = !show;
      });
    });
    searchInput.addEventListener("keydown", function (e) {
      if (e.key !== "Enter") return;
      var q = (searchInput.value || "").trim();
      var asNum = parseInt(q, 10);
      if (!state.go) return;
      e.preventDefault();
      if (asNum && state.total && asNum >= 1 && asNum <= state.total) {
        state.go(asNum);
        return;
      }
      var first = toc && toc.querySelector(".reader__toc-item:not([hidden])");
      if (first) state.go(parseInt(first.getAttribute("data-page"), 10) || 1);
    });
  }

  function syncBookmarkBtn() {
    if (!bookmarkBtn) return;
    var on = bookmarks.indexOf(state.page) >= 0;
    bookmarkBtn.classList.toggle("is-active", on);
    bookmarkBtn.setAttribute("aria-pressed", on ? "true" : "false");
    bookmarkBtn.title = on ? "Remove bookmark" : "Bookmark this page";
  }

  function toggleBookmark() {
    if (!bookmarkUrl || !csrf) return;
    fetch(bookmarkUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrf,
        "X-Requested-With": "fetch",
      },
      body: JSON.stringify({ page: state.page }),
      credentials: "same-origin",
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data || !data.ok) return;
        bookmarks = data.bookmarks || [];
        syncBookmarkBtn();
        highlightToc();
      })
      .catch(function () {});
  }

  if (bookmarkBtn) {
    bookmarkBtn.addEventListener("click", toggleBookmark);
  }

  /* ---- Appearance prefs ---- */
  function loadPrefs() {
    try {
      return JSON.parse(localStorage.getItem(PREF_KEY) || "{}") || {};
    } catch (e) {
      return {};
    }
  }

  function savePrefs(prefs) {
    try {
      localStorage.setItem(PREF_KEY, JSON.stringify(prefs));
    } catch (e) {}
  }

  function applyPrefs(prefs) {
    root.setAttribute("data-theme", prefs.theme || "light");
    root.setAttribute("data-font", prefs.font || "md");
    root.setAttribute("data-line", prefs.line || "normal");
    root.setAttribute("data-zoom", prefs.zoom || "md");
    if (appearancePanel) {
      appearancePanel.querySelectorAll("[data-theme]").forEach(function (btn) {
        btn.classList.toggle("is-active", btn.getAttribute("data-theme") === (prefs.theme || "light"));
      });
      appearancePanel.querySelectorAll("[data-font]").forEach(function (btn) {
        btn.classList.toggle("is-active", btn.getAttribute("data-font") === (prefs.font || "md"));
      });
      appearancePanel.querySelectorAll("[data-line]").forEach(function (btn) {
        btn.classList.toggle("is-active", btn.getAttribute("data-line") === (prefs.line || "normal"));
      });
      appearancePanel.querySelectorAll("[data-zoom]").forEach(function (btn) {
        btn.classList.toggle("is-active", btn.getAttribute("data-zoom") === (prefs.zoom || "md"));
      });
    }
    if (typeof state.rerender === "function") state.rerender();
  }

  var prefs = loadPrefs();
  applyPrefs(prefs);

  function setAppearanceOpen(open) {
    if (!appearancePanel || !appearanceBtn) return;
    appearancePanel.hidden = !open;
    appearanceBtn.setAttribute("aria-expanded", open ? "true" : "false");
    appearanceBtn.classList.toggle("is-active", open);
  }

  if (appearanceBtn) {
    appearanceBtn.addEventListener("click", function () {
      setAppearanceOpen(appearancePanel && appearancePanel.hidden);
    });
  }

  if (appearancePanel) {
    appearancePanel.addEventListener("click", function (e) {
      var themeBtn = e.target.closest("[data-theme]");
      var fontBtn = e.target.closest("[data-font]");
      var lineBtn = e.target.closest("[data-line]");
      var zoomBtn = e.target.closest("[data-zoom]");
      if (themeBtn) prefs.theme = themeBtn.getAttribute("data-theme");
      if (fontBtn) prefs.font = fontBtn.getAttribute("data-font");
      if (lineBtn) prefs.line = lineBtn.getAttribute("data-line");
      if (zoomBtn) prefs.zoom = zoomBtn.getAttribute("data-zoom");
      if (themeBtn || fontBtn || lineBtn || zoomBtn) {
        savePrefs(prefs);
        applyPrefs(prefs);
      }
    });
  }

  document.addEventListener("click", function (e) {
    if (!appearancePanel || appearancePanel.hidden) return;
    if (appearancePanel.contains(e.target) || (appearanceBtn && appearanceBtn.contains(e.target))) {
      return;
    }
    setAppearanceOpen(false);
  });

  /* ---- More menu actions ---- */
  var printBtn = document.getElementById("reader-print");
  if (printBtn) {
    printBtn.addEventListener("click", function () {
      window.print();
    });
  }

  var shareBtn = document.getElementById("reader-share");
  if (shareBtn) {
    shareBtn.addEventListener("click", function () {
      var url = window.location.href;
      if (navigator.share) {
        navigator.share({ title: document.title, url: url }).catch(function () {});
        return;
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(url).then(function () {
          shareBtn.textContent = "Link copied";
          window.setTimeout(function () { shareBtn.textContent = "Share"; }, 1600);
        }).catch(function () {});
      }
    });
  }

  var shortcutsDialog = document.getElementById("reader-shortcuts-dialog");
  var shortcutsBtn = document.getElementById("reader-shortcuts-btn");
  var shortcutsClose = document.getElementById("reader-shortcuts-close");
  if (shortcutsBtn && shortcutsDialog) {
    shortcutsBtn.addEventListener("click", function () {
      var more = document.getElementById("reader-more");
      if (more) more.open = false;
      if (shortcutsDialog.showModal) shortcutsDialog.showModal();
      else shortcutsDialog.setAttribute("open", "");
    });
  }
  if (shortcutsClose && shortcutsDialog) {
    shortcutsClose.addEventListener("click", function () {
      if (shortcutsDialog.close) shortcutsDialog.close();
      else shortcutsDialog.removeAttribute("open");
    });
  }

  document.addEventListener("keydown", function (e) {
    if (e.target && /input|textarea|select/i.test(e.target.tagName)) return;
    if (e.key === "Escape") {
      setAppearanceOpen(false);
      var more = document.getElementById("reader-more");
      if (more) more.open = false;
      return;
    }
    if (e.key === "ArrowLeft" && state.go) {
      e.preventDefault();
      state.go(state.page - 1);
    } else if (e.key === "ArrowRight" && state.go) {
      e.preventDefault();
      state.go(state.page + 1);
    } else if ((e.key === "b" || e.key === "B") && bookmarkBtn) {
      e.preventDefault();
      toggleBookmark();
    }
  });

  /* ---- PDF (pdf.js, one page at a time) ---- */
  function bootPdf() {
    if (!canvas || !fileUrl || !window.pdfjsLib) {
      if (statusEl) statusEl.textContent = "PDF viewer failed to load.";
      return;
    }
    var pdfjsLib = window.pdfjsLib;
    pdfjsLib.GlobalWorkerOptions.workerSrc =
      "https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/build/pdf.worker.min.js";

    // Say what went wrong rather than "could not open this PDF", which is the
    // same sentence whether the file is locked, half-uploaded, or opened fine
    // and only failed to draw — and leaves nobody anything to act on.
    function reportPdfProblem(err, stage) {
      var name = (err && err.name) || "";
      var says = stage === "draw"
        ? "This PDF opened, but the page wouldn't draw."
        : "We couldn't open this PDF here.";
      if (stage === "xfa") {
        says = "This is a LiveCycle form, which needs its own reader.";
      }
      if (name === "PasswordException") {
        says = "This PDF is locked with a password, so it can't be opened here.";
      } else if (name === "InvalidPDFException") {
        says = "This file isn't a PDF we can read — it may not have finished "
          + "uploading.";
      } else if (name === "MissingPDFException") {
        says = "This file isn't on the server any more, so there's nothing to "
          + "open. It needs uploading again in Studio.";
      } else if (name === "UnexpectedResponseException") {
        says = "The file didn't come through. Check your connection and reload.";
      }
      if (statusEl) {
        statusEl.textContent = says + " ";
        var link = document.createElement("a");
        link.href = fileUrl;
        link.target = "_blank";
        link.rel = "noopener";
        link.textContent = "Open it in a new tab instead";
        statusEl.appendChild(link);
      }
      if (window.console && console.error) {
        console.error("reader: PDF " + stage + " failed —",
                      name || "unknown error", (err && err.message) || "", err);
      }
    }

    var opened = false;
    pdfjsLib
      .getDocument({
        url: fileUrl,
        withCredentials: true,
        // Forms built in LiveCycle are XFA, and without this PDF.js shows the
        // "please wait" sheet such a file carries instead of the form.
        enableXfa: true,
        // The site's script policy has no unsafe-eval, so keep PDF.js on its
        // own interpreter for PDF functions rather than building JS at runtime.
        isEvalSupported: false,
      })
      .promise.then(function (pdf) {
        opened = true;
        // A LiveCycle form keeps its real content in XFA, which needs a viewer
        // of its own to draw. Rendering the page would give a blank sheet or
        // the "please wait" notice baked into the file, so hand it over to the
        // browser's own reader instead of pretending.
        if (pdf.isPureXfa) {
          reportPdfProblem({ name: "XfaOnly" }, "xfa");
          return null;
        }
        state.pdf = pdf;
        state.total = pdf.numPages || 0;
        if (state.page > state.total && state.total > 0) state.page = state.total;
        syncPageUi();
        buildToc();
        computePercent();
        prefetchText(1, Math.min(state.total, 12));
        return renderPage(state.page);
      })
      .then(function () {
        saveProgress();
      })
      .catch(function (err) {
        reportPdfProblem(err, opened ? "draw" : "open");
      });

    function prefetchText(from, to) {
      if (!state.pdf) return;
      var i = from;
      function next() {
        if (i > to || i > state.total) return;
        var pageNum = i++;
        state.pdf.getPage(pageNum).then(function (page) {
          return page.getTextContent();
        }).then(function (content) {
          var text = (content.items || []).map(function (it) {
            return it.str || "";
          }).join(" ").toLowerCase();
          state.pageText[pageNum] = text;
        }).catch(function () {}).then(next);
      }
      next();
    }

    function renderPage(num) {
      if (!state.pdf) return Promise.resolve();
      var token = ++state.renderToken;
      if (statusEl) statusEl.textContent = "Loading page " + num + "…";
      return state.pdf.getPage(num).then(function (page) {
        if (token !== state.renderToken) return;
        // The stage is shared with any written extracts attached to this
        // file, so measure the pane the page actually renders into.
        var pane = document.getElementById("reader-viewer")
          || document.getElementById("reader-stage");
        var zoom = prefs.zoom || "md";
        // Fit / Smaller: contain the full page in the pane (no scroll).
        // Larger: fill the pane width so text is readable; scroll vertically if needed.
        var availW = 800;
        var availH = 700;
        if (pane) {
          var cs = window.getComputedStyle(pane);
          var padX = (parseFloat(cs.paddingLeft) || 0) + (parseFloat(cs.paddingRight) || 0);
          var padY = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0);
          var chip = document.getElementById("reader-page-chip");
          var chipH = (chip && !chip.hidden) ? (chip.offsetHeight + 10) : 0;
          availW = Math.max(280, pane.clientWidth - padX);
          availH = Math.max(280, pane.clientHeight - padY - chipH);
        }
        var unscaled = page.getViewport({ scale: 1 });
        var contain = Math.min(availW / unscaled.width, availH / unscaled.height);
        var scale;
        if (zoom === "lg") {
          // Always larger than Fit: width-fill, or 1.28× contain when width already fills.
          var widthFill = availW / unscaled.width;
          scale = Math.max(widthFill, contain * 1.28);
        } else if (zoom === "sm") {
          scale = contain * 0.85;
        } else {
          scale = contain;
        }
        var viewport = page.getViewport({ scale: scale });
        var cssWidth = Math.floor(viewport.width);
        var cssHeight = Math.floor(viewport.height);
        var outputScale = Math.min(window.devicePixelRatio || 1, 2.5);
        canvas.width = Math.floor(cssWidth * outputScale);
        canvas.height = Math.floor(cssHeight * outputScale);
        // Explicit equal CSS sizing — never stretch one axis independently.
        canvas.style.width = cssWidth + "px";
        canvas.style.height = cssHeight + "px";
        canvas.style.maxWidth = "none";
        var ctx = canvas.getContext("2d");
        var transform =
          outputScale !== 1 ? [outputScale, 0, 0, outputScale, 0, 0] : null;
        // The default paints annotations, boxes to fill in included, straight
        // onto the page. They were kept off it while they were being drawn as
        // real inputs over the top, which would now leave a form looking blank.
        return page
          .render({
            canvasContext: ctx, viewport: viewport, transform: transform,
          })
          .promise.then(function () {
            if (statusEl) statusEl.textContent = "";
            if (!state.pageText[num]) {
              page.getTextContent().then(function (content) {
                state.pageText[num] = (content.items || []).map(function (it) {
                  return it.str || "";
                }).join(" ").toLowerCase();
              }).catch(function () {});
            }
          });
      });
    }

    function go(to) {
      if (!state.total) return;
      var next = Math.max(1, Math.min(state.total, to));
      state.page = next;
      computePercent();
      syncPageUi();
      renderPage(state.page).then(saveSoon);
    }
    state.go = go;
    state.rerender = function () {
      if (state.pdf && state.page) renderPage(state.page);
    };

    var prev = document.getElementById("reader-prev");
    var nextBtn = document.getElementById("reader-next");
    if (prev) prev.addEventListener("click", function () { go(state.page - 1); });
    if (nextBtn) nextBtn.addEventListener("click", function () { go(state.page + 1); });
    if (pageInput) {
      pageInput.addEventListener("change", function () {
        go(parseInt(pageInput.value, 10) || 1);
      });
    }
    window.addEventListener("resize", function () {
      if (state.rerenderTimer) window.clearTimeout(state.rerenderTimer);
      state.rerenderTimer = window.setTimeout(function () {
        state.rerender();
      }, 180);
    });
  }

  /* ---- Text / HTML ---- */
  function bootText() {
    var el = document.getElementById("reader-text");
    if (!el || !fileUrl) return;
    fetch(fileUrl, { credentials: "same-origin" })
      .then(function (r) { return r.text(); })
      .then(function (text) {
        if (kind === "html") {
          el.innerHTML = text;
        } else {
          el.textContent = text;
          el.style.whiteSpace = "pre-wrap";
        }
        state.total = 1;
        state.page = 1;
        if (state.percent < 5) state.percent = 5;
        state.go = function () {};
        syncPageUi();
        buildToc();
        computePercent();
        saveProgress();
      })
      .catch(function () {
        el.textContent = "Could not load this file.";
      });
  }

  /* ---- H5P ---- */
  function bootH5p() {
    var mount = document.getElementById("reader-h5p");
    var base = (root.getAttribute("data-h5p-path") || "").replace(/\/?$/, "/");
    if (!mount || !base || !window.H5PStandalone) {
      if (mount) mount.innerHTML = "<p class=\"field-help\">Could not load H5P player.</p>";
      return;
    }
    mount.innerHTML = "";
    var options = {
      h5pJsonPath: base,
      frameJs:
        "https://cdn.jsdelivr.net/npm/h5p-standalone@3.7.0/dist/frame.bundle.js",
      frameCss:
        "https://cdn.jsdelivr.net/npm/h5p-standalone@3.7.0/dist/styles/h5p.css",
    };
    Promise.resolve(new window.H5PStandalone.H5P(mount, options))
      .then(function () {
        state.total = 1;
        state.page = 1;
        if (state.percent < 8) {
          state.percent = Math.max(state.percent, 8);
        }
        state.go = function () {};
        syncPageUi();
        buildToc();
        setPill();
        saveProgress();
      })
      .catch(function () {
        mount.innerHTML = "<p class=\"field-help\">Could not open this H5P package.</p>";
      });
  }

  /* ---- Media / mark complete ---- */
  function bootSimpleMedia() {
    state.total = 1;
    state.page = 1;
    if (state.percent < 5) state.percent = 5;
    state.go = function () {};
    syncPageUi();
    buildToc();
    setPill();
    saveProgress();
  }

  var markBtn = document.getElementById("reader-mark-done");
  if (markBtn) {
    markBtn.addEventListener("click", function () {
      state.page = Math.max(state.page, state.total || 1);
      state.total = Math.max(state.total, 1);
      saveProgress({ percent: 100 });
      markBtn.textContent = "Completed";
      markBtn.disabled = true;
    });
  }

  setPill();
  syncBookmarkBtn();

  if (kind === "pdf") {
    function waitPdf() {
      if (window.pdfjsLib) bootPdf();
      else window.setTimeout(waitPdf, 40);
    }
    waitPdf();
  } else if (kind === "h5p") {
    function waitH5p() {
      if (window.H5PStandalone) bootH5p();
      else window.setTimeout(waitH5p, 40);
    }
    waitH5p();
  } else if (kind === "text" || kind === "html") {
    bootText();
  } else if (kind === "image" || kind === "video" || kind === "audio") {
    bootSimpleMedia();
  } else if (tocLoading) {
    tocLoading.textContent = "Open the file to continue.";
  }
})();
