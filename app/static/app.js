// Abhaken ohne Seitenreload.
(function () {
  "use strict";

  function updateProgress(stats) {
    var bar = document.getElementById("progress-bar");
    var text = document.getElementById("progress-text");
    if (bar) bar.style.width = stats.percent + "%";
    if (text) text.textContent = stats.done + "/" + stats.total;
  }

  function refreshSlot(slotEl) {
    if (!slotEl) return;
    var boxes = slotEl.querySelectorAll("input[data-intake]");
    var done = 0;
    boxes.forEach(function (box) { if (box.checked) done += 1; });
    var meta = slotEl.querySelector(".slot-head .muted");
    if (meta) meta.textContent = meta.textContent.replace(/\d+\/\d+$/, done + "/" + boxes.length);
    slotEl.classList.toggle("complete", done === boxes.length);
    var button = slotEl.querySelector("[data-slot-done]");
    if (button) button.style.display = done === boxes.length ? "none" : "";
  }

  document.addEventListener("change", function (event) {
    var input = event.target.closest("input[data-intake]");
    if (!input) return;
    var label = input.closest(".check");
    label.classList.toggle("checked", input.checked);
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
        slotEl.querySelectorAll("input[data-intake]").forEach(function (box) {
          box.checked = true;
          box.closest(".check").classList.add("checked");
        });
        updateProgress(data.stats);
        refreshSlot(slotEl);
      })
      .catch(function () { alert("Konnte nicht gespeichert werden."); })
      .finally(function () { button.disabled = false; });
  });

  // Plan-Zeile im Supplement-Formular optisch mitschalten
  document.addEventListener("change", function (event) {
    var input = event.target.closest('.slot-row input[type="checkbox"][name^="slot_"]');
    if (!input) return;
    input.closest(".slot-row").classList.toggle("on", input.checked);
  });
})();
