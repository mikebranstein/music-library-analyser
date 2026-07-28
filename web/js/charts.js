/*
 * charts.js - dependency-free charts built from the same design tokens.
 * Horizontal bars use plain divs; the completeness donut uses inline SVG.
 */
(function () {
  "use strict";
  var MLG = (window.MLG = window.MLG || {});
  var el = MLG.el;

  // rows: [{ label, value, max?, display? }]
  MLG.barChart = function (rows, opts) {
    opts = opts || {};
    var max = opts.max || rows.reduce(function (m, r) { return Math.max(m, r.value || 0); }, 0) || 1;
    var chart = el("div", { class: "chart", role: "img", "aria-label": opts.ariaLabel || "bar chart" });
    rows.forEach(function (r) {
      var pct = Math.max(0, Math.min(100, (100 * (r.value || 0)) / max));
      var track = el("div", { class: "chart__track" });
      var bar = el("div", { class: "chart__bar" });
      bar.style.width = pct + "%";
      if (r.variant) bar.style.background = "var(--" + r.variant + ")";
      track.appendChild(bar);
      chart.appendChild(
        el("div", { class: "chart__row" }, [
          el("span", { class: "chart__label", title: r.label, text: r.label }),
          track,
          el("span", { class: "chart__value", text: r.display != null ? r.display : MLG.fmt.num(r.value) }),
        ])
      );
    });
    return chart;
  };

  // Completeness gauge: a single-value donut (0-100).
  MLG.donut = function (value, label) {
    var v = Math.max(0, Math.min(100, value || 0));
    var r = 52, c = 2 * Math.PI * r, off = c * (1 - v / 100);
    var ns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 120 120");
    svg.setAttribute("width", "120");
    svg.setAttribute("height", "120");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", (label || "value") + " " + v.toFixed(0) + "%");

    function circle(color, dash, offset, width) {
      var el2 = document.createElementNS(ns, "circle");
      el2.setAttribute("cx", "60");
      el2.setAttribute("cy", "60");
      el2.setAttribute("r", String(r));
      el2.setAttribute("fill", "none");
      el2.setAttribute("stroke", color);
      el2.setAttribute("stroke-width", String(width));
      if (dash != null) el2.setAttribute("stroke-dasharray", String(dash));
      if (offset != null) el2.setAttribute("stroke-dashoffset", String(offset));
      el2.setAttribute("transform", "rotate(-90 60 60)");
      el2.setAttribute("stroke-linecap", "round");
      return el2;
    }
    svg.appendChild(circle("var(--border)", null, null, 12));
    var variant = v >= 90 ? "success" : v >= 60 ? "warning" : "error";
    svg.appendChild(circle("var(--" + variant + ")", c, off, 12));

    var text = document.createElementNS(ns, "text");
    text.setAttribute("x", "60");
    text.setAttribute("y", "64");
    text.setAttribute("text-anchor", "middle");
    text.setAttribute("font-size", "26");
    text.setAttribute("font-weight", "700");
    text.setAttribute("fill", "var(--text)");
    text.textContent = v.toFixed(0) + "%";
    svg.appendChild(text);
    return svg;
  };
})();
