// Abhaken ohne Seitenreload.
(function () {
  "use strict";

  // Animation neu starten, auch wenn die Klasse schon gesetzt war
  function replay(el, cls) {
    if (!el) return;
    el.classList.remove(cls);
    void el.offsetWidth;
    el.classList.add(cls);
  }

  function confetti() {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    var colors = ["#0f766e", "#3fb9a8", "#5b6fd6", "#e3a547", "#f07167", "#8c9cf0"];
    for (var i = 0; i < 70; i++) {
      var piece = document.createElement("span");
      piece.className = "confetti";
      piece.style.left = Math.random() * 100 + "vw";
      piece.style.background = colors[i % colors.length];
      piece.style.setProperty("--y0", (-10 - Math.random() * 20) + "vh");
      piece.style.setProperty("--dx", (Math.random() * 160 - 80) + "px");
      piece.style.setProperty("--rot", (Math.random() * 900 - 450) + "deg");
      piece.style.setProperty("--dur", (1.4 + Math.random() * 1.2) + "s");
      piece.style.animationDelay = Math.random() * .25 + "s";
      piece.addEventListener("animationend", function () { this.remove(); });
      document.body.appendChild(piece);
    }
  }

  function updateProgress(stats) {
    var bar = document.getElementById("progress-bar");
    var text = document.getElementById("progress-text");
    var ring = bar && bar.closest(".progress-ring");
    var wasFull = ring && ring.classList.contains("full");
    var full = stats.total > 0 && stats.done === stats.total;
    if (bar) bar.style.width = stats.percent + "%";
    if (text && text.textContent !== stats.done + "/" + stats.total) {
      text.textContent = stats.done + "/" + stats.total;
      replay(text, "bump");
    }
    var percent = document.getElementById("progress-percent");
    if (percent) percent.textContent = stats.percent + " %";
    if (ring) ring.classList.toggle("full", full);
    if (full && ring && !wasFull) confetti();
  }

  function refreshSlot(slotEl) {
    if (!slotEl) return;
    var boxes = slotEl.querySelectorAll("input[data-intake]");
    var done = 0;
    boxes.forEach(function (box) { if (box.checked) done += 1; });
    var meta = slotEl.querySelector(".slot-head .muted");
    if (meta) meta.textContent = meta.textContent.replace(/\d+\/\d+$/, done + "/" + boxes.length);
    var wasComplete = slotEl.classList.contains("complete");
    slotEl.classList.toggle("complete", done === boxes.length);
    if (done === boxes.length && !wasComplete) replay(slotEl, "celebrate");
    var button = slotEl.querySelector("[data-slot-done]");
    if (button) button.hidden = done === boxes.length;
    var pill = slotEl.querySelector("[data-slot-pill]");
    if (pill) pill.hidden = done !== boxes.length;
  }

  document.addEventListener("change", function (event) {
    var input = event.target.closest("input[data-intake]");
    if (!input) return;
    var label = input.closest(".check");
    label.classList.toggle("checked", input.checked);
    if (input.checked) replay(label, "pop");
    input.disabled = true;

    fetch("/api/intake/" + input.dataset.intake + "/toggle", { method: "POST" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (data) {
        input.checked = data.taken;
        label.classList.toggle("checked", data.taken);
        updateProgress(data.stats);
        refreshSlot(input.closest(".slot"));
      })
      .catch(function () {
        input.checked = !input.checked;
        label.classList.toggle("checked", input.checked);
        alert("Konnte nicht gespeichert werden.");
      })
      .finally(function () { input.disabled = false; });
  });

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-slot-done]");
    if (!button) return;
    event.preventDefault();
    button.disabled = true;
    var slotId = button.dataset.slotDone;
    var day = button.dataset.day;
    fetch("/api/slot/" + slotId + "/done?day=" + encodeURIComponent(day), { method: "POST" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var slotEl = button.closest(".slot");
        var delay = 0;
        slotEl.querySelectorAll("input[data-intake]").forEach(function (box) {
          if (box.checked) return;
          box.checked = true;
          var label = box.closest(".check");
          // Haken nacheinander setzen, nicht alle auf einmal
          setTimeout(function () { label.classList.add("checked"); replay(label, "pop"); }, delay);
          delay += 70;
        });
        updateProgress(data.stats);
        refreshSlot(slotEl);
      })
      .catch(function () { alert("Konnte nicht gespeichert werden."); })
      .finally(function () { button.disabled = false; });
  });

  // Pillenbox-Fach: alle Einnahmen des Tages auf einmal
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-day-done]");
    if (!button) return;
    event.preventDefault();
    button.disabled = true;
    fetch("/api/day/done?source=nfc&day=" + encodeURIComponent(button.dataset.dayDone), { method: "POST" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (data) {
        document.querySelectorAll(".slot").forEach(function (slotEl) {
          slotEl.querySelectorAll("input[data-intake]").forEach(function (box) {
            box.checked = true;
            box.closest(".check").classList.add("checked");
          });
          refreshSlot(slotEl);
        });
        updateProgress(data.stats);
        button.closest("[data-day-box]").outerHTML =
          '<div class="alert ok"><svg class="icon" viewBox="0 0 24 24" width="20" height="20" fill="none" ' +
          'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
          '<circle cx="12" cy="12" r="9.5"/><path d="m8.5 12 2.5 2.5 4.5-5"/></svg>' +
          '<div>Erledigt – gut gemacht!</div></div>';
        confetti();
      })
      .catch(function () {
        button.disabled = false;
        alert("Konnte nicht gespeichert werden.");
      });
  });

  // Plan-Zeile im Supplement-Formular optisch mitschalten
  document.addEventListener("change", function (event) {
    var input = event.target.closest('.slot-row input[type="checkbox"][name^="slot_"]');
    if (!input) return;
    input.closest(".slot-row").classList.toggle("on", input.checked);
  });
})();
