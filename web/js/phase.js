/* phase.js - generic, data-driven renderer for any of the 8 pipeline phase pages. */
(function () {
  "use strict";
  var MLG = (window.MLG = window.MLG || {});
  var el = MLG.el;

  var ID_FIELD = { document: "doc_id", page: "page_id", piece: "piece_id" };
  var ENTITY_PAGE = { document: "pages/document.html", page: "pages/page.html", piece: "pages/piece.html" };

  MLG.renderPage = function () {
    var app = document.getElementById("app");
    var n = parseInt(document.body.getAttribute("data-phase"), 10);
    var meta = MLG.phaseByNumber(n) || { n: n, title: "Phase " + n, blurb: "" };

    MLG.breadcrumbs([
      { label: "Dashboard", href: MLG.rel("index.html") },
      { label: "Pipeline \u00b7 Phase " + n },
    ]);
    if (MLG.warnIfNoData(app)) return;

    var phases = MLG.get("phases", {});
    var data = phases[String(n)] || {};
    var summary = data.summary || {};

    app.appendChild(MLG.pageHead("Phase " + n, meta.title));
    app.appendChild(el("p", { class: "muted", text: meta.blurb }));

    // Rich, explanatory description behind a collapsible toggle: what this phase does, how it
    // works, and why. Collapsed by default so the page leads with metrics, not prose.
    if (meta.detail) {
      var about = el("div", { class: "phase-about", html: meta.detail });
      app.appendChild(MLG.detailsSection("How this phase works", about, false));
    }

    // Summary line + deferred timing note
    var summaryCard = el("div", { class: "card stack" });
    summaryCard.appendChild(el("p", { html:
      "<strong>" + MLG.fmt.num(summary.records) + "</strong> " +
      MLG.fmt.text(summary.unit || "records") + " processed." }));
    if (summary.note) summaryCard.appendChild(el("p", { class: "card__hint", text: summary.note }));
    summaryCard.appendChild(el("p", { class: "card__hint", html:
      "Last run " + MLG.fmt.date(summary.timestamp) +
      " \u00b7 run duration <em>not captured yet</em>." }));
    app.appendChild(summaryCard);

    // Stat KPI cards
    if (data.stats && data.stats.length) {
      app.appendChild(el("div", { class: "grid grid--kpi", style: "margin-top:var(--space-4)" },
        data.stats.map(MLG.kpiCard)));
    }

    // Charts
    (data.charts || []).forEach(function (ch) {
      app.appendChild(el("h2", { text: ch.title }));
      app.appendChild(el("div", { class: "card" }, [MLG.barChart(ch.rows || [], { ariaLabel: ch.title })]));
    });

    // Drill-down table
    if (data.table) renderTable(app, data.table);
  };

  function renderTable(app, spec) {
    // Rows come either from a registered dataset (`source`) or inline in the spec (`rows`),
    // the latter for aggregates that aren't a standalone entity (e.g. missing-section rollups).
    var rows = (spec.rows || MLG.get(spec.source, [])).slice();
    if (spec.filter && spec.filter.field && spec.filter.in) {
      rows = rows.filter(function (r) { return spec.filter.in.indexOf(r[spec.filter.field]) >= 0; });
    }
    var entity = spec.entity;
    var idField = ID_FIELD[entity];
    var page = ENTITY_PAGE[entity];

    // Precompute the max for any in-cell bar columns that scale to the column max (used only
    // when a column has no per-row `barOf` denominator of its own).
    var barMax = {};
    (spec.columns || []).forEach(function (c) {
      if (c.bar && !c.barOf) {
        barMax[c.key] = rows.reduce(function (m, r) {
          return Math.max(m, Number(r[c.key]) || 0);
        }, 0) || 1;
      }
    });

    var columns = (spec.columns || []).map(function (c) {
      return {
        key: c.key,
        label: c.label,
        num: !!c.num,
        filterText: function (row) { return row[c.key]; },
        sortValue: c.bar ? function (row) { return Number(row[c.key]) || 0; } : undefined,
        render: function (row) {
          var v = row[c.key];
          if (c.badge === "severity") return MLG.severityBadge(v);
          if (c.display === "pct") return MLG.fmt.pctValue(v);
          if (c.bar) {
            // `barOf` makes the bar a true per-row percentage (value / row[barOf]); otherwise it
            // scales to the collection-wide max for the column.
            var denom = c.barOf ? (Number(row[c.barOf]) || 0) : barMax[c.key];
            return cellBar(Number(v) || 0, denom, c.variant);
          }
          if (c.link && page && idField) {
            return el("a", { href: MLG.rel(MLG.href(page, { id: row[idField] })), text: MLG.fmt.text(v) });
          }
          return v == null ? null : String(v);
        },
      };
    });

    app.appendChild(el("h2", { text: spec.caption || "Details" }));
    app.appendChild(MLG.table(rows, columns, {
      filterPlaceholder: "Filter\u2026",
      sortKey: spec.sortKey || null,
      dir: spec.dir || "asc",
    }));
  }

  // A compact in-cell horizontal bar (the "chart" graphic) paired with its raw value.
  function cellBar(value, max, variant) {
    var pct = Math.max(0, Math.min(100, max ? (100 * value) / max : 0));
    var bar = el("div", { class: "cell-bar__fill" });
    bar.style.width = pct + "%";
    if (variant) bar.style.background = "var(--" + variant + ")";
    var track = el("div", { class: "cell-bar__track", title: Math.round(pct) + "%" }, [bar]);
    return el("div", { class: "cell-bar" }, [
      el("span", { class: "cell-bar__num", text: MLG.fmt.num(value) }),
      track,
    ]);
  }
})();
