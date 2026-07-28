/*
 * core.js - shared foundation for the Music Library static site.
 *
 * Runs from file:// with plain <script> tags (no ES modules, no fetch). Every data file in
 * web/data/*.js calls MLG.register(key, value) to populate MLG.data before this boots the page.
 *
 * Responsibilities:
 *   - the window.MLG namespace + data registry
 *   - settings persisted in localStorage (theme, library base directory)
 *   - the shared header / footer chrome, nav highlighting, breadcrumbs
 *   - formatters and label/badge helpers shared by every renderer
 *   - the global cross-entity search
 *   - client-side composition of absolute file:// links to the original PDFs
 */
(function () {
  "use strict";

  var MLG = (window.MLG = window.MLG || {});
  MLG.data = MLG.data || {};

  // --- data registry -------------------------------------------------------
  MLG.register = function (key, value) {
    MLG.data[key] = value;
    return value;
  };
  MLG.get = function (key, fallback) {
    return Object.prototype.hasOwnProperty.call(MLG.data, key) ? MLG.data[key] : fallback;
  };

  // --- the 8 pipeline phases (static metadata; metrics come from MLG.data.phases) ---
  MLG.PHASES = [
    { n: 1, slug: "phase-01-inventory", title: "Inventory", blurb: "Scan the library and group every PDF into pieces." },
    { n: 2, slug: "phase-02-extract", title: "Extract & Read", blurb: "Render pages, OCR the scans, and vision-analyse each document." },
    { n: 3, slug: "phase-03-classify", title: "Part Classification", blurb: "Predict the instrument / part each document represents." },
    { n: 4, slug: "phase-04-expected", title: "Expected Instrumentation", blurb: "Infer the expected part set for each piece." },
    { n: 5, slug: "phase-05-quality", title: "Quality Checks", blurb: "Score scan quality and flag legibility issues." },
    { n: 6, slug: "phase-06-pieces", title: "Piece Reports", blurb: "Score completeness and severity for every piece." },
    { n: 7, slug: "phase-07-collection", title: "Collection Report", blurb: "Roll up collection-wide statistics." },
    { n: 8, slug: "phase-08-review", title: "Manual Review Pack", blurb: "Prioritize the pieces that most need human review." },
  ];
  MLG.phaseBySlug = function (slug) {
    return MLG.PHASES.filter(function (p) { return p.slug === slug; })[0] || null;
  };
  MLG.phaseByNumber = function (n) {
    return MLG.PHASES.filter(function (p) { return p.n === n; })[0] || null;
  };

  // --- settings (localStorage) ---------------------------------------------
  var LS_THEME = "mlg.theme";
  var LS_LIBROOT = "mlg.libraryRoot";

  MLG.settings = {
    getTheme: function () {
      try { return localStorage.getItem(LS_THEME); } catch (e) { return null; }
    },
    setTheme: function (t) {
      try { localStorage.setItem(LS_THEME, t); } catch (e) { /* ignore */ }
      document.documentElement.setAttribute("data-theme", t);
    },
    getLibraryRoot: function () {
      var stored;
      try { stored = localStorage.getItem(LS_LIBROOT); } catch (e) { stored = null; }
      if (stored) return stored;
      var m = MLG.get("manifest", {});
      return (m && m.library_root) || "";
    },
    setLibraryRoot: function (v) {
      try { localStorage.setItem(LS_LIBROOT, v || ""); } catch (e) { /* ignore */ }
    },
  };

  // Apply the stored theme as early as possible (this script is in <head> defer).
  (function applyTheme() {
    var t = MLG.settings.getTheme();
    if (t === "light" || t === "dark") {
      document.documentElement.setAttribute("data-theme", t);
    }
  })();

  // --- small DOM helpers ---------------------------------------------------
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        if (k === "class") node.className = attrs[k];
        else if (k === "html") node.innerHTML = attrs[k];
        else if (k === "text") node.textContent = attrs[k];
        else if (k in node && k !== "list") node[k] = attrs[k];
        else node.setAttribute(k, attrs[k]);
      });
    }
    (children || []).forEach(function (c) {
      if (c == null) return;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return node;
  }
  MLG.el = el;
  MLG.$ = function (sel, root) { return (root || document).querySelector(sel); };
  MLG.$$ = function (sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  };

  // --- formatters ----------------------------------------------------------
  var fmt = (MLG.fmt = {});
  fmt.num = function (n) {
    if (n == null || isNaN(n)) return "\u2014";
    return Number(n).toLocaleString();
  };
  fmt.pct = function (part, whole, digits) {
    if (!whole) return "\u2014";
    var v = (100 * part) / whole;
    return v.toFixed(digits == null ? 0 : digits) + "%";
  };
  fmt.pctValue = function (v, digits) {
    if (v == null || isNaN(v)) return "\u2014";
    return Number(v).toFixed(digits == null ? 0 : digits) + "%";
  };
  fmt.date = function (iso) {
    if (!iso) return "\u2014";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    return d.toLocaleString();
  };
  fmt.text = function (v) {
    return v == null || v === "" ? "\u2014" : String(v);
  };
  fmt.title = function (s) {
    return String(s || "").replace(/[_-]+/g, " ").replace(/\b\w/g, function (c) {
      return c.toUpperCase();
    });
  };

  // --- badges (color is always paired with a text label) -------------------
  var SEVERITY_CLASS = { high: "high", review: "review", ok: "ok" };
  var QUALITY_CLASS = { good: "good", fair: "fair", poor: "poor", unknown: "unknown" };

  MLG.badge = function (text, variant) {
    return el("span", { class: "badge badge--" + (variant || "muted"), text: fmt.text(text) });
  };
  MLG.severityBadge = function (sev) {
    var key = String(sev || "").toLowerCase();
    return MLG.badge(fmt.title(sev), SEVERITY_CLASS[key] || "muted");
  };
  MLG.qualityBadge = function (q) {
    var key = String(q || "").toLowerCase();
    return MLG.badge(fmt.title(q), QUALITY_CLASS[key] || "unknown");
  };

  // --- original-PDF links (composed client-side from the base dir + relative path) ---
  MLG.pdfUrl = function (relPath) {
    if (!relPath) return null;
    var base = MLG.settings.getLibraryRoot();
    if (!base) return null;
    var joined = String(base).replace(/[\\/]+$/, "") + "/" + String(relPath).replace(/\\/g, "/");
    var normalized = joined.replace(/\\/g, "/");
    if (!/^file:/i.test(normalized)) {
      // Windows drive path -> file:///C:/...  ;  POSIX -> file:///...
      normalized = "file:///" + normalized.replace(/^\/+/, "");
    }
    return encodeURI(normalized);
  };

  // Render a "open original PDF" control: link (if base set) + always-copyable path.
  MLG.pdfControl = function (relPath) {
    var wrap = el("div", { class: "stack" });
    if (!relPath) {
      wrap.appendChild(el("p", { class: "muted", text: "No source PDF recorded." }));
      return wrap;
    }
    var url = MLG.pdfUrl(relPath);
    if (url) {
      wrap.appendChild(
        el("a", { class: "btn btn--accent", href: url, target: "_blank", rel: "noopener", text: "Open original PDF" })
      );
      wrap.appendChild(el("p", { class: "pdf-path", text: decodeURI(url.replace(/^file:\/\/\//, "")) }));
    } else {
      wrap.appendChild(
        el("p", { class: "muted", text: "Set the library location (\u2699) to enable the PDF link." })
      );
      wrap.appendChild(el("p", { class: "pdf-path", text: String(relPath).replace(/\\/g, "/") }));
    }
    return wrap;
  };

  // --- query string helpers ------------------------------------------------
  MLG.param = function (name) {
    var m = new RegExp("[?&]" + name + "=([^&]*)").exec(window.location.search);
    return m ? decodeURIComponent(m[1].replace(/\+/g, " ")) : null;
  };
  MLG.href = function (page, params) {
    var qs = Object.keys(params || {})
      .filter(function (k) { return params[k] != null; })
      .map(function (k) { return k + "=" + encodeURIComponent(params[k]); })
      .join("&");
    return page + (qs ? "?" + qs : "");
  };

  // --- breadcrumbs ---------------------------------------------------------
  MLG.breadcrumbs = function (trail) {
    var host = document.getElementById("breadcrumbs");
    if (!host) return;
    host.innerHTML = "";
    trail.forEach(function (item, i) {
      if (i > 0) host.appendChild(el("span", { class: "sep", text: "/" }));
      if (item.href) host.appendChild(el("a", { href: item.href, text: item.label }));
      else host.appendChild(el("span", { text: item.label }));
    });
  };

  // --- header / footer chrome (injected so all pages stay DRY) -------------
  function brandMark() {
    return (
      '<svg class="brand__mark" viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
      '<path d="M9 18V5l10-2v13" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>' +
      '<circle cx="6" cy="18" r="3" stroke="currentColor" stroke-width="2"/>' +
      '<circle cx="16" cy="16" r="3" stroke="currentColor" stroke-width="2"/></svg>'
    );
  }

  function buildHeader(activePage) {
    var header = el("div", { class: "site-header__inner" });

    header.appendChild(
      el("a", { class: "brand", href: MLG.rel("index.html"), html: brandMark() + "<span>Music Library</span>" })
    );

    var nav = el("nav", { class: "nav", "aria-label": "Primary" });
    function link(page, label, key) {
      var a = el("a", { href: MLG.rel(page), html: '<span class="nav-label">' + label + "</span>" });
      if (activePage === key) a.setAttribute("aria-current", "page");
      return a;
    }
    nav.appendChild(link("index.html", "Dashboard", "dashboard"));

    // Pipeline dropdown
    var menu = el("div", { class: "menu" });
    var toggle = el("button", { class: "btn", type: "button", "aria-haspopup": "true", "aria-expanded": "false", text: "Pipeline \u25be" });
    var panel = el("div", { class: "menu__panel" });
    MLG.PHASES.forEach(function (p) {
      panel.appendChild(
        el("a", { href: MLG.rel("pages/" + p.slug + ".html"), text: p.n + ". " + p.title })
      );
    });
    toggle.addEventListener("click", function (e) {
      e.stopPropagation();
      var open = panel.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
    document.addEventListener("click", function () {
      panel.classList.remove("open");
      toggle.setAttribute("aria-expanded", "false");
    });
    menu.appendChild(toggle);
    menu.appendChild(panel);
    nav.appendChild(menu);

    nav.appendChild(link("pages/pieces.html", "Pieces", "pieces"));
    nav.appendChild(link("pages/documents.html", "Documents", "documents"));
    header.appendChild(nav);

    // Tools: search + theme + settings
    var tools = el("div", { class: "header-tools" });
    tools.appendChild(buildSearch());
    tools.appendChild(buildThemeToggle());
    tools.appendChild(buildSettingsButton());
    header.appendChild(tools);

    return header;
  }

  function buildThemeToggle() {
    var btn = el("button", {
      class: "btn btn--icon",
      type: "button",
      title: "Toggle light / dark",
      "aria-label": "Toggle light or dark theme",
      text: "\u25D0",
    });
    btn.addEventListener("click", function () {
      var current = document.documentElement.getAttribute("data-theme");
      if (!current) {
        current = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
          ? "dark" : "light";
      }
      MLG.settings.setTheme(current === "dark" ? "light" : "dark");
    });
    return btn;
  }

  function buildSettingsButton() {
    var btn = el("button", {
      class: "btn btn--icon",
      type: "button",
      title: "Settings",
      "aria-label": "Settings",
      text: "\u2699",
    });
    btn.addEventListener("click", openSettings);
    return btn;
  }

  function openSettings() {
    var dlg = document.getElementById("mlg-settings");
    if (!dlg) {
      dlg = el("dialog", { id: "mlg-settings", class: "settings" });
      dlg.innerHTML =
        "<h2>Settings</h2>" +
        '<div class="settings__field">' +
        '<label for="mlg-libroot">Library location (base directory)</label>' +
        '<input id="mlg-libroot" class="input" type="text" placeholder="e.g. C:\\Temp\\NSCB" />' +
        '<p class="settings__hint">Absolute path to the folder that holds the original PDFs. ' +
        "Links to source files are built from this + each document\u2019s relative path, so they keep " +
        "working if this site folder moves.</p></div>" +
        '<div class="row" style="justify-content:flex-end">' +
        '<button class="btn" id="mlg-settings-cancel" type="button">Close</button>' +
        '<button class="btn btn--accent" id="mlg-settings-save" type="button">Save</button></div>';
      document.body.appendChild(dlg);
      dlg.querySelector("#mlg-settings-cancel").addEventListener("click", function () { dlg.close(); });
      dlg.querySelector("#mlg-settings-save").addEventListener("click", function () {
        MLG.settings.setLibraryRoot(dlg.querySelector("#mlg-libroot").value.trim());
        dlg.close();
        window.location.reload();
      });
    }
    dlg.querySelector("#mlg-libroot").value = MLG.settings.getLibraryRoot();
    if (typeof dlg.showModal === "function") dlg.showModal();
    else dlg.setAttribute("open", "");
  }

  // --- global search -------------------------------------------------------
  function buildSearch() {
    var wrap = el("div", { class: "search" });
    var input = el("input", {
      class: "input search__input",
      type: "search",
      placeholder: "Search\u2026",
      "aria-label": "Search pieces, documents and pages",
      autocomplete: "off",
    });
    var results = el("div", { class: "search__results", role: "listbox" });
    wrap.appendChild(input);
    wrap.appendChild(results);

    function close() { results.classList.remove("open"); }
    input.addEventListener("input", function () {
      var q = input.value.trim().toLowerCase();
      results.innerHTML = "";
      if (q.length < 2) { close(); return; }
      var hits = MLG.search(q, 8);
      if (!hits.length) {
        results.appendChild(el("div", { class: "search__result", text: "No matches." }));
      } else {
        var lastGroup = null;
        hits.forEach(function (h) {
          if (h.group !== lastGroup) {
            results.appendChild(el("div", { class: "search__group", text: h.group }));
            lastGroup = h.group;
          }
          results.appendChild(
            el("a", { class: "search__result", href: MLG.rel(h.href), html: h.label + " <small>" + h.sub + "</small>" })
          );
        });
      }
      results.classList.add("open");
    });
    input.addEventListener("blur", function () { setTimeout(close, 150); });
    return wrap;
  }

  MLG.search = function (q, limit) {
    var out = [];
    var pieces = MLG.get("pieces", []);
    var docs = MLG.get("documents", []);
    var pages = MLG.get("pages", []);

    pieces.forEach(function (p) {
      var hay = ((p.title || "") + " " + (p.catalog_number || "") + " " + (p.piece_id || "")).toLowerCase();
      if (hay.indexOf(q) >= 0) {
        out.push({
          group: "Pieces",
          label: MLG.fmt.text(p.title || p.piece_id),
          sub: p.catalog_number ? "#" + p.catalog_number : "",
          href: MLG.href("pages/piece.html", { id: p.piece_id }),
        });
      }
    });
    docs.forEach(function (d) {
      var hay = ((d.pdf_filename || "") + " " + (d.instrument || "") + " " + (d.doc_id || "")).toLowerCase();
      if (hay.indexOf(q) >= 0) {
        out.push({
          group: "Documents",
          label: MLG.fmt.text(d.pdf_filename || d.doc_id),
          sub: MLG.fmt.text(d.instrument || ""),
          href: MLG.href("pages/document.html", { id: d.doc_id }),
        });
      }
    });
    pages.forEach(function (pg) {
      var hay = ((pg.page_id || "") + " " + (pg.first_text || "")).toLowerCase();
      if (hay.indexOf(q) >= 0) {
        out.push({
          group: "Pages",
          label: "Page " + MLG.fmt.text(pg.page_num),
          sub: MLG.fmt.text(pg.doc_filename || pg.doc_id),
          href: MLG.href("pages/page.html", { id: pg.page_id }),
        });
      }
    });
    return out.slice(0, limit || 12);
  };

  // --- relative-path resolver (pages/ live one level below the site root) ---
  MLG.rel = function (pathFromRoot) {
    var depth = document.body.getAttribute("data-depth") || "0";
    var prefix = depth === "1" ? "../" : "";
    return prefix + pathFromRoot;
  };

  // --- boot ----------------------------------------------------------------
  MLG.bootChrome = function () {
    var activePage = document.body.getAttribute("data-page");
    var headerHost = document.getElementById("site-header");
    if (headerHost) {
      headerHost.className = "site-header";
      headerHost.appendChild(buildHeader(activePage));
    }
    var footerHost = document.getElementById("site-footer");
    if (footerHost) {
      var m = MLG.get("manifest", {});
      footerHost.className = "site-footer";
      footerHost.innerHTML =
        "<span>Generated by Script 09 \u00b7 site schema <code>" +
        fmt.text(m.site_schema_version) + "</code></span>" +
        "<span>run <code>" + fmt.text(m.run_id) + "</code></span>" +
        "<span>" + fmt.date(m.generated_at) + "</span>";
    }
  };

  MLG.warnIfNoData = function (host) {
    if (MLG.get("manifest") == null) {
      host.appendChild(
        el("div", { class: "callout", html:
          "No generated data found. Run <code>09_static_site.py</code> to populate " +
          "<code>web/data/</code>, or open with the bundled sample fixture." })
      );
      return true;
    }
    return false;
  };

  document.addEventListener("DOMContentLoaded", function () {
    MLG.bootChrome();
    if (typeof MLG.renderPage === "function") {
      try { MLG.renderPage(); } catch (e) {
        var app = document.getElementById("app");
        if (app) app.appendChild(el("div", { class: "callout", text: "Render error: " + e.message }));
        if (window.console) console.error(e);
      }
    }
  });
})();
