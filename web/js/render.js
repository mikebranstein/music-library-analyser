/* render.js - small presentational helpers shared by the page renderers. */
(function () {
  "use strict";
  var MLG = (window.MLG = window.MLG || {});
  var el = MLG.el;

  MLG.thumbEl = function (src, alt, cls) {
    if (src) {
      return el("img", { class: "thumb " + (cls || ""), src: MLG.rel(src), alt: alt || "", loading: "lazy" });
    }
    var ph = el("div", {
      class: "thumb " + (cls || ""),
      style: "display:flex;align-items:center;justify-content:center;color:var(--text-muted);font-size:0.8rem;aspect-ratio:3/4;",
      text: "no preview",
    });
    ph.setAttribute("role", "img");
    ph.setAttribute("aria-label", alt || "no preview");
    return ph;
  };

  MLG.kpiCard = function (k) {
    var children = [
      el("div", { class: "card__label", text: k.label }),
      el("div", { class: "card__value", text: k.display != null ? k.display : MLG.fmt.num(k.value) }),
    ];
    if (k.hint) children.push(el("div", { class: "card__hint", text: k.hint }));
    if (k.href) {
      return el("a", { class: "card card--link", href: MLG.rel(k.href) }, children);
    }
    return el("div", { class: "card" }, children);
  };

  MLG.kpiGrid = function (list) {
    return el("div", { class: "grid grid--kpi" }, (list || []).map(MLG.kpiCard));
  };

  MLG.facts = function (pairs) {
    var dl = el("dl", { class: "facts" });
    pairs.forEach(function (p) {
      if (p == null) return;
      dl.appendChild(el("dt", { text: p[0] }));
      var dd = el("dd");
      var v = p[1];
      if (v == null) dd.textContent = "\u2014";
      else if (typeof v === "string" || typeof v === "number") dd.textContent = String(v);
      else dd.appendChild(v);
      dl.appendChild(dd);
    });
    return dl;
  };

  MLG.pageHead = function (eyebrow, title) {
    var head = el("div", { class: "page-head" });
    if (eyebrow) head.appendChild(el("div", { class: "page-head__eyebrow", text: eyebrow }));
    head.appendChild(el("h1", { text: title }));
    return head;
  };

  MLG.expander = function (summary, text) {
    if (!text) return null;
    var d = el("details", { class: "expander" });
    d.appendChild(el("summary", { text: summary }));
    d.appendChild(el("pre", { text: text }));
    return d;
  };

  // A collapsible section wrapping arbitrary DOM (not just <pre> text). Open by default unless
  // `open` is explicitly false.
  MLG.detailsSection = function (summary, node, open) {
    var d = el("details", { class: "expander expander--section" });
    if (open !== false) d.setAttribute("open", "");
    d.appendChild(el("summary", { text: summary }));
    if (node) d.appendChild(node);
    return d;
  };

  MLG.section = function (title, node) {
    var frag = document.createDocumentFragment();
    frag.appendChild(el("h2", { text: title }));
    if (node) frag.appendChild(node);
    return frag;
  };
})();
