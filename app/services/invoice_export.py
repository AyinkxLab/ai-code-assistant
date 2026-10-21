"""Invoice export helpers (CSV and PDF) with bounded output."""
import csv
import io
from decimal import Decimal

MAX_LINE_ITEMS = 500
MAX_BYTES = 2_000_000
MAX_TEXT_LEN = 255
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class ExportTooLargeError(ValueError):
    """Raised when an export exceeds the configured bounds."""


    def _safe(value):
        text = "" if value is None else str(value)[:MAX_TEXT_LEN]
            if text.startswith(_FORMULA_PREFIXES):
                    text = "'" + text
                        return text


                        def _money(value):
                            return f"{Decimal(str(value)):.2f}"


                            def _rows(invoice):
                                items = invoice.get("items") or []
                                    if len(items) > MAX_LINE_ITEMS:
                                            raise ExportTooLargeError(
                                                        f"Invoice has {len(items)} items; limit is {MAX_LINE_ITEMS}"
                                                                )
                                                                    rows = []
                                                                        for item in items:
                                                                                qty = Decimal(str(item.get("quantity", 0)))
                                                                                        price = Decimal(str(item.get("unit_price", 0)))
                                                                                                rows.append((_safe(item.get("description")), qty, price, qty * price))
                                                                                                    return rows


                                                                                                    def _check_size(data):
                                                                                                        if len(data) > MAX_BYTES:
                                                                                                                raise ExportTooLargeError(f"Export exceeds {MAX_BYTES} bytes")
                                                                                                                    return data


                                                                                                                    def export_invoice_csv(invoice):
                                                                                                                        number = _safe(invoice.get("number"))
                                                                                                                            currency = _safe(invoice.get("currency"))
                                                                                                                                buf = io.StringIO()
                                                                                                                                    writer = csv.writer(buf)
                                                                                                                                        writer.writerow(
                                                                                                                                                ["invoice_number", "currency", "description",
                                                                                                                                                         "quantity", "unit_price", "line_total"]
                                                                                                                                                             )
                                                                                                                                                                 total = Decimal("0")
                                                                                                                                                                     for desc, qty, price, line in _rows(invoice):
                                                                                                                                                                             total += line
                                                                                                                                                                                     writer.writerow([number, currency, desc, qty, _money(price), _money(line)])
                                                                                                                                                                                         writer.writerow([number, currency, "TOTAL", "", "", _money(total)])
                                                                                                                                                                                             return _check_size(buf.getvalue().encode("utf-8"))


                                                                                                                                                                                             def _latin(text):
                                                                                                                                                                                                 return text.encode("latin-1", "replace").decode("latin-1")


                                                                                                                                                                                                 def export_invoice_pdf(invoice):
                                                                                                                                                                                                     from fpdf import FPDF

                                                                                                                                                                                                         rows = _rows(invoice)
                                                                                                                                                                                                             pdf = FPDF()
                                                                                                                                                                                                                 pdf.add_page()
                                                                                                                                                                                                                     pdf.set_font("Helvetica", "B", 16)
                                                                                                                                                                                                                         pdf.cell(0, 10, _latin(f"Invoice {_safe(invoice.get('number'))}"),
                                                                                                                                                                                                                                      new_x="LMARGIN", new_y="NEXT")
                                                                                                                                                                                                                                          pdf.set_font("Helvetica", size=10)
                                                                                                                                                                                                                                              pdf.cell(0, 8, _latin(f"Currency: {_safe(invoice.get('currency'))}"),
                                                                                                                                                                                                                                                           new_x="LMARGIN", new_y="NEXT")
                                                                                                                                                                                                                                                               pdf.ln(4)
                                                                                                                                                                                                                                                                   for head, width in (("Description", 90), ("Qty", 20),
                                                                                                                                                                                                                                                                                           ("Unit price", 35), ("Total", 35)):
                                                                                                                                                                                                                                                                                                   pdf.cell(width, 8, head, border=1)
                                                                                                                                                                                                                                                                                                       pdf.ln()
                                                                                                                                                                                                                                                                                                           total = Decimal("0")
                                                                                                                                                                                                                                                                                                               for desc, qty, price, line in rows:
                                                                                                                                                                                                                                                                                                                       total += line
                                                                                                                                                                                                                                                                                                                               pdf.cell(90, 8, _latin(desc[:45]), border=1)
                                                                                                                                                                                                                                                                                                                                       pdf.cell(20, 8, str(qty), border=1)
                                                                                                                                                                                                                                                                                                                                               pdf.cell(35, 8, _money(price), border=1)
                                                                                                                                                                                                                                                                                                                                                       pdf.cell(35, 8, _money(line), border=1)
                                                                                                                                                                                                                                                                                                                                                               pdf.ln()
                                                                                                                                                                                                                                                                                                                                                                   pdf.set_font("Helvetica", "B", 11)
                                                                                                                                                                                                                                                                                                                                                                       pdf.cell(145, 8, "TOTAL", border=1)
                                                                                                                                                                                                                                                                                                                                                                           pdf.cell(35, 8, _money(total), border=1)
                                                                                                                                                                                                                                                                                                                                                                               return _check_size(bytes(pdf.output()))