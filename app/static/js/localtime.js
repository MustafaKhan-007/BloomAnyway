/* Bloom Anyway — every time on the reader's own clock.
 *
 * The browser already knows which IANA zone the device keeps, so nothing here
 * asks for anyone's location: no permission prompt, no guessing from an IP
 * address, and it stays right through daylight saving. The zone goes into a
 * cookie (and onto the account, for anyone signed in) so the server can render
 * the next page in it.
 *
 * That leaves the first page of a first visit, which the server had to render
 * in UTC because it had not been told anything yet. Every timestamp carries
 * the instant it stands for and the wording it was written in, so those are
 * redrawn here rather than being an hour or a day out until the next click.
 */
(function () {
  "use strict";

  var zone = "";
  try {
    zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  } catch (e) { zone = ""; }
  if (!zone) return;

  /* ---- remember it, so the server renders the next page in it ----
   * Written plain: a zone name is only letters, digits and separators, and
   * escaping it left the server holding "America%2FDenver", which is not a
   * place. */
  var secure = window.location.protocol === "https:" ? ";Secure" : "";
  document.cookie = "tz=" + zone.replace(/[^A-Za-z0-9_+\/-]/g, "") +
    ";path=/;max-age=31536000;SameSite=Lax" + secure;

  var body = document.body;
  var syncUrl = body && body.getAttribute("data-tz-sync");
  var csrf = body && body.getAttribute("data-csrf");
  if (syncUrl && csrf) {
    fetch(syncUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrf,
        "X-Requested-With": "fetch",
        "Accept": "application/json"
      },
      credentials: "same-origin",
      body: JSON.stringify({ timezone: zone })
    }).catch(function () {});
  }

  var root = document.documentElement;
  var served = root.getAttribute("data-tz") || "";

  /* ---- tell the settings page which zone this device keeps ---- */
  document.querySelectorAll("[data-tz-detected]").forEach(function (el) {
    if (el.tagName === "INPUT") el.value = zone;
    else el.textContent = zone;
  });
  if (served && served !== zone) {
    // Only worth mentioning when it isn't the zone they're already reading in.
    document.querySelectorAll("[data-tz-device-only]").forEach(function (el) {
      el.hidden = false;
    });
  }

  /* ---- redraw anything the server wrote on a different clock ---- */
  // Somebody who picked a zone in settings meant it, even sitting somewhere else.
  if (root.hasAttribute("data-tz-pinned")) return;
  if (!served || served === zone) return;

  var cache = {};
  function parts(date) {
    if (!cache.a) {
      cache.a = new Intl.DateTimeFormat("en-US", {
        timeZone: zone, weekday: "long", month: "long",
        day: "2-digit", year: "numeric"
      });
      cache.b = new Intl.DateTimeFormat("en-US", {
        timeZone: zone, weekday: "short", month: "short"
      });
      cache.c = new Intl.DateTimeFormat("en-US", {
        timeZone: zone, month: "2-digit", hour: "2-digit",
        minute: "2-digit", second: "2-digit", hour12: true
      });
      cache.d = new Intl.DateTimeFormat("en-US", {
        timeZone: zone, hour: "2-digit", hourCycle: "h23"
      });
    }
    var out = { long: {}, short: {}, num: {}, h23: "" };
    cache.a.formatToParts(date).forEach(function (p) { out.long[p.type] = p.value; });
    cache.b.formatToParts(date).forEach(function (p) { out.short[p.type] = p.value; });
    cache.c.formatToParts(date).forEach(function (p) { out.num[p.type] = p.value; });
    cache.d.formatToParts(date).forEach(function (p) {
      if (p.type === "hour") out.h23 = p.value;
    });
    return out;
  }

  function pad(value) {
    value = String(value == null ? "" : value);
    return value.length < 2 ? "0" + value : value;
  }

  function token(letter, p) {
    switch (letter) {
      case "a": return p.short.weekday;
      case "A": return p.long.weekday;
      case "b": return p.short.month;
      case "B": return p.long.month;
      case "d": return pad(p.long.day);
      case "m": return pad(p.num.month);
      case "Y": return p.long.year;
      case "y": return String(p.long.year).slice(-2);
      case "H": return pad(p.h23);
      case "I": return pad(p.num.hour);
      case "M": return pad(p.num.minute);
      case "S": return pad(p.num.second);
      case "p": return (p.num.dayPeriod || "").toUpperCase();
      case "%": return "%";
      default: return null;
    }
  }

  /* The same wording Python wrote, read back one token at a time. A pattern
   * with anything unfamiliar in it is left exactly as the server sent it. */
  function restate(pattern, date) {
    var p = parts(date);
    var out = "";
    for (var i = 0; i < pattern.length; i++) {
      if (pattern[i] !== "%") { out += pattern[i]; continue; }
      var piece = token(pattern[i + 1], p);
      if (piece == null) return null;
      out += piece;
      i++;
    }
    return out;
  }

  function apply(within) {
    (within || document).querySelectorAll("time[data-when]").forEach(function (el) {
      var stamp = el.getAttribute("datetime");
      var pattern = el.getAttribute("data-when");
      if (!stamp || !pattern) return;
      var date = new Date(stamp);
      if (isNaN(date.getTime())) return;
      var text = restate(pattern, date);
      if (text) el.textContent = text;
    });
  }

  apply(document);
  window.BloomLocalTime = { zone: zone, apply: apply };
})();
