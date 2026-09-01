/* Support session Daily.co room join (CSP-safe external script). */
(function () {
  function boot() {
    var root = document.getElementById("sg-daily-root");
    var statusEl = document.getElementById("sg-daily-status");
    var cfg = document.getElementById("sg-daily-config");
    function setStatus(msg) {
      if (statusEl) statusEl.textContent = msg;
    }
    if (!root || !cfg) {
      setStatus("Could not load the video room. Refresh and try again.");
      return;
    }
    if (!window.DailyIframe) {
      setStatus("Video library blocked or failed to load. Refresh, or check that Daily.co isn’t blocked.");
      return;
    }
    var roomUrl = cfg.getAttribute("data-room-url") || "";
    var token = cfg.getAttribute("data-token") || "";
    var wrapUrl = cfg.getAttribute("data-wrap-url") || "";
    if (!roomUrl || !token) {
      setStatus("This session is missing a video room. Leave and rejoin, or contact support.");
      return;
    }
    var callFrame;
    try {
      callFrame = window.DailyIframe.createFrame(root, {
        showLeaveButton: true,
        showFullscreenButton: true,
        iframeStyle: {
          width: "100%",
          height: "100%",
          border: "0",
          borderRadius: "18px",
          background: "#2a1524"
        },
        theme: {
          colors: {
            accent: "#7A2E62",
            accentText: "#FFF8F3",
            background: "#2a1524",
            backgroundAccent: "#3d1431",
            baseText: "#FFF8F3",
            border: "#5c2f4c",
            mainAreaBg: "#2a1524",
            mainAreaBgAccent: "#3d1431",
            mainAreaText: "#FFF8F3",
            supportiveText: "#E8D5DF"
          }
        }
      });
    } catch (err) {
      setStatus((err && err.message) ? err.message : "Couldn’t start the video room.");
      return;
    }
    var joined = false;
    var joinTimer = setTimeout(function () {
      if (!joined) {
        setStatus("Still connecting… If this stays blank, refresh the page. Rooms open at the scheduled start.");
      }
    }, 12000);
    function markJoined() {
      if (joined) return;
      joined = true;
      clearTimeout(joinTimer);
      setStatus("You’re in the room. Camera and mic start off — turn them on when ready.");
    }
    callFrame.on("joined-meeting", markJoined);
    callFrame.on("error", function (ev) {
      clearTimeout(joinTimer);
      var detail = (ev && (ev.errorMsg || ev.error)) || "";
      setStatus(detail
        ? ("Couldn’t join: " + detail)
        : "Couldn’t join the room. Check camera/mic permissions and try again.");
    });
    callFrame.join({ url: roomUrl, token: token })
      .then(markJoined)
      .catch(function (err) {
        clearTimeout(joinTimer);
        var detail = (err && (err.message || err.errorMsg)) || "";
        setStatus(detail
          ? ("Couldn’t join: " + detail)
          : "Couldn’t join the room. Check permissions and try again.");
      });
    callFrame.on("left-meeting", function () {
      if (wrapUrl) window.location.href = wrapUrl;
    });

    startEndingNotice(cfg);
  }

  /* A quiet "5 minutes left" down the side of the room, so the end doesn't
     arrive out of nowhere. Timed against the server's clock rather than the
     browser's, which can be minutes out. */
  function startEndingNotice(cfg) {
    var WARN_MS = 5 * 60 * 1000;
    var endsMs = parseInt(cfg.getAttribute("data-ends-ms") || "0", 10);
    var serverMs = parseInt(cfg.getAttribute("data-server-ms") || "0", 10);
    var box = document.getElementById("sg-room-ending");
    var label = document.getElementById("sg-room-ending-time");
    if (!endsMs || !serverMs || !box || !label) return;
    var skew = serverMs - Date.now();

    function tick() {
      var left = endsMs - (Date.now() + skew);
      if (left <= 0) {
        label.textContent = "Time's up";
        box.hidden = false;
        return;
      }
      if (left > WARN_MS) {
        box.hidden = true;
        return;
      }
      var mins = Math.ceil(left / 60000);
      label.textContent = mins === 1 ? "1 minute left" : mins + " minutes left";
      box.hidden = false;
    }

    tick();
    window.setInterval(tick, 15000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
