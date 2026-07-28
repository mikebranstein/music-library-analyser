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

    // Rich, explanatory description: what this phase does, how it works, and why.
    if (meta.detail) {
      app.appendChild(el("div", { class: "card phase-about", html: meta.detail }));
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
    var rows = MLG.get(spec.source, []).slice();
    if (spec.filter && spec.filter.field && spec.filter.in) {
      rows = rows.filter(function (r) { return spec.filter.in.indexOf(r[spec.filter.field]) >= 0; });
    }
    var entity = spec.entity;
    var idField = ID_FIELD[entity];
    var page = ENTITY_PAGE[entity];

    var columns = (spec.columns || []).map(function (c) {
      return {
        key: c.key,
        label: c.label,
        num: !!c.num,
        filterText: function (row) { return row[c.key]; },
        render: function (row) {
          var v = row[c.key];
          if (c.badge === "severity") return MLG.severityBadge(v);
          if (c.display === "pct") return MLG.fmt.pctValue(v);
          if (c.link && page && idField) {
            return el("a", { href: MLG.rel(MLG.href(page, { id: row[idField] })), text: MLG.fmt.text(v) });
          }
          return v == null ? null : String(v);
        },
      };
    });

    app.appendChild(el("h2", { text: spec.caption || "Details" }));
    app.appendChild(MLG.table(rows, columns, { filterPlaceholder: "Filter\u2026" }));
  }
})();
