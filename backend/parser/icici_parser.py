"""ICICI Bank statement parser.

Handles two different real ICICI export templates:

1. The retail savings-account statement (`S No | Transaction Date |
   Cheque Number | Transaction Remarks | Withdrawal | Deposit |
   Balance`, and the older "Wealth Management" variant of the same
   shape: `DATE | MODE** | PARTICULARS | DEPOSITS | WITHDRAWALS |
   BALANCE`). Several things about this layout break the fully generic
   engine:

   a. Every row's own Withdrawal/Deposit/Balance numbers are actually
      printed on the same physical line as its `<S No> <Date>` - e.g.
      "1 03.04.2025 299.00 10456.69" - but the "Transaction Remarks"
      cell wraps as TWO separate pieces around that line, not below it:
      a short bold type label (e.g. "Bil Payment", "NACH trxn", a UPI
      handle) on the line immediately BEFORE it, and the wrapped
      reference/detail text on the line(s) immediately after:

          Bil Payment
          1 03.04.2025 299.00 10456.69
          BIL/NUCL/000992983591/Recharge/NUCLEI service

      `generic_parser.build_transactions` groups strictly by "read the
      next line(s) after a date line as this row's continuation", so
      that leading label line was being read while the *previous*
      transaction was still open and got appended to *its* narration
      instead - every row's narration carried the *next* row's label.
      `_reorder_wrapped_label_lines` swaps each label back onto its own
      row before any date-based grouping runs.

      Because the amounts are already fully present on the date line,
      searching the wrapped continuation for them too (needed for the
      older Wealth Management variant, and for ICICI's current/OD
      export - see `amount_search_scope="full"` below) is actively
      harmful here: continuation text routinely contains numeric-
      looking fragments that are NOT additional amount columns - a
      dotted reference date embedded in narration ("BCB/01.09.2025"),
      or a fee/GST breakdown ("chgRs5.00GSTRs0.90") - and counting
      either as a 3rd/4th "amount" silently corrupted the parsed
      closing balance and, as a knock-on effect, mis-filed the real
      amount into both the Withdrawal AND Deposit columns at once.
      `generic_parser.build_transactions` now prefers the date line's
      own amounts whenever it already has 2+ of them, only falling
      back to the full wrapped block when it doesn't - which is what
      still makes the Wealth Management/OD case work.
   b. The account holder's name and the column header repeat on every
      one of the (often 40-60) pages, and the "Sincerely,"/legend
      sign-off (misspelled "Sincerly," on at least one real export)
      that follows the last transaction is exactly one abbreviation
      table with plenty of its own numeric-looking fragments. Both
      ends are trimmed and the repeated per-page header/footer lines
      are stripped before handing the text to the generic engine -
      otherwise they get glued onto whichever transaction is open at
      that point as bogus narration, corrupting its amount search too.

2. The current/OD-account "Detailed Statement" export (e.g. a running
   overdraft account), a materially different template: `Sl No | Tran
   Id | Value Date | Transaction Date | Transaction Posted Date |
   Cheque no / Ref No | Transaction Remarks | Withdrawal (Dr) |
   Deposit (Cr) | Balance`. Every row starts with an alphanumeric
   `Tran Id` (e.g. "S69185699") sitting *between* the serial number and
   the date - the generic engine's row-start detector only ever
   tolerates a bare leading number before the date, so it never
   recognises where a row begins and the entire statement parses to
   zero transactions. See `_ICICI_OD_ROW_PREFIX_RE` below for the fix.
   This template also has no closing "Sincerely," - it runs straight
   from the last transaction into a "Page Total" grand-total block and
   a "Legends Used in Account Statement" glossary, so that tail is
   truncated on those markers instead.
"""

import re
from decimal import Decimal, InvalidOperation

from .generic_parser import BOILER_RE, DATE_START_RE, build_transactions, parse_any_date, to_float

_HEADER_NAME_RE = re.compile(r"^(?:MR|MRS|MS|M/S)\.?\s+[A-Z][A-Z .]{3,60}$")
_ICICI_NOISE_RE = re.compile(
    r"(DATE\s+MODE\*{0,2}\s+PARTICULARS|"
    r"S\s*No\.?\s+Transaction\s+Date\s+Cheque\s+Number|"
    # Retail savings-account template: the 3-line column-header block
    # repeats on every single page (see `Transaction Withdrawal Deposit
    # Balance` / `S No. Cheque Number Transaction Remarks` / `Date
    # Amount (INR) Amount (INR) (INR)`, always in that order). Left in,
    # it gets glued onto whichever transaction is open at that page
    # break as bogus narration/continuation.
    r"^Transaction\s+Withdrawal\s+Deposit\s+Balance$|"
    r"^S\s*No\.?\s+Cheque\s+Number\s+Transaction\s+Remarks$|"
    r"^Date\s+Amount\s*\(INR\)\s+Amount\s*\(INR\)\s+\(INR\)$|"
    r"Statement of Transactions in Saving(?:s)?\s+Account|"
    r"ACCOUNT\s+DETAILS\s*-\s*INR|ACCOUNT\s+TYPE|"
    r"Did you know\?|Nomination Regd|Your Base Branch|"
    r"Nominee name is displayed|Account Related Other Information|"
    r"This is a system-generated statement|Team ICICI Bank|"
    r"ICICI\s+Bank$|Wealth\s+Management|STATEMENT SUMMARY for Customer|"
    # Retail savings-account template footer, repeated on every page.
    r"www\.icici\.bank\.in|Please call from your registered mobile|"
    r"Never share your OTP|Sincerly,?$|"
    # Current/OD "Detailed Statement" header block.
    r"^Detailed$|^Sl\s*No\s+Tran\s*Id\s+Value\s*Date|"
    r"^A/C\s+Branch\s*:|^Branch\s+Address\s*:|^A/C\s+No\s*:|"
    r"^Jt\.?\s*Holder\s*:|^Transaction\s+Date\s+from\s*:|"
    r"^Transaction\s+Period\s*:|^Statement\s+Request/Download\s+Date\s*:|"
    r"^Account\s+Currency\s*:|^Advanced\s+Search$|^Amount\s+from\s*:|"
    r"^Cheque\s+number\s+from\s*:|^Transaction\s+remarks\s*:$|"
    r"^Transaction\s+type\s*:|"
    # Current/OD tail: page/grand-total block + legend glossary.
    r"^Page\s+Total$|^Opening\s+Bal\s*:|^Withdrawls\s*:|^Deposits\s*:|"
    r"^Closing\s+Bal\s*:|^Legends\s+Used\s+in\s+Account\s+Statement|"
    r"^-+End Of Statement-+$)",
    re.I,
)

# Every row of the current/OD "Detailed Statement" export starts with
# `<Sl No> <Tran Id> <Value Date>...`, e.g. "1 S69185699 01/Apr/2025
# 02/Apr/2025 02/04/2025 04:04:14 AM". The Tran Id (a single letter
# followed by ~5-9 digits, e.g. "S69185699", "M339692", "C728976") sits
# between the serial number and the date, which the generic engine's
# row-start matcher (an optional bare number, then the date - never an
# alphanumeric token) can't see past, so it never recognises any row
# as the start of a transaction and the whole statement parses to zero
# rows.
#
# The Tran Id column is narrow enough that it routinely wraps onto a
# second physical line by itself (e.g. "S6918" / "5699", "M3396" /
# "92") - not just between the Tran Id and the date, as a first,
# narrower-scoped version of this fix assumed. `(?:\s+\d{1,9})?` below
# optionally absorbs that second fragment too, wherever the line break
# actually falls, without needing to know the exact split point.
# Dropping the whole Tran Id (keeping the serial number) lets the
# generic engine's own date-anchoring take over unmodified.
_ICICI_OD_ROW_PREFIX_RE = re.compile(
    r"^(\d{1,5})\s+[A-Z]\d{1,9}(?:\s+\d{1,9})?\s*(?=\d{2}/[A-Za-z]{3}/\d{4})",
    re.M,
)

_ICICI_OD_HEADER_RE = re.compile(
    r"Sl\s+Tran\s+Value\s+Transaction\s+Transaction\s+Cheque.*Withdra.*Deposit.*Balance",
    re.I | re.S,
)
_ICICI_OD_SERIAL_RE = re.compile(r"^\d{1,5}$")
_ICICI_OD_DATE_RE = re.compile(r"^\d{2}/[A-Za-z]{3}/\d{4}$")
_ICICI_OD_MONEY_RE = re.compile(r"\d[\d,]*\.\d{2}")


def is_icici_od_detailed_statement(text):
    """Whether text is ICICI's positional current/OD ledger template."""
    return bool(_ICICI_OD_HEADER_RE.search(text))


def _od_column_amount(words):
    """Read one numeric table cell from its positioned PDF words.

    ICICI wraps wide values at the right edge (``1,95,332.`` / ``00``).
    Combining words *only from the same printed column* repairs that
    wrapping without ever accepting dates, IDs, or narration fragments.
    """
    raw = "".join(word["text"] for word in words)
    match = _ICICI_OD_MONEY_RE.search(raw)
    if not match:
        return Decimal("0.00")
    try:
        return Decimal(match.group(0).replace(",", ""))
    except InvalidOperation:
        return Decimal("0.00")


def build_icici_od_transactions(pages_words, pdf_name, bank, st_from, st_to, opening_balance, seq_start):
    """Parse ICICI's Detailed Statement using the actual PDF columns.

    This format cannot be safely parsed from flattened text: transaction-ID,
    dates, amount cells and balance cells wrap independently.  Page-word
    coordinates let us bind each amount to its printed Withdraw (Dr) or
    Deposit (Cr) column, instead of deriving direction from a guessed balance
    delta.  Thus a row can never receive a date or amount from another column.
    """
    records = []
    seq = seq_start

    for words in pages_words:
        # A data row begins at the left-hand serial-number column.  Header
        # labels and page footers live outside this x-range / are non-numeric.
        starts = [
            i for i, word in enumerate(words)
            if 85 <= word["x0"] < 106
            and word["top"] > 20
            and _ICICI_OD_SERIAL_RE.fullmatch(word["text"])
            # Require the printed Transaction Date on the same baseline.
            # Without this, a wrapped serial/ID fragment on later pages can
            # look like a fresh row and steals its neighbour's amount cell.
            and any(
                172 <= candidate["x0"] < 224
                and abs(candidate["top"] - word["top"]) < 0.2
                and _ICICI_OD_DATE_RE.fullmatch(candidate["text"])
                for candidate in words
            )
        ]
        for row_index, start in enumerate(starts):
            end = starts[row_index + 1] if row_index + 1 < len(starts) else len(words)
            row = words[start:end]

            # The Transaction Date is the second date column (x 175-224),
            # explicitly printed on the PDF.  Do not fall back to another
            # date-like token in narration or a reference number.
            transaction_date_words = [w for w in row if 172 <= w["x0"] < 224]
            transaction_date = next(
                (parse_any_date(w["text"]) for w in transaction_date_words if _ICICI_OD_DATE_RE.fullmatch(w["text"])),
                "",
            )
            if not transaction_date:
                continue
            value_date_words = [w for w in row if 135 <= w["x0"] < 172]
            value_date = next(
                (parse_any_date(w["text"]) for w in value_date_words if _ICICI_OD_DATE_RE.fullmatch(w["text"])),
                transaction_date,
            )

            withdrawal = _od_column_amount([w for w in row if 395 <= w["x0"] < 434])
            deposit = _od_column_amount([w for w in row if 434 <= w["x0"] < 472])
            closing_balance = _od_column_amount([w for w in row if w["x0"] >= 472])
            # A dash in the balance cell is ICICI's debit-balance marker.
            if any(w["text"] == "-" for w in row if w["x0"] >= 472):
                closing_balance = -closing_balance

            # A well-formed statement row has one populated flow column.
            # Retain both only if the PDF genuinely printed both values.
            reference = " ".join(w["text"] for w in row if 270 <= w["x0"] < 315)
            narration = " ".join(w["text"] for w in row if 315 <= w["x0"] < 395)
            narration = re.sub(r"\s+", " ", narration).strip()
            if not narration:
                narration = reference

            seq += 1
            records.append({
                "seq": seq,
                "source_file": pdf_name,
                "statement_from": st_from,
                "statement_to": st_to,
                "date": transaction_date,
                "value_date": value_date,
                "narration": narration[:500],
                "reference": reference[:200],
                "direction": "Receipt" if deposit > 0 else "Payment",
                "withdrawal": to_float(withdrawal),
                "deposit": to_float(deposit),
                "amount": to_float(deposit if deposit > 0 else withdrawal),
                "closing_balance": to_float(closing_balance),
                "bank": bank,
            })
    return records


_ICICI_PAGE_NUMBER_BEFORE_HEADER_RE = re.compile(
    r"^[ \t]*\d{1,3}[ \t]*\n(?=[ \t]*Transaction\s+Withdrawal\s+Deposit\s+Balance)",
    re.M | re.I,
)


def _strip_icici_noise(text):
    # The retail template prints a bare page-number line (just "2", "3",
    # ...) directly above the repeated column-header block at the top of
    # every page after the first. That has to be recognised *before* the
    # header-block lines below are stripped out line-by-line - once they
    # are gone, the digit is no longer "immediately followed by" the
    # header text and this can no longer tell it apart from a genuine
    # bare-digit narration fragment. And it has to stay this narrow:
    # ICICI's own BBPS/UPI reference numbers routinely wrap so that a
    # lone 1-3 digit tail lands on its own line too (e.g.
    # ".../Maharashtr/00159" then "3" on the next line alone) - blindly
    # dropping every standalone digit line would eat that real narration.
    text = _ICICI_PAGE_NUMBER_BEFORE_HEADER_RE.sub("", text)

    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if BOILER_RE.search(stripped) or _ICICI_NOISE_RE.search(stripped) or _HEADER_NAME_RE.match(stripped):
            continue
        kept.append(stripped)
    return "\n".join(kept)


def _reorder_wrapped_label_lines(text):
    """Swap each transaction's leading remarks label back onto its own row.

    The retail template prints the two-line "Transaction Remarks" cell
    with the bold type label (e.g. "Bil Payment", "NACH trxn", a UPI
    handle) as its FIRST line and the wrapped reference/detail text as
    the line(s) after - but in the flattened PDF text, that label line
    sits immediately BEFORE the row's own "<S No> <Date> <Withdrawal/
    Deposit> <Balance>" line, not after it:

        Bil Payment
        1 03.04.2025 299.00 10456.69
        BIL/NUCL/000992983591/Recharge/NUCLEI service

    Left alone, the generic engine's date-anchored grouping reads that
    label line while the *previous* transaction is still the open
    "current" row, so it gets appended to that row's continuation/
    narration instead of the transaction it actually describes - every
    row's narration ends up carrying the *next* row's label, and the
    very first row picks up whatever boilerplate preceded it. Swapping
    each label line with the date line immediately following it - a
    single adjacent-pair swap, since this template never wraps the
    label itself onto a second line - puts every label back with the
    row it belongs to before any date-based grouping ever runs.
    """
    lines = text.split("\n")
    for i in range(len(lines) - 1):
        if not DATE_START_RE.match(lines[i]) and DATE_START_RE.match(lines[i + 1]):
            lines[i], lines[i + 1] = lines[i + 1], lines[i]
    return "\n".join(lines)


def build_icici_transactions(text, pdf_name, bank, st_from, st_to, opening_balance, seq_start):
    # Everything from the closing "Sincerely," onward is the abbreviation
    # legend ("VAT - Cash Withdrawal...", "RTGS - Real Time Gross
    # Settlement...") that follows the transaction table on the retail
    # savings-account template, not more rows - left in, its own
    # numeric-looking fragments get glued onto the last real
    # transaction as noise. The current/OD "Detailed Statement"
    # template has no such sign-off; it runs into a one-time "Legends
    # Used in Account Statement" glossary instead, cut the same way.
    #
    # Deliberately NOT truncating on "Page Total" even though it also
    # marks the very end of this sample: the label says "Page" for a
    # reason - a running per-page subtotal block on a real multi-page
    # OD statement would recur on every page, and cutting the text at
    # its first occurrence would silently discard almost the entire
    # statement instead of just the trailing summary. It's still
    # filtered out (wherever it occurs) as a noise line below.
    #
    # `Sincer(?:e)?ly` also matches this export's own "Sincerly," -
    # ICICI's actual PDF has that typo (missing the middle "e"), not the
    # dictionary spelling; matching only "Sincerely" left this whole
    # sign-off + legend glossary completely untruncated, so the entire
    # "RCHG - Recharge / BPAY - Bill payment / ..." abbreviation table
    # ran straight into the last real transaction's continuation as
    # bogus narration. Likewise the retail template's own glossary
    # heading reads "Legends for transactions in your Account
    # Statement", not the current/OD template's "Legends Used in
    # Account Statement" - both are matched here since either can appear
    # depending on which export this is.
    end_match = re.search(
        r"\bSincer(?:e)?ly\s*,|"
        r"\bLegends\s+(?:Used\s+in|for\s+transactions\s+in\s+your)\s+Account\s+Statement\b",
        text,
        re.I,
    )
    if end_match:
        text = text[: end_match.start()]

    # Symmetrically, everything up to and including the first printed
    # occurrence of the retail template's own column-header block (the
    # transaction table's actual start) is the statement title, account
    # holder name/address, and branch block - never a transaction. Left
    # in, it's whatever happens to still be in the generic engine's
    # bounded `pending` buffer (its last few lines only) that gets
    # glued onto the very first real row as bogus leading narration.
    # Slicing on the header text itself (rather than trying to
    # enumerate every possible holder name/address shape) makes this
    # robust to any customer's details.
    start_match = re.search(r"Date\s+Amount\s*\(INR\)\s+Amount\s*\(INR\)\s+\(INR\)", text, re.I)
    if start_match:
        text = text[start_match.end():]

    text, od_row_count = _ICICI_OD_ROW_PREFIX_RE.subn(r"\1 ", text)
    # The current/OD "Detailed Statement" template prints a *drawn*
    # balance rather than an available-funds balance: a withdrawal
    # increases it and a deposit decreases it, backwards from every
    # ordinary account statement (confirmed against this template's own
    # printed Opening/Closing Bal reconciliation - see icici_parser
    # module tests). Whether the Tran-Id-prefix substitution above
    # actually fired is a reliable signal for this specific template,
    # since no other ICICI layout has that row shape.
    is_od_layout = od_row_count > 0
    cleaned = _strip_icici_noise(text)
    if not is_od_layout:
        # The label-before-date line swap is specific to the retail
        # template's own printed layout (see `_reorder_wrapped_label_lines`)
        # - the current/OD template's rows are single physical lines with
        # no separate leading label line, so there is nothing to reorder
        # there, and running it anyway would risk reshuffling genuine
        # narration in a template this function was never validated
        # against.
        cleaned = _reorder_wrapped_label_lines(cleaned)
    return build_transactions(
        cleaned,
        pdf_name,
        bank,
        st_from,
        st_to,
        opening_balance,
        seq_start,
        amount_search_scope="full",
        invert_delta_sign=is_od_layout,
    )
