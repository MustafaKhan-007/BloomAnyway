/* Sunflower page loader — shows while navigating / submitting forms. */
(function () {
  "use strict";

  var el = document.getElementById("page-loader");
  if (!el) return;

  var hideTimer = null;
  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function show() {
    if (hideTimer) {
      clearTimeout(hideTimer);
      hideTimer = null;
    }
    el.hidden = false;
    el.setAttribute("aria-hidden", "false");
    document.documentElement.classList.add("is-page-loading");
  }

  function hide() {
    el.hidden = true;
    el.setAttribute("aria-hidden", "true");
    document.documentElement.classList.remove("is-page-loading");
  }

  function softHide() {
    // Brief linger so fast navigations still feel intentional.
    hideTimer = setTimeout(hide, reduced ? 0 : 120);
  }

  // Initial page load
  if (document.readyState !== "complete") {
    show();
    window.addEventListener("load", softHide);
  }
  window.addEventListener("pageshow", function () {
    hide();
  });

  function sameOrigin(href) {
    try {
      var u = new URL(href, window.location.href);
      return u.origin === window.location.origin;
    } catch (err) {
      return false;
    }
  }

  document.addEventListener("click", function (e) {
    if (e.defaultPrevented || e.button !== 0) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    if (!a) return;
    if (a.target && a.target !== "_self") return;
    if (a.hasAttribute("download")) return;
    var href = a.getAttribute("href") || "";
    if (!href || href.charAt(0) === "#") return;
    if (href.indexOf("javascript:") === 0 || href.indexOf("mailto:") === 0
        || href.indexOf("tel:") === 0) return;
    if (!sameOrigin(href)) return;
    // Same-page hash-only after path
    try {
      var next = new URL(href, window.location.href);
      if (next.pathname === window.location.pathname
          && next.search === window.location.search
          && next.hash) return;
    } catch (err) {}
    show();
  }, true);

  document.addEventListener("submit", function (e) {
    if (e.defaultPrevented) return;
    var form = e.target;
    if (!form || form.tagName !== "FORM") return;
    if (form.getAttribute("data-no-loader") != null) return;
    if (form.target && form.target !== "_self") return;
    // Confirm dialogs intercept submit in the bubble phase. Skip the loader until
    // the user accepts (dataset.confirmAccepted), or Cancel leaves it spinning forever.
    if (form.hasAttribute("data-confirm") && form.dataset.confirmAccepted !== "1") return;
    show();
  }, true);

  document.addEventListener("site-confirm-dismiss", hide);

  /* ---- remember scroll across reload / form redirects / same-page updates ---- */
  (function () {
    var PREFIX = "ba:scroll:";
    var saveTimer = null;

    function storageKey(pathname, search) {
      return PREFIX + (pathname || "") + (search || "");
    }

    function currentKey() {
      return storageKey(window.location.pathname, window.location.search);
    }

    function save() {
      try {
        var y = window.scrollY || window.pageYOffset || 0;
        sessionStorage.setItem(currentKey(), String(Math.max(0, Math.round(y))));
      } catch (err) { /* private mode / quota */ }
    }

    function scheduleSave() {
      if (saveTimer) clearTimeout(saveTimer);
      saveTimer = setTimeout(save, 120);
    }

    // Whether the person is in a field right now. On a phone, opening the
    // keyboard scrolls the page, and scrolling it back from under them cancels
    // that — the keyboard flashes up and drops straight back down, so the
    // field can't be typed in. Where they left off last time never matters
    // more than the field they are in now.
    function typing() {
      var el = document.activeElement;
      if (!el || el === document.body) return false;
      var tag = (el.tagName || "").toLowerCase();
      return tag === "input" || tag === "textarea" || tag === "select"
        || el.isContentEditable === true;
    }

    // Restoring runs a few times, because the page is still settling and one
    // scroll often doesn't stick. Any of those can land after a tap, so each
    // repeat checks again, and the first touch of the page calls the whole
    // thing off: they are driving now.
    var touched = false;
    function handOver() { touched = true; }
    ["pointerdown", "touchstart", "keydown", "wheel"].forEach(function (evt) {
      window.addEventListener(evt, handOver, { passive: true });
    });
    // Coming back to a page held in memory is a fresh arrival, so it is owed
    // its scroll position again. Registered here, before the listener that
    // does the restoring, so the flag is clear by the time that runs.
    window.addEventListener("pageshow", function () { touched = false; });

    function restore() {
      if (window.location.hash || touched || typing()) return;
      var raw;
      try {
        raw = sessionStorage.getItem(currentKey());
      } catch (err) {
        return;
      }
      if (raw == null || raw === "") return;
      var y = parseInt(raw, 10);
      if (!isFinite(y) || y < 1) return;

      var apply = function () {
        if (touched || typing()) return;
        window.scrollTo(0, y);
      };
      apply();
      requestAnimationFrame(function () {
        apply();
        setTimeout(apply, 60);
        setTimeout(apply, 250);
      });
    }

    if ("scrollRestoration" in history) {
      try {
        history.scrollRestoration = "manual";
      } catch (err) { /* ignore */ }
    }

    window.addEventListener("scroll", scheduleSave, { passive: true });
    window.addEventListener("pagehide", save);
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden") save();
    });

    // Keep scroll for reloads / same-page form redirects; open other pages at the top.
    document.addEventListener("click", function (e) {
      if (e.defaultPrevented || e.button !== 0) return;
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      var a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
      if (!a || (a.target && a.target !== "_self") || a.hasAttribute("download")) return;
      var href = a.getAttribute("href") || "";
      if (!href || href.charAt(0) === "#" || href.indexOf("javascript:") === 0) return;
      if (!sameOrigin(href)) return;
      try {
        var next = new URL(href, window.location.href);
        if (next.pathname !== window.location.pathname
            || next.search !== window.location.search) {
          sessionStorage.removeItem(storageKey(next.pathname, next.search));
        }
      } catch (err) { /* ignore */ }
      save();
    }, true);

    document.addEventListener("submit", function () {
      save();
    }, true);

    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", restore);
    } else {
      restore();
    }
    window.addEventListener("load", restore);
    window.addEventListener("pageshow", restore);
  })();
})();
