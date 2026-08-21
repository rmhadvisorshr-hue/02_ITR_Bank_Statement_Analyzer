"""Axis Bank statement parser.

Table shape: `Tran Date | Chq No | Particulars | Debit | Credit |
Balance | Init. Br`. As with ICICI (see icici_parser.py), a wrapped
Particulars narration (every IMPS/UPI/NEFT/MOB-TD entry, which is most
rows) puts the Debit/Credit/Balance numbers on a *continuation* line,
never on the line the date itself sits on - e.g.:

    02-04-2025
    UPI/P2M/100280759897/BAJAJ FIN/HDFC
    BANK/UPI Mand//P2V/

    2318.00 99411.00 5114

The old version of this module delegated straight to the generic
engine with its default line-only amount search, which only ever
checks the date's own line for numbers - on a statement shaped like
this, that line never has any, so `len(amount_matches) < 2` and the
row is silently dropped. Every wrapped transaction (the large majority
of any real Axis statement) was lost this way; the module's own
original docstring admitted as much ("No Axis sample was in the
failing-PDF batch this module was written against"). Fixed the same
way ICICI was: search the whole wrapped block for amounts.

Two more things need handling before that fix is safe:

1. "OPENING BALANCE <amount>" has no date, so it isn't its own
   transaction - it would otherwise get glued onto the very first
   dated row as leading narration/continuation, adding a spurious
   number to that row's amount search.
2. The statement ends with a "TRANSACTION TOTAL / <debits> / <credits>
   / CLOSING BALANCE / <balance>" summary block, immediately followed
   by disclaimer text and a "Legends :" glossary - none of it dated,
   so left in, it would all get glued onto the actual last transaction
   as continuation, and its numbers would corrupt that row's
   debit/credit amount. Everything from "TRANSACTION TOTAL" onward is
   cut, mirroring how icici_parser.py cuts at the closing "Sincerely,".
"""

import re

from .generic_parser import build_transactions

_OPENING_BALANCE_RE = re.compile(r"^OPENING BALANCE\b", re.I)
_AXIS_NOISE_RE = re.compile(
    r"(^Tran\s+Date\s+Chq\s+No\s+Particulars|"
    r"^Statement of Axis Account No|"
    r"^Joint Holder\s*:|^Customer ID\s*:|^IFSC Code\s*:|^MICR Code\s*:|"
    r"^Nominee Registered\s*:|^Registered Mobile No\s*:|"
    r"^Registered Email ID\s*:|^Scheme\s*:|^CKYC No\s*:|^Currency\s*:|"
    r"^PAN\s*:)",
    re.I,
)


def _strip_axis_noise(text):
    # Everything from "TRANSACTION TOTAL" onward is the page-total /
    # closing-balance summary plus the disclaimer and legends glossary
    # that follow it on every Axis statement, not more rows.
    total_match = re.search(r"\bTRANSACTION TOTAL\b", text, re.I)
    if total_match:
        text = text[: total_match.start()]

    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _OPENING_BALANCE_RE.match(stripped) or _AXIS_NOISE_RE.search(stripped):
            continue
        kept.append(stripped)
    return "\n".join(kept)


def build_axis_transactions(text, pdf_name, bank, st_from, st_to, opening_balance, seq_start):
    cleaned = _strip_axis_noise(text)
    return build_transactions(
        cleaned,
        pdf_name,
        bank,
        st_from,
        st_to,
        opening_balance,
        seq_start,
        amount_search_scope="full",
    )
