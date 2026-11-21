/* Fraction explorer front end. No build step, no framework: plain DOM plus
 * uPlot (canvas) for charts, because 10,000-point series need canvas.
 *
 * State lives in the URL query (view, col, dist, f, frac, role, runs), so a
 * bookmark reopens the same view. Times arrive as "plant clock seconds" and
 * are formatted in UTC on purpose: that prints the wall-clock time the
 * datalog stored, whatever zone this browser is set to.
 */
(() => {
  "use strict";

  // ---------------------------------------------------------------- helpers
  const $app = document.getElementById("app");
  const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const enc = encodeURIComponent;

  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "html") el.innerHTML = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) {
      if (kid == null || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }

  async function api(path, opts) {
    const r = await fetch(path, opts);
    let body;
    try { body = await r.json(); } catch { body = { error: `HTTP ${r.status}` }; }
    if (!r.ok || body.error) throw new Error(body.error || `HTTP ${r.status}`);
    return body;
  }

  const num = (v, d = 1) => (v == null || Number.isNaN(v) ? "–" : Number(v).toFixed(d));
  const pad = (n) => String(n).padStart(2, "0");
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  // "2026-07-28 01:00:00" -> "28 Jul 01:00" (plant time, no zone maths)
  function when(s, withYear) {
    if (!s) return "–";
    const [d, t] = s.split(/[ T]/);
    const [y, m, dd] = d.split("-");
    return `${+dd} ${MONTHS[+m - 1]}${withYear ? " " + y : ""} ${(t || "").slice(0, 5)}`;
  }
  const dur = (h) => (h == null ? "–" : h >= 1 ? `${Math.floor(h)}h ${pad(Math.round((h % 1) * 60))}m` : `${Math.round(h * 60)}m`);
  const plantSec = (s) => Date.parse(s.replace(" ", "T") + "Z") / 1000;
  const clockLabel = (sec) => { const d = new Date(sec * 1000); return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`; };

  const state = {};
  function readURL() {
    const q = new URLSearchParams(location.search);
    Object.keys(state).forEach((k) => delete state[k]);
    for (const [k, v] of q) state[k] = v;
    state.view = state.view || "runs";
  }
  function go(patch, replace) {
    const next = { ...state, ...patch };
    for (const k of Object.keys(next)) if (next[k] == null || next[k] === "") delete next[k];
    const qs = new URLSearchParams(next).toString();
    history[replace ? "replaceState" : "pushState"](null, "", "?" + qs);
    readURL();
    render();
  }
  window.addEventListener("popstate", () => { readURL(); render(); });

  function skeleton(kind, n = 6) { return h("div", { class: "skeleton" + (kind ? " skeleton--" + kind : ""), "aria-busy": "true", "aria-label": "Loading" }, ...Array.from({ length: n }, () => h("i"))); }
  function stateBox(title, text, kind, action) {
    return h("div", { class: "state" + (kind ? " state--" + kind : ""), role: kind === "error" ? "alert" : null },
      h("h3", null, title), h("p", null, text), action ? h("p", { style: "margin-top:12px" }, action) : null);
  }
  const errorBox = (e, retry) => stateBox("Could not load this", String(e.message || e), "error",
    retry ? h("button", { class: "btn", type: "button", onclick: retry }, "Try again") : null);

  // ---------------------------------------------------------------- meta
  let META = null;
  const colMeta = (code) => META.columns.find((c) => c.code === code);

  async function loadMeta() {
    META = await api("/api/meta");
    const ef = META.eflow;
    document.getElementById("eflow-status").textContent = ef
      ? `Eflow cache ${ef.rows} rows, refreshed ${when(ef.at.replace("T", " "))}`
      : "Eflow cache empty";
    renderColSwitch();
  }

  function renderColSwitch() {
    const box = document.getElementById("colswitch");
    box.replaceChildren(...META.columns.map((c) =>
      h("button", { type: "button", "aria-pressed": String(state.col === c.code),
        title: c.has_process_data ? `${c.label}, process data logged` : `${c.label}, Eflow fractions only, no process data logged`,
        onclick: () => go({ col: c.code, dist: null, f: null, frac: null, runs: null }) },
        c.label, c.has_process_data ? null : h("span", { class: "note" }, "no log"))));
  }

  // ---------------------------------------------------------------- charts
  const syncKey = uPlot.sync("frac");
  const charts = new Set();
  const ro = new ResizeObserver((entries) => {
    for (const e of entries) {
      const u = e.target._u;
      if (u) u.setSize({ width: Math.max(e.contentRect.width, 200), height: u.height });
    }
  });
  function disposeCharts() { for (const u of charts) { ro.unobserve(u.root.parentNode); u.destroy(); } charts.clear(); }

  function axisStyle(extra) {
    return { stroke: css("--color-muted"), font: `12px ${css("--font-outlier")}`,
      grid: { stroke: css("--color-rule"), width: 1 }, ticks: { stroke: css("--color-rule"), width: 1, size: 4 }, ...extra };
  }

  /* opts: {title, unit, series:[{label, x, y, color, width}], percent, gaps:[[a,b]],
   *        marks:[{x, label}], height, xRange:[a,b], focus} */
  function chart(host, opts) {
    const wrap = h("div", { class: "chart" },
      h("div", { class: "chart-title" }, h("b", null, opts.title), h("span", null, opts.note || opts.unit || "")));
    const box = h("div");
    wrap.append(box);
    host.append(wrap);
    const tables = opts.series.map((s) => [s.x, s.y]);
    const data = opts.shared ? [opts.series[0].x, ...opts.series.map((s) => s.y)]
      : (tables.length ? uPlot.join(tables, tables.map(() => [1])) : [[]]);
    const gapFill = css("--gap-fill"), eventFill = css("--event-fill"), markStroke = css("--color-rule-2"), markText = css("--color-muted");
    const u = new uPlot({
      width: Math.max(host.clientWidth - 16, 300), height: opts.height || 220,
      cursor: { sync: opts.percent ? undefined : { key: syncKey.key }, focus: { prox: 16 }, drag: { x: true, y: false } },
      focus: { alpha: 0.18 },
      scales: { x: { time: !opts.percent, range: opts.xRange ? () => opts.xRange : undefined } },
      tzDate: (ts) => uPlot.tzDate(new Date(ts * 1e3), "Etc/UTC"),
      legend: { live: true },
      axes: [
        axisStyle(opts.percent ? { values: (u, vals) => vals.map((v) => v + "%") } : {}),
        axisStyle({ size: 56, label: opts.unit, labelFont: `12px ${css("--font-body")}`, labelSize: 18 }),
      ],
      series: [
        { label: opts.percent ? "Progress" : "Time",
          value: opts.percent ? (u, v) => (v == null ? "–" : v.toFixed(1) + "%") : (u, v) => (v == null ? "–" : clockLabel(v)) },
        ...opts.series.map((s) => ({ label: s.label, stroke: s.color, width: s.width || 1.5, dash: s.dash, points: { show: false },
          value: (u, v) => (v == null ? "–" : v.toFixed(s.digits ?? 2)) })),
      ],
      hooks: {
        drawAxes: [(u) => {
          const { ctx, bbox } = u;
          ctx.save();
          ctx.fillStyle = gapFill;
          for (const [a, b] of opts.gaps || []) {
            const x0 = u.valToPos(a, "x", true), x1 = u.valToPos(b, "x", true);
            ctx.fillRect(x0, bbox.top, Math.max(x1 - x0, 1), bbox.height);
          }
          ctx.fillStyle = eventFill;
          for (const [a, b] of opts.events || []) {
            const x0 = u.valToPos(a, "x", true), x1 = u.valToPos(b, "x", true);
            ctx.fillRect(x0, bbox.top, Math.max(x1 - x0, 2), bbox.height);
          }
          ctx.strokeStyle = markStroke; ctx.fillStyle = markText; ctx.lineWidth = 1;
          ctx.font = `${11 * devicePixelRatio}px ${css("--font-body")}`;
          for (const m of opts.marks || []) {
            const x = Math.round(u.valToPos(m.x, "x", true)) + 0.5;
            ctx.beginPath(); ctx.moveTo(x, bbox.top); ctx.lineTo(x, bbox.top + bbox.height); ctx.stroke();
            if (m.label) ctx.fillText(m.label, x + 4 * devicePixelRatio, bbox.top + 12 * devicePixelRatio);
          }
          ctx.restore();
        }],
      },
    }, data, box);
    u.root.querySelectorAll(".u-series").forEach((row, i) => {
      const mk = row.querySelector(".u-marker"), sr = u.series[i];
      if (mk && i > 0) mk.style.background = sr._stroke || opts.series[i - 1].color;
    });
    wrap._u = u;
    ro.observe(wrap);
    charts.add(u);
    return u;
  }

  // series colours: cool ramp for the leading instruments, neutrals for context
  function colourFor(role, i) {
    if (role === "pot_temp") return css("--ramp-1");
    if (role === "condenser") return css("--ramp-2");
    if (role === "vacuum") return css("--ramp-1");
    if (role === "vacuum_2") return css("--ctx-2");
    if (role === "temp_sp") return css("--color-ink-2");
    return [css("--ctx-1"), css("--ctx-2"), css("--ctx-3"), css("--ramp-3")][i % 4];
  }

  // interpolate two oklch tokens (newest run = cool, oldest = muted)
  function parseOklch(s) { const m = s.match(/oklch\(\s*([\d.]+)%\s+([\d.]+)\s+([\d.]+)/); return m ? m.slice(1).map(Number) : [70, 0.1, 245]; }
  function ramp(n) {
    const a = parseOklch(css("--color-cool")), b = parseOklch(css("--ctx-3"));
    return Array.from({ length: n }, (_, i) => {
      const t = n <= 1 ? 0 : i / (n - 1);
      return `oklch(${a[0] + (b[0] - a[0]) * t}% ${a[1] + (b[1] - a[1]) * t} ${a[2]})`;
    });
  }

  function exportLinks(kind, params) {
    const q = new URLSearchParams(params).toString();
    return h("span", { class: "actions" },
      h("a", { class: "btn btn--quiet", href: `/export/${kind}.csv?${q}` }, "CSV"),
      h("a", { class: "btn btn--quiet", href: `/export/${kind}.xlsx?${q}` }, "Excel"));
  }

  // ---------------------------------------------------------------- runs view
  let runsCache = { col: null, runs: null };

  async function renderRuns() {
    const aside = h("aside", { class: "aside", "aria-label": "Runs" });
    const main = h("section", { class: "main" });
    $app.replaceChildren(h("div", { class: "view" }, aside, main));
    const cm = colMeta(state.col);

    aside.append(h("div", { class: "aside-head" }, h("h2", { style: "font-size:var(--text-md)" }, `${cm.label} runs`),
      exportLinks("runs", { col: state.col })));
    const body = h("div", { class: "aside-body" }, skeleton(null, 12));
    aside.append(body);

    let runs;
    try {
      if (runsCache.col !== state.col) runsCache = { col: state.col, runs: (await api(`/api/runs?col=${enc(state.col)}`)).runs };
      runs = runsCache.runs;
    } catch (e) { body.replaceChildren(errorBox(e, render)); return; }

    if (!runs.length) {
      body.replaceChildren(stateBox("No runs", `Eflow has no fractions for ${cm.label}. Refresh from Eflow if one was just drummed off.`));
      main.append(stateBox("Nothing to show", "Pick a column with runs, or refresh the Eflow cache."));
      return;
    }
    if (!state.dist || !runs.some((r) => r.distillation === state.dist)) { go({ dist: runs[0].distillation, f: null }, true); return; }

    const tbody = h("tbody");
    for (const r of runs) {
      const sel = r.distillation === state.dist;
      tbody.append(h("tr", { class: "pick", tabindex: "0", "aria-selected": String(sel),
        onclick: () => go({ dist: r.distillation, f: null }),
        onkeydown: (e) => { if (e.key === "Enter") go({ dist: r.distillation, f: null }); } },
        h("td", { class: "num" }, r.distillation), h("td", null, when(r.start, true)),
        h("td", { class: "num" }, r.fractions), h("td", { class: "num" }, num(r.kg, 0))));
    }
    body.replaceChildren(h("table", null, h("thead", null, h("tr", null,
      h("th", { class: "num" }, "Run"), h("th", null, "Started"), h("th", { class: "num" }, "Fr."), h("th", { class: "num" }, "kg"))), tbody));
    const selRow = tbody.querySelector('[aria-selected="true"]');
    if (selRow) selRow.scrollIntoView({ block: "nearest" });

    await renderRunMain(main, cm);
  }

  async function renderRunMain(main, cm) {
    main.replaceChildren(skeleton(null, 3), skeleton("chart", 2));
    let run;
    try { run = await api(`/api/run?col=${enc(state.col)}&dist=${enc(state.dist)}`); }
    catch (e) { main.replaceChildren(errorBox(e, render)); return; }

    const numbered = run.fractions.filter((f) => !f.reactor);
    const kg = run.fractions.reduce((s, f) => s + (f.kg || 0), 0);   // Reactor (pot residue) counts
    const head = h("div", null,
      h("div", { class: "head-row" },
        h("h1", null, `${cm.label} distillation ${run.distillation}`),
        h("div", { class: "actions" }, exportLinks("run", { col: state.col, dist: state.dist }))),
      h("dl", { class: "facts" },
        h("div", null, h("dt", null, "Start"), h("dd", { class: "measured" }, when(run.start, true))),
        h("div", null, h("dt", null, "End"), h("dd", { class: "measured" }, when(run.end, true))),
        h("div", null, h("dt", null, "Fractions"), h("dd", { class: "measured" }, numbered.length)),
        h("div", null, h("dt", null, "Drummed"), h("dd", { class: "measured" }, `${num(kg, 0)} kg`)),
        run.overview ? h("div", null, h("dt", null, "Pot-temp coverage"),
          h("dd", { class: "measured" + (run.overview.coverage_pct < 99 ? " flag" : "") }, `${num(run.overview.coverage_pct)}%`,
            run.overview.backfilled_pct > 0 ? h("span", { class: "muted" }, ` (${num(run.overview.backfilled_pct, 0)}% backfilled)`) : null)) : null));

    const tl = timeline(run);
    const detail = h("div");
    main.replaceChildren(head, tl, detail, fractionsTable(run, cm));

    if (!run.has_process_data) {
      detail.append(h("div", { class: "panel" }, stateBox("No process data logged",
        `${cm.label} has no datalog tags configured; logging is not set up on this column yet. ` +
        "The fractions above come from Eflow and are complete; there is nothing to chart.")));
      return;
    }
    if (state.f) await renderFraction(detail, +state.f);
    else renderOverview(detail, run);
  }

  function timeline(run) {
    const panel = h("section", { class: "panel", "aria-label": "Run timeline" });
    const t0 = plantSec(run.start), t1 = plantSec(run.end), span = Math.max(t1 - t0, 1);
    const pct = (s) => ((plantSec(s) - t0) / span) * 100;
    const reactorLane = h("div", { class: "tl-lane tl-lane--reactor" });
    const lane = h("div", { class: "tl-lane" });
    for (const f of run.fractions) {
      if (!f.start || !f.end) continue;
      const left = pct(f.start), width = Math.max(pct(f.end) - left, 0.15);
      const style = `left:${left}%;width:${width}%`;
      if (f.reactor) {
        reactorLane.append(h("div", { class: "tl-bar tl-bar--reactor", style,
          title: `Reactor (left in the pot), ${when(f.start)} to ${when(f.end)}${f.lot ? `\nLot ${f.lot}` : ""}${f.kg != null ? `, ${f.kg} kg` : ""}` },
          h("span", { class: "f" }, `Reactor, left in the pot${f.lot ? `, ${f.lot}` : ""}${f.kg != null ? `, ${num(f.kg, 0)} kg` : ""}`)));
        continue;
      }
      const m = f.metrics || {};
      const holey = m.coverage_pct != null && m.coverage_pct < 95;
      lane.append(h("button", { type: "button", class: "tl-bar" + (holey ? " tl-bar--gap" : ""), style,
        "aria-pressed": String(String(f.id) === state.f),
        title: `Fraction ${f.fraction}, lot ${f.lot || "none"}, ${f.kg ?? "?"} kg\n${when(f.start)} to ${when(f.end)} (${dur(f.duration_h)})` +
          (m.coverage_pct != null ? `\nPot-temp coverage ${m.coverage_pct}%` : ""),
        onclick: () => go({ f: String(f.id) === state.f ? null : String(f.id) }) },
        h("span", { class: "f" }, `F${f.fraction}`),
        h("span", { class: "m" }, !f.lot ? "" : width < 9 ? "…" + f.lot.slice(-4) : f.lot),
        h("span", { class: "m" }, f.kg != null ? `${num(f.kg, 0)} kg` : "")));
    }
    // day ticks along the bottom
    const axis = h("div", { class: "tl-axis", "aria-hidden": "true" });
    const firstMidnight = Math.ceil(t0 / 86400) * 86400;
    for (let s = firstMidnight; s < t1; s += 86400) {
      const d = new Date(s * 1000);
      axis.append(h("span", { style: `left:${((s - t0) / span) * 100}%` }, `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`));
    }
    panel.append(h("div", { class: "panel-head" }, h("h2", null, "Timeline"),
      h("span", { class: "muted" }, "Bars to scale. Click a fraction for its profile. Hatched means pot-temp data is missing in part of it.")),
      h("div", { class: "timeline" }, h("div", { class: "timeline-inner" }, reactorLane, lane, axis)));
    return panel;
  }

  function fractionsTable(run, cm) {
    const has = run.has_process_data;
    const cols = [["Fraction"], ["Lot"], ["kg", 1], ["Start"], ["Duration", 1]];
    if (has) cols.push(["Coverage", 1], ["Pot avg °C", 1], ["ΔT start", 1], ["ΔT end", 1], ["Vac std", 1], ["Excursions", 1], ["Steady after", 1]);
    const tbody = h("tbody");
    const reactors = run.fractions.filter((f) => f.reactor);
    for (const f of run.fractions) {
      if (f.reactor) continue;
      const m = f.metrics || {};
      const cells = [h("td", null, `F${f.fraction}`), h("td", { class: "measured" }, f.lot || "–"),
        h("td", { class: "num" }, num(f.kg, 0)), h("td", { class: "measured" }, when(f.start)), h("td", { class: "num" }, dur(f.duration_h))];
      if (has) cells.push(
        h("td", { class: "num" }, m.coverage_pct == null ? "–" : h("span", { class: m.coverage_pct < 95 ? "flag" : "" }, `${num(m.coverage_pct, 0)}%`)),
        h("td", { class: "num" }, num(m.pot_mean)), h("td", { class: "num" }, num(m.dT_start)), h("td", { class: "num" }, num(m.dT_end)),
        h("td", { class: "num" }, num(m.vac_std, 2)), h("td", { class: "num" }, m.vac_excursions ?? "–"),
        h("td", { class: "num" }, m.time_to_steady_min == null ? "–" : `${num(m.time_to_steady_min, 0)} min`));
      tbody.append(h("tr", { class: "pick", tabindex: "0", "aria-selected": String(String(f.id) === state.f),
        onclick: () => go({ f: String(f.id) }), onkeydown: (e) => { if (e.key === "Enter") go({ f: String(f.id) }); } }, cells));
    }
    // Reactor: what is left in the pot. Lot and kilos, no column statistics.
    for (const f of reactors) {
      const cells = [h("td", null, "Reactor"), h("td", { class: "measured" }, f.lot || "–"), h("td", { class: "num" }, num(f.kg, 0)),
        h("td", { class: "measured" }, when(f.start)), h("td", { class: "num" }, dur(f.duration_h))];
      if (has) cells.push(h("td", { class: "muted", colspan: "7" }, "left in the pot, no column statistics"));
      tbody.append(h("tr", null, cells));
    }
    return h("section", { class: "panel" },
      h("div", { class: "panel-head" }, h("h2", null, "Fractions"), exportLinks("run", { col: state.col, dist: state.dist })),
      h("div", { class: "table-wrap" }, h("table", null,
        h("thead", null, h("tr", null, cols.map(([c, n]) => h("th", { class: n ? "num" : null }, c)))), tbody)));
  }

  function renderOverview(host, run) {
    disposeCharts();
    const ov = run.overview;
    const panel = h("section", { class: "panel" },
      h("div", { class: "panel-head" }, h("h2", null, "Whole run"),
        h("span", { class: "muted" }, "Vertical lines are fraction cuts. Red shading is missing data. Drag to zoom, double-click to reset.")));
    const box = h("div", { class: "charts" });
    panel.append(box);
    host.append(panel);
    const marks = run.fractions.filter((f) => !f.reactor && f.start).map((f) => ({ x: plantSec(f.start), label: `F${f.fraction}` }));
    const by = (r) => ov.series.find((s) => s.role === r);
    const temps = ["pot_temp", "temp_sp", "condenser"].map(by).filter(Boolean);
    const vac = by("vacuum");
    if (temps.length) chart(box, { title: "Temperatures", unit: "°C", gaps: ov.gaps, marks, xRange: [ov.t0, ov.t1],
      series: temps.map((s, i) => ({ label: s.label, x: s.x, y: s.y, color: colourFor(s.role, i), width: s.role === "temp_sp" ? 1 : 1.5, digits: 1 })) });
    if (vac) chart(box, { title: vac.label, unit: vac.unit, gaps: ov.gaps, marks, height: 170, xRange: [ov.t0, ov.t1],
      series: [{ label: vac.label, x: vac.x, y: vac.y, color: colourFor("vacuum"), digits: 2 }] });
    if (!ov.series.some((s) => s.x.length)) box.replaceChildren(stateBox("No samples in this window",
      "The datalog has no plausible readings for this run. Check the Coverage view for an outage."));
  }

  async function renderFraction(host, fid) {
    disposeCharts();
    host.replaceChildren(skeleton("chart", 3));
    let d;
    try { d = await api(`/api/fraction/${fid}`); } catch (e) { host.replaceChildren(errorBox(e, render)); return; }
    const fr = d.fraction, m = d.metrics || {}, c = d.chart;
    const unitOf = (role) => (d.stats.find((s) => s.role === role) || {}).unit || "";
    const close = h("button", { class: "btn btn--quiet", type: "button", onclick: () => go({ f: null }) }, "Back to whole run");
    const head = h("div", { class: "panel-head" },
      h("h2", null, `Fraction ${fr.fraction}`),
      h("div", { class: "actions" }, exportLinks("fraction", { id: fid }), close));
    const facts = h("dl", { class: "facts", style: "padding: 8px 16px 12px; margin:0" },
      h("div", null, h("dt", null, "Lot"), h("dd", { class: "measured" }, fr.lot || "–")),
      h("div", null, h("dt", null, "Drummed"), h("dd", { class: "measured" }, fr.kg != null ? `${num(fr.kg, 0)} kg` : "–")),
      h("div", null, h("dt", null, "From"), h("dd", { class: "measured" }, when(fr.start, true))),
      h("div", null, h("dt", null, "To"), h("dd", { class: "measured" }, when(fr.end, true))),
      h("div", null, h("dt", null, "Duration"), h("dd", { class: "measured" }, dur((plantSec(fr.end) - plantSec(fr.start)) / 3600))),
      h("div", null, h("dt", null, "Pot-temp coverage"), h("dd", { class: "measured" + (c.coverage_pct < 99 ? " flag" : "") }, `${num(c.coverage_pct)}%`)),
      h("div", null, h("dt", null, "Backfilled"), h("dd", { class: "measured" }, `${num(c.backfilled_pct, 0)}%`)),
      h("div", null, h("dt", null, "Dropouts removed"), h("dd", { class: "measured" }, m.dropouts_removed ?? 0)));

    const box = h("div", { class: "charts" });
    const readings = readingsPanel(m, unitOf("vacuum"));
    const panel = h("section", { class: "panel" }, head, facts, readings, box);
    const statsP = statsPanel(d.stats);
    host.replaceChildren(panel, eventsPanel(d.vac_events || [], m, unitOf("vacuum")), statsP);

    const by = (r) => c.series.filter((s) => s.role === r);
    const xr = [c.t0, c.t1];
    const temps = [...by("pot_temp"), ...by("temp_sp"), ...by("condenser"), ...by("column")];
    if (temps.length) chart(box, { title: "Pot, setpoint, condenser and column temps", unit: "°C", gaps: c.gaps, xRange: xr,
      series: temps.map((s, i) => ({ label: s.label, x: s.x, y: s.y, color: colourFor(s.role, i), digits: 1,
        width: s.role === "pot_temp" ? 2 : s.role === "temp_sp" ? 1 : 1.25,
        dash: s.role === "column" ? [[6, 3], [2, 3], [10, 3, 2, 3], [1, 2]][i % 4] : s.role === "temp_sp" ? [4, 4] : undefined })) });
    if (c.delta_t && c.delta_t.x.length) chart(box, { title: "Pot − condenser ΔT", unit: "°C", note: "closing ΔT means the next component is coming over", gaps: c.gaps, xRange: xr, height: 170,
      series: [{ label: "ΔT", x: c.delta_t.x, y: c.delta_t.y, color: colourFor("pot_temp"), digits: 1 }] });
    const vac = [...by("vacuum"), ...by("vacuum_2")];
    if (vac.length) {
      const band = m.vac_median != null ? `median ${num(m.vac_median, 2)}, band ±${num(m.vac_band, 2)}, excursions shaded amber` : "";
      chart(box, { title: "Vacuum", unit: vac[0].unit, note: band, gaps: c.gaps, xRange: xr, height: 190,
        events: (d.vac_events || []).map((e) => [e.x0, e.x1]),
        series: vac.map((s) => ({ label: s.label, x: s.x, y: s.y, color: colourFor(s.role), digits: 2 })) });
    }
    const lv = by("level");   // unit status is in the table below; a 0/1/2 state reads badly on a % axis
    if (lv.length) chart(box, { title: "Level", unit: lv[0].unit, gaps: c.gaps, xRange: xr, height: 110,
      series: lv.map((s, i) => ({ label: s.label, x: s.x, y: s.y, color: colourFor("column", i), digits: 1 })) });
    if (!c.series.some((s) => s.x.length)) box.replaceChildren(stateBox("No samples in this fraction",
      "The datalog has no plausible readings between these times. See Coverage for outages."));
  }

  function readingsPanel(m, vacUnit) {
    const r = (label, v, unit, d = 1, bad) => h("div", null, h("dt", null, label),
      h("dd", { class: bad ? "bad" : null }, v == null ? "–" : num(v, d), v == null ? null : h("small", null, unit)));
    return h("dl", { class: "readings", style: "margin:0" },
      r("ΔT at start", m.dT_start, "°C"), r("ΔT at end", m.dT_end, "°C"), r("ΔT trend", m.dT_slope_c_per_h, "°C/h", 2),
      r("Vacuum std", m.vac_std, vacUnit, 3),
      r("Vacuum excursions", m.vac_excursions, "", 0, m.vac_excursions > 0),
      r("Time outside band", m.vac_min_outside_band, "min", 0),
      r("Worst excursion peak", m.vac_worst_peak, vacUnit, 2, m.vac_worst_peak != null),
      r("Steady after", m.time_to_steady_min, "min", 0),
      r("Steepest pot ramp", m.pot_ramp_max_c_per_h, "°C/h"),
      r(`Above ${m.pot_threshold_c ?? "–"} °C`, m.pot_min_above_threshold, "min", 0));
  }

  // Each vacuum excursion with its time, so a leak, a pump trip and an
  // operator change can be told apart and checked against the shift log.
  function eventsPanel(events, m, unit) {
    if (!events.length) return h("div");
    const tbody = h("tbody");
    for (const e of events) {
      tbody.append(h("tr", null, h("td", { class: "measured" }, when(e.at)), h("td", { class: "num" }, `${num(e.start_s / 60, 0)} min`),
        h("td", { class: "num" }, dur(e.minutes / 60)), h("td", { class: "num" }, num(e.peak, 2)),
        h("td", { class: "num" }, `${e.peak_dev > 0 ? "+" : ""}${num(e.peak_dev, 2)}`)));
    }
    return h("section", { class: "panel" },
      h("div", { class: "panel-head" }, h("h3", null, `Vacuum excursions (${events.length})`),
        h("span", { class: "muted" }, `Outside ${num(m.vac_median, 2)} ± ${num(m.vac_band, 2)} ${unit} for at least a minute`)),
      h("div", { class: "table-wrap" }, h("table", null, h("thead", null, h("tr", null,
        h("th", null, "Started"), h("th", { class: "num" }, "Into fraction"), h("th", { class: "num" }, "Lasted"),
        h("th", { class: "num" }, "Peak"), h("th", { class: "num" }, "From median"))), tbody)));
  }

  function statsPanel(stats) {
    const tbody = h("tbody");
    for (const s of stats) {
      tbody.append(h("tr", null, h("td", null, s.label), h("td", { class: "measured muted col-tag" }, s.name),
        h("td", { class: "num" }, num(s.min, 2)), h("td", { class: "num" }, num(s.max, 2)), h("td", { class: "num" }, num(s.mean, 2)),
        h("td", { class: "num" }, num(s.std, 3)), h("td", { class: "muted" }, s.unit),
        h("td", { class: "num" }, h("span", { class: s.coverage_pct < 95 ? "flag" : "" }, `${num(s.coverage_pct, 0)}%`)),
        h("td", { class: "num" }, s.dropouts_removed)));
    }
    return h("section", { class: "panel" },
      h("div", { class: "panel-head" }, h("h3", null, "Per tag, time-weighted, dropouts removed")),
      h("div", { class: "table-wrap" }, h("table", null, h("thead", null, h("tr", null,
        h("th", null, "Signal"), h("th", { class: "col-tag" }, "Tag"), h("th", { class: "num" }, "Min"), h("th", { class: "num" }, "Max"),
        h("th", { class: "num" }, "Avg"), h("th", { class: "num" }, "Std"), h("th", null, "Unit"),
        h("th", { class: "num" }, "Coverage"), h("th", { class: "num" }, "Dropouts"))), tbody)));
  }

  // ---------------------------------------------------------------- compare view
  async function renderCompare() {
    disposeCharts();
    const cm = colMeta(state.col);
    const aside = h("aside", { class: "aside", "aria-label": "Runs to compare" });
    const main = h("section", { class: "main" });
    $app.replaceChildren(h("div", { class: "view" }, aside, main));
    if (!cm.has_process_data) {
      aside.append(stateBox("No process data", `${cm.label} has no datalog tags.`));
      main.append(stateBox("Nothing to compare", `${cm.label} has fractions in Eflow but no process data logged, so there are no profiles to overlay.`));
      return;
    }
    main.append(skeleton("chart", 2));
    let labels;
    try { labels = (await api(`/api/fraction-labels?col=${enc(state.col)}`)).labels; } catch (e) { main.replaceChildren(errorBox(e, render)); return; }
    if (!labels.length) { main.replaceChildren(stateBox("No fractions", "Eflow has no numbered fractions for this column.")); return; }
    if (!state.frac || !labels.includes(state.frac)) { go({ frac: labels[0] }, true); return; }
    const role = state.role || "pot_temp";
    const picked = state.runs ? state.runs.split(",") : null;

    let d;
    try { d = await api(`/api/compare?col=${enc(state.col)}&frac=${enc(state.frac)}&role=${enc(role)}${picked ? "&runs=" + enc(picked.join(",")) : ""}`); }
    catch (e) { main.replaceChildren(errorBox(e, render)); return; }

    // aside: which runs
    const chosen = new Set(d.runs.map((r) => r.distillation));
    const list = h("div", { class: "aside-body", style: "padding: 4px 16px" });
    for (const a of d.available) {
      const id = `run-${a.distillation}`;
      list.append(h("label", { class: "check", for: id },
        h("input", { type: "checkbox", id, checked: chosen.has(a.distillation), onchange: (e) => {
          const s = new Set(chosen);
          e.target.checked ? s.add(a.distillation) : s.delete(a.distillation);
          go({ runs: [...s].join(",") || null });
        } }),
        h("span", { class: "measured" }, a.distillation), h("span", { class: "muted" }, when(a.start, true))));
    }
    aside.append(h("div", { class: "aside-head" }, h("h2", { style: "font-size:var(--text-md)" }, `Runs with F${state.frac}`),
      picked ? h("button", { class: "btn btn--quiet", type: "button", onclick: () => go({ runs: null }) }, "Latest 30") : null), list);

    // controls
    const fracSel = h("select", { "aria-label": "Fraction", onchange: (e) => go({ frac: e.target.value, runs: null }) },
      labels.map((l) => h("option", { value: l, selected: l === state.frac }, `Fraction ${l}`)));
    const roleSeg = h("div", { class: "seg", role: "group", "aria-label": "Signal" },
      META.compare_roles.map(([r, label]) => h("button", { type: "button", "aria-pressed": String(r === role), title: label, onclick: () => go({ role: r }) }, r === "delta_t" ? "ΔT" : label)));
    main.replaceChildren(
      h("div", { class: "head-row" }, h("h1", null, `${cm.label}, fraction ${state.frac} across runs`),
        h("div", { class: "actions" }, fracSel, roleSeg)),
      h("p", { class: "muted" }, "X is percent of the fraction complete, so a 10-hour cut and a 14-hour cut line up by progress. Hover a row below to bring one run forward."));

    if (!d.runs.length) { main.append(stateBox("No runs selected", "Tick runs on the left to overlay them.")); return; }
    const colours = ramp(d.runs.length);
    const panel = h("section", { class: "panel" },
      h("div", { class: "panel-head" }, h("h2", null, "Overlay by percent complete"), exportLinks("compare", { col: state.col, frac: state.frac, role, ...(picked ? { runs: picked.join(",") } : {}) })));
    const box = h("div", { class: "charts" });
    panel.append(box);
    main.append(panel);
    const u = chart(box, { title: d.label, unit: d.unit, percent: true, height: 340, shared: true, xRange: [0, 100],
      series: d.runs.map((r, i) => ({ label: r.distillation, x: d.x, y: r.y, color: colours[i], digits: 2 })) });

    // metrics across runs: the table purity will join onto (by lot)
    const tbody = h("tbody");
    d.runs.forEach((r, i) => {
      const m = r.metrics || {};
      tbody.append(h("tr", { class: "pick", tabindex: "0",
        onmouseenter: () => u.setSeries(i + 1, { focus: true }), onmouseleave: () => u.setSeries(null, { focus: false }),
        onfocus: () => u.setSeries(i + 1, { focus: true }),
        onclick: () => go({ view: "runs", dist: r.distillation, f: null, frac: null, role: null, runs: null }) },
        h("td", null, h("span", { class: "swatch", style: `background:${colours[i]}` }), h("span", { class: "measured" }, r.distillation)),
        h("td", { class: "measured" }, r.lot || "–"), h("td", { class: "measured" }, when(r.start, true)),
        h("td", { class: "num" }, dur(r.duration_h)), h("td", { class: "num" }, num(r.kg, 0)),
        h("td", { class: "num" }, h("span", { class: r.coverage_pct < 95 ? "flag" : "" }, `${num(r.coverage_pct, 0)}%`)),
        h("td", { class: "num" }, num(m.pot_mean)), h("td", { class: "num" }, num(m.dT_start)), h("td", { class: "num" }, num(m.dT_end)),
        h("td", { class: "num" }, num(m.vac_std, 2)), h("td", { class: "num" }, m.vac_excursions ?? "–"),
        h("td", { class: "num" }, m.time_to_steady_min == null ? "–" : num(m.time_to_steady_min, 0))));
    });
    main.append(h("section", { class: "panel" },
      h("div", { class: "panel-head" }, h("h2", null, `Fraction ${state.frac}, per run`), h("span", { class: "muted" }, "Click a row to open that run")),
      h("div", { class: "table-wrap" }, h("table", null, h("thead", null, h("tr", null,
        ["Run", "Lot", "Started", "Duration", "kg", "Coverage", "Pot avg °C", "ΔT start", "ΔT end", "Vac std", "Excursions", "Steady min"]
          .map((c, i) => h("th", { class: i >= 3 ? "num" : null }, c)))), tbody))));
  }

  // ---------------------------------------------------------------- coverage view
  async function renderCoverage() {
    disposeCharts();
    const main = h("section", { class: "main" });
    $app.replaceChildren(h("div", { class: "view view--single" }, main));
    main.append(h("div", { class: "head-row" }, h("h1", null, "Data coverage"),
      h("div", { class: "legend" },
        h("span", null, h("span", { class: "swatch", style: `background:${css("--color-cool")}` }), "live"),
        h("span", null, h("span", { class: "swatch", style: `background:${css("--ramp-4")}` }), "backfilled"),
        h("span", null, h("span", { class: "swatch", style: `background:${css("--color-bad")}` }), "no plausible data"))),
      h("p", { class: "muted" }, "Minutes per hour holding at least one plausible pot-temp reading (dropouts excluded). Runs from Eflow sit under each strip; red runs are under 95% covered."));
    const unlogged = META.columns.filter((c) => !c.has_process_data);
    if (unlogged.length) main.append(h("section", { class: "panel unlogged" },
      unlogged.map((c) => c.label).join(", ") + ": no datalog tags, so no coverage to show. Eflow has " +
      unlogged.map((c) => `${c.runs} runs on ${c.label}`).join(" and ") + "."));
    for (const c of META.columns.filter((c) => c.has_process_data)) {
      const sec = h("section", { class: "panel" });
      main.append(sec);
      coverageColumn(sec, c);
    }
  }

  async function coverageColumn(sec, c) {
    const head = h("div", { class: "panel-head" }, h("h2", null, c.label));
    sec.replaceChildren(head);
    if (!c.has_process_data) {
      sec.append(stateBox("No process data logged", `${c.label} has ${c.runs} runs in Eflow and no datalog tags. Nothing to cover until logging is set up.`));
      return;
    }
    const body = h("div", null, skeleton(null, 2));
    sec.append(body);
    const scan = async (full) => {
      btn.setAttribute("aria-busy", "true"); btn2.setAttribute("aria-busy", "true");
      btn.textContent = "Scanning…";
      try { await api(`/api/coverage/scan?col=${enc(c.code)}${full ? "&full=1" : ""}`, { method: "POST" }); coverageColumn(sec, c); }
      catch (e) { body.replaceChildren(errorBox(e, () => scan(full))); }
    };
    const btn = h("button", { class: "btn", type: "button", onclick: () => scan(false) }, "Scan new data");
    const btn2 = h("button", { class: "btn btn--quiet", type: "button", onclick: () => scan(true) }, "Rescan all");
    head.append(h("div", { class: "actions" }, exportLinks("coverage", { col: c.code }), btn2, btn));

    let cov, runs;
    try {
      [cov, runs] = await Promise.all([api(`/api/coverage?col=${enc(c.code)}`), api(`/api/runs?col=${enc(c.code)}`)]);
    } catch (e) { body.replaceChildren(errorBox(e, () => coverageColumn(sec, c))); return; }
    if (!cov.meta) {
      body.replaceChildren(stateBox("Not scanned yet", "Coverage comes from one grouped query per 30 days against the datalog. The first scan reads the whole history and can take a minute.",
        null, h("button", { class: "btn btn--primary", type: "button", onclick: () => scan(true) }, "Scan the datalog")));
      return;
    }
    const from = plantSec(cov.meta.scanned_from + ":00"), to = plantSec(cov.meta.scanned_to + ":00");
    const hours = new Map(cov.hours.map((r) => [plantSec(r.hour + ":00"), r]));
    const canvas = h("canvas", { class: "cov-canvas", role: "img", "aria-label": `${c.label} coverage strip` });
    const runCanvas = h("canvas", { class: "cov-runs", "aria-hidden": "true" });
    const readout = h("div", { class: "muted measured", style: "min-height:20px; padding: 0 16px" }, `${clockLabel(from)} to ${clockLabel(to)}, scanned ${when((cov.meta.computed_at || "").replace("T", " "))}`);
    body.replaceChildren(h("div", { class: "cov-row" }, h("span", { class: "muted" }, "Pot temp"), h("div", null, canvas, runCanvas)), readout);

    // runs coverage from the hourly table
    const runCov = runs.runs.filter((r) => r.start && r.end).map((r) => {
      const a = plantSec(r.start), b = plantSec(r.end);
      let got = 0, total = 0;
      for (let t = Math.floor(a / 3600) * 3600; t < b; t += 3600) {
        const ov = Math.min(b, t + 3600) - Math.max(a, t);
        const row = hours.get(t);
        total += ov;
        if (row) got += ov * Math.min((row.live_min + row.backfilled_min) / 60, 1);
      }
      return { ...r, a, b, pct: total ? (got / total) * 100 : 0 };
    });

    function draw() {
      const dpr = devicePixelRatio || 1;
      const w = canvas.clientWidth, H = canvas.clientHeight;
      canvas.width = w * dpr; canvas.height = H * dpr;
      runCanvas.width = w * dpr; runCanvas.height = runCanvas.clientHeight * dpr;
      const ctx = canvas.getContext("2d");
      const n = Math.max(1, Math.floor(w * dpr));
      const span = to - from;
      const live = new Float64Array(n), bf = new Float64Array(n), cnt = new Float64Array(n);
      for (let t = from; t < to; t += 3600) {
        const i = Math.min(n - 1, Math.floor(((t - from) / span) * n));
        cnt[i] += 60;
        const r = hours.get(t);
        if (r) { live[i] += r.live_min; bf[i] += r.backfilled_min; }
      }
      const cLive = css("--color-cool"), cBf = css("--ramp-4"), cBad = css("--color-bad");
      const Hc = canvas.height;
      const paint = (x0, x1, liveMin, bfMin, totMin) => {
        const L = (liveMin / totMin) * Hc, B = (bfMin / totMin) * Hc, w = Math.max(x1 - x0, 1);
        ctx.fillStyle = cBad; ctx.fillRect(x0, 0, w, Hc - L - B);
        ctx.fillStyle = cBf; ctx.fillRect(x0, Hc - L - B, w, B);
        ctx.fillStyle = cLive; ctx.fillRect(x0, Hc - L, w, L);
      };
      if (span / 3600 <= n) {
        // fewer hours than pixels: one rectangle per hour, edges snapped so
        // neighbours touch (no hairline seams)
        for (let t = from; t < to; t += 3600) {
          const r = hours.get(t);
          paint(Math.floor(((t - from) / span) * n), Math.ceil(((t + 3600 - from) / span) * n), r ? r.live_min : 0, r ? r.backfilled_min : 0, 60);
        }
      } else {
        for (let i = 0; i < n; i++) if (cnt[i]) paint(i, i + 1, live[i], bf[i], cnt[i]);
      }
      const rc = runCanvas.getContext("2d");
      for (const r of runCov) {
        const x0 = ((r.a - from) / span) * runCanvas.width, x1 = ((r.b - from) / span) * runCanvas.width;
        if (x1 < 0 || x0 > runCanvas.width) continue;
        rc.fillStyle = r.pct < 95 ? cBad : css("--color-rule-2");
        rc.fillRect(x0, 0, Math.max(x1 - x0 - dpr, 1), runCanvas.height);
      }
    }
    draw();
    new ResizeObserver(draw).observe(canvas);
    canvas.addEventListener("mousemove", (e) => {
      const rect = canvas.getBoundingClientRect();
      const t = from + ((e.clientX - rect.left) / rect.width) * (to - from);
      const hr = Math.floor(t / 3600) * 3600, r = hours.get(hr);
      readout.textContent = `${clockLabel(hr)}  live ${r ? r.live_min : 0} min, backfilled ${r ? r.backfilled_min : 0} min of 60`;
    });

    const bad = runCov.filter((r) => r.pct < 95).sort((a, b) => a.pct - b.pct);
    const gapRows = cov.gaps.map((g) => h("tr", null, h("td", { class: "measured" }, when(g.from, true)), h("td", { class: "measured" }, when(g.to, true)),
      h("td", { class: "num" }, g.hours >= 48 ? `${num(g.hours / 24, 1)} d` : `${num(g.hours, 0)} h`)));
    const badRows = bad.map((r) => h("tr", { class: "pick", tabindex: "0", onclick: () => go({ view: "runs", col: c.code, dist: r.distillation, f: null }) },
      h("td", { class: "measured" }, r.distillation), h("td", { class: "measured" }, when(r.start, true)),
      h("td", { class: "num" }, h("span", { class: "flag" }, `${num(r.pct, 0)}%`))));
    body.append(h("div", { class: "split", style: "padding: 0 16px 12px" },
      h("div", null, h("h3", { style: "margin: 8px 0" }, `Outages of 2 hours or more (${cov.gaps.length})`),
        cov.gaps.length ? h("div", { class: "table-wrap" }, h("table", null, h("thead", null, h("tr", null, h("th", null, "From"), h("th", null, "To"), h("th", { class: "num" }, "Length"))), h("tbody", null, gapRows)))
          : h("p", { class: "muted" }, "None in the scanned range.")),
      h("div", null, h("h3", { style: "margin: 8px 0" }, `Runs with holes (${bad.length})`),
        bad.length ? h("div", { class: "table-wrap" }, h("table", null, h("thead", null, h("tr", null, h("th", null, "Run"), h("th", null, "Started"), h("th", { class: "num" }, "Covered"))), h("tbody", null, badRows)))
          : h("p", { class: "muted" }, "Every run in range is at least 95% covered."))));
  }

  // ---------------------------------------------------------------- eflow refresh
  const $report = document.getElementById("report");
  document.getElementById("eflow-refresh").addEventListener("click", async (e) => {
    const b = e.currentTarget;
    b.setAttribute("aria-busy", "true"); b.disabled = true; b.textContent = "Refreshing…";
    try {
      const r = await api("/api/eflow/refresh", { method: "POST" });
      const list = (title, items) => items.length ? h("details", null, h("summary", null, `${title} (${items.length})`),
        h("pre", null, items.map((x) => (Array.isArray(x) ? x.join("  ") : `${x.key.join("  ")}  ${JSON.stringify(x.diff)}`)).join("\n"))) : null;
      $report.replaceChildren(
        h("div", { class: "head-row" }, h("strong", null, `Eflow refreshed: ${r.rows} rows, ${r.added.length} added, ${r.removed.length} removed, ${r.changed.length} changed.`),
          h("button", { class: "btn btn--quiet", type: "button", onclick: () => { $report.hidden = true; } }, "Dismiss")),
        ...r.warnings.map((w) => h("div", { class: "warn" }, w)),
        list("Added", r.added), list("Removed", r.removed), list("Changed", r.changed));
      $report.hidden = false;
      runsCache = { col: null, runs: null };
      await loadMeta();
      render();
    } catch (err) {
      $report.replaceChildren(h("div", { class: "warn" }, `Eflow refresh failed: ${err.message}`),
        h("button", { class: "btn btn--quiet", type: "button", onclick: () => { $report.hidden = true; } }, "Dismiss"));
      $report.hidden = false;
    } finally { b.removeAttribute("aria-busy"); b.disabled = false; b.textContent = "Refresh from Eflow"; }
  });

  // ---------------------------------------------------------------- boot
  function render() {
    document.querySelectorAll(".tabs a").forEach((a) => {
      a.setAttribute("href", `?view=${a.dataset.view}${state.col ? "&col=" + enc(state.col) : ""}`);
      a.toggleAttribute("aria-current", a.dataset.view === state.view);
      if (a.dataset.view === state.view) a.setAttribute("aria-current", "page");
    });
    if (!META) return;
    if (!state.col || !colMeta(state.col)) {
      const first = META.columns.find((c) => c.has_process_data && c.runs) || META.columns[0];
      go({ col: first.code }, true);
      return;
    }
    renderColSwitch();
    disposeCharts();
    if (state.view === "compare") return renderCompare();
    if (state.view === "coverage") return renderCoverage();
    return renderRuns();
  }

  async function boot() {
    readURL();
    $app.replaceChildren(skeleton(null, 8));
    try { await loadMeta(); }
    catch (e) { $app.replaceChildren(errorBox(e, boot)); return; }
    api("/api/db").then((s) => {
      if (!s.ok) document.querySelector(".bar").after(h("div", { class: "notice notice--bad", role: "alert" },
        `Cannot reach the datalog (${s.backend}): ${s.message}. Eflow fractions still show; charts will fail until it is back.`));
    }).catch(() => {});
    if (!META.columns.some((c) => c.runs)) {
      $app.replaceChildren(stateBox("The Eflow cache is empty",
        META.eflow_configured ? "Pull the fraction list once; after that the app works from the local copy."
          : "No Eflow URL is configured yet. Put it in config.local.toml under [eflow], restart, then refresh.",
        null, META.eflow_configured ? h("button", { class: "btn btn--primary", type: "button", onclick: () => document.getElementById("eflow-refresh").click() }, "Refresh from Eflow") : null));
      return;
    }
    render();
  }
  boot();
})();
