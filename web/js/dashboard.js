/* dashboard.js - the top-level overview with full drill-down. */
(function () {
  "use strict";
  var MLG = (window.MLG = window.MLG || {});
  var el = MLG.el;

  MLG.renderPage = function () {
    var app = document.getElementById("app");
    MLG.breadcrumbs([{ label: "Dashboard" }]);
    if (MLG.warnIfNoData(app)) return;

    var d = MLG.get("dashboard", {});
    app.appendChild(MLG.pageHead("Overview", "Music Library"));

    // KPIs
    app.appendChild(MLG.kpiGrid(d.kpis || []));

    // Completeness gauge + state breakdown
    var row = el("div", { class: "grid grid--2", style: "margin-top:var(--space-4)" });

    var gaugeCard = el("div", { class: "card" }, [
      el("div", { class: "card__label", text: (d.completeness && d.completeness.label) || "Completeness" }),
      el("div", { class: "row", style: "margin-top:var(--space-3);gap:var(--space-5)" }, [
        MLG.donut(d.completeness ? d.completeness.score : 0, "Completeness"),
        el("div", { class: "stack" }, [
          MLG.severityChips(d.severity || {}),
          MLG.qualityChips(d.quality || {}),
        ]),
      ]),
    ]);
    row.appendChild(gaugeCard);

    // Attention list
    var attnCard = el("div", { class: "card stack" });
    attnCard.appendChild(el("div", { class: "card__label", text: "Needs attention" }));
    var attn = d.attention || [];
    if (!attn.length) {
      attnCard.appendChild(el("p", { class: "muted", text: "Nothing flagged \u2014 the collection looks healthy." }));
    } else {
      attn.forEach(function (a) {
        var line = el("a", { class: "card card--link", href: MLG.rel(MLG.href("pages/piece.html", { id: a.piece_id })) }, [
          el("div", { class: "row", style: "justify-content:space-between" }, [
            el("strong", { text: (a.catalog_number ? a.catalog_number + " \u00b7 " : "") + MLG.fmt.text(a.title) }),
            MLG.severityBadge(a.severity),
          ]),
          el("div", { class: "card__hint", text: MLG.fmt.text(a.reason) }),
        ]);
        attnCard.appendChild(line);
      });
    }
    row.appendChild(attnCard);
    app.appendChild(row);

    // Pipeline flow strip -> each phase page
    app.appendChild(el("h2", { text: "Pipeline" }));
    var flow = {};
    (d.phase_flow || []).forEach(function (f) { flow[f.n] = f; });
    var grid = el("div", { class: "grid grid--phases" });
    MLG.PHASES.forEach(function (p) {
      var f = flow[p.n] || {};
      grid.appendChild(
        el("a", { class: "card card--link phase-card", href: MLG.rel("pages/" + p.slug + ".html") }, [
          el("div", { class: "phase-card__idx", text: "Phase " + p.n }),
          el("div", { class: "phase-card__title", text: p.title }),
          el("div", { class: "phase-card__blurb", text: f.note || p.blurb }),
          el("div", { class: "phase-card__count", text: f.records != null ? MLG.fmt.num(f.records) + " records" : "" }),
        ])
      );
    });
    app.appendChild(grid);
  };

  MLG.severityChips = function (sev) {
    return el("div", { class: "row" }, [
      MLG.badge((sev.high || 0) + " high", "high"),
      MLG.badge((sev.review || 0) + " review", "review"),
      MLG.badge((sev.ok || 0) + " ok", "ok"),
    ]);
  };
  MLG.qualityChips = function (q) {
    return el("div", { class: "row" }, [
      MLG.badge((q.good || 0) + " good", "good"),
      MLG.badge((q.fair || 0) + " fair", "fair"),
      MLG.badge((q.poor || 0) + " poor", "poor"),
    ]);
  };
})();
