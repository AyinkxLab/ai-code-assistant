import csv
import io

import pytest

from app.services import invoice_export as ex


def make_invoice(n=2, desc="Widget"):
    return {
        "number": "INV-1",
        "currency": "USD",
        "items": [{"description": desc, "quantity": 2, "unit_price": "10.50"} for _ in range(n)],
    }


def test_csv_content():
    rows = list(csv.reader(io.StringIO(ex.export_invoice_csv(make_invoice()).decode())))
    assert rows[0][0] == "invoice_number"
    assert len(rows) == 4  # header + 2 items + total
    assert rows[1][5] == "21.00"
    assert rows[-1][2] == "TOTAL"
    assert rows[-1][5] == "42.00"


def test_csv_blocks_formula_injection():
    data = ex.export_invoice_csv(make_invoice(1, desc="=cmd()")).decode()
    assert "'=cmd()" in data


def test_pdf_is_valid_pdf():
    data = ex.export_invoice_pdf(make_invoice())
    assert data.startswith(b"%PDF")
    assert len(data) > 200


def test_item_limit(monkeypatch):
    monkeypatch.setattr(ex, "MAX_LINE_ITEMS", 3)
    ex.export_invoice_csv(make_invoice(3))
    with pytest.raises(ex.ExportTooLargeError):
        ex.export_invoice_csv(make_invoice(4))
    with pytest.raises(ex.ExportTooLargeError):
        ex.export_invoice_pdf(make_invoice(4))


def test_byte_limit(monkeypatch):
    monkeypatch.setattr(ex, "MAX_BYTES", 50)
    with pytest.raises(ex.ExportTooLargeError):
        ex.export_invoice_csv(make_invoice(5))
