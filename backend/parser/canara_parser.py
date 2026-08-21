"""Canara Bank statement parser.

Brand new bank - no prior support existed anywhere in this codebase
(no IFSC prefix, no name-detection phrase, no builder). Table shape:
`Txn Date | Value Date | Cheque No. | Description | Branch Code |
Debit | Credit | Balance`. Every row's Txn Date carries a full
timestamp and Value Date is printed right after it in a different,
shorter format ("01 Apr 2025", not Txn Date's "01-04-2025"), with the
UPI/NEFT narration (and its own embedded reference hash + repeated
date/time) wrapping across several lines *before* the Branch Code/
Debit-or-Credit amount/Balance ever appear - never on the date's own
line:

    01-04-2025 19:29:50 01 Apr 2025 545754270853 UPI/DR/545754270853/Malesh
    R/YESB/**38321@ybl/UPI//
    ICI9edaaf614bcb403183832201600edf9b/01/04/2025
    19:29:50
    33 200.00 83,404.56

This is the same wrapped-narration shape ICICI/Axis/SBI already needed
fixing for: the generic engine's default line-only amount search never
finds 2+ numbers on the date's own line, so every transaction would be
silently dropped without `amount_search_scope="full"`.

Only one Debit-or-Credit amount is ever printed per row (never an
explicit blank/"-" placeholder the way SBI's export does), so
direction has to come from the generic engine's own balance-delta
reconciliation - which is deliberately preferred there over trusting
any "DR"/"CR" text found in the narration, since this exact narration
shape embeds both indiscriminately for other purposes (e.g. a
self-transfer line reading "MB-WITHIN-DR:XXXX9663-CR:XXXX4206-..."
names the source and destination sub-accounts, not this statement's
own transaction direction).

Two more things need handling before that fix is safe:

1. The statement opens with a "Current & Saving Account Statement"
   cover block (customer name/address, Customer Id, Branch Name, MICR/
   IFSC Code, Searched By period, Account Number, Product Name, Opening/
   Closing Balance) with no transactions in it at all - left in,
   whatever's still in the generic engine's bounded `pending` buffer
   becomes bogus leading narration on the first real row. Slicing the
   text to start at the transaction table's own column header ("Txn
   Date Value Date ... Balance") removes this in one cut, the same way
   icici_parser.py's and sbi_parser.py's `head_match` does.
2. The repeated per-page column header (however it happens to
   line-wrap on a given page - "Branch" / "Code" often split across two
   lines) is filtered line-by-line since it recurs throughout the
   132-page sample rather than only at the very start.
"""

import re

from .generic_parser import build_transactions

_CANARA_HEAD_ANCHOR_RE = re.compile(r"Txn\s+Date\s+Value\s+Date.{0,150}?Balance", re.I | re.S)
_CANARA_TAIL_RE = re.compile(
    r"Statement\s+Summary\s*:|\bLegends\b|is a system generated statement|"
    r"does not require any signature|End of Statement|Disclaimer\s*:",
    re.I,
)
_CANARA_NOISE_RE = re.compile(
    r"(^Current\s*&\s*Saving\s+Account\s+Statement$|^Account\s+Statement\s+as\s+of\s*:?|"
    r"^Account\s+Holders\s+Name\s*:?|^Customer\s*Id\s*:?|^Branch\s+Name\s*:?|"
    r"^MICR\s+Code\s*:?|^IFSC\s+Code\s*:?|^Searched\s+By\s*:?|"
    r"^Account\s+Number\s*:?|^Account\s+Currency\s*:?|^Product\s+Name\s*:?|"
    r"^Opening\s+Balance\s*:?|^Closing\s+Balance\s*:?|"
    r"^Txn\s+Date\s+Value\s+Date|^Cheque\s+No\.?$|^Description$|^Branch$|"
    r"^Code$|^Debit\s+Credit\s+Balance$|^Balance$|"
    r"^Page\s*(?:No\.?)?\s*:?\s*\d+$|^Canara\s+Bank$)",
    re.I,
)


def _strip_canara_noise(text):
    tail_match = _CANARA_TAIL_RE.search(text)
    if tail_match:
        text = text[: tail_match.start()]

    head_match = _CANARA_HEAD_ANCHOR_RE.search(text)
    if head_match:
        text = text[head_match.end():]

    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or _CANARA_NOISE_RE.search(stripped):
            continue
        kept.append(stripped)
    return "\n".join(kept)


def build_canara_transactions(text, pdf_name, bank, st_from, st_to, opening_balance, seq_start):
    cleaned = _strip_canara_noise(text)
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
