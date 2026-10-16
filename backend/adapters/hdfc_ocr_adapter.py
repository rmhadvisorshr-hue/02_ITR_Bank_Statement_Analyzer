"""Parser for HDFC statements that had to go through OCR.

Some HDFC exports have no extractable text layer at all - typically
because the PDF's embedded font has no ToUnicode map, so the text
*renders* correctly on screen but pdfplumber/pdfminer can't recover the
underlying characters. Rendering the page to an image and OCR'ing it
works fine for these (the glyphs draw normally), but Tesseract's output
carries the noise any OCR pass does: the header/branch/address block
that HDFC repeats on every single page gets re-read (with typos) on
every page too, and a blank Withdrawal or Deposit cell routinely OCRs
as junk ("000", "sid", "oo") instead of empty space.

Reusing the shared generic engine handles the row parsing itself just
fine once that per-page header noise is stripped out first - blank-cell
junk like "000"/"sid" doesn't match the strict `\\d+\\.\\d{1,2}` amount
shape, so it's already invisible to the generic engine's amount scan.
What isn't already handled is the repeated header block: left alone, it
would get glued onto whichever transaction happens to sit at a page
break as OCR "continuation" text, and any of its own decimal-looking
figures (Expected AMB, OD Limit, ...) would then contaminate that row's
amount detection - exactly the failure mode `icici_parser.py` was built
to avoid for ICICI's repeated per-page name/header line.
"""

import re
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.resolve()))
from parser.generic_parser import BOILER_RE, DATE_START_RE, build_transactions  # noqa: E402

# Fixed template text HDFC prints identically on every statement - safe to
# match literally regardless of whose account it is. Customer-specific text
# (name, address, business name) is NEVER safe here - see _in_page_zone
# below, which is what actually strips that.
_HDFC_OCR_NOISE_RE = re.compile(
    r"(Account\s*Branch|Account\s*Status|Account\s*number|A/C\s*Open\s*Date|"
    r"Expected\s*AMB|Cust\s*ID|Pr\.?\s*Code|Br\.?\s*Code|JOINT\s*HOLDERS|"
    r"Nomination|Statement\s*From|Page\s+\d+\s+of\s+\d+|"
    r"Phone\s*No|Email\s*:|Currency\s*:|OD\s*Limit|\bLimit\s*:|"
    r"IFSC|MICR|^Address\s*:|^City\s*:|^State\s*:|"
    r"We understand your world|HDFC\s*BANK$|Not\s*Applicable|"
    r"HDFC BANK LIMITED|\*Closing balance includes|\bthis statement\b|"
    r"Registered Office Address|State account branch GSTN|HDFC Bank GSTIN|"
    r"https://www\.hdfcbank\.com)",
    re.I,
)
_PAGE_MARKER_RE = re.compile(r"Page\s*No\s*\.?\s*:", re.I)


def build_hdfc_ocr_transactions(text, pdf_name, bank, st_from, st_to, opening_balance, seq_start, issues):
    # Everything from "STATEMENT SUMMARY" onward is the aggregate
    # totals table, not more transactions - left in, its own
    # Debit/Credit/Closing-Balance figures get mistaken for one final
    # (wildly wrong) transaction row, exactly what `build_hdfc_transactions`
    # already guards against for the non-OCR'd text.
    summary_match = re.search(r"STATEMENT\s*SUMMARY", text, re.I)
    if summary_match:
        text = text[: summary_match.start()]

    kept = []
    # HDFC repeats a customer name/branch/address box around the "Page No
    # .: N" marker on every page. Its wording is different per account (a
    # different customer's own name and street address each time), so it
    # can never be matched by content the way the fixed template text
    # above can - see hdfc_parser.build_hdfc_transactions's identical
    # in_page_footer zone for the non-OCR'd version of this same fix.
    # Track it by position instead: start dropping lines the moment a
    # page marker is seen (or at document start, before any transaction
    # has been found yet) and keep dropping until the next real
    # transaction row.
    in_page_zone = True
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if DATE_START_RE.match(stripped):
            in_page_zone = False
        elif _PAGE_MARKER_RE.search(stripped):
            in_page_zone = True
        if in_page_zone:
            continue
        if BOILER_RE.search(stripped) or _HDFC_OCR_NOISE_RE.search(stripped):
            continue
        kept.append(stripped)
    cleaned = "\n".join(kept)

    # Some OCR runs render the statement table's own column gridlines as
    # literal "|" characters ("01/04/25 | NEFT CR-... 108 | 01/04/25
    # 150,000.00 244,048.86"), which puts the narration BETWEEN the
    # transaction date and the value date rather than after both. The
    # generic engine below always slices narration from after the LAST
    # date on the line, which is empty for that shape (it swallows the
    # narration into the amount region, then falls back to keeping the
    # *whole* raw line as narration). HDFC's own non-OCR'd parser already
    # slices narration the right way for this exact two-date shape - the
    # text between the first and last date - so reuse it here once the
    # pipes are blanked out to plain whitespace, without touching the
    # generic engine other OCR'd HDFC layouts (no pipes) still rely on.
    if re.search(r"^\d{2}/\d{2}/\d{2}\s*\|", cleaned, re.M):
        from parser.hdfc_parser import build_hdfc_transactions
        records = build_hdfc_transactions(
            cleaned.replace("|", " "),
            pdf_name,
            bank,
            st_from,
            st_to,
            opening_balance,
            seq_start,
            issues,
        )
    else:
        records = build_transactions(
            cleaned,
            pdf_name,
            bank,
            st_from,
            st_to,
            opening_balance,
            seq_start,
            amount_search_scope="full",
        )
    if not records:
        issues.append({
            "file": pdf_name,
            "issue": "HDFC statement required OCR but no transaction rows could be recognised in the OCR'd text - please verify the source scan quality",
        })
    return records
