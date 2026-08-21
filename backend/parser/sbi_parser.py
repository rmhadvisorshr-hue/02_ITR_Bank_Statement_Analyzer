"""State Bank of India statement parser.

SBI's own "STATEMENT OF ACCOUNT" export (confirmed against two real
samples from different customers/branches - PRAGATHINAGAR and VASAI
ROAD (W) - so this is SBI's own template, not one account's quirk)
shares the exact wrapped-narration problem ICICI/Axis already had:
every row's Value Date/Post Date sit alone on their own line, with the
"DEP TFR"/"WDL TFR" label and UPI/narration detail wrapping across
several lines *after* it, and the Ref No/Debit/Credit/Balance cells
only appearing at the very end of that block (often each on its own
line, since the column is narrow) - never on the date's own line:

    02/04/2025 02/04/2025
    DEP TFR
    UPI/CR/509226910069/MARAM
    SR/UTIB/lakshmired/UPI
    0097735162098 AT 21831 PRAGATHINAGAR
    -
    -
    2,700.00
    2,792.27

Delegating with the generic engine's default line-only amount search
(the old behaviour here) never finds 2+ numbers on that first line, so
every single transaction was silently dropped. Fixed the same way as
ICICI/Axis: search the whole wrapped block (`amount_search_scope="full"`).
The "-" placeholders for an empty Ref No/Debit/Credit cell don't
confuse this - they simply don't match `AMOUNT_RE`, so only the real
printed numbers (in this example the credit amount and the running
balance) ever get picked up, in the same order regardless of which of
Debit/Credit actually held the value.

Two more things need handling before that fix is safe:

1. Page 1 of this export is a "Relationship Summary" (Account Summary
   / Transaction Accounts / Term Accounts / Loans tables) that has no
   transactions at all - left in, whatever's still in the generic
   engine's bounded `pending` buffer when the first real dated row
   shows up becomes bogus leading narration on it. Slicing the text to
   start at the transaction table's own column header ("Value Date
   Post Date") removes this and the page-2 account/branch header block
   (CIF Number, IFSC Code, Statement From, etc.) in one cut, the same
   way icici_parser.py's `start_match` does.
2. The statement ends with a "Statement Summary :" trailer (Brought
   Forward / Dr Count / Cr Count / Total Debits / Total Credits /
   Closing Balance) immediately followed by disclaimer bullets - none
   of it dated, so left in, it all glues onto the last real transaction
   as continuation and its own totals get mistaken for that row's
   amount/balance. Cut the same way axis_parser.py cuts at
   "TRANSACTION TOTAL".

The repeated per-page column-header block ("Value Date Post Date
Details Ref No/Cheque No Debit Credit Balance", however it happens to
line-wrap on a given page) is filtered line-by-line the same way
icici_parser.py's per-page header is, since it recurs throughout the
document rather than only at the very start.
"""

import re

from .generic_parser import build_transactions

# OCR reads a scanned statement's table gridlines/cell borders as stray
# punctuation dropped into the middle of a column-header label - a "|"
# where a space belongs ("Date| Post"), an "&"/"<" where a heavier
# gridline was ("Debit & Credit", "Cheque < Debit"). Plain `\s+` between
# these header words breaks on exactly that noise (see the OCR-specific
# failure this caused, documented on `_strip_sbi_noise` below); matching
# "a short run of non-alphanumeric characters, possibly none" instead
# absorbs any of it without caring which symbol Tesseract happened to
# invent.
_SEP = r"[^A-Za-z0-9]{0,4}"
_SBI_HEAD_ANCHOR_RE = re.compile(rf"Value{_SEP}Date{_SEP}Post{_SEP}Date.{{0,120}}?Balance", re.I | re.S)
# "Statement From : DD-MM-YYYY to DD-MM-YYYY" is printed exactly once,
# immediately before the transaction table's own column-header row -
# never repeated per-page the way that header row is. It's a much safer
# anchor for "where the real cover-page/account-info block ends" than
# the header row itself: see `_strip_sbi_noise`'s docstring-comment for
# why the header row can't be trusted blindly.
_SBI_STMT_FROM_RE = re.compile(
    r"Statement\s+From\s*:?\s*\d{2}[-/]\d{2}[-/]\d{4}\s+to\s+\d{2}[-/]\d{2}[-/]\d{4}", re.I
)
_SBI_TAIL_RE = re.compile(r"Statement\s+Summary\s*:", re.I)
_SBI_NOISE_RE = re.compile(
    r"(^Date\s+of\s+Statement\s*:|^Clear\s+Balance\s*:|^Uncleared\s+Amount\s*:|"
    r"^\+?MOD\s+Bal\s*:|^Lien\s*:?$|^Limit\s*:|^Monthly\s+Avg\s+Balance\s*:|"
    r"^Interest\s+Rate\s*:|^Drawing\s+Power\s*:|^Account\s*open\s+Date\s*:|"
    r"^Branch\s+Code\s*:|^Branch\s+Name\s*:|^Branch\s+Email\s+ID\s*:|"
    r"^Branch\s+Phone\s*:|^CIF\s+Number\s*:|^Account\s+Number\s*:|"
    r"^Product\s*:|^IFSC\s+Code\s*:|^Currency\s*:|^CKYCR?\s+Number\s*:|"
    r"^MICR\s+Code\s*:|^Nominee\s+Name\s*:|^Statement\s+From\s*:|"
    rf"^Value{_SEP}Date{_SEP}Post{_SEP}Date|^Details$|^Description$|^Cheque$|^No$|"
    rf"^Ref\s*No/?\s*$|₹?{_SEP}Debit{_SEP}₹?{_SEP}Credit{_SEP}Balance|^Balance$|"
    r"^Page\s*(?:No\.?)?\s*:?\s*\d+$|^STATEMENT OF ACCOUNT$|^State Bank of India$|"
    r"^SBI Relationship Summary|^MY ACCOUNTS$|^MY INFORMATION$|"
    r"^BRANCH INFORMATION$|^Account Summary$|^Transaction Accounts$|"
    r"^Term Accounts$|^Other Deposits$|^Loans$|"
    r"do not share your (?:ATM|User|PIN)|Please do not share|"
    r"Each depositor is insured|Deposit Insurance and Credit Guarantee|"
    r"Brought\s+Forward|Dr\s+Count|Cr\s+Count|Total\s+Debits|Total\s+Credits)",
    re.I,
)


def _strip_sbi_noise(text):
    tail_match = _SBI_TAIL_RE.search(text)
    if tail_match:
        text = text[: tail_match.start()]

    # Cutting blindly at the FIRST `_SBI_HEAD_ANCHOR_RE` match used to be
    # safe because that header row only ever appears once before the
    # transaction table starts, in a clean text-layer extraction. But on
    # an OCR'd (scanned) statement, page 1's own copy of that row can come
    # out of Tesseract with a stray character where a space belongs (a
    # table gridline misread as "|", e.g. "Value Date| Post Date ...")
    # which breaks this regex's `\s+` on page 1 alone - every *later*
    # page's copy of the same row still OCRs cleanly. `.search` then
    # happily matches THAT one instead, and slicing there throws away
    # every real transaction between the true start of the table and
    # that later page - silently, with no error, no dropped-row warning.
    # This is exactly what happened on a real customer statement: all 3
    # of page 1's transactions vanished this way.
    #
    # "Statement From : DD-MM-YYYY to DD-MM-YYYY" (`_SBI_STMT_FROM_RE`)
    # sits immediately before that header row and, unlike the row itself,
    # is never repeated on later pages - so if the header anchor's match
    # lands far past it, that's the signal the match skipped pages 2+
    # rather than heading page 1's own table. In that case, cut right
    # after "Statement From" instead and let the per-line noise filter
    # below (plus `build_transactions`' own leading-narration handling)
    # deal with whatever's left of the header row - a little narration
    # noise on the first transaction is a far smaller loss than dropping
    # every transaction on the page outright.
    cut_at = 0
    stmt_from_match = _SBI_STMT_FROM_RE.search(text)
    if stmt_from_match:
        cut_at = stmt_from_match.end()

    head_match = _SBI_HEAD_ANCHOR_RE.search(text)
    if head_match and head_match.end() - cut_at < 400:
        cut_at = head_match.end()

    text = text[cut_at:]

    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or _SBI_NOISE_RE.search(stripped):
            continue
        kept.append(stripped)
    return "\n".join(kept)


def build_sbi_transactions(text, pdf_name, bank, st_from, st_to, opening_balance, seq_start):
    cleaned = _strip_sbi_noise(text)
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
