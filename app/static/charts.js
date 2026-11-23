// Platen dashboard charts: dependency-free SVG line charts with a crosshair
// tooltip, plus a shared hover tooltip for server-rendered bars/segments.
(function () {
  "use strict";
  const SVG = "http://www.w3.org/2000/svg";
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function el(name, attrs, parent) {
    const node = document.createElementNS(SVG, name);
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
    if (parent) parent.appendChild(node);
    return node;
  }

  function niceMax(max) {
    if (max <= 0) return 4;
    const exp = Math.pow(10, Math.floor(Math.log10(max)));
    for (const m of [1, 2, 4, 6, 8, 10]) {  // /4 gives clean tick steps
      if (m * exp >= max) return m * exp;
    }
    return 10 * exp;
  }

  function fmtNum(n) {
    if (Math.abs(n) >= 10000) return (n / 1000).toFixed(n % 1000 === 0 ? 0 : 1).replace(/\.0$/, "") + "K";
    return Math.round(n).toLocaleString();
  }

  function fmtDate(ms, unit) {
    const d = new Date(ms);
    if (unit === "month") return MONTHS[d.getMonth()] + " " + String(d.getFullYear()).slice(2);
    return d.getDate() + " " + MONTHS[d.getMonth()];
  }

  // series: [{name, color, points: [{x: ms, y}]}]; opts: {unit, yMax, ySuffix}
  function lineChart(root, series, opts) {
    const tooltip = document.createElement("div");
    tooltip.className = "chart-tooltip";
    tooltip.hidden = true;

    function render() {
      root.textContent = "";
      root.appendChild(tooltip);
      const width = Math.max(root.clientWidth, 280);
      const plotH = 200;
      const m = { top: 12, right: 64, bottom: 28, left: 44 };
      const height = m.top + plotH + m.bottom;
      const innerW = width - m.left - m.right;

      const xs = [...new Set(series.flatMap((s) => s.points.map((p) => p.x)))].sort((a, b) => a - b);
      if (!xs.length) return;
      const x0 = xs[0], x1 = xs[xs.length - 1];
      const allY = series.flatMap((s) => s.points.map((p) => p.y));
      const yMax = opts.yMax || niceMax(Math.max(...allY, 0));
      const sx = (x) => m.left + (x1 === x0 ? innerW / 2 : ((x - x0) / (x1 - x0)) * innerW);
      const sy = (y) => m.top + plotH - (y / yMax) * plotH;
      const suffix = opts.ySuffix || "";

      const svg = el("svg", { width, height, viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true" }, root);

      // Recessive grid + y ticks (clean numbers).
      for (let i = 0; i <= 4; i++) {
        const v = (yMax / 4) * i;
        const y = sy(v);
        el("line", { x1: m.left, x2: width - m.right, y1: y, y2: y, class: i === 0 ? "axis" : "grid" }, svg);
        const t = el("text", { x: m.left - 8, y: y + 4, class: "tick", "text-anchor": "end" }, svg);
        t.textContent = fmtNum(v) + suffix;
      }
      // X ticks: at most ~6, evenly spread over the data positions.
      const every = Math.max(1, Math.ceil(xs.length / Math.max(2, Math.floor(innerW / 90))));
      xs.forEach((x, i) => {
        if (i % every !== 0 && i !== xs.length - 1) return;
        if (i === xs.length - 1 && i % every !== 0 && xs.length > 1 && sx(x) - sx(xs[i - (i % every)]) < 60) return;
        const t = el("text", { x: sx(x), y: m.top + plotH + 18, class: "tick", "text-anchor": "middle" }, svg);
        t.textContent = fmtDate(x, opts.unit);
      });

      // Lines (2px, round joins); lone points get a ringed marker.
      series.forEach((s) => {
        if (!s.points.length) return;
        if (s.points.length === 1) {
          const p = s.points[0];
          el("circle", { cx: sx(p.x), cy: sy(p.y), r: 4, fill: s.color, class: "ring" }, svg);
          return;
        }
        const d = s.points.map((p, i) => (i ? "L" : "M") + sx(p.x).toFixed(1) + " " + sy(p.y).toFixed(1)).join(" ");
        el("path", { d, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
      });

      // Direct end labels, unless they would collide (legend + tooltip cover it then).
      const ends = series.filter((s) => s.points.length).map((s) => {
        const p = s.points[s.points.length - 1];
        return { s, p, y: sy(p.y) };
      });
      const collide = ends.some((a, i) => ends.some((b, j) => i < j && Math.abs(a.y - b.y) < 14));
      if (!collide) {
        ends.forEach(({ p, y }) => {
          const t = el("text", { x: sx(p.x) + 8, y: y + 4, class: "end-label" }, svg);
          t.textContent = fmtNum(p.y) + suffix;
        });
      }

      // Crosshair layer: snaps to the nearest data x; hit target is the whole plot.
      const cross = el("line", { y1: m.top, y2: m.top + plotH, class: "crosshair", visibility: "hidden" }, svg);
      const dots = series.map((s) => el("circle", { r: 4, fill: s.color, class: "ring", visibility: "hidden" }, svg));
      const hit = el("rect", { x: m.left, y: m.top, width: innerW, height: plotH, fill: "transparent" }, svg);

      function valueAt(s, x) {
        let best = null;
        for (const p of s.points) if (p.x <= x) best = p;
        return best || null;
      }

      let current = -1;
      function show(i) {
        current = Math.max(0, Math.min(xs.length - 1, i));
        const x = xs[current];
        const px = sx(x);
        cross.setAttribute("x1", px);
        cross.setAttribute("x2", px);
        cross.setAttribute("visibility", "visible");
        tooltip.textContent = "";
        const head = document.createElement("div");
        head.className = "tt-head";
        head.textContent = opts.unit === "time" ? new Date(x).toLocaleString() : fmtDate(x, opts.unit === "month" ? "month" : "day") + (opts.unit === "month" ? "" : " " + new Date(x).getFullYear());
        tooltip.appendChild(head);
        series.forEach((s, k) => {
          const p = valueAt(s, x);
          if (!p) { dots[k].setAttribute("visibility", "hidden"); return; }
          dots[k].setAttribute("cx", sx(p.x));
          dots[k].setAttribute("cy", sy(p.y));
          dots[k].setAttribute("visibility", "visible");
          const row = document.createElement("div");
          row.className = "tt-row";
          const key = document.createElement("span");
          key.className = "tt-key";
          key.style.background = s.color;
          const val = document.createElement("strong");
          val.textContent = fmtNum(p.y) + suffix;
          const name = document.createElement("span");
          name.textContent = s.name;
          row.append(key, val, name);
          tooltip.appendChild(row);
        });
        tooltip.hidden = false;
        const left = px + 12 + tooltip.offsetWidth > width ? px - 12 - tooltip.offsetWidth : px + 12;
        tooltip.style.left = left + "px";
        tooltip.style.top = m.top + "px";
      }
      function hide() {
        tooltip.hidden = true;
        cross.setAttribute("visibility", "hidden");
        dots.forEach((d) => d.setAttribute("visibility", "hidden"));
      }
      function nearest(clientX) {
        const box = svg.getBoundingClientRect();
        const px = clientX - box.left;
        let bi = 0, bd = Infinity;
        xs.forEach((x, i) => { const d = Math.abs(sx(x) - px); if (d < bd) { bd = d; bi = i; } });
        return bi;
      }
      hit.addEventListener("pointermove", (e) => show(nearest(e.clientX)));
      hit.addEventListener("pointerleave", hide);
      root.onfocus = () => show(current < 0 ? xs.length - 1 : current);
      root.onblur = hide;
      root.onkeydown = (e) => {
        if (e.key === "ArrowLeft") { show(current - 1); e.preventDefault(); }
        if (e.key === "ArrowRight") { show(current + 1); e.preventDefault(); }
        if (e.key === "Escape") hide();
      };
    }

    root.tabIndex = 0;
    render();
    if (window.ResizeObserver) {
      let w = root.clientWidth;
      new ResizeObserver(() => { if (root.clientWidth !== w) { w = root.clientWidth; render(); } }).observe(root);
    }
  }

  function localDate(iso) {
    const [y, mo, d] = iso.split("-").map(Number);
    return new Date(y, mo - 1, d).getTime();
  }

  function init() {
    const dataEl = document.getElementById("chart-data");
    if (!dataEl) return;
    const data = JSON.parse(dataEl.textContent);

    const pages = document.querySelector('[data-chart="pages"]');
    if (pages && data.series && data.series.length) {
      lineChart(pages, [
        { name: "Printed", color: "var(--series-1)", points: data.series.map((r) => ({ x: localDate(r.date), y: r.printed })) },
        { name: "Scanned", color: "var(--series-2)", points: data.series.map((r) => ({ x: localDate(r.date), y: r.scanned })) },
      ], { unit: data.unit });
    }

    const supplies = document.querySelector('[data-chart="supplies"]');
    if (supplies && data.supplies && data.supplies.length) {
      const multiPrinter = new Set(data.supplies.map((s) => s.printer)).size > 1;
      lineChart(supplies, data.supplies.slice(0, 8).map((s, i) => ({
        name: multiPrinter ? s.marker + " · " + s.printer : s.marker,
        color: `var(--series-${i + 1})`,
        points: s.points.map((p) => ({ x: Date.parse(p.t), y: p.level })).filter((p) => p.y >= 0),
      })), { unit: "time", yMax: 100, ySuffix: "%" });
    }

    // Shared tooltip for server-rendered marks (bars, segments) with data-tip.
    const tip = document.createElement("div");
    tip.className = "chart-tooltip floating";
    tip.hidden = true;
    document.body.appendChild(tip);
    const showTip = (target) => {
      tip.textContent = target.dataset.tip;
      tip.hidden = false;
      const r = target.getBoundingClientRect();
      tip.style.left = Math.min(window.scrollX + r.left, window.scrollX + window.innerWidth - tip.offsetWidth - 8) + "px";
      tip.style.top = window.scrollY + r.top - tip.offsetHeight - 6 + "px";
    };
    document.querySelectorAll("[data-tip]").forEach((node) => {
      node.addEventListener("pointerenter", () => showTip(node));
      node.addEventListener("focus", () => showTip(node));
      node.addEventListener("pointerleave", () => { tip.hidden = true; });
      node.addEventListener("blur", () => { tip.hidden = true; });
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
