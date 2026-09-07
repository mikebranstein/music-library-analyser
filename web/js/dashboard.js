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

    // Attention list (capped; full list lives on the Pieces page)
    var ATTENTION_LIMIT = 5;
    var attnCard = el("div", { class: "card stack" });
    var attn = d.attention || [];
    var attnHeadRow = el("div", { class: "row", style: "justify-content:space-between;align-items:baseline" }, [
      el("div", { class: "card__label", text: "Needs attention" }),
    ]);
    if (attn.length > ATTENTION_LIMIT) {
      attnHeadRow.appendChild(
        el("a", { href: MLG.rel(MLG.href("pages/pieces.html", { severity: "high" })), text: "View all " + attn.length + " \u2192" })
      );
    }
    attnCard.appendChild(attnHeadRow);
    if (!attn.length) {
      attnCard.appendChild(el("p", { class: "muted", text: "Nothing flagged \u2014 the collection looks healthy." }));
    } else {
      attn.slice(0, ATTENTION_LIMIT).forEach(function (a) {
        var line = el("a", { class: "card card--link", href: MLG.rel(MLG.href("pages/piece.html", { id: a.piece_id })) }, [
          el("div", { class: "row", style: "justify-content:space-between" }, [
            el("strong", { text: (a.catalog_number ? a.catalog_number + " \u00b7 " : "") + MLG.fmt.text(a.title) }),
            MLG.severityBadge(a.severity),
          ]),
          el("div", { class: "card__hint", text: MLG.fmt.text(a.reason) }),
        ]);
        attnCard.appendChild(line);
      });
      if (attn.length > ATTENTION_LIMIT) {
        attnCard.appendChild(
          el("a", { class: "card__hint", href: MLG.rel(MLG.href("pages/pieces.html", { severity: "high" })),
            text: "+ " + (attn.length - ATTENTION_LIMIT) + " more flagged piece" + (attn.length - ATTENTION_LIMIT === 1 ? "" : "s") + "\u2026" }),
        );
      }
    }
    row.appendChild(attnCard);
    app.appendChild(row);

    // Pipeline strip -> each phase page (compact single-row overview; details are one click away)
    app.appendChild(el("h2", { text: "Pipeline" }));
    var flow = {};
    (d.phase_flow || []).forEach(function (f) { flow[f.n] = f; });
    var strip = el("div", { class: "pipeline-strip" });
    MLG.PHASES.forEach(function (p, i) {
      var f = flow[p.n] || {};
      strip.appendChild(
        el("a", {
          class: "pipeline-step",
          href: MLG.rel("pages/" + p.slug + ".html"),
          title: (f.note || p.blurb) + (f.records != null ? " (" + MLG.fmt.num(f.records) + " records)" : ""),
        }, [
          el("span", { class: "pipeline-step__idx", text: String(p.n) }),
          el("span", { class: "pipeline-step__title", text: p.title }),
        ])
      );
      if (i < MLG.PHASES.length - 1) {
        strip.appendChild(el("span", { class: "pipeline-step__arrow", "aria-hidden": "true", text: "\u2192" }));
      }
    });
    app.appendChild(strip);
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
