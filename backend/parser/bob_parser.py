"""Bank of Baroda statement parser.

Brand new bank - no prior support existed anywhere in this codebase (no
builder was registered for it, even though `parser_selector.py` already
recognised the "BANK OF BARODA" letterhead phrase and the "BARB" IFSC
prefix, so every such statement silently fell through to the fully
generic engine). Table shape: `DATE PARTICULARS CHQ.NO. WITHDRAWALS
DEPOSITS BALANCE`, one flow amount per row (never both), with the
running balance carrying an explicit "Cr"/"Dr" suffix the generic
engine's own `parse_balance_from_match` already understands - so the
amounts/direction/balance were actually being parsed correctly even
before this module existed.

What wasn't fine is narration hygiene. Every page reprints a full
letterhead + account-header block above its own transaction rows and a
"Page Total:" footer below them:

    ----------------------------------------------------------------
    ------
    Note: Cheques received in inward clearing will be considered for debiting/
    returning on the basis opening balance in account
    Unless the constituent notifies the bank of any discrepancy in this statement
    BANK OF BARODA Date :05-05-2026
    WALKESHWAR,MUMBAI Time : 11:19:11
    ADDRESS: 59,WALKESHWAR ROAD OPP.SHITAL BAUG,MUMBA,MH,400006
    HELPLINE NO. : 18005700/18005000 CYBER CRIME HELPLINE : 1930
    BRANCH PHONE NO. :022-23612625
    MICR CODE: 400012071 IFSC CODE: BARB0WALKES Page No: 2
    Repayment credit [06130300109889]
    ----------------------------------------------------------------
    ------
    A/C Number : 06130100013456 Account Open Date :16-11-2017
    Statement of account for the period of 01-04-2025 to 31-03-2026
    ----------------------------------------------------------------
    ------
    DATE PARTICULARS CHQ.NO. WITHDRAWALS DEPOSITS BALANCE
    ----------------------------------------------------------------
    ------
    20-04-25 DIGITA-MUMBAI/ 1.00 582.56Cr

None of that carries a date at the start of its own line, so the
generic engine's date-anchored parser has nowhere to stop: it all gets
appended as "continuation" text onto whatever transaction happened to
be open when the page broke - the LAST row of the previous page - and
`narration[:500]` mostly just makes the wreckage disappear into the
character-500 cutoff rather than actually fixing it. Filtering every
one of those decoration lines out line-by-line, before handing the
rest to the shared generic engine, is the fix.

Note one genuine narration continuation hides in that same gap - e.g.
"Repayment credit [06130300109889]" above really is the second line of
the *previous* page's last transaction's narration (its row got split
by the page break). It doesn't match any noise pattern below, so it
correctly falls through to the generic engine's own
"append-to-current-transaction" branch instead of being stripped.

Every noise pattern here keys off a label BoB's own statement generator
prints on every account (bank name, "HELPLINE NO.", "MICR CODE:", "A/C
Number :", "Page Total:", the boilerplate cheque-clearing disclaimer,
"Grand Total:", ...) - never the customer's own branch name, address, or
account number - so this generalises across accounts without
overfitting to this one. See `_strip_bob_noise`.
"""

import re

from .generic_parser import build_transactions

_BOB_HEAD_ANCHOR_RE = re.compile(
    r"DATE\s+PARTICULARS\s+CHQ\.?\s*NO\.?\s+WITHDRAWALS\s+DEPOSITS\s+BALANCE", re.I
)
_BOB_TAIL_RE = re.compile(r"Grand\s+Total\s*:", re.I)
_BOB_NOISE_RE = re.compile(
    r"(^BANK\s+OF\s+BARODA\s+Date\s*:|Time\s*:\s*\d{1,2}:\d{2}:\d{2}\s*$|"
    r"^ADDRESS\s*:|^HELPLINE\s+NO\.?|^BRANCH\s+PHONE\s+NO\.?|^MICR\s+CODE\s*:|"
    r"^A/C\s+Number\s*:.*Account\s+Open\s+Date|^Statement\s+of\s+account\s+for\s+the\s+period|"
    r"^DATE\s+PARTICULARS\s+CHQ\.?\s*NO\.?\s+WITHDRAWALS\s+DEPOSITS\s+BALANCE|"
    r"^Page\s+Total\s*:|^Note\s*:\s*Cheques\s+received|"
    r"^returning\s+on\s+the\s+basis\s+opening\s+balance|"
    r"^Unless\s+the\s+constituent\s+notifies|"
    r"^-[-\s]*$)",
    re.I,
)


def _strip_bob_noise(text):
    tail_match = _BOB_TAIL_RE.search(text)
    if tail_match:
        text = text[: tail_match.start()]

    head_match = _BOB_HEAD_ANCHOR_RE.search(text)
    if head_match:
        text = text[head_match.end():]

    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or _BOB_NOISE_RE.search(stripped):
            continue
        kept.append(stripped)
    return "\n".join(kept)


def build_bob_transactions(text, pdf_name, bank, st_from, st_to, opening_balance, seq_start):
    cleaned = _strip_bob_noise(text)
    return build_transactions(
        cleaned,
        pdf_name,
        bank,
        st_from,
        st_to,
        opening_balance,
        seq_start,
    )
