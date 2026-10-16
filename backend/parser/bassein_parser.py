"""Bassein Catholic Co-op Bank Ltd statement parser.

Brand new bank - `parser_selector.py` already recognised the "BASSEIN
CATHOLIC" letterhead phrase, but had no dedicated builder, and its
labelled-IFSC lookup didn't cover this bank's "BACB" prefix at all - so a
statement either fell through to the fully generic engine (correctly
labelled, if the letterhead happened to be in the text) or got
mislabelled entirely as whichever *other* bank's name happened to appear
first in a transaction narration (a UPI/NEFT counterparty), since this
bank's own name is never printed anywhere on its "new"-layout export (see
below).

Two account-statement templates were observed, both handled here:

- "old" (Current Account export): letterhead "BASSEIN CATHOLIC CO-OP BANK
  LTD", columns `TRANS DATE VALUE DATE REFF DESCRIPTION DEBITS CREDITS
  BALANCE`, dates like `05-APR-2025`. The per-page repeated column header
  is already covered by generic_parser's own shared `BOILER_RE` (it
  matches on "TRANS DATE VALUE DATE"), so no bank-specific noise-filtering
  is needed for this layout beyond the two fixes below.
- "new" (savings/current export with no bank letterhead at all - only
  identifiable via its own "IFSC Code / MICR Code : BACB..." line, which
  is what parser_selector.py's broadened IFSC regex now catches): columns
  `Transaction Description Reference Number Value Date Debit Credit
  Balance` wrapping onto a trailing "Date" line, repeated per page under a
  generic "Account Statement" banner, dates like `01 Apr 2025`.

Both templates share two defects the shared generic engine can't handle
on its own:

1. The old layout's opening balance is carried forward as its own dated
   "row" (`... B/F ... 0.00 440,463.40 440,463.40`) rather than only being
   stated in a separate summary line - `extract_opening_balance` already
   picks up the real opening balance elsewhere in the text, so treating
   this row as a genuine transaction double-counts that amount as a
   credit. "B/F" is a standard "brought forward" accounting label (already
   used the same way by `generic_parser.extract_opening_balance`'s own
   fallback pattern), not customer data, so it's safe to filter by name
   here.
2. Multi-line cheque-deposit narrations print a "DATED DD-MMM-YYYY" (or,
   in the new layout, "Dated DD Mon YYYY") clearance-reference date that
   sometimes wraps onto its own physical line with nothing else on it -
   which `generic_parser`'s shared bare-date-continuation fix now handles
   generically for every bank, not just this one.

The new layout additionally reprints a generic "Account Statement" title
and its own column header on every page, and states its opening/closing
balance summary in the text ("Account Summary: Opening Balance ...") at
the very end - both fixed template text, never customer data, so they're
filtered/cut structurally rather than by whose account it is.
"""

import re

from .generic_parser import build_transactions

_OLD_LAYOUT_HEADER_RE = re.compile(
    r"TRANS\s+DATE\s+VALUE\s+DATE\s+REFF\s+DESCRIPTION\s+DEBITS\s+CREDITS\s+BALANCE", re.I
)
_NEW_LAYOUT_HEADER_RE = re.compile(
    r"Transaction\s+Description\s+Reference\s+Number\s+Value\s+Date\s+Debit\s+Credit\s+Balance", re.I
)
_BANNER_RE = re.compile(r"^Account Statement$", re.I)
_FROM_TO_RE = re.compile(r"^From Date\s*-\s*To Date\s*:", re.I)
_TAIL_RE = re.compile(r"(?:Statement Summary|Account Summary)\s*:", re.I)
# The old layout's opening-balance carry-forward row: a normal dated row
# whose REFF/DESCRIPTION field is just "B/F" (optionally followed by
# filler dots) with no real narration - see module docstring point 1.
_BF_ROW_RE = re.compile(
    r"^\d{2}[-/][A-Za-z]{3}[-/]\d{4}\s+\d{2}[-/][A-Za-z]{3}[-/]\d{4}\s+B/F\b", re.I
)
# The new layout's own row shape puts its value date and flow/balance
# amounts in a different order than the old layout (and than every other
# bank generic_parser.build_transactions was written against): "DATE1
# narration-start REF DATE2 amount balance", with genuine narration text
# sitting *between* the two dates rather than after both of them. The
# shared engine only ever keeps text found after the *last* date on a
# row (correct for every layout that puts both dates up front, which is
# every other layout in this codebase), so left alone it silently drops
# that between-dates text. Rather than changing that shared assumption
# for every bank, reorder just this layout's own rows structurally -
# moving the value date to sit right after the transaction date, the
# same position it already occupies in the old layout - so the shared
# engine's existing narration slicing keeps working unmodified. This is
# a pure token reordering keyed on shape (two "DD Mon YYYY" dates and a
# trailing run of decimal amounts), never on what the narration says.
_NEW_ROW_RE = re.compile(
    r"^(\d{2}\s+[A-Za-z]{3}\s+\d{4})\s+(.*?)\s+(\d{2}\s+[A-Za-z]{3}\s+\d{4})\s+"
    r"([\d,]+\.\d{2}(?:\s+[\d,]+\.\d{2})*)$"
)


def _reorder_new_layout_row(line):
    match = _NEW_ROW_RE.match(line)
    if not match:
        return line
    date1, middle, date2, amounts = match.groups()
    return f"{date1} {date2} {middle} {amounts}"


def build_bassein_transactions(text, pdf_name, bank, st_from, st_to, opening_balance, seq_start):
    tail_match = _TAIL_RE.search(text)
    if tail_match:
        text = text[: tail_match.start()]

    is_new_layout = bool(_NEW_LAYOUT_HEADER_RE.search(text))
    header_re = _NEW_LAYOUT_HEADER_RE if is_new_layout else _OLD_LAYOUT_HEADER_RE
    head_match = header_re.search(text)
    if head_match:
        text = text[head_match.end():]

    kept = []
    # Whatever comes immediately after the header we just cut past (or
    # after any repeat of it further down) is that header's own trailing
    # "Date" column label wrapping onto its own line - true at the very
    # first header (already consumed by the head-anchor cut above, hence
    # starting this True) and at every later per-page repeat the loop
    # below detects itself.
    prev_was_header = True
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _BF_ROW_RE.match(stripped):
            prev_was_header = False
            continue
        if header_re.search(stripped):
            prev_was_header = True
            continue
        if _BANNER_RE.match(stripped) or _FROM_TO_RE.match(stripped):
            prev_was_header = False
            continue
        if prev_was_header and stripped.lower() == "date":
            prev_was_header = False
            continue
        prev_was_header = False
        if is_new_layout:
            stripped = _reorder_new_layout_row(stripped)
        kept.append(stripped)
    cleaned = "\n".join(kept)

    return build_transactions(cleaned, pdf_name, bank, st_from, st_to, opening_balance, seq_start)
