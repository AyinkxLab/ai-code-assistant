# Invoice export

`GET /api/invoices/<id>/export?format=csv|pdf`

- CSV: one row per line item plus a TOTAL row.
- PDF: simple one-page-style table rendering.
- Limits: 500 line items and 2 MB per export (exceeding returns 413).
- Text fields are cut at 255 characters; CSV cells starting with
  `= + - @` are prefixed with `'` to prevent formula injection.