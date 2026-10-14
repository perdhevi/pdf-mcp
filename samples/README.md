# Sample documents and expected answers

Copy these into the inbox (`xcopy /E /I samples\inbox data\inbox`, or `cp -r samples/inbox/* data/inbox/`) and wait for the
watcher, or run `docker compose exec pdf-mcp pdf-mcp reindex`. Every question below lists the answer a correct setup returns.

```
samples/inbox/
  ACME/    ACME_test.pdf + 6 invoices + 1 duplicate copy
  GLOBEX/  GLOBEX_test.pdf + 1 invoice
```

## 1. Extraction paths: `ACME_test.pdf` (3 pages)

| Page | Content | Exercises | Ask (ACME) | Expected |
|---|---|---|---|---|
| 1 | Service agreement, text layer | PyMuPDF text | "What is the notice period for termination?" | 90 days |
| 2 | Invoice INV-4471, **image only** | Tesseract OCR | "How much is invoice INV-4471?" | USD 12,600 |
| 3 | **Vector** bar chart (19 drawing ops) | Vision model on vector pages | "Which quarter had the most support tickets?" | Q2, about 30 (Q1 12, Q3 18, Q4 7) |

- Page 2 not found → OCR is not running (check Tesseract).
- Page 3 answered with axis numbers only (for example "Q1 with 35") → the page never reached the vision model.
  Check that `VISION_VECTOR_MIN_DRAWINGS` ≤ 19, then rerun `reindex --customer ACME --force`.

## 2. Invoices: telling near-duplicates apart and finding items

All ACME invoices use one template and share two line items (SVC-001 support fee, LIC-210 M365 licences), so
retrieval has to rely on the details that differ.

| Invoice | File | Date | PO | Distinct items | Total |
|---|---|---|---|---|---|
| INV-2026-0101 | ACME_INV-2026-0101.pdf | 10 Jan 2026 | PO-ACME-5501 | Dell PowerEdge **R750** ×2 | USD 17,107.88 |
| INV-2026-0102 | ACME_INV-2026-0102.pdf | 10 Feb 2026 | PO-ACME-5502 | Dell PowerEdge **R760** ×1, Cisco Catalyst 9200 switch ×3 | USD 19,050.38 |
| INV-2026-0103 | ACME_INV-2026-0103.pdf | 10 Mar 2026 | PO-ACME-5503 | APC Smart-UPS 3000VA ×2, Cat6 patch cables ×4 | USD 5,652.67 |
| INV-2026-0104 | ACME_INV-2026-0104.pdf (2 pages) | 10 Apr 2026 | PO-ACME-5504 | 14 fibre patch cords (p1); **FortiGate 60F** + Catalyst 9200 ×1 on **page 2** | USD 9,792.98 |
| INV-2026-0105 | ACME_INV-2026-0105.pdf | 10 May 2026 | PO-ACME-5505 | Base items only; a *note* mentions an R750 warranty (not billed) | USD 2,011.88 |
| INV-2026-0106 | ACME_INV-2026-0106_scan.pdf (**scanned**) | 10 Jun 2026 | PO-ACME-5506 | Synology DS923+ NAS ×2, APC Smart-UPS ×1 | USD 4,739.70 |
| (duplicate) | ACME_copy_of_0102.pdf | | | Byte-identical copy of 0102 | |
| INV-2026-0201 | GLOBEX/GLOBEX_INV-2026-0201.pdf | 15 Mar 2026 | PO-GLX-900 | Synology DS923+ NAS ×3, FortiGate 60F ×2 | |

**Item → which invoice** (customer ACME)

| # | Ask | Expected | What it checks |
|---|---|---|---|
| 1 | Which invoices contain the Cisco Catalyst 9200 switch? | 0102 (×3) **and** 0104 (×1, page 2) | Multi-document recall |
| 2 | Which invoice billed a Dell PowerEdge R760? | 0102 only | Similar model names (R750 vs R760) |
| 3 | Which invoice billed R750 servers? | 0101 only, **not** 0105 | Mentioned-but-not-billed trap |
| 4 | Which invoice has the FortiGate firewall, and on which page? | 0104, page 2 | Page citation and `#page=2` link |
| 5 | Which invoices include an APC Smart-UPS? | 0103 (×2) and 0106 (×1) | Scanned invoice is searchable |
| 6 | Did we sell ACME a Synology NAS? | Yes, 0106 | OCR-only content |

**Uniqueness**

| # | Ask / action | Expected |
|---|---|---|
| 7 | What is the total of invoice INV-2026-0103? | USD 5,652.67, with no figures from other invoices |
| 8 | Which invoice references PO-ACME-5504? | 0104 |
| 9 | When is INV-2026-0102 due? | 12 March 2026 |
| 10 | `list_documents ACME` | 7 documents (6 invoices + ACME_test). The copy of 0102 is skipped as a duplicate; the log shows "is a duplicate of … skipped" |

**Isolation**

| # | Ask | Expected |
|---|---|---|
| 11 | GLOBEX: Which invoices contain a FortiGate? | INV-2026-0201 only, never ACME 0104 |
| 12 | GLOBEX: search "zebra invoice 4471" | Nothing relevant (that text is ACME's) |
| 13 | ACME: orders with PO-GLX-900 | Nothing |

For "which invoices contain X" questions, `search`/`find_documents` return the top chunks. If one invoice
crowds out the others, raise `limit` before blaming the model.
