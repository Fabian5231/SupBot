// Pillenbox: NFC-Chips lesen ("Box scannen") und beschreiben (Einstellungen).
// Web NFC gibt es nur in Chrome auf Android – sonst bleiben die Knoepfe versteckt
// und die Chips funktionieren trotzdem, weil Android den Link selbst oeffnet.
(function () {
  "use strict";

  var BOX_PATH = /^\/box\/(mo|di|mi|do|fr|sa|so)\/?$/;
  var supported = "NDEFReader" in window && window.isSecureContext;
  var controller = null;

  document.querySelectorAll("[data-nfc-only]").forEach(function (el) { el.hidden = !supported; });
  document.querySelectorAll("[data-nfc-missing]").forEach(function (el) { el.hidden = supported; });
  document.querySelectorAll("[data-box-url]").forEach(function (el) {
    el.textContent = location.origin + el.dataset.boxUrl;
  });

  function status(text) {
    document.querySelectorAll("[data-nfc-status]").forEach(function (el) { el.textContent = text; });
  }

  function errorText(err) {
    if (err && err.name === "NotAllowedError") return "NFC-Zugriff wurde nicht erlaubt.";
    if (err && err.name === "AbortError") return "";
    return "NFC nicht verfügbar – ist NFC am Handy eingeschaltet?";
  }

  // Laufenden Scan/Schreibvorgang beenden, bevor ein neuer startet
  function fresh() {
    if (controller) controller.abort();
    controller = new AbortController();
    return controller.signal;
  }

  function boxPath(event) {
    var decoder = new TextDecoder();
    for (var i = 0; i < event.message.records.length; i++) {
      var record = event.message.records[i];
      if (record.recordType !== "url" && record.recordType !== "absolute-url") continue;
      try {
        var path = new URL(decoder.decode(record.data), location.origin).pathname;
        if (BOX_PATH.test(path)) return path;
      } catch (e) { /* kein gueltiger Link */ }
    }
    return null;
  }

  function scan(button) {
    var reader = new NDEFReader();
    var signal = fresh();
    button.disabled = true;
    reader.onreading = function (event) {
      var path = boxPath(event);
      if (!path) {
        status("Unbekannter Chip – in den Einstellungen anlernen.");
        return;
      }
      controller.abort();
      status("Fach erkannt …");
      location.href = path;
    };
    reader.onreadingerror = function () { status("Chip nicht lesbar – nochmal ranhalten."); };
    reader.scan({ signal: signal })
      .then(function () { status("Halte das Handy an ein Fach …"); })
      .catch(function (err) { status(errorText(err)); })
      .finally(function () { button.disabled = false; });
  }

  function write(button) {
    var row = button.closest(".box-row");
    var url = location.origin + row.querySelector("[data-box-url]").dataset.boxUrl;
    var name = row.querySelector(".box-name").textContent;
    var reader = new NDEFReader();
    button.disabled = true;
    status("Halte jetzt den Chip für " + name + " ans Handy …");
    reader.write({ records: [{ recordType: "url", data: url }] }, { signal: fresh() })
      .then(function () { status("Chip für " + name + " beschrieben."); })
      .catch(function (err) { status(errorText(err) || "Abgebrochen."); })
      .finally(function () { button.disabled = false; });
  }

  function copy(button) {
    var url = button.closest(".box-row").querySelector("[data-box-url]").textContent;
    var done = function () {
      button.textContent = "Kopiert";
      setTimeout(function () { button.textContent = "Kopieren"; }, 1500);
    };
    if (navigator.clipboard) {
      navigator.clipboard.writeText(url).then(done, function () { prompt("Link kopieren:", url); });
    } else {
      prompt("Link kopieren:", url);
    }
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-nfc-scan], [data-nfc-write], [data-copy-box]");
    if (!button) return;
    if (button.matches("[data-copy-box]")) return copy(button);
    if (!supported) return;
    if (button.matches("[data-nfc-scan]")) return scan(button);
    write(button);
  });
})();
