/* guide.js - "User guide" page: how to navigate this site and read what it shows you. */
(function () {
  "use strict";
  var MLG = (window.MLG = window.MLG || {});
  var el = MLG.el;

  MLG.renderPage = function () {
    var app = document.getElementById("app");
    MLG.breadcrumbs([{ label: "Dashboard", href: MLG.rel("index.html") }, { label: "User guide" }]);
    if (MLG.warnIfNoData(app)) return;

    app.appendChild(MLG.pageHead("Reference", "User guide"));
    app.appendChild(el("p", { class: "muted", text:
      "A short tour of the site \u2014 what each page shows and how to find what needs your attention." }));

    app.appendChild(el("h2", { text: "Getting oriented" }));
    app.appendChild(el("div", { class: "prose" }, [
      el("p", { html:
        "The <a href=\"" + MLG.rel("index.html") + "\">Dashboard</a> is the home page and gives you the whole " +
        "collection at a glance: headline counts, an overall completeness score, a short list of the pieces that need " +
        "attention most, and a strip showing the eight processing stages the data passed through. Everything else on the " +
        "site is one click away from there." }),
      el("p", { html:
        "The header at the top of every page stays the same: <strong>Dashboard</strong> takes you home, the " +
        "<strong>Pipeline</strong> menu lists the eight processing stages, and <strong>Pieces</strong> / " +
        "<strong>Documents</strong> open the full browsable lists. The search box searches pieces, documents, and pages " +
        "at once \u2014 start typing a title, catalog number, or filename and pick a result. The breadcrumb trail just " +
        "below the header always shows where you are and lets you jump back up a level." }),
    ]));

    app.appendChild(el("h2", { text: "Finding what needs attention" }));
    app.appendChild(el("div", { class: "prose" }, [
      el("p", { html:
        "Each piece is given a <strong>severity</strong>: <span class=\"badge badge--high\">High</span> means real " +
        "problems were found (missing required parts, no score, etc.) and it is worth checking soon; " +
        "<span class=\"badge badge--review\">Review</span> means something is uncertain enough to warrant a second look, " +
        "but nothing is confirmed broken; <span class=\"badge badge--ok\">Ok</span> means nothing was flagged. The " +
        "dashboard's \u201CNeeds attention\u201D list shows the top few high-severity pieces \u2014 click " +
        "<strong>View all \u2192</strong> to see the complete, filterable list on the Pieces page." }),
      el("p", { html:
        "Separately, each individual file carries a <strong>quality</strong> rating \u2014 " +
        "<span class=\"badge badge--good\">Good</span>, <span class=\"badge badge--fair\">Fair</span>, or " +
        "<span class=\"badge badge--poor\">Poor</span> \u2014 describing how legible the scan itself is. This is " +
        "informational only: a poor scan does not make a piece \u201Cincomplete,\u201D and a complete piece can still " +
        "contain a poor scan worth re-scanning." }),
    ]));

    app.appendChild(el("h2", { text: "Browsing pieces and documents" }));
    app.appendChild(el("div", { class: "prose" }, [
      el("p", { html:
        "<strong>Pieces</strong> lists every piece of music in the collection, one row each, with its severity, " +
        "completeness percentage, and document/page counts. Click a title to open that piece's own page, which shows its " +
        "detected parts, its expected part list (present vs. missing), scan quality, and any flagged reasons for review." }),
      el("p", { html:
        "<strong>Documents</strong> lists every individual PDF file, one row each, with its predicted instrument part, " +
        "quality band, and notation source (printed vs. handwritten). Click a filename to open that document's own page, " +
        "which includes a page-by-page thumbnail preview and a link to the original PDF (once you set your library " +
        "location \u2014 see below)." }),
      el("p", { html:
        "Every table on the site can be <strong>filtered</strong> (type into the box above a table to narrow the rows " +
        "shown) and <strong>sorted</strong> (click any column heading; click again to reverse the direction)." }),
    ]));

    app.appendChild(el("h2", { text: "Understanding the pipeline pages" }));
    app.appendChild(el("div", { class: "prose" }, [
      el("p", { html:
        "The <strong>Pipeline</strong> menu lists the eight processing stages the collection passed through, from " +
        "initial inventory through to the final prioritized review queue. Each stage's page starts with a plain-language " +
        "explanation of what that stage does and why, followed by its own metrics and a drill-down table. If you want the " +
        "bigger picture of how the whole system works and how much to trust it, see " +
        "<a href=\"" + MLG.rel("pages/about.html") + "\">About this report</a>." }),
    ]));

    app.appendChild(el("h2", { text: "Settings: linking to your original files" }));
    app.appendChild(el("div", { class: "prose" }, [
      el("p", { html:
        "This site only stores small preview thumbnails, not the original PDFs, so links to \u201Copen the real file\u201D " +
        "need to know where your library actually lives on disk. Click the \u2699 <strong>Settings</strong> button in the " +
        "header and enter the absolute folder path that contains your library (for example " +
        "<code>C:\\Music\\Library</code>) once, and every document page will be able to link straight to its original " +
        "PDF. This is saved in your browser only \u2014 it is never uploaded anywhere." }),
      el("p", { html:
        "The same \u25D0 button next to Settings switches between light and dark themes; your choice is remembered for " +
        "next time." }),
    ]));

    app.appendChild(el("h2", { text: "A note on trust" }));
    app.appendChild(el("div", { class: "prose" }, [
      el("p", { html:
        "Everything on this site comes from an automated review, not a human check of every page. It is a well-informed " +
        "starting point, not a guaranteed error-free record \u2014 see " +
        "<a href=\"" + MLG.rel("pages/about.html") + "\">About this report</a> for the full explanation of how it was " +
        "produced and where it can be wrong." }),
    ]));
  };
})();
