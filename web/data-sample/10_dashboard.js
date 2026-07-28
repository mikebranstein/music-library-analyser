MLG.register("dashboard", {
  kpis: [
    { label: "Pieces", value: 2, hint: "in the collection", href: "pages/pieces.html" },
    { label: "Documents", value: 3, hint: "PDF parts & scores", href: "pages/documents.html" },
    { label: "Pages", value: 5, hint: "rendered & analysed" },
    { label: "Need review", value: 1, hint: "flagged high severity" },
  ],
  completeness: { score: 78, tier: "partial", label: "Collection completeness" },
  severity: { high: 1, review: 0, ok: 1 },
  quality: { good: 2, review: 1, poor: 0, unknown: 0 },
  attention: [
    {
      piece_id: "p001",
      title: "Mexican Hat Dance",
      catalog_number: "001",
      severity: "high",
      completeness_score: 62,
      reason: "2 required parts missing; 1 low-quality scan",
    },
  ],
  phase_flow: [
    { n: 1, records: 3, note: "3 PDFs \u2192 2 pieces" },
    { n: 2, records: 5, note: "5 pages rendered, OCR + vision" },
    { n: 3, records: 3, note: "3 documents classified" },
    { n: 4, records: 2, note: "expected parts for 2 pieces" },
    { n: 5, records: 3, note: "3 documents quality-checked" },
    { n: 6, records: 2, note: "2 piece reports" },
    { n: 7, records: 1, note: "1 collection report" },
    { n: 8, records: 1, note: "1 review queue (1 flagged)" },
  ],
});
