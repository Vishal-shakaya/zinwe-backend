"""
FR-22: a report as a one-to-few-page PDF (fpdf2): the official logo, the
period at a glance, a day-by-day bar chart of time worked, the per-person table (team) or each day (one person),
and the top apps, websites and data use. Attached to report emails and downloadable from the Reports page.

FIX-012: names and company names print in any language the fonts cover, not "?". The text is set in Noto Sans
(Latin, Greek, Cyrillic) with Noto Sans Devanagari as the fallback (Hindi, Marathi, Nepali), both bundled in
z_devices/fonts/ under the SIL Open Font License 1.1 (OFL-*.txt there). Only the glyphs used are embedded, so a
report stays tens of KB. With uharfbuzz installed, Devanagari is shaped properly (conjuncts, vowel signs).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from django.utils import timezone

from z_main.branding import draw_pdf_logo

from .reports import clock, period_label, report_title, size

INK = (20, 20, 20)
MUTED = (110, 110, 107)
LINE = (230, 230, 225)
ORANGE = (253, 109, 2)
TINT = (255, 241, 230)
PAPER = (247, 247, 244)


FONTS = Path(__file__).resolve().parent / "fonts"
FONT = "NotoSans"
FALLBACK = "NotoSansDevanagari"


def _text(text: Any) -> str:
    """Text for the PDF as is: the bundled Unicode fonts cover it (FIX-012; was squeezed into Latin-1)."""
    return str(text if text is not None else "")


def _use_fonts(pdf: Any) -> None:
    """Register the bundled Noto fonts; Devanagari falls back to its own font, shaped when uharfbuzz is there."""
    for family, file in ((FONT, "NotoSans"), (FALLBACK, "NotoSansDevanagari")):
        pdf.add_font(family, "", str(FONTS / f"{file}-Regular.ttf"))
        pdf.add_font(family, "B", str(FONTS / f"{file}-Bold.ttf"))
    pdf.set_fallback_fonts([FALLBACK])
    try:
        import uharfbuzz  # noqa: F401

        pdf.set_text_shaping(True)
    except ImportError:  # still prints every letter, just without joined Devanagari conjuncts
        pass


def _report_pdf_class() -> Any:
    from fpdf import FPDF  # imported here: only needed for downloads and emails

    class ReportPDF(FPDF):
        """
        fpdf2 2.8 makes the fallback font the page's font when a cell *starts* in it (a Hindi name right after
        set_font), and the Latin text after it then prints as garbage. Putting the chosen font on the page first
        avoids that.
        """

        def _pin_font(self) -> None:
            if not self.current_font_is_set_on_page and self.current_font is not None:
                self._out(self._set_font_for_page(self.current_font, self.font_size_pt))

        def cell(self, *args: Any, **kwargs: Any) -> Any:
            self._pin_font()
            return super().cell(*args, **kwargs)

        def multi_cell(self, *args: Any, **kwargs: Any) -> Any:
            self._pin_font()
            return super().multi_cell(*args, **kwargs)

    return ReportPDF


def _duration(seconds: int) -> str:
    """Seconds as "7h 45m" (tiles and tables)."""
    minutes = int(seconds or 0) // 60
    h, m = divmod(minutes, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def report_filename(report: dict[str, Any], ext: str = "pdf") -> str:
    member = report.get("member") or {}
    who = member.get("name") or "team"
    # ASCII only (it goes in a Content-Disposition header); a name in another script becomes "person-<id>".
    slug = "-".join("".join(ch if ch.isascii() and ch.isalnum() else " " for ch in who).split()).lower()
    slug = slug or (f"person-{member['id']}" if member.get("id") else "team")
    return f"Zinwe-report-{slug}-{report['from']}-to-{report['to']}.{ext}"


def report_pdf(report: dict[str, Any], company: str) -> bytes:
    pdf = _report_pdf_class()(format="A4")
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.set_margins(14, 14, 14)
    pdf.alias_nb_pages()
    _use_fonts(pdf)
    made = timezone.localtime()

    def footer() -> None:
        pdf.set_y(-12)
        pdf.set_font(FONT, "", 7.5)
        pdf.set_text_color(*MUTED)
        pdf.cell(0, 5, _text(f"Made by Zinwe on {made.day} {made:%b %Y, %I:%M %p}. Times are local (IST). "
                              f"Page {pdf.page_no()} of {{nb}}"), align="C")

    pdf.footer = footer  # type: ignore[method-assign]
    pdf.add_page()
    w = pdf.w - 28

    # Header: logo, title on the right, then company and period
    draw_pdf_logo(pdf, w / 2, height=7)
    pdf.set_text_color(*INK)
    pdf.set_font(FONT, "B", 13)
    pdf.cell(w / 2, 10, _text(report_title(report)).upper(), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(FONT, "", 10)
    pdf.set_text_color(*MUTED)
    days = report["days"]
    pdf.cell(w, 6, _text(f"{company} - {period_label(report)} ({days} {'day' if days == 1 else 'days'})"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(*ORANGE)
    pdf.set_line_width(0.6)
    pdf.line(14, pdf.get_y() + 2, 14 + w, pdf.get_y() + 2)
    pdf.set_line_width(0.2)
    pdf.ln(6)

    _tiles(pdf, w, report)
    _trend(pdf, w, report)
    if report.get("member"):
        _person_days(pdf, w, report)
    else:
        _people(pdf, w, report)
    _tops(pdf, w, report)
    return bytes(pdf.output())


def _heading(pdf: Any, text: str) -> None:
    if pdf.get_y() > pdf.h - 40:
        pdf.add_page()
    pdf.set_font(FONT, "B", 11)
    pdf.set_text_color(*INK)
    pdf.cell(0, 7, _text(text), new_x="LMARGIN", new_y="NEXT")


def _tiles(pdf: Any, w: float, report: dict[str, Any]) -> None:
    t = report["totals"]
    person_days = t.get("person_days") or 0
    avg = t["worked_seconds"] // person_days if person_days else 0
    if report.get("member"):
        tiles = [
            (str(person_days), "days worked"),
            (_duration(t["worked_seconds"]), "worked in total"),
            (_duration(avg), "average a day"),
            (_duration(t["break_seconds"]), "on breaks"),
        ]
        p = report["people"][0] if report["people"] else None
        tiles += [
            (clock(p["first_in"]) if p else "-", "usual start"),
            (clock(p["last_out"]) if p else "-", "usual finish"),
        ]
        if t["active_seconds"]:
            tiles.append((_duration(t["active_seconds"]), "active on the PC"))
        if t["data_bytes"]:
            tiles.append((size(t["data_bytes"]), "internet data"))
        tiles.append((str(t["alerts_all"]), "alerts"))
    else:
        tiles = [
            (str(t["people_worked"]), "people worked"),
            (_duration(t["worked_seconds"]), "worked in total"),
            (_duration(avg), "average a person-day"),
            (_duration(t["break_seconds"]), "on breaks"),
        ]
        if t["active_seconds"]:
            tiles.append((_duration(t["active_seconds"]), "active on the PC"))
        if t["data_bytes"]:
            tiles.append((size(t["data_bytes"]), "internet data"))
        tiles += [(str(t["to_review"]), "waiting for your OK"), (str(t["alerts_all"]), "alerts")]
    gap = 3
    tw = (w - gap * 3) / 4
    top = pdf.get_y()
    for i, (value, label) in enumerate(tiles):
        x = 14 + (i % 4) * (tw + gap)
        y = top + (i // 4) * 19
        pdf.set_fill_color(*PAPER)
        pdf.set_draw_color(*LINE)
        pdf.rect(x, y, tw, 16, style="DF")
        pdf.set_xy(x + 3, y + 2)
        pdf.set_font(FONT, "B", 12)
        pdf.set_text_color(*INK)
        pdf.cell(tw - 6, 7, _text(value))
        pdf.set_xy(x + 3, y + 9)
        pdf.set_font(FONT, "", 8)
        pdf.set_text_color(*MUTED)
        pdf.cell(tw - 6, 5, _text(label))
    pdf.set_xy(14, top + ((len(tiles) + 3) // 4) * 19 + 3)


def _trend(pdf: Any, w: float, report: dict[str, Any]) -> None:
    """Hours worked each day as bars (only for 2 to 62 days: one day has no trend, a year is a smudge)."""
    daily = report.get("daily") or []
    if not (2 <= len(daily) <= 62) or not any(d["worked_seconds"] or d["active_seconds"] for d in daily):
        return
    _heading(pdf, "Day by day: hours worked")
    field = "worked_seconds" if any(d["worked_seconds"] for d in daily) else "active_seconds"
    top = max(d[field] for d in daily) or 1
    height, base_y = 32, pdf.get_y() + 34
    slot = w / len(daily)
    bar = max(1.2, slot * 0.62)
    pdf.set_draw_color(*LINE)
    pdf.line(14, base_y, 14 + w, base_y)
    pdf.set_font(FONT, "", 6.5)
    pdf.set_text_color(*MUTED)
    pdf.set_xy(14, base_y - height - 3)
    pdf.cell(30, 3, _text(f"max {_duration(top)}"))
    every = max(1, round(len(daily) / 10))
    for i, d in enumerate(daily):
        x = 14 + i * slot + (slot - bar) / 2
        h = height * d[field] / top
        if h > 0:
            pdf.set_fill_color(*ORANGE)
            pdf.rect(x, base_y - h, bar, h, style="F")
        if i % every == 0:
            day = date.fromisoformat(d["date"])
            pdf.set_xy(14 + i * slot - 4, base_y + 0.8)
            pdf.cell(slot + 8, 3, _text(f"{day.day} {day:%b}" if len(daily) > 7 else f"{day:%a}"), align="C")
    pdf.set_xy(14, base_y + 7)


def _drop(columns: list[tuple[str, float, str]], rows: list[list[str]], total: list[str] | None,
          hide: set[str]) -> tuple[list[tuple[str, float, str]], list[list[str]], list[str] | None]:
    """Remove the columns titled in `hide`; the first column gets their width."""
    keep = [i for i, (title, _, _) in enumerate(columns) if title not in hide]
    freed = sum(frac for title, frac, _ in columns if title in hide)
    cols = [columns[i] for i in keep]
    cols[0] = (cols[0][0], cols[0][1] + freed, cols[0][2])
    return cols, [[r[i] for i in keep] for r in rows], [total[i] for i in keep] if total else None


def _table(pdf: Any, w: float, columns: list[tuple[str, float, str]], rows: list[list[str]], total: list[str] | None = None) -> None:
    """A plain table: columns are (title, width fraction, align)."""
    widths = [w * frac for _, frac, _ in columns]

    def head() -> None:
        pdf.set_font(FONT, "B", 8)
        pdf.set_fill_color(*TINT)
        pdf.set_text_color(138, 60, 0)
        for (title, _, align), width in zip(columns, widths):
            pdf.cell(width, 7, _text(title), fill=True, align=align)
        pdf.ln(7)

    head()
    pdf.set_draw_color(*LINE)
    for i, row in enumerate([*rows, *([total] if total else [])]):
        if pdf.get_y() > pdf.h - 24:
            pdf.add_page()
            head()
        is_total = total is not None and i == len(rows)
        pdf.set_font(FONT, "B" if is_total else "", 8.5)
        pdf.set_text_color(*INK)
        for (_, _, align), width, value in zip(columns, widths, row):
            pdf.cell(width, 6.5, _text(value)[:40], align=align)
        pdf.ln(6.5)
        pdf.line(14, pdf.get_y(), 14 + w, pdf.get_y())
    pdf.ln(4)


def _people(pdf: Any, w: float, report: dict[str, Any]) -> None:
    _heading(pdf, "By person")
    if not report["people"]:
        pdf.set_font(FONT, "", 9)
        pdf.set_text_color(*MUTED)
        pdf.cell(0, 6, "Nothing in this period: no one logged in from their Zinwe widget, and there were no alerts.",
                 new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)
        return
    columns = [("Person", 0.22, "L"), ("Days", 0.06, "R"), ("Worked", 0.1, "R"), ("A day", 0.09, "R"),
               ("Breaks", 0.09, "R"), ("Start", 0.1, "R"), ("Finish", 0.1, "R"), ("Active", 0.09, "R"),
               ("Data", 0.09, "R"), ("Alerts", 0.06, "R")]
    rows = []
    for p in report["people"]:
        per_day = p["worked_seconds"] // p["days_worked"] if p["days_worked"] else 0
        rows.append([p["name"], str(p["days_worked"]), _duration(p["worked_seconds"]), _duration(per_day),
                     _duration(p["break_seconds"]), clock(p["first_in"]), clock(p["last_out"]),
                     _duration(p["active_seconds"]), size(p["data_bytes"]), str(p["alerts"])])
    t = report["totals"]
    total = ["Everyone", str(t["person_days"]), _duration(t["worked_seconds"]), "", _duration(t["break_seconds"]),
             "", "", _duration(t["active_seconds"]), size(t["data_bytes"]), str(t["alerts"])]
    hide = {name for name, key in (("Active", "active_seconds"), ("Data", "data_bytes")) if not t[key]}
    _table(pdf, w, *_drop(columns, rows, total, hide))
    if report["no_activity"]:
        pdf.set_font(FONT, "", 8.5)
        pdf.set_text_color(*MUTED)
        pdf.multi_cell(w, 5, _text("No activity in this period: " + ", ".join(report["no_activity"])),
                       new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)


def _person_days(pdf: Any, w: float, report: dict[str, Any]) -> None:
    _heading(pdf, "Each day")
    days = report.get("person_days") or []
    if not days:
        pdf.set_font(FONT, "", 9)
        pdf.set_text_color(*MUTED)
        pdf.cell(0, 6, "Nothing recorded in this period.", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)
        return
    columns = [("Day", 0.22, "L"), ("Started", 0.12, "R"), ("Finished", 0.12, "R"), ("Worked", 0.12, "R"),
               ("Breaks", 0.14, "R"), ("Active", 0.14, "R"), ("Alerts", 0.14, "R")]
    rows = []
    for d in days:
        day = date.fromisoformat(d["date"])
        breaks = f"{_duration(d['break_seconds'])} ({d['breaks']})" if d["breaks"] else "-"
        rows.append([f"{day:%a} {day.day} {day:%b}", clock(d["first_in"]), clock(d["last_out"]),
                     _duration(d["worked_seconds"]), breaks, _duration(d["active_seconds"]), str(d["alerts"])])
    hide = set() if any(d["active_seconds"] for d in days) else {"Active"}
    _table(pdf, w, *_drop(columns, rows, None, hide))


def _tops(pdf: Any, w: float, report: dict[str, Any]) -> None:
    lists = [
        ("Top apps", [(i["key"], _duration(i["seconds"])) for i in report["top_apps"][:8]]),
        ("Top websites", [(i["key"], _duration(i["seconds"])) for i in report["top_sites"][:8]]),
        ("Most internet data", [(i["key"], size(i["bytes"])) for i in report["top_data"][:8]]),
    ]
    lists = [(title, items) for title, items in lists if items]
    if not lists:
        return
    if pdf.get_y() > pdf.h - 70:
        pdf.add_page()
    gap = 4
    cw = (w - gap * (len(lists) - 1)) / len(lists)
    y0 = pdf.get_y()
    bottom = y0
    for n, (title, items) in enumerate(lists):
        x = 14 + n * (cw + gap)
        pdf.set_xy(x, y0)
        pdf.set_font(FONT, "B", 10)
        pdf.set_text_color(*INK)
        pdf.cell(cw, 7, _text(title))
        y = y0 + 8
        for key, value in items:
            pdf.set_xy(x, y)
            pdf.set_font(FONT, "", 8.5)
            pdf.set_text_color(*INK)
            pdf.cell(cw * 0.66, 5.5, _text(key)[:32])
            pdf.set_font(FONT, "B", 8.5)
            pdf.cell(cw * 0.34, 5.5, _text(value), align="R")
            y += 5.5
        bottom = max(bottom, y)
    pdf.set_xy(14, bottom + 4)
