/* Choosing who a gift is for.

   Somebody types an address or a name and the page shows them whose it is
   before any money moves — a gift sent one letter wrong goes to a stranger
   and there is no getting it back. Picking a face fixes the account by id,
   so a typo afterwards can't change where it lands.

   Without JavaScript the form still works: the address they wrote is used
   as written. (CSP-safe: no inline handlers.) */
(function () {
  function boot() {
    var form = document.querySelector("[data-gift-form]");
    if (!form) return;

    var input = form.querySelector("[data-gift-input]");
    var hidden = form.querySelector("[data-gift-id]");
    var list = form.querySelector("[data-gift-list]");
    var picked = form.querySelector("[data-gift-picked]");
    var pickedName = form.querySelector("[data-gift-picked-name]");
    var pickedMeta = form.querySelector("[data-gift-picked-meta]");
    var pickedFace = form.querySelector("[data-gift-picked-face]");
    var clearBtn = form.querySelector("[data-gift-clear]");
    var none = form.querySelector("[data-gift-none]");
    var note = form.querySelector("[data-gift-note]");
    var left = form.querySelector("[data-gift-left]");
    var url = form.getAttribute("data-gift-who");
    if (!input || !list) return;

    var rows = [];
    var highlight = -1;
    var timer = null;
    var reqId = 0;
    var lastAsked = "";

    function looksWhole(text) {
      var at = text.indexOf("@");
      return at > 0 && text.indexOf(".", at) > at + 1;
    }

    function hide() {
      list.hidden = true;
      list.innerHTML = "";
      rows = [];
      highlight = -1;
      input.setAttribute("aria-expanded", "false");
    }

    function showNone(on) {
      if (none) none.hidden = !on;
    }

    function face(row) {
      var span = document.createElement("span");
      span.className = "avatar avatar--sm";
      if (row.avatar) {
        span.style.backgroundImage = "url('" + row.avatar + "')";
      } else {
        span.textContent = row.initials || "";
      }
      return span;
    }

    function meta(row) {
      var bits = [];
      if (row.handle) bits.push(row.handle);
      if (row.tier) bits.push(row.tier);
      return bits.join(" \u00b7 ");
    }

    function choose(row) {
      if (hidden) hidden.value = String(row.id || "");
      if (pickedName) pickedName.textContent = row.name || "";
      if (pickedMeta) pickedMeta.textContent = meta(row);
      if (pickedFace) {
        pickedFace.style.backgroundImage = row.avatar
          ? "url('" + row.avatar + "')" : "";
        pickedFace.textContent = row.avatar ? "" : (row.initials || "");
      }
      if (picked) picked.hidden = false;
      showNone(false);
      hide();
      if (note) note.focus();
    }

    function unchoose() {
      if (hidden) hidden.value = "";
      if (picked) picked.hidden = true;
      hide();
      input.focus();
      input.select();
    }

    function render(found, asked) {
      rows = found || [];
      list.innerHTML = "";
      if (!rows.length) {
        hide();
        // Only worth saying about a whole address: half of one finding
        // nobody means nothing yet.
        showNone(looksWhole(asked));
        return;
      }
      showNone(false);
      rows.forEach(function (row, i) {
        var item = document.createElement("button");
        item.type = "button";
        item.className = "gift-who__row";
        item.setAttribute("role", "option");
        item.setAttribute("aria-selected", "false");
        item.appendChild(face(row));
        var who = document.createElement("span");
        who.className = "gift-who__who";
        var strong = document.createElement("strong");
        strong.textContent = row.name || "";
        who.appendChild(strong);
        var sub = document.createElement("span");
        sub.className = "gift-who__meta";
        sub.textContent = meta(row);
        who.appendChild(sub);
        item.appendChild(who);
        item.addEventListener("click", function () { choose(row); });
        item.addEventListener("mousemove", function () { mark(i); });
        list.appendChild(item);
      });
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
      mark(0);
    }

    function mark(i) {
      highlight = i;
      [].slice.call(list.children).forEach(function (el, n) {
        el.classList.toggle("is-on", n === i);
        el.setAttribute("aria-selected", n === i ? "true" : "false");
      });
    }

    function ask() {
      var text = (input.value || "").trim();
      if (!url || text.length < 2) { hide(); showNone(false); return; }
      if (text === lastAsked) return;
      lastAsked = text;
      var mine = ++reqId;
      fetch(url + "?q=" + encodeURIComponent(text), {
        headers: { "Accept": "application/json" },
        credentials: "same-origin"
      }).then(function (res) {
        if (!res.ok) throw new Error("no");
        return res.json();
      }).then(function (found) {
        if (mine !== reqId) return;
        render(Array.isArray(found) ? found : [], text);
      }).catch(function () {
        if (mine !== reqId) return;
        // Signed out, or the lookup is down. The address they wrote still
        // works; it just isn't shown back to them.
        hide();
        showNone(looksWhole(text));
      });
    }

    input.addEventListener("input", function () {
      if (hidden && hidden.value) unchoose();
      if (timer) clearTimeout(timer);
      timer = setTimeout(ask, 160);
    });

    input.addEventListener("keydown", function (e) {
      if (list.hidden || !rows.length) return;
      if (e.key === "ArrowDown") {
        e.preventDefault();
        mark((highlight + 1) % rows.length);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        mark((highlight - 1 + rows.length) % rows.length);
      } else if (e.key === "Enter" || e.key === "Tab") {
        if (highlight >= 0 && rows[highlight]) {
          e.preventDefault();
          choose(rows[highlight]);
        }
      } else if (e.key === "Escape") {
        hide();
      }
    });

    document.addEventListener("click", function (e) {
      if (!list.hidden && !list.contains(e.target) && e.target !== input) {
        hide();
      }
    });

    if (clearBtn) clearBtn.addEventListener("click", unchoose);

    if (note && left) {
      var max = Number(note.getAttribute("maxlength")) || 400;
      var count = function () {
        left.textContent = String(Math.max(0, max - (note.value || "").length));
      };
      note.addEventListener("input", count);
      count();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
