/* Live countdowns — the time left on a sale, a shelf date, a locked module.
   The server already wrote the words; this keeps them moving (CSP-safe). */
(function () {
  function boot() {
    var nodes = [].slice.call(document.querySelectorAll("[data-countdown]"));
    if (!nodes.length) return;

    // A device clock that is out by hours would otherwise call a live sale
    // over, or keep counting one that has ended, so the page carries ours.
    var served = Number(document.documentElement.getAttribute("data-now-ms"));
    var skew = isFinite(served) && served > 0 ? served - Date.now() : 0;
    var timer = null;
    var refreshAt = 0;

    function pad(n) { return n < 10 ? "0" + n : String(n); }

    function some(n, word) { return n + " " + word + (n === 1 ? "" : "s"); }

    /* Same ladder as time_left_words() in app/services/timefmt.py, so nothing
       jumps between what arrived with the page and the first tick. */
    function words(ms) {
      var total = Math.floor(ms / 1000);
      if (total <= 0) return "";
      var days = Math.floor(total / 86400);
      var hours = Math.floor((total % 86400) / 3600);
      var mins = Math.floor((total % 3600) / 60);
      var secs = total % 60;
      if (days >= 7) return some(days, "day") + " left";
      if (days >= 1) {
        if (!hours) return some(days, "day") + " left";
        return some(days, "day") + ", " + some(hours, "hour") + " left";
      }
      if (hours) return hours + ":" + pad(mins) + ":" + pad(secs) + " left";
      return mins + ":" + pad(secs) + " left";
    }

    function reload() {
      // Nobody wants a tab they left open reloading itself out of sight; it
      // waits until they come back and look at it.
      if (document.hidden) {
        document.addEventListener("visibilitychange", function once() {
          document.removeEventListener("visibilitychange", once);
          if (!document.hidden) window.location.reload();
        });
        return;
      }
      window.location.reload();
    }

    function tick() {
      var now = Date.now() + skew;
      var nearest = Infinity;
      var live = 0;
      nodes = nodes.filter(function (el) {
        var when = Date.parse(el.getAttribute("data-countdown"));
        if (!isFinite(when)) return false;
        var left = when - now;
        var text = words(left);
        if (text) {
          live += 1;
          if (left < nearest) nearest = left;
          el.classList.remove("countdown--done");
        } else {
          text = el.getAttribute("data-countdown-zero") || "";
          if (!text) { el.parentNode && el.parentNode.removeChild(el); return false; }
          el.classList.add("countdown--done");
          if (el.getAttribute("data-countdown-refresh") === "1" && !refreshAt) {
            refreshAt = now + 4000;
          }
        }
        if (el.textContent !== text) el.textContent = text;
        return true;
      });
      if (refreshAt && now >= refreshAt) { reload(); return; }
      if (!live && !refreshAt) return;
      // Seconds are showing inside a day, so tick every one; further out
      // nothing changes for minutes at a time.
      var wait = refreshAt ? 500 : (nearest < 86400000 ? 1000 : 30000);
      timer = window.setTimeout(tick, wait);
    }

    if (timer) window.clearTimeout(timer);
    tick();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
