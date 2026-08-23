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
    // Overview spans the wide left column (metadata + score); Completeness is a
    // narrow panel on the right.
    var meta = p.metadata || {};
    var row = el("div", { class: "grid grid--overview", style: "margin-top:var(--space-4); margin-bottom:var(--space-5)" });

    var overview = el("div", { class: "card" }, [el("div", { class: "card__label", text: "Overview" })]);
    if (meta.summary) {
      overview.appendChild(el("p", { class: "piece-summary", text: fmt.text(meta.summary) }));
    }
    overview.appendChild(MLG.facts([
      meta.composer ? ["Composer", fmt.text(meta.composer)] : null,
      meta.arranger ? ["Arranger", fmt.text(meta.arranger)] : null,
      meta.publisher ? ["Publisher", fmt.text(meta.publisher)] : null,
      meta.year ? ["Year", fmt.text(meta.year)] : null,
      ["Score", pieceScoreFact(p)],
      ["Catalog #", p.catalog_number],
      ["Folder", p.piece_folder],
      ["Documents", fmt.num(p.document_count)],
      ["Pages", fmt.num(p.page_count)],
      ["Missing required", fmt.num(p.missing_required_count)],
    ]));
    row.appendChild(overview);

    row.appendChild(el("div", { class: "card" }, [
      el("div", { class: "card__label", text: "Completeness" }),
      el("div", { class: "row", style: "margin-top:var(--space-3); justify-content:center" }, [MLG.donut(p.completeness_score, "Completeness")]),
    ]));
    app.appendChild(row);

    // --- Instrumentation (collapsible) --------------------------------------
    var instrumentation = p.instrumentation || [];
    var unlisted = p.unlisted || [];
    // No authoritative instrumentation was resolved for this piece: make it explicit that whatever
    // parts we show are the library's holdings alone, never a verified required list.
    if (!p.has_expected_parts) {
      var scoreInfo = p.score || {};
      var noScoreMsg = scoreInfo.score_missing
        ? "No score was found for this piece and no authoritative instrumentation could be resolved from any source."
        : "No authoritative instrumentation could be resolved for this piece.";
      app.appendChild(el("div", { class: "callout", html:
        "<strong>No score \u2014 held parts only.</strong> " + noScoreMsg +
        " The parts listed below are the library's holdings, not a verified required instrumentation, so completeness cannot be judged." }));
    }
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
    } else if (!unlisted.length) {
      app.appendChild(el("p", { class: "muted", text: "No authoritative instrumentation available for this piece." }));
    }

    // --- Held but not in the instrumentation ("Present (unlisted)") ----------
    // Parts the library owns that the resolved edition's instrumentation does not list (e.g. an
    // alternate-transposition copy or a surplus part). Informational only; excluded from
    // completeness. Shown in their own section so the instrumentation grid stays a faithful mirror
    // of the published edition.
    if (unlisted.length) {
      app.appendChild(MLG.detailsSection(
        "Also held \u2014 not in instrumentation (" + unlisted.length + ")",
        unlistedTable(unlisted),
        false
      ));
    }

    // --- Instrumentation source / provenance (collapsible) ------------------
    if (p.instrumentation_source) {
      app.appendChild(MLG.detailsSection(
        "Instrumentation source",
        instrumentationSourceCard(p.instrumentation_source),
        false
      ));
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

  // Score fact for the Overview panel: presence, full/partial indicator, and link(s) to the
  // score document(s). Returned as a single node so it can live in the Overview facts list.
  function canonicalInstrumentName(canonical) {
    var value = fmt.text(canonical || "");
    if (value === "—") return value;
    var overrides = {
      alb: "Alto B",
      alto_sax: "Alto Saxophone",
      baritone_sax: "Baritone Saxophone",
      bass_sax: "Bass Saxophone",
      bass_trombone: "Bass Trombone",
      clarinet: "Clarinet",
      contrabassoon: "Contrabassoon",
      cornet: "Cornet",
      eb_clarinet: "E-flat Clarinet",
      english_horn: "English Horn",
      euphonium: "Euphonium",
      flute: "Flute",
      horn: "Horn in F",
      oboe: "Oboe",
      piccolo: "Piccolo",
      soprano_sax: "Soprano Saxophone",
      tenor_sax: "Tenor Saxophone",
      trombone: "Trombone",
      trumpet: "Trumpet",
      tuba: "Tuba",
    };
    if (overrides[value.replace(/\s+/g, "_").toLowerCase()]) {
      return overrides[value.replace(/\s+/g, "_").toLowerCase()];
    }
    var normalized = String(value).replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim();
    return normalized ? fmt.title(normalized) : "—";
  }

  function standardSectionName(section) {
    var raw = fmt.text(section || "");
    if (raw === "—") return raw;
    var normalized = String(raw).trim().toLowerCase().replace(/[_\-\s]+/g, "_").replace(/^_+|_+$/g, "");
    var overrides = {
      flutes: "Flutes",
      double_reeds: "Double Reeds",
      clarinets: "Clarinets",
      saxophones: "Saxophones",
      cornets_trumpets: "Cornets & Trumpets",
      horns: "Horns",
      low_brass: "Low Brass",
      tubas: "Low Brass",
      strings: "Strings",
      keyboards: "Keyboards",
      voices: "Voices",
      percussion: "Percussion",
      score: "Score",
    };
    return overrides[normalized] || fmt.title(raw);
  }

  function pieceScoreFact(p) {
  var score = p.score || {};
  var docs = score.documents || [];
  var wrap = el("div", { class: "stack-tight" });

    var head = el("div", { class: "row" });
    if (docs.length) {
      var types = score.score_types || [];
      var isFull = types.indexOf("full_score") !== -1;
      if (isFull) {
        head.appendChild(MLG.badge("Full score", "ok"));
      } else if (types.length) {
        head.appendChild(MLG.badge("Partial score", "warning"));
        head.appendChild(el("span", { class: "muted", text: types.map(fmt.title).join(", ") }));
      } else {
        head.appendChild(MLG.badge("Score present", "ok"));
      }
    } else if (score.score_missing) {
      head.appendChild(MLG.badge("No score", "high"));
    } else {
      head.appendChild(MLG.badge("Score unknown", "muted"));
    }
    wrap.appendChild(head);

    docs.forEach(function (d) {
      wrap.appendChild(link("document.html", { id: d.doc_id }, d.filename));
    });
    return wrap;
  }

  // Instrumentation grid: canonical order, required/optional, present/missing. The instrument
  // name links to the original document when one satisfies the part.
  function instrumentationTable(parts) {
    var t = el("table", { class: "data instrumentation" });
    t.appendChild(el("thead", {}, [el("tr", {}, [
      el("th", { class: "num", text: "Part #" }),
      el("th", { text: "Instrument" }),
      el("th", { text: "Section" }),
      el("th", { text: "Required" }),
      el("th", { text: "Status" }),
    ])]));
    var tb = el("tbody");
    parts.forEach(function (part, i) {
      var idx = part.part_index != null ? part.part_index : i + 1;
      var missingRequired = part.required && !part.present;
      var tr = el("tr", missingRequired ? { class: "is-missing" } : {});
      tr.appendChild(el("td", { class: "num", text: String(idx) }));
      tr.appendChild(el("td", {}, [instrumentCell(part)]));
      tr.appendChild(el("td", { text: standardSectionName(part.section) }));
      tr.appendChild(el("td", {}, [MLG.badge(part.required ? "Required" : "Optional", part.required ? "muted" : "plain")]));
      tr.appendChild(el("td", {}, [part.present ? MLG.badge("Present", "ok") : MLG.badge("Missing", part.required ? "high" : "muted")]));
      tb.appendChild(tr);
    });
    t.appendChild(tb);
    return el("div", { class: "table-wrap" }, [t]);
  }

  // "Held but not in the instrumentation" grid: parts the library owns that the resolved edition
  // does not list. Every row is present by definition and marked "Present (unlisted)"; these never
  // count toward completeness.
  function unlistedTable(parts) {
    var t = el("table", { class: "data instrumentation" });
    t.appendChild(el("thead", {}, [el("tr", {}, [
      el("th", { class: "num", text: "Part #" }),
      el("th", { text: "Instrument" }),
      el("th", { class: "num", text: "Copies" }),
      el("th", { text: "Status" }),
    ])]));
    var tb = el("tbody");
    parts.forEach(function (part, i) {
      var idx = part.part_index != null ? part.part_index : i + 1;
      var tr = el("tr", {});
      tr.appendChild(el("td", { class: "num", text: String(idx) }));
      tr.appendChild(el("td", { text: canonicalInstrumentName(part.canonical_instrument || part.label) }));
      tr.appendChild(el("td", { class: "num", text: String(part.count || 1) }));
      tr.appendChild(el("td", {}, [MLG.badge("Present (unlisted)", "muted")]));
      tb.appendChild(tr);
    });
    t.appendChild(tb);
    return el("div", { class: "table-wrap" }, [t]);
  }

  // Instrument name, linked to the original document when the part is satisfied by one.
  function instrumentCell(part) {
    var canonical = part.canonical_instrument || part.label || "";
    var label = canonicalInstrumentName(canonical);
    var originalLabel = fmt.text(part.label || part.canonical_instrument || "");
    var docs = part.documents || [];
    if (docs.length) {
      var linkNode = link("document.html", { id: docs[0].doc_id }, label);
      if (originalLabel && originalLabel !== label) {
        linkNode.setAttribute("title", originalLabel);
      }
      return linkNode;
    }
    var node = document.createTextNode(label);
    if (originalLabel && originalLabel !== label) {
      node = document.createTextNode(label);
    }
    return node;
  }

  // "Where did this instrumentation come from?" \u2014 provenance summary for the piece detail
  // page: the resolution method, confidence, LLM notes, and any web source links. Kept collapsed
  // so it sits beneath the instrumentation grid without crowding the overview.
  function instrumentationSourceCard(src) {
    var METHOD_VARIANT = {
      local_score_ocr: "ok",
      score_image_ocr: "ok",
      windrep_lookup: "review",
      authority_lookup: "review",
      conservative_fallback: "muted",
    };
    var wrap = el("div", { class: "stack-tight" });

    var head = el("div", { class: "row" }, [
      MLG.badge(fmt.text(src.method_label || src.method), METHOD_VARIANT[src.method] || "muted"),
    ]);
    if (src.status) head.appendChild(MLG.badge("Lookup: " + fmt.title(src.status), "plain"));
    if (src.confidence != null) {
      head.appendChild(el("span", { class: "muted", text: "Confidence " + fmt.pctValue(src.confidence * 100) }));
    }
    wrap.appendChild(head);

    if (src.summary) wrap.appendChild(el("p", { class: "piece-summary", text: fmt.text(src.summary) }));

    var factPairs = [
      src.model ? ["LLM model", fmt.text(src.model)] : null,
      src.local_score_path ? ["Score file OCR'd", fmt.text(src.local_score_path)] : null,
      src.ocr_source ? ["OCR source", fmt.title(src.ocr_source)] : null,
    ].filter(Boolean);
    if (factPairs.length) wrap.appendChild(MLG.facts(factPairs));

    if (src.notes) {
      wrap.appendChild(el("div", { class: "card__label", text: "LLM notes" }));
      wrap.appendChild(el("p", { class: "piece-summary", text: fmt.text(src.notes) }));
    }

    var sources = src.sources || [];
    if (sources.length) {
      wrap.appendChild(el("div", { class: "card__label", text: "Sources (" + sources.length + ")" }));
      var ul = el("ul", { class: "source-list" });
      sources.forEach(function (s) {
        var li = el("li");
        if (s.url) {
          li.appendChild(el("a", { href: s.url, text: fmt.text(s.title || s.url), target: "_blank", rel: "noopener noreferrer" }));
        } else {
          li.appendChild(document.createTextNode(fmt.text(s.title)));
        }
        if (s.snippet) li.appendChild(el("div", { class: "muted source-snippet", text: fmt.text(s.snippet) }));
        ul.appendChild(li);
      });
      wrap.appendChild(ul);
    }
    return wrap;
  }

  function missingRequiredTable(missing) {
    var t = el("table", { class: "data" });
    t.appendChild(el("thead", {}, [el("tr", {}, [el("th", { text: "Part" }), el("th", { text: "Instrument" }), el("th", { text: "Section" })])]));
    var tb = el("tbody");
    missing.forEach(function (m) {
      tb.appendChild(el("tr", { class: "is-missing" }, [
        el("td", { text: fmt.text(m.label) }),
        el("td", { text: canonicalInstrumentName(m.canonical_instrument || m.label) }),
        el("td", { text: standardSectionName(m.section) }),
      ]));
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

  // ---- section detail (drill-down from phase 7 "Top missing sections") ----
  // Lists every piece that is missing one or more *required* parts belonging to this section.
  function renderSectionDetail(app) {
    if (MLG.warnIfNoData(app)) return;
    var id = MLG.param("id"); // raw section key, e.g. "flutes"
    var label = fmt.title(id);
    MLG.breadcrumbs([
      { label: "Dashboard", href: MLG.rel("index.html") },
      { label: "Collection Report", href: MLG.rel("pages/phase-07-collection.html") },
      { label: id ? label : "Section" },
    ]);
    if (!id) return notFound(app, "Section");

    var rows = [];
    MLG.get("pieces", []).forEach(function (p) {
      var missing = (p.missing_required || []).filter(function (m) { return m.section === id; });
      if (missing.length) {
        rows.push({
          piece_id: p.piece_id,
          catalog_number: p.catalog_number,
          title: p.title,
          severity: p.severity,
          completeness_score: p.completeness_score,
          missing_in_section: missing.length,
          missing_parts: missing.map(function (m) { return fmt.text(m.label); }).join(", "),
        });
      }
    });

    app.appendChild(MLG.pageHead("Missing section", label));
    app.appendChild(el("p", { class: "muted", text:
      rows.length + " piece" + (rows.length === 1 ? "" : "s") +
      " missing one or more required " + label + " parts." }));

    if (!rows.length) {
      app.appendChild(el("div", { class: "callout", text: "No pieces are missing required parts in this section." }));
      return;
    }

    app.appendChild(MLG.table(rows, [
      { key: "catalog_number", label: "#", filterText: function (r) { return r.catalog_number; } },
      { key: "title", label: "Piece", render: function (r) { return link("piece.html", { id: r.piece_id }, r.title); }, filterText: function (r) { return r.title; } },
      { key: "severity", label: "Severity", render: function (r) { return MLG.severityBadge(r.severity); }, sortValue: function (r) { return r.severity; }, filterText: function (r) { return r.severity; } },
      { key: "completeness_score", label: "Complete", num: true, render: function (r) { return fmt.pctValue(r.completeness_score); } },
      { key: "missing_in_section", label: "Missing here", num: true },
      { key: "missing_parts", label: "Missing parts", filterText: function (r) { return r.missing_parts; } },
    ], { sortKey: "missing_in_section", dir: "desc", filterPlaceholder: "Filter pieces\u2026" }));
  }

  var VIEWS = {
    pieces: renderPiecesIndex,
    piece: renderPieceDetail,
    documents: renderDocumentsIndex,
    document: renderDocumentDetail,
    page: renderPageDetail,
    section: renderSectionDetail,
  };

  MLG.renderPage = function () {
    var app = document.getElementById("app");
    var view = document.body.getAttribute("data-view");
    var fn = VIEWS[view];
    if (fn) fn(app);
    else app.appendChild(el("div", { class: "callout", text: "Unknown view: " + view }));
  };
})();
