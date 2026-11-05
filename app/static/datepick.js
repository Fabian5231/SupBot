/* ---------- datepick v1: eigener Kalender statt <input type="date"> ----------
 * Gemeinsame Kopie fuer alle Apps auf fabian-social.dev (Quelle:
 * garminCon/shared/datepick.js). Bei Aenderungen in alle Apps kopieren.
 *
 * DatePick.enhance(input, opts) versteckt das native Feld und zeigt statt
 * dessen einen Knopf mit deutschem Datum; ein Klick oeffnet einen Kalender
 * (Montag zuerst, KW-Spalte, Heute-Knopf). Der Wert bleibt im nativen Feld:
 * bestehender Code liest/schreibt input.value wie bisher und bekommt beim
 * Waehlen ganz normal "input" + "change".
 *
 * opts.weeks / opts.months: KW bzw. Monatstitel sind waehlbar und rufen
 * opts.onPick({kind: "week"|"month", start, end}) - fuer Zeitraum-Waehler.
 * opts.range: () => ({start, end}) markiert einen Zeitraum im Kalender.
 * opts.trigger: eigenes Element, das den Kalender oeffnet (kein Knopf).
 * opts.label: Text, solange nichts gewaehlt ist.
 * opts.clearable: zeigt "Leeren" (fuer optionale Felder).
 * opts.format: "long" (Do., 05.11.2026, Standard) oder "short" (05.11.2026).
 */
(function () {
  "use strict";
  if (window.DatePick) return;

  const DAY = 86400000;
  const pad = (n) => String(n).padStart(2, "0");
  const toIso = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const fromIso = (s) => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
  const valid = (s) => typeof s === "string" && /^\d{4}-\d{2}-\d{2}$/.test(s);
  const add = (s, n) => { const d = fromIso(s); d.setDate(d.getDate() + n); return toIso(d); };
  const todayIso = () => toIso(new Date());
  const monday = (s) => { const d = fromIso(s); d.setDate(d.getDate() - ((d.getDay() + 6) % 7)); return toIso(d); };
  const monthFirst = (s) => s.slice(0, 8) + "01";
  const monthShift = (s, n) => { const d = fromIso(monthFirst(s)); d.setMonth(d.getMonth() + n); return toIso(d); };
  const monthLast = (s) => add(monthShift(s, 1), -1);
  const kw = (s) => {
    const d = fromIso(s);
    d.setDate(d.getDate() + 3 - ((d.getDay() + 6) % 7));
    const jan4 = new Date(d.getFullYear(), 0, 4);
    return 1 + Math.round(((d - jan4) / DAY - 3 + ((jan4.getDay() + 6) % 7)) / 7);
  };
  const WD = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
  const fmtLong = (s) => fromIso(s).toLocaleDateString("de-DE", { weekday: "short", day: "2-digit", month: "2-digit", year: "numeric" });
  const fmtShort = (s) => fromIso(s).toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit", year: "numeric" });
  const fmtFull = (s) => fromIso(s).toLocaleDateString("de-DE", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
  const fmtMonth = (s) => fromIso(s).toLocaleDateString("de-DE", { month: "long", year: "numeric" });

  const ICON = '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3.5" y="5" width="17" height="15.5" rx="2.5"/><path d="M3.5 10h17M8 3v4M16 3v4"/></svg>';
  const node = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

  const valueDesc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
  let open = null;   // the one picker that is showing

  function enhance(input, opts = {}) {
    if (!input || input.dataset.datepick) return input && input._datepick;
    input.dataset.datepick = "1";
    const api = { input, opts, refresh: () => paint() };
    input._datepick = api;

    let trigger = opts.trigger;
    let label = null;
    if (!trigger) {
      trigger = node("button", "dp-btn");
      trigger.type = "button";
      trigger.innerHTML = ICON;
      label = node("span", "dp-btn-text");
      trigger.appendChild(label);
      if (input.id) trigger.id = "dp-" + input.id;
      input.after(trigger);
      // a <label for=…> must now point at the button
      for (const l of document.querySelectorAll(`label[for="${input.id}"]`)) if (input.id) l.htmlFor = trigger.id;
    }
    trigger.setAttribute("aria-haspopup", "dialog");
    trigger.setAttribute("aria-expanded", "false");
    input.classList.add("dp-native");
    input.tabIndex = -1;
    input.setAttribute("aria-hidden", "true");

    function paint() {
      if (!label) return;
      const v = valueDesc.get.call(input);
      const aria = input.getAttribute("aria-label");
      if (valid(v)) {
        label.textContent = opts.format === "short" ? fmtShort(v) : fmtLong(v);
        trigger.classList.remove("dp-empty");
        trigger.setAttribute("aria-label", `${aria ? aria + ": " : ""}${fmtFull(v)}, ändern`);
      } else {
        label.textContent = opts.label || "Datum wählen";
        trigger.classList.add("dp-empty");
        trigger.setAttribute("aria-label", `${aria ? aria + ": " : ""}${opts.label || "Datum wählen"}`);
      }
      trigger.disabled = input.disabled;
    }
    // Code elsewhere sets input.value directly; keep the button in step.
    Object.defineProperty(input, "value", {
      configurable: true,
      get() { return valueDesc.get.call(this); },
      set(v) { valueDesc.set.call(this, v); paint(); },
    });
    input.addEventListener("change", paint);
    // validation code focuses the field - send that to the visible button
    input.focus = (o) => trigger.focus(o);
    paint();

    trigger.addEventListener("click", (e) => {
      e.preventDefault();
      if (input.disabled) return;
      if (open && open.api === api) return close();
      show(api, trigger);
    });
    return api;
  }

  function close(refocus) {
    if (!open) return;
    const { pop, shade, trigger } = open;
    pop.remove();
    shade && shade.remove();
    trigger.setAttribute("aria-expanded", "false");
    document.removeEventListener("pointerdown", open.outside, true);
    window.removeEventListener("resize", open.place);
    window.removeEventListener("scroll", open.place, true);
    open = null;
    if (refocus) trigger.focus({ preventScroll: true });
  }

  function show(api, trigger) {
    close();
    const { input, opts } = api;
    const min = () => (valid(input.min) ? input.min : null);
    const max = () => (valid(input.max) ? input.max : null);
    const allowed = (s) => !(min() && s < min()) && !(max() && s > max());
    const value = valueDesc.get.call(input);
    const sheet = window.matchMedia("(max-width: 560px)").matches;

    let view = monthFirst(valid(value) ? value : (max() && todayIso() > max() ? max() : todayIso()));
    let focusDay = valid(value) ? value : (allowed(todayIso()) ? todayIso() : (max() || min() || todayIso()));

    const pop = node("div", "dp-pop" + (sheet ? " dp-sheet" : "") + (opts.weeks ? " dp-with-kw-pop" : ""));
    pop.setAttribute("role", "dialog");
    pop.setAttribute("aria-label", input.getAttribute("aria-label") || "Datum wählen");
    const shade = sheet ? node("div", "dp-shade") : null;
    // inside a modal <dialog> only its own subtree is clickable (top layer)
    const host = trigger.closest("dialog[open]") || document.body;
    if (shade) { shade.addEventListener("click", () => close(true)); host.appendChild(shade); }
    host.appendChild(pop);

    function pick(s) {
      valueDesc.set.call(input, s);
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
      api.refresh();
      close(true);
    }
    function pickSpan(kind, start, end) {
      close(true);
      opts.onPick && opts.onPick({ kind, start, end });
    }

    function render() {
      pop.replaceChildren();
      const range = opts.range ? opts.range() : null;
      const sel = valueDesc.get.call(input);

      if (sheet) pop.appendChild(node("div", "dp-grip"));
      const head = node("div", "dp-head");
      const prev = node("button", "dp-nav", "‹");
      prev.type = "button"; prev.setAttribute("aria-label", "Vorheriger Monat");
      prev.disabled = !!min() && add(view, -1) < min();
      prev.onclick = () => { view = monthShift(view, -1); focusDay = clampTo(view); render(); };
      const next = node("button", "dp-nav", "›");
      next.type = "button"; next.setAttribute("aria-label", "Nächster Monat");
      next.disabled = !!max() && monthShift(view, 1) > max();
      next.onclick = () => { view = monthShift(view, 1); focusDay = clampTo(view); render(); };
      let title;
      if (opts.months) {
        title = node("button", "dp-title dp-pickable", fmtMonth(view));
        title.type = "button";
        title.title = "Ganzen Monat wählen";
        const ms = view, me = monthLast(view);
        if (range && range.start === ms && range.end === me) title.classList.add("dp-on");
        title.onclick = () => pickSpan("month", ms, me);
      } else {
        title = node("div", "dp-title", fmtMonth(view));
        title.setAttribute("aria-live", "polite");
      }
      head.append(prev, title, next);
      pop.appendChild(head);

      const grid = node("div", "dp-grid" + (opts.weeks ? " dp-with-kw" : ""));
      grid.setAttribute("role", "grid");
      if (opts.weeks) grid.appendChild(node("div", "dp-wd dp-kwh", "KW"));
      for (const w of WD) grid.appendChild(node("div", "dp-wd", w));

      const first = monday(view);
      const last = monday(monthLast(view));
      const t = todayIso();
      for (let wk = first; wk <= last; wk = add(wk, 7)) {
        if (opts.weeks) {
          const b = node("button", "dp-kw", String(kw(wk)));
          b.type = "button";
          b.title = `KW ${kw(wk)} wählen`;
          b.setAttribute("aria-label", `Kalenderwoche ${kw(wk)} wählen`);
          if (range && range.start === wk && range.end === add(wk, 6)) b.classList.add("dp-on");
          b.disabled = !!min() && add(wk, 6) < min() || !!max() && wk > max();
          b.onclick = () => pickSpan("week", wk, add(wk, 6));
          grid.appendChild(b);
        }
        for (let i = 0; i < 7; i++) {
          const s = add(wk, i);
          const b = node("button", "dp-day", String(fromIso(s).getDate()));
          b.type = "button";
          b.dataset.day = s;
          b.setAttribute("aria-label", fmtFull(s));
          if (s.slice(0, 7) !== view.slice(0, 7)) b.classList.add("dp-out");
          if (s === t) { b.classList.add("dp-today"); b.setAttribute("aria-current", "date"); }
          if (s === sel && !(range && range.start !== range.end)) { b.classList.add("dp-on"); b.setAttribute("aria-pressed", "true"); }
          if (range && range.start && range.start !== range.end && s >= range.start && s <= range.end) {
            b.classList.add("dp-in");
            if (s === range.start) b.classList.add("dp-in-start");
            if (s === range.end) b.classList.add("dp-in-end");
          }
          if (!allowed(s)) b.disabled = true;
          b.tabIndex = s === focusDay ? 0 : -1;
          b.onclick = () => pick(s);
          grid.appendChild(b);
        }
      }
      pop.appendChild(grid);

      const foot = node("div", "dp-foot");
      if (allowed(t)) {
        const tb = node("button", "dp-link", "Heute");
        tb.type = "button";
        tb.onclick = () => pick(t);
        foot.appendChild(tb);
      }
      if (opts.clearable && valid(sel)) {
        const cb = node("button", "dp-link dp-quiet", "Leeren");
        cb.type = "button";
        cb.onclick = () => pick("");
        foot.appendChild(cb);
      }
      const hint = opts.weeks || opts.months
        ? node("span", "dp-hint", opts.weeks && opts.months ? "Tag, KW oder Monat antippen" : opts.weeks ? "Tag oder KW antippen" : "Tag oder Monat antippen")
        : null;
      if (hint) foot.appendChild(hint);
      if (foot.childNodes.length) pop.appendChild(foot);
    }

    function clampTo(month) {
      let s = focusDay && focusDay.slice(0, 7) === month.slice(0, 7) ? focusDay : month;
      if (min() && s < min()) s = min();
      if (max() && s > max()) s = max();
      return s;
    }

    function focusCurrent() {
      const b = pop.querySelector(`.dp-day[data-day="${focusDay}"]`);
      (b && !b.disabled ? b : pop.querySelector(".dp-day:not(:disabled)") || pop.querySelector("button"))?.focus({ preventScroll: true });
    }

    pop.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); return close(true); }   // not the dialog too
      if (e.key === "Tab") {         // keep focus inside
        const f = [...pop.querySelectorAll("button:not(:disabled)")].filter((b) => b.tabIndex !== -1 || b === document.activeElement);
        if (!f.length) return;
        const i = f.indexOf(document.activeElement);
        if (e.shiftKey && i <= 0) { e.preventDefault(); f[f.length - 1].focus(); }
        else if (!e.shiftKey && i === f.length - 1) { e.preventDefault(); f[0].focus(); }
        return;
      }
      if (!document.activeElement?.classList.contains("dp-day")) return;
      const moves = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 };
      let to = null;
      if (e.key in moves) to = add(focusDay, moves[e.key]);
      else if (e.key === "PageUp") to = toIso(new Date(fromIso(focusDay).setMonth(fromIso(focusDay).getMonth() - 1)));
      else if (e.key === "PageDown") to = toIso(new Date(fromIso(focusDay).setMonth(fromIso(focusDay).getMonth() + 1)));
      else if (e.key === "Home") to = monday(focusDay);
      else if (e.key === "End") to = add(monday(focusDay), 6);
      if (!to) return;
      e.preventDefault();
      if (!allowed(to)) return;
      focusDay = to;
      if (to.slice(0, 7) !== view.slice(0, 7)) view = monthFirst(to);
      render();
      focusCurrent();
    });

    function place() {
      if (sheet) return;
      const r = trigger.getBoundingClientRect();
      const w = pop.offsetWidth, h = pop.offsetHeight;
      const vw = document.documentElement.clientWidth, vh = window.innerHeight;
      const ideal = r.width > w ? r.left + (r.width - w) / 2 : r.left;   // wide buttons: centred below
      let left = Math.min(Math.max(8, ideal), vw - w - 8);
      let top = r.bottom + 6;
      if (top + h > vh - 8 && r.top - h - 6 > 8) top = r.top - h - 6;
      pop.style.left = `${Math.round(left)}px`;
      pop.style.top = `${Math.round(Math.max(8, top))}px`;
    }
    const outside = (e) => {
      if (pop.contains(e.target) || trigger.contains(e.target)) return;
      close();
    };

    open = { api, pop, shade, trigger, outside, place };
    trigger.setAttribute("aria-expanded", "true");
    render();
    place();
    document.addEventListener("pointerdown", outside, true);
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    focusCurrent();
  }

  window.DatePick = {
    enhance,
    close: () => close(),
    /** Every <input type="date"> below root that is not opted out with data-no-datepick. */
    enhanceAll(root = document, opts = {}) {
      for (const i of root.querySelectorAll('input[type="date"]:not([data-no-datepick])')) enhance(i, opts);
    },
  };
})();
