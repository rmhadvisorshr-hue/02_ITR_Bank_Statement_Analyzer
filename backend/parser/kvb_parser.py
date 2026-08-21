"""Karur Vysya Bank statement parser.

Brand new bank - no prior support existed anywhere in this codebase
(no IFSC prefix, no name-detection phrase, no builder). Table shape:
`Transaction Date | Value Date | Branch | Cheque No | Description |
Debit | Credit | Balance`. Transaction Date carries a full timestamp
("02-04-2025 15:49:01"); Value Date right after it is date-only
("02-04-2025"). Short one-line descriptions keep the row's Debit/
Credit/Balance numbers on that same physical line, but longer ones
(most NEFT/RTGS/IMPS/MB-WITHIN transfers) wrap the description across
1-2 more lines first and only print the amount + balance at the very
end of that block:

    05-04-2025 21:35:57 05-04-2025 2101 NEFT DR-KVBLH00232517905-K
    uma-ICIC0003633-MUMBAI-FORT
    10,000.00 12,06,628.61

Only one Debit-or-Credit amount is ever printed per row, so direction
comes from the generic engine's own balance-delta reconciliation
rather than any "DR"/"CR" text in the narration - deliberately so,
since this bank's own internal-transfer narration embeds both
indiscriminately for an unrelated purpose (e.g. "MB-WITHIN-
DR:XXXX9663-CR:XXXX4206-..." names the source/destination sub-account
of a self-transfer, not this statement's own transaction direction).
The same reasoning applies to cheque-return reversal pairs (a large
debit that drives the balance negative, immediately reversed by an
equal credit two rows later, e.g. "TO CLG:...": debit then "By
Clg(Rej):...": credit) - delta reconciliation reads the actual signed
balance movement instead of trying to infer direction from wording,
so it resolves these correctly even through a negative intermediate
balance.

Passing `amount_search_scope="full"` (the same fix ICICI/Axis/SBI/
Canara all needed) is what makes the wrapped-description rows above
findable at all - the generic engine's line-only default never finds
2+ numbers on the date's own line whenever the description wraps.

The front-matter account/branch header block (Account Number, Branch,
IFSC/MICR Code, Customer ID, Statement Period, and the "Opening
Balance ( Balance B/F ) <amount>" line - note the parenthetical between
label and figure, which is why `generic_parser.extract_opening_balance`
needs its already-existing `[^\\d]+` fallback pattern to read it at
all, not a code change here) has no transactions in it - slicing the
text to start at the transaction table's own column header removes it
in one cut, the same way every other bank parser's `head_match` does.
"""

import re

from .generic_parser import build_transactions

_KVB_HEAD_ANCHOR_RE = re.compile(r"Transaction\s+Date\s+Value\s+Date.{0,150}?Balance", re.I | re.S)
_KVB_TAIL_RE = re.compile(
    r"Statement\s+Summary\s*:|\bLegends\b|is a system generated statement|"
    r"does not require any signature|End of Statement|Disclaimer\s*:",
    re.I,
)
_KVB_NOISE_RE = re.compile(
    r"(^Account\s+Number\s*:?|^Branch\s*:?|^IFSC\s+Code\s*:?|^MICR\s+Code\s*:?|"
    r"^Customer\s*ID\s*:?|^Statement\s+Period\s*:?|^Opening\s+Balance\s*\(|"
    r"^Transaction\s+Date\s+Value\s+Date|^Branch$|^Cheque\s+No\.?$|"
    r"^Description$|^Debit\s+Credit\s+Balance$|^Balance$|"
    r"^Page\s*(?:No\.?)?\s*:?\s*\d+$|^Karur\s+Vysya\s+Bank$)",
    re.I,
)


def _strip_kvb_noise(text):
    tail_match = _KVB_TAIL_RE.search(text)
    if tail_match:
        text = text[: tail_match.start()]

    head_match = _KVB_HEAD_ANCHOR_RE.search(text)
    if head_match:
        text = text[head_match.end():]

    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or _KVB_NOISE_RE.search(stripped):
            continue
        kept.append(stripped)
    return "\n".join(kept)


def build_kvb_transactions(text, pdf_name, bank, st_from, st_to, opening_balance, seq_start):
    cleaned = _strip_kvb_noise(text)
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
