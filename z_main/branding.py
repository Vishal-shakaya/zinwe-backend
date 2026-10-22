"""The official Zinwe logo on generated PDFs (quotes, invoices). Owner rule (2026-10-22): every PDF, newsletter
and email carries the real logo; plain text is only the fallback when the image can't be used."""
from __future__ import annotations

from pathlib import Path
from typing import Any

# Same file as zinwe.io/brand/zinwe-logo.png: transparent PNG, orange Z + charcoal "inwe", 837 x 206.
LOGO_PNG = Path(__file__).resolve().parent / "static" / "zinwe" / "brand" / "zinwe-logo.png"
LOGO_RATIO = 837 / 206


def draw_pdf_logo(pdf: Any, width: float, height: float = 8.0) -> None:
    """Draw the logo at the current position and move right across `width` (an fpdf2 cell's worth).
    Falls back to the orange "Zinwe" wordmark in text if the image can't be placed."""
    x, y = pdf.get_x(), pdf.get_y()
    try:
        pdf.image(str(LOGO_PNG), x=x, y=y + (10 - height) / 2, h=height, w=height * LOGO_RATIO)
        pdf.set_xy(x + width, y)
    except Exception:  # noqa: BLE001 - a missing or unreadable logo must never block a quote or invoice
        pdf.set_xy(x, y)
        pdf.set_font("Helvetica", "B", 20)
        pdf.set_text_color(253, 109, 2)
        pdf.cell(width, 10, "Zinwe", new_x="RIGHT", new_y="TOP")
        pdf.set_text_color(20, 20, 20)
