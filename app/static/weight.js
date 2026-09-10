/* Gewichtsseite: Foto der Waage -> Erkennung -> pruefen -> speichern. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const DEFAULT_CROP = { x: 0.15, y: 0.34, w: 0.70, h: 0.32 };
  const MIN_CROP = 0.06;
  const ENTRY_PAGE = 12;

  const state = {
    measurements: [],
    stats: {},
    rangeDays: Number(localStorage.getItem("supbot.weight.range") || 30),
    crop: readStoredCrop(),
    file: null,
    imageToken: null,
    ocrRaw: null,
    ocrConfidence: null,
    source: "manual",
    entriesExpanded: false,
  };

  function readStoredCrop() {
    try {
      const raw = JSON.parse(localStorage.getItem("supbot.weight.crop"));
      if (raw && ["x", "y", "w", "h"].every((k) => typeof raw[k] === "number")) return raw;
    } catch (err) { /* Standardrahmen nutzen */ }
    return { ...DEFAULT_CROP };
  }

  // ---------------------------------------------------------------- API

  async function api(path, options = {}) {
    const settings = { ...options };
    settings.headers = { ...(options.headers || {}) };
    if (options.json !== undefined) {
      settings.headers["Content-Type"] = "application/json";
      settings.body = JSON.stringify(options.json);
      settings.method = settings.method || "POST";
    }
    const response = await fetch(path, settings);
    if (response.status === 401) {
      window.location.href = "/login";
      throw new Error("nicht angemeldet");
    }
    const text = await response.text();
    const data = text ? JSON.parse(text) : null;
    if (!response.ok) throw new Error((data && data.detail) || `Fehler ${response.status}`);
    return data;
  }

  // ------------------------------------------------------------ Formate

  const nf1 = new Intl.NumberFormat("de-DE", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const fmt = (value) => (value === null || value === undefined ? "–" : nf1.format(value));
  const fmtDelta = (value) =>
    value === null || value === undefined ? "–" : (value > 0 ? "+" : "") + nf1.format(value);

  const parseDay = (day) => {
    const [y, m, d] = day.split("-").map(Number);
    return new Date(y, m - 1, d);
  };
  const todayIso = () => {
    const now = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  };
  const dayLabel = (day) =>
    parseDay(day).toLocaleDateString("de-DE", { weekday: "short", day: "2-digit", month: "short" });

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  let toastTimer = null;
  function toast(message) {
    const el = $("toast");
    el.textContent = message;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 2600);
  }

  // ------------------------------------------------------------- Laden

  async function refresh() {
    const data = await api("/api/gewicht");
    state.measurements = data.measurements || [];
    state.stats = data.stats || {};
    renderHero();
    renderChart();
    renderEntries();
  }

  function renderHero() {
    const stats = state.stats;
    $("hero-weight").textContent = stats.count ? fmt(stats.latest) : "–";
    $("hero-meta").textContent = stats.count
      ? `${dayLabel(stats.latest_day)} · ${stats.count} ${stats.count === 1 ? "Eintrag" : "Einträge"}`
      : "Noch kein Eintrag";

    const trend = $("hero-trend");
    if (stats.delta7 === null || stats.delta7 === undefined) {
      trend.innerHTML = "";
    } else {
      const dir = stats.delta7 < 0 ? "down" : stats.delta7 > 0 ? "up" : "";
      trend.innerHTML =
        `<strong class="${dir}">${escapeHtml(fmtDelta(stats.delta7))} kg</strong>` +
        `<span class="muted small">7 Tage</span>`;
    }

    const tiles = [
      ["Ø 7 Tage", stats.avg7 === undefined ? "–" : fmt(stats.avg7)],
      ["Ø 30 Tage", stats.avg30 === undefined ? "–" : fmt(stats.avg30)],
      ["Minimum", stats.min === undefined ? "–" : fmt(stats.min)],
      ["Gesamt", stats.total_change === undefined ? "–" : fmtDelta(stats.total_change)],
    ];
    $("tiles").innerHTML = tiles
      .map(([key, value]) =>
        `<div class="tile"><strong>${escapeHtml(value)}</strong>` +
        `<span class="muted small">${escapeHtml(key)}</span></div>`)
      .join("");
  }

  // ---------------------------------------------------------- Diagramm

  const SERIES = [
    { label: "Messung", color: "var(--series-1)" },
    { label: "Ø 7 Tage", color: "var(--series-2)" },
  ];

  function movingAverage(points, days) {
    return points.map((point, index) => {
      const cutoff = point.time - (days - 1) * 86400000;
      let sum = 0;
      let count = 0;
      for (let i = index; i >= 0 && points[i].time >= cutoff; i--) {
        sum += points[i].value;
        count++;
      }
      return { time: point.time, value: sum / count };
    });
  }

  function niceStep(span, target) {
    const rough = span / target;
    const magnitude = Math.pow(10, Math.floor(Math.log10(rough)));
    for (const factor of [1, 2, 2.5, 5, 10]) {
      if (magnitude * factor >= rough) return magnitude * factor;
    }
    return magnitude * 10;
  }

  function renderChart() {
    const wrap = $("chart");
    const cutoff = state.rangeDays > 0 ? Date.now() - state.rangeDays * 86400000 : -Infinity;
    const points = state.measurements
      .map((row) => ({ time: parseDay(row.day).getTime(), value: row.weight_kg, day: row.day }))
      .filter((p) => p.time >= cutoff)
      .sort((a, b) => a.time - b.time);

    if (points.length < 2) {
      $("legend").innerHTML = "";
      wrap.innerHTML = `<p class="muted">${points.length === 1
        ? "Ab dem zweiten Eintrag entsteht hier eine Kurve."
        : "Noch keine Daten in diesem Zeitraum."}</p>`;
      return;
    }

    $("legend").innerHTML = SERIES.map(
      (s) => `<span class="item"><span class="swatch" style="background:${s.color}"></span>${s.label}</span>`
    ).join("");

    const width = Math.max(280, wrap.clientWidth || 320);
    const height = 220;
    const pad = { top: 12, right: 14, bottom: 24, left: 38 };
    const innerW = width - pad.left - pad.right;
    const innerH = height - pad.top - pad.bottom;

    const avg = movingAverage(points, 7);
    const values = points.map((p) => p.value).concat(avg.map((p) => p.value));
    let lo = Math.min(...values);
    let hi = Math.max(...values);
    if (hi - lo < 1.5) { const mid = (hi + lo) / 2; lo = mid - 0.75; hi = mid + 0.75; }
    const margin = (hi - lo) * 0.12;
    lo -= margin; hi += margin;

    const t0 = points[0].time;
    const t1 = points[points.length - 1].time;
    const sx = (t) => pad.left + (t1 === t0 ? innerW / 2 : ((t - t0) / (t1 - t0)) * innerW);
    const sy = (v) => pad.top + innerH - ((v - lo) / (hi - lo)) * innerH;

    const step = niceStep(hi - lo, 4);
    const gridLines = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
      const y = sy(v);
      gridLines.push(
        `<line x1="${pad.left}" y1="${y.toFixed(1)}" x2="${width - pad.right}" y2="${y.toFixed(1)}" stroke="var(--line)" stroke-width="1"/>` +
        `<text x="${pad.left - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end" font-size="10" fill="var(--muted)">${nf1.format(v)}</text>`
      );
    }

    const toPath = (series) =>
      series.map((p, i) => `${i ? "L" : "M"}${sx(p.time).toFixed(1)},${sy(p.value).toFixed(1)}`).join(" ");

    const dots = points.length <= 45
      ? points.map((p) =>
          `<circle cx="${sx(p.time).toFixed(1)}" cy="${sy(p.value).toFixed(1)}" r="3.5" fill="var(--series-1)" stroke="var(--surface)" stroke-width="2"/>`
        ).join("")
      : "";

    const xLabels = [];
    const labelCount = Math.min(4, points.length);
    for (let i = 0; i < labelCount; i++) {
      const p = points[Math.round((i * (points.length - 1)) / (labelCount - 1 || 1))];
      const anchor = i === 0 ? "start" : i === labelCount - 1 ? "end" : "middle";
      xLabels.push(
        `<text x="${sx(p.time).toFixed(1)}" y="${height - 6}" text-anchor="${anchor}" font-size="10" fill="var(--muted)">` +
        `${parseDay(p.day).toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" })}</text>`
      );
    }

    const last = points[points.length - 1];
    const lastLabelY = Math.max(pad.top + 10, sy(last.value) - 10);

    wrap.innerHTML = `
      <svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="img"
           aria-label="Gewichtsverlauf mit gleitendem 7-Tage-Mittel">
        ${gridLines.join("")}
        ${xLabels.join("")}
        <path d="${toPath(points)}" fill="none" stroke="var(--series-1)" stroke-width="2"
              stroke-linejoin="round" stroke-linecap="round"/>
        <path d="${toPath(avg)}" fill="none" stroke="var(--series-2)" stroke-width="2"
              stroke-dasharray="5 4" stroke-linecap="round"/>
        ${dots}
        <text x="${Math.min(width - pad.right, sx(last.time)).toFixed(1)}" y="${lastLabelY.toFixed(1)}"
              text-anchor="end" font-size="11" font-weight="600" fill="var(--text)">${nf1.format(last.value)}</text>
        <g id="crosshair" style="display:none">
          <line y1="${pad.top}" y2="${pad.top + innerH}" stroke="var(--muted)" stroke-width="1"/>
          <circle r="5" fill="var(--series-1)" stroke="var(--surface)" stroke-width="2"/>
          <circle r="5" fill="var(--series-2)" stroke="var(--surface)" stroke-width="2"/>
        </g>
        <rect id="hit-area" x="${pad.left}" y="${pad.top}" width="${innerW}" height="${innerH}" fill="transparent"/>
      </svg>
      <div class="chart-tooltip" id="tooltip" hidden></div>`;

    attachHover(wrap, points, avg, sx, sy);
  }

  function attachHover(wrap, points, avg, sx, sy) {
    const svg = wrap.querySelector("svg");
    const hit = wrap.querySelector("#hit-area");
    const cross = wrap.querySelector("#crosshair");
    const tooltip = wrap.querySelector("#tooltip");
    const line = cross.querySelector("line");
    const dotRaw = cross.querySelectorAll("circle")[0];
    const dotAvg = cross.querySelectorAll("circle")[1];

    const move = (event) => {
      const rect = svg.getBoundingClientRect();
      const scale = svg.viewBox.baseVal.width / rect.width;
      const x = (event.clientX - rect.left) * scale;
      let best = 0;
      let bestDist = Infinity;
      points.forEach((p, i) => {
        const dist = Math.abs(sx(p.time) - x);
        if (dist < bestDist) { bestDist = dist; best = i; }
      });
      const point = points[best];
      const average = avg[best];
      const px = sx(point.time);
      cross.style.display = "";
      line.setAttribute("x1", px); line.setAttribute("x2", px);
      dotRaw.setAttribute("cx", px); dotRaw.setAttribute("cy", sy(point.value));
      dotAvg.setAttribute("cx", px); dotAvg.setAttribute("cy", sy(average.value));

      tooltip.hidden = false;
      tooltip.innerHTML =
        `<strong>${escapeHtml(dayLabel(point.day))}</strong>` +
        `<span><span class="swatch" style="background:var(--series-1)"></span>${fmt(point.value)} kg</span>` +
        `<span><span class="swatch" style="background:var(--series-2)"></span>${fmt(average.value)} kg</span>`;
      const left = (px / svg.viewBox.baseVal.width) * rect.width;
      tooltip.style.left = `${Math.min(Math.max(left, 54), wrap.clientWidth - 54)}px`;
      tooltip.style.top = `${sy(point.value) - 12}px`;
    };

    const leave = () => { cross.style.display = "none"; tooltip.hidden = true; };
    hit.addEventListener("pointermove", move);
    hit.addEventListener("pointerdown", move);
    hit.addEventListener("pointerleave", leave);
    hit.addEventListener("pointerup", leave);
  }

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(renderChart, 150);
  });

  document.querySelectorAll("#range button").forEach((button) => {
    button.addEventListener("click", () => {
      state.rangeDays = Number(button.dataset.days);
      localStorage.setItem("supbot.weight.range", String(state.rangeDays));
      document.querySelectorAll("#range button").forEach((b) => b.classList.remove("active"));
      button.classList.add("active");
      renderChart();
    });
  });

  // ---------------------------------------------------------- Eintraege

  function renderEntries() {
    const rows = [...state.measurements].sort((a, b) => (a.day < b.day ? 1 : -1));
    $("entry-count").textContent = rows.length ? `${rows.length} gesamt` : "";
    if (!rows.length) {
      $("entries").innerHTML = `<li class="muted">Noch nichts gespeichert.</li>`;
      return;
    }
    const shown = state.entriesExpanded ? rows.length : Math.min(ENTRY_PAGE, rows.length);
    $("entries").innerHTML = rows.slice(0, shown).map((row, index) => {
      const previous = rows[index + 1];
      const delta = previous ? row.weight_kg - previous.weight_kg : null;
      const dir = delta === null ? "" : delta < 0 ? "down" : delta > 0 ? "up" : "";
      const origin = row.source === "ocr" ? "Foto" : "manuell";
      return `<li>
        <span class="e-day">${escapeHtml(dayLabel(row.day))}
          <span class="muted small">${escapeHtml(origin)}</span></span>
        <span class="e-delta ${dir}">${delta === null ? "" : escapeHtml(fmtDelta(delta))}</span>
        <span class="e-weight">${escapeHtml(fmt(row.weight_kg))} kg</span>
        <button class="btn danger icon" data-day="${escapeHtml(row.day)}" aria-label="Eintrag löschen">×</button>
      </li>`;
    }).join("");

    if (rows.length > ENTRY_PAGE) {
      $("entries").insertAdjacentHTML(
        "beforeend",
        `<li><button class="btn ghost small" id="btn-more">${
          state.entriesExpanded ? "Weniger anzeigen" : `Alle ${rows.length} anzeigen`
        }</button></li>`
      );
      $("btn-more").addEventListener("click", () => {
        state.entriesExpanded = !state.entriesExpanded;
        renderEntries();
      });
    }

    $("entries").querySelectorAll("button[data-day]").forEach((button) => {
      button.addEventListener("click", async () => {
        const day = button.dataset.day;
        if (!confirm(`Eintrag vom ${dayLabel(day)} löschen?`)) return;
        try {
          await api(`/api/gewicht/${day}`, { method: "DELETE" });
          await refresh();
          toast("Eintrag gelöscht");
        } catch (err) {
          toast(err.message);
        }
      });
    });
  }

  // ------------------------------------------------------ Foto & Rahmen

  $("btn-photo").addEventListener("click", () => $("photo-input").click());

  $("btn-manual").addEventListener("click", () => {
    resetReview();
    $("review").hidden = false;
    $("weight-input").focus();
  });

  $("photo-input").addEventListener("change", async (event) => {
    const file = event.target.files && event.target.files[0];
    event.target.value = "";
    if (!file) return;
    resetReview();
    try {
      state.file = await downscale(file);
    } catch (err) {
      toast("Bild konnte nicht gelesen werden");
      return;
    }
    $("preview").src = URL.createObjectURL(state.file);
    $("crop-stage").hidden = false;
    $("crop-hint").hidden = false;
    $("btn-recognize").hidden = false;
    $("review").hidden = false;
    $("preview").onload = () => {
      applyCropBox();
      $("review").scrollIntoView({ behavior: "smooth", block: "start" });
    };
  });

  async function downscale(file, maxEdge = 1600) {
    const bitmap = await createImageBitmap(file);
    const scale = Math.min(1, maxEdge / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * scale);
    canvas.height = Math.round(bitmap.height * scale);
    canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    if (bitmap.close) bitmap.close();
    return new Promise((resolve, reject) => {
      canvas.toBlob(
        (blob) => (blob ? resolve(blob) : reject(new Error("toBlob fehlgeschlagen"))),
        "image/jpeg",
        0.88
      );
    });
  }

  function applyCropBox() {
    const box = $("crop-box");
    const c = state.crop;
    box.style.left = `${c.x * 100}%`;
    box.style.top = `${c.y * 100}%`;
    box.style.width = `${c.w * 100}%`;
    box.style.height = `${c.h * 100}%`;
  }

  (function setupCropDrag() {
    const stage = $("crop-stage");
    const box = $("crop-box");
    let drag = null;

    const onDown = (event, handle) => {
      event.preventDefault();
      event.stopPropagation();
      drag = {
        handle,
        rect: stage.getBoundingClientRect(),
        startX: event.clientX,
        startY: event.clientY,
        crop: { ...state.crop },
      };
      if (event.target.setPointerCapture) event.target.setPointerCapture(event.pointerId);
    };

    box.addEventListener("pointerdown", (event) => {
      if (!event.target.dataset.handle) onDown(event, "move");
    });
    box.querySelectorAll(".handle").forEach((handle) => {
      handle.addEventListener("pointerdown", (event) => onDown(event, handle.dataset.handle));
    });

    const onMove = (event) => {
      if (!drag) return;
      const dx = (event.clientX - drag.startX) / drag.rect.width;
      const dy = (event.clientY - drag.startY) / drag.rect.height;
      const start = drag.crop;
      let { x, y, w, h } = start;

      if (drag.handle === "move") {
        x = Math.min(Math.max(0, start.x + dx), 1 - start.w);
        y = Math.min(Math.max(0, start.y + dy), 1 - start.h);
      } else {
        const west = drag.handle.includes("w");
        const north = drag.handle.includes("n");
        if (west) {
          x = Math.min(Math.max(0, start.x + dx), start.x + start.w - MIN_CROP);
          w = start.x + start.w - x;
        } else {
          w = Math.min(Math.max(MIN_CROP, start.w + dx), 1 - start.x);
        }
        if (north) {
          y = Math.min(Math.max(0, start.y + dy), start.y + start.h - MIN_CROP);
          h = start.y + start.h - y;
        } else {
          h = Math.min(Math.max(MIN_CROP, start.h + dy), 1 - start.y);
        }
      }
      state.crop = { x, y, w, h };
      applyCropBox();
    };

    const onUp = () => {
      if (!drag) return;
      drag = null;
      localStorage.setItem("supbot.weight.crop", JSON.stringify(state.crop));
    };

    stage.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  })();

  // --------------------------------------------------------- Erkennung

  $("btn-recognize").addEventListener("click", async () => {
    if (!state.file) return;
    const button = $("btn-recognize");
    const status = $("ocr-status");
    button.disabled = true;
    button.textContent = "Erkenne …";
    status.hidden = false;
    status.textContent = "Das Bild wird ausgewertet.";

    try {
      const form = new FormData();
      form.append("image", state.file, "waage.jpg");
      form.append("crop", JSON.stringify(state.crop));
      const result = await api("/api/gewicht/ocr", { method: "POST", body: form });

      state.imageToken = result.image_token || null;
      state.ocrRaw = result.raw_text || null;
      state.ocrConfidence = result.confidence === undefined ? null : result.confidence;

      if (result.weight !== null && result.weight !== undefined) {
        state.source = "ocr";
        $("weight-input").value = nf1.format(result.weight);
        status.textContent = `Erkannt mit ${Math.round(result.confidence)} % Sicherheit. Bitte prüfen.`;
      } else {
        state.source = "manual";
        status.textContent = "Nichts erkannt. Rahmen enger ziehen oder Wert eintippen.";
      }
      renderCandidates(result.candidates || []);
    } catch (err) {
      status.textContent = `Fehler: ${err.message}`;
    } finally {
      button.disabled = false;
      button.textContent = "Erneut erkennen";
    }
  });

  function renderCandidates(candidates) {
    const box = $("candidates");
    const others = candidates.slice(1, 5);
    if (!others.length) { box.hidden = true; box.innerHTML = ""; return; }
    box.hidden = false;
    box.innerHTML = `<span class="muted small">Alternativen:</span>` +
      others.map((c) =>
        `<button class="btn ghost small" data-value="${c.value}">${nf1.format(c.value)} kg</button>`
      ).join("");
    box.querySelectorAll("button[data-value]").forEach((chip) => {
      chip.addEventListener("click", () => {
        $("weight-input").value = nf1.format(Number(chip.dataset.value));
      });
    });
  }

  // ---------------------------------------------------------- Speichern

  document.querySelectorAll("[data-step]").forEach((button) => {
    button.addEventListener("click", () => {
      const fallback = state.stats.latest === undefined ? 80 : state.stats.latest;
      const current = parseWeight($("weight-input").value);
      const base = current === null ? fallback : current;
      $("weight-input").value = nf1.format(Math.round((base + Number(button.dataset.step)) * 10) / 10);
    });
  });

  function parseWeight(raw) {
    const value = parseFloat(String(raw).replace(",", ".").replace(/[^0-9.]/g, ""));
    return Number.isFinite(value) ? value : null;
  }

  $("btn-save").addEventListener("click", async () => {
    const kilos = parseWeight($("weight-input").value);
    if (kilos === null) { toast("Bitte ein Gewicht eingeben"); return; }
    if (kilos < window.WEIGHT.min || kilos > window.WEIGHT.max) {
      toast(`Wert muss zwischen ${window.WEIGHT.min} und ${window.WEIGHT.max} kg liegen`);
      return;
    }

    $("btn-save").disabled = true;
    try {
      await api("/api/gewicht", {
        json: {
          day: $("day-input").value || todayIso(),
          weight_kg: kilos,
          source: state.source,
          ocr_raw: state.ocrRaw,
          ocr_confidence: state.ocrConfidence,
          image_token: state.imageToken,
        },
      });
      resetReview();
      $("review").hidden = true;
      await refresh();
      toast(`${nf1.format(kilos)} kg gespeichert`);
    } catch (err) {
      toast(err.message);
    } finally {
      $("btn-save").disabled = false;
    }
  });

  $("btn-cancel").addEventListener("click", () => {
    resetReview();
    $("review").hidden = true;
  });

  function resetReview() {
    state.file = null;
    state.imageToken = null;
    state.ocrRaw = null;
    state.ocrConfidence = null;
    state.source = "manual";
    $("weight-input").value = "";
    $("day-input").value = todayIso();
    $("crop-stage").hidden = true;
    $("crop-hint").hidden = true;
    $("btn-recognize").hidden = true;
    $("btn-recognize").textContent = "Gewicht erkennen";
    $("ocr-status").hidden = true;
    $("candidates").hidden = true;
    $("candidates").innerHTML = "";
  }

  // ------------------------------------------------------------- Start

  document.querySelectorAll("#range button").forEach((b) => {
    b.classList.toggle("active", Number(b.dataset.days) === state.rangeDays);
  });
  $("day-input").value = todayIso();
  refresh().catch((err) => toast(`Laden fehlgeschlagen: ${err.message}`));
})();
