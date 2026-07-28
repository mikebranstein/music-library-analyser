/* entities.js - pieces / piece / documents / document / page renderers.
 * The correct view is chosen from <body data-view="...">. */
(function () {
  "use strict";
  var MLG = (window.MLG = window.MLG || {});
  var el = MLG.el;
  var fmt = MLG.fmt;

  function docById(id) {
    return MLG.get("documents", []).filter(function (d) { return d.doc_id === id; })[0] || null;
  }
  function pieceById(id) {
    return MLG.get("pieces", []).filter(function (p) { return p.piece_id === id; })[0] || null;
  }
  function pageById(id) {
    return MLG.get("pages", []).filter(function (p) { return p.page_id === id; })[0] || null;
  }
  function link(page, params, label) {
    return el("a", { href: MLG.rel(MLG.href("pages/" + page, params)), text: fmt.text(label) });
  }
  function notFound(app, what) {
    app.appendChild(el("div", { class: "callout", text: what + " not found. It may have a different id in the current data." }));
  }

  // ---- pieces index -------------------------------------------------------
  function renderPiecesIndex(app) {
    MLG.breadcrumbs([{ label: "Dashboard", href: MLG.rel("index.html") }, { label: "Pieces" }]);
    if (MLG.warnIfNoData(app)) return;
    app.appendChild(MLG.pageHead("Browse", "Pieces"));
    var rows = MLG.get("pieces", []);
    var columns = [
      { key: "catalog_number", label: "#", filterText: function (r) { return r.catalog_number; } },
      { key: "title", label: "Title", render: function (r) { return link("piece.html", { id: r.piece_id }, r.title); }, filterText: function (r) { return r.title; } },
      { key: "severity", label: "Severity", render: function (r) { return MLG.severityBadge(r.severity); }, sortValue: function (r) { return r.severity; }, filterText: function (r) { return r.severity; } },
      { key: "completeness_score", label: "Complete", num: true, render: function (r) { return fmt.pctValue(r.completeness_score); } },
      { key: "completeness_tier", label: "Tier", filterText: function (r) { return r.completeness_tier; } },
      { key: "document_count", label: "Docs", num: true },
      { key: "page_count", label: "Pages", num: true },
      { key: "missing_required_count", label: "Missing", num: true },
    ];
    app.appendChild(MLG.table(rows, columns, { sortKey: "catalog_number", filterPlaceholder: "Filter pieces\u2026" }));
  }

  // ---- piece detail -------------------------------------------------------
  function renderPieceDetail(app) {
    if (MLG.warnIfNoData(app)) return;
    var id = MLG.param("id");
    var p = pieceById(id);
    MLG.breadcrumbs([
      { label: "Dashboard", href: MLG.rel("index.html") },
      { label: "Pieces", href: MLG.rel("pages/pieces.html") },
      { label: p ? fmt.text(p.title) : "Piece" },
    ]);
    if (!p) return notFound(app, "Piece");

    app.appendChild(MLG.pageHead((p.catalog_number ? "Piece #" + p.catalog_number : "Piece"), p.title));
    app.appendChild(el("div", { class: "row" }, [MLG.severityBadge(p.severity), MLG.badge(fmt.title(p.completeness_tier), "muted")]));

    // --- Overview / metadata ------------------------------------------------
    var row = el("div", { class: "grid grid--2", style: "margin-top:var(--space-4)" });
    row.appendChild(el("div", { class: "card" }, [
      el("div", { class: "card__label", text: "Completeness" }),
      el("div", { class: "row", style: "margin-top:var(--space-3)" }, [MLG.donut(p.completeness_score, "Completeness")]),
    ]));
    row.appendChild(el("div", { class: "card" }, [
      el("div", { class: "card__label", text: "Overview" }),
      MLG.facts([
        ["Catalog #", p.catalog_number],
        ["Folder", p.piece_folder],
        ["Documents", fmt.num(p.document_count)],
        ["Pages", fmt.num(p.page_count)],
        ["Missing required", fmt.num(p.missing_required_count)],
      ]),
    ]));
    app.appendChild(row);

    // --- Score (broken out near the metadata, not with the other files) -----
    app.appendChild(el("h2", { text: "Score" }));
    app.appendChild(pieceScoreCard(p));

    // --- Instrumentation (collapsible) --------------------------------------
    var instrumentation = p.instrumentation || [];
    if (instrumentation.length) {
      app.appendChild(MLG.detailsSection(
        "Instrumentation (" + instrumentation.length + ")",
        instrumentationTable(instrumentation),
        true
      ));
    } else if (p.missing_required && p.missing_required.length) {
      // Fallback when no authoritative expected-parts list exists: at least surface the gaps.
      app.appendChild(MLG.detailsSection(
        "Missing required parts (" + p.missing_required.length + ")",
        missingRequiredTable(p.missing_required),
        true
      ));
    } else {
      app.appendChild(el("p", { class: "muted", text: "No authoritative instrumentation available for this piece." }));
    }

    // --- Documents (collapsed by default, at the bottom) --------------------
    var docs = MLG.get("documents", []).filter(function (d) { return d.piece_id === id; });
    app.appendChild(MLG.detailsSection(
      "Documents (" + docs.length + ")",
      MLG.table(docs, [
        { key: "pdf_filename", label: "Document", render: function (r) { return link("document.html", { id: r.doc_id }, r.pdf_filename); }, filterText: function (r) { return r.pdf_filename; } },
        { key: "instrument", label: "Instrument" },
        { key: "section", label: "Section" },
        { key: "page_count", label: "Pages", num: true },
        { key: "quality", label: "Quality", render: function (r) { return MLG.qualityBadge(r.quality); }, filterText: function (r) { return r.quality; } },
      ], { filterPlaceholder: "Filter documents\u2026" }),
      false
    ));
  }

  // Score card: presence, type(s), and links to the score document(s) + original PDF.
  function pieceScoreCard(p) {
    var score = p.score || {};
    var docs = score.documents || [];
    var card = el("div", { class: "card" });
    var head = el("div", { class: "row" });
    if (docs.length) {
      head.appendChild(MLG.badge("Score present", "ok"));
    } else if (score.score_missing) {
      head.appendChild(MLG.badge("No score detected", "high"));
    } else {
      head.appendChild(MLG.badge("Score unknown", "muted"));
    }
    if (score.score_types && score.score_types.length) {
      head.appendChild(el("span", { class: "muted", text: score.score_types.map(fmt.title).join(", ") }));
    }
    card.appendChild(head);

    if (docs.length) {
      var list = el("div", { class: "stack", style: "margin-top:var(--space-3)" });
      docs.forEach(function (d) {
        var wrap = el("div", { class: "stack" });
        wrap.appendChild(link("document.html", { id: d.doc_id }, d.filename));
        wrap.appendChild(MLG.pdfControl(d.pdf_path));
        list.appendChild(wrap);
      });
      card.appendChild(list);
    } else {
      card.appendChild(el("p", { class: "muted", style: "margin-top:var(--space-2)", text: "No full/condensed score is linked to this piece." }));
    }
    return card;
  }

  // Instrumentation grid: canonical order, required/optional, present/missing, and doc links.
  function instrumentationTable(parts) {
    var t = el("table", { class: "data instrumentation" });
    t.appendChild(el("thead", {}, [el("tr", {}, [
      el("th", { class: "num", text: "#" }),
      el("th", { text: "Instrument" }),
      el("th", { text: "Section" }),
      el("th", { text: "Required" }),
      el("th", { text: "Status" }),
      el("th", { text: "Document" }),
    ])]));
    var tb = el("tbody");
    parts.forEach(function (part, i) {
      var idx = part.part_index != null ? part.part_index : i + 1;
      var missingRequired = part.required && !part.present;
      var tr = el("tr", missingRequired ? { class: "is-missing" } : {});
      tr.appendChild(el("td", { class: "num", text: String(idx) }));
      tr.appendChild(el("td", { text: fmt.text(part.label || part.canonical_instrument) }));
      tr.appendChild(el("td", { text: fmt.text(part.section) }));
      tr.appendChild(el("td", {}, [MLG.badge(part.required ? "Required" : "Optional", part.required ? "muted" : "plain")]));
      tr.appendChild(el("td", {}, [part.present ? MLG.badge("Present", "ok") : MLG.badge("Missing", part.required ? "high" : "muted")]));
      tr.appendChild(el("td", {}, [partDocuments(part.documents)]));
      tb.appendChild(tr);
    });
    t.appendChild(tb);
    return el("div", { class: "table-wrap" }, [t]);
  }

  function partDocuments(docs) {
    if (!docs || !docs.length) return document.createTextNode("\u2014");
    var wrap = el("div", { class: "stack-tight" });
    docs.forEach(function (d) {
      wrap.appendChild(link("document.html", { id: d.doc_id }, d.filename));
    });
    return wrap;
  }

  function missingRequiredTable(missing) {
    var t = el("table", { class: "data" });
    t.appendChild(el("thead", {}, [el("tr", {}, [el("th", { text: "Part" }), el("th", { text: "Instrument" }), el("th", { text: "Section" })])]));
    var tb = el("tbody");
    missing.forEach(function (m) {
      tb.appendChild(el("tr", { class: "is-missing" }, [el("td", { text: fmt.text(m.label) }), el("td", { text: fmt.text(m.canonical_instrument) }), el("td", { text: fmt.text(m.section) })]));
    });
    t.appendChild(tb);
    return el("div", { class: "table-wrap" }, [t]);
  }

  // ---- documents index ----------------------------------------------------
  function renderDocumentsIndex(app) {
    MLG.breadcrumbs([{ label: "Dashboard", href: MLG.rel("index.html") }, { label: "Documents" }]);
    if (MLG.warnIfNoData(app)) return;
    app.appendChild(MLG.pageHead("Browse", "Documents"));
    var rows = MLG.get("documents", []);
    app.appendChild(MLG.table(rows, [
      { key: "pdf_filename", label: "Document", render: function (r) { return link("document.html", { id: r.doc_id }, r.pdf_filename); }, filterText: function (r) { return r.pdf_filename; } },
      { key: "piece_title", label: "Piece", render: function (r) { return link("piece.html", { id: r.piece_id }, r.piece_title); }, filterText: function (r) { return r.piece_title; } },
      { key: "instrument", label: "Instrument" },
      { key: "section", label: "Section" },
      { key: "page_count", label: "Pages", num: true },
      { key: "quality", label: "Quality", render: function (r) { return MLG.qualityBadge(r.quality); }, filterText: function (r) { return r.quality; } },
      { key: "notation_source", label: "Notation" },
    ], { sortKey: "pdf_filename", filterPlaceholder: "Filter documents\u2026" }));
  }

  // ---- document detail ----------------------------------------------------
  function renderDocumentDetail(app) {
    if (MLG.warnIfNoData(app)) return;
    var id = MLG.param("id");
    var d = docById(id);
    MLG.breadcrumbs([
      { label: "Dashboard", href: MLG.rel("index.html") },
      { label: "Documents", href: MLG.rel("pages/documents.html") },
      { label: d ? fmt.text(d.pdf_filename) : "Document" },
    ]);
    if (!d) return notFound(app, "Document");

    app.appendChild(MLG.pageHead("Document", d.pdf_filename));
    app.appendChild(el("div", { class: "row" }, [
      MLG.qualityBadge(d.quality),
      MLG.badge(fmt.title(d.notation_source), "muted"),
      d.piece_id ? link("piece.html", { id: d.piece_id }, "\u2190 " + fmt.text(d.piece_title)) : null,
    ]));

    var row = el("div", { class: "grid grid--2", style: "margin-top:var(--space-4)" });
    row.appendChild(el("div", { class: "card" }, [MLG.thumbEl(d.thumbnail, d.pdf_filename)]));
    row.appendChild(el("div", { class: "card" }, [
      el("div", { class: "card__label", text: "Facts" }),
      MLG.facts([
        ["Instrument", d.instrument],
        ["Section", d.section],
        ["Pages", fmt.num(d.page_count)],
        ["Notation source", d.notation_source],
        ["Legibility", d.legibility],
        ["OCR", d.ocr_status + " (" + fmt.pctValue((d.ocr_confidence || 0) * 100) + ")"],
        ["Vision", d.vision_status + " (" + fmt.pctValue((d.vision_confidence || 0) * 100) + ")"],
      ]),
    ]));
    app.appendChild(row);

    // Original PDF
    app.appendChild(el("h2", { text: "Original file" }));
    app.appendChild(el("div", { class: "card" }, [MLG.pdfControl(d.pdf_path)]));

    // Heavy text
    var ocr = MLG.expander("Extracted text (OCR)", d.first_text);
    if (ocr) { app.appendChild(el("h2", { text: "Text & notes" })); app.appendChild(ocr); }
    var notes = MLG.expander("Vision notes", d.vision_notes);
    if (notes) app.appendChild(notes);

    // Pages
    var pages = MLG.get("pages", []).filter(function (pg) { return pg.doc_id === id; });
    app.appendChild(el("h2", { text: "Pages (" + pages.length + ")" }));
    app.appendChild(MLG.table(pages, [
      { key: "page_num", label: "Page", num: true, render: function (r) { return link("page.html", { id: r.page_id }, "Page " + r.page_num); } },
      { key: "orientation", label: "Orientation" },
      { key: "staff_line_count", label: "Staves", num: true },
      { key: "text_density", label: "Density", num: true, render: function (r) { return fmt.text(r.text_density); } },
      { key: "is_blank", label: "Blank", render: function (r) { return r.is_blank ? "yes" : "no"; } },
    ], { filter: false }));
  }

  // ---- page detail (deepest) ----------------------------------------------
  function renderPageDetail(app) {
    if (MLG.warnIfNoData(app)) return;
    var id = MLG.param("id");
    var pg = pageById(id);
    var d = pg ? docById(pg.doc_id) : null;
    var piece = pg ? pieceById(pg.piece_id) : null;
    MLG.breadcrumbs([
      { label: "Dashboard", href: MLG.rel("index.html") },
      piece ? { label: fmt.text(piece.title), href: MLG.rel(MLG.href("pages/piece.html", { id: piece.piece_id })) } : { label: "Pieces", href: MLG.rel("pages/pieces.html") },
      d ? { label: fmt.text(d.pdf_filename), href: MLG.rel(MLG.href("pages/document.html", { id: d.doc_id })) } : { label: "Document" },
      { label: pg ? "Page " + pg.page_num : "Page" },
    ]);
    if (!pg) return notFound(app, "Page");

    app.appendChild(MLG.pageHead("Page " + pg.page_num, fmt.text(pg.doc_filename)));

    var row = el("div", { class: "grid grid--2" });
    row.appendChild(el("div", { class: "card" }, [MLG.thumbEl(pg.thumbnail, "Page " + pg.page_num)]));
    row.appendChild(el("div", { class: "card" }, [
      el("div", { class: "card__label", text: "Geometry & render" }),
      MLG.facts([
        ["Page", fmt.num(pg.page_num)],
        ["Render DPI", fmt.num(pg.render_dpi)],
        ["Estimated DPI", fmt.num(pg.estimated_dpi)],
        ["Size (px)", pg.width_px + " \u00d7 " + pg.height_px],
        ["Orientation", pg.orientation],
        ["Aspect ratio", pg.aspect_ratio],
        ["Skew (deg)", pg.skew_angle_deg],
      ]),
    ]));
    app.appendChild(row);

    app.appendChild(el("h2", { text: "Content metrics" }));
    app.appendChild(el("div", { class: "card" }, [
      MLG.facts([
        ["Text density", pg.text_density],
        ["Black/white ratio", pg.black_white_ratio],
        ["Contrast (std)", pg.contrast_std],
        ["Blur variance", pg.blur_variance],
        ["Blank", pg.is_blank ? "yes" : "no"],
        ["Has staves", pg.has_staves ? "yes" : "no"],
        ["Staff lines", fmt.num(pg.staff_line_count)],
        ["OSD rotation", pg.osd_rotation],
        ["OSD orient. conf.", pg.osd_orientation_conf],
        ["OSD script", pg.osd_script],
      ]),
    ]));

    if (d) {
      app.appendChild(el("h2", { text: "Original file" }));
      app.appendChild(el("div", { class: "card" }, [MLG.pdfControl(d.pdf_path)]));
    }

    var ocr = MLG.expander("Raw OCR text", pg.first_text);
    if (ocr) { app.appendChild(el("h2", { text: "Text" })); app.appendChild(ocr); }
  }

  var VIEWS = {
    pieces: renderPiecesIndex,
    piece: renderPieceDetail,
    documents: renderDocumentsIndex,
    document: renderDocumentDetail,
    page: renderPageDetail,
  };

  MLG.renderPage = function () {
    var app = document.getElementById("app");
    var view = document.body.getAttribute("data-view");
    var fn = VIEWS[view];
    if (fn) fn(app);
    else app.appendChild(el("div", { class: "callout", text: "Unknown view: " + view }));
  };
})();
