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
from parser.generic_parser import BOILER_RE, build_transactions  # noqa: E402

_HDFC_OCR_NOISE_RE = re.compile(
    r"(Account\s*Branch|Account\s*Status|Account\s*number|A/C\s*Open\s*Date|"
    r"Expected\s*AMB|Cust\s*ID|Pr\.?\s*Code|Br\.?\s*Code|JOINT\s*HOLDERS|"
    r"Nomination|Statement\s*From|Page\s+\d+\s+of\s+\d+|"
    r"Phone\s*No|Email\s*:|Currency\s*:|OD\s*Limit|\bLimit\s*:|"
    r"IFSC|MICR|^Address\s*:|^City\s*:|^State\s*:|"
    r"We understand your world|HDFC\s*BANK$|Not\s*Applicable)",
    re.I,
)


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
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if BOILER_RE.search(stripped) or _HDFC_OCR_NOISE_RE.search(stripped):
            continue
        kept.append(stripped)
    cleaned = "\n".join(kept)

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
