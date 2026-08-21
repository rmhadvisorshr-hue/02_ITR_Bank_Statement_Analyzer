"""Bank of India statement parser.

Brand new bank - no prior support existed anywhere in this codebase
(`parser_selector.py` already recognised the "BANK OF INDIA" letterhead
phrase and the "BKID" IFSC prefix, so a statement got correctly labelled
but then fell through to the fully generic engine with no bank-specific
cleanup at all).

The sample this was built against is a "Detailed Statement" export that
only exists as a scanned/rasterised PDF (154 pages, no text layer), so
`extract_bank.py` always routes it through OCR. That matters for two
reasons this module exists to handle:

1. A one-time cover block at the very top of page 1 (bank letterhead,
   "Detailed Statement", account holder/address, "Transaction Date
   from/to", "Amount from/to", "Cheque from/to", "Transaction type") has
   no date-anchored line for the generic engine to stop on, so left in
   it becomes bogus leading narration glued onto the first real
   transaction row - same class of problem `_strip_bob_noise` handles
   for Bank of Baroda. Slicing the text to start right after
   "Transaction type: ..." (a label the statement always prints,
   regardless of which account it's for) removes it in one cut.
2. A closing disclaimer paragraph ("NOTE: Any discrepancy in the
   account statement should be notified to the bank within period of 30
   days...") at the very end of the last page has nothing after it to
   stop on either, so it was observed getting appended onto the last
   transaction's narration. Slicing the text to end right before that
   paragraph's own "NOTE:" label removes it the same way.

Unlike Bank of Baroda, this layout does NOT reprint a letterhead/column
header on every page - transactions simply continue across the page
boundary with no decoration in between - so no per-line noise filter is
needed here, just the one head cut and one tail cut.

Note on OCR quality: this scan is dense enough that Tesseract
occasionally fails to read a row's flow amount at all (leaving only the
running balance on that line). The generic engine correctly requires at
least 2 numbers on a transaction's line and skips ones that don't have
that, rather than guessing the missing amount from the balance delta -
which is the right call given amounts must never be invented, but it
does mean a small number of transactions can go unparsed on a
low-quality scan. That's a scan-quality limit, not something a
noise-stripping regex can fix.
"""

import re

from .generic_parser import build_transactions

_BOI_HEAD_ANCHOR_RE = re.compile(r"Transaction\s+type\s*:[^\n]*\n", re.I)
_BOI_TAIL_RE = re.compile(r"NOTE\s*:\s*\n\s*Any\s+discrepancy\s+in\s+the\s+account\s+statement", re.I)
# Whatever column-header row is printed between "Transaction type: ..." and
# the first real transaction (observed as a lone garbled "EC") isn't worth
# matching by its own OCR'd text - a different scan of the same layout could
# render it completely differently. Instead, skip forward to the first line
# that actually starts like a transaction row (an optional serial number
# then a DD-MM-YYYY date), which is the same shape the generic engine's own
# `DATE_START_RE` looks for.
_BOI_FIRST_ROW_RE = re.compile(r"^\s*(?:\d+\s+)?\d{2}-\d{2}-\d{4}\b", re.M)


def _strip_boi_noise(text):
    tail_match = _BOI_TAIL_RE.search(text)
    if tail_match:
        text = text[: tail_match.start()]

    head_match = _BOI_HEAD_ANCHOR_RE.search(text)
    if head_match:
        text = text[head_match.end():]
        first_row_match = _BOI_FIRST_ROW_RE.search(text)
        if first_row_match:
            text = text[first_row_match.start():]

    return text


def build_boi_transactions(text, pdf_name, bank, st_from, st_to, opening_balance, seq_start):
    cleaned = _strip_boi_noise(text)
    return build_transactions(
        cleaned,
        pdf_name,
        bank,
        st_from,
        st_to,
        opening_balance,
        seq_start,
    )
