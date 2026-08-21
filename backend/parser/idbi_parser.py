"""IDBI Bank statement parser.

IDBI's own PDF export is internally inconsistent: most rows print the
"Txn Date" cell as `DD/MM/YYYY HH:MM:SS AM/PM`, but a scattered subset of
rows (interest credits, sweep transfers, card-fee debits - roughly one
in twenty, seemingly whichever row a particular back-end job wrote)
instead print a raw `YYYY-MM-DD HH:MM:SS.mmm` timestamp straight out of
the core-banking database, e.g.:

    31/03/2026 09:55:59 AM 31/03/2026 UPI/...                 <- normal
    2026-03-10 11:34:12.000 10/03/2026 NEFT-IN...-POONAM ...  <- quirky

`generic_parser.DATE_START_RE` / `DATE_TOKEN_RE` only recognise
DD-first date tokens, so a naive scan silently fails to see the quirky
row as the start of a new transaction. It used to get appended as a
"continuation" of the *previous* row, which then absorbed its amount
and closing-balance figures - silently corrupting the previous
transaction's amount and balance, and dropping the quirky transaction
entirely (no error surfaced anywhere). This parser recognises both
date shapes as valid row starts so every transaction gets its own
record with its own real amount/balance.
"""

import re
from decimal import Decimal

from .generic_parser import (
    BOILER_RE,
    DATE_TOKEN_RE,
    AMOUNT_RE,
    parse_amount,
    parse_any_date,
    money_close,
    to_float,
)

# `YYYY-MM-DD HH:MM:SS[.mmm]` - the core-banking timestamp quirk described above.
ISO_DATETIME_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?\b")
# Same shape, anchored to the start of a line (optionally after a leading
# serial-number column) - used to decide whether a line begins a new row.
ISO_DATETIME_START_RE = re.compile(r"^\s*(?:\d+\s+)?\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}", re.I)
DATE_START_RE = re.compile(
    r"^\s*(?:\d+\s+)?'?(?:\d{2}[-/.]\d{2}[-/.]\d{4}|\d{2}[-/.]\d{2}[-/.]\d{2}|\d{2}[-/][A-Za-z]{3}[-/]\d{4}|\d{2}\s+[A-Za-z]{3}\s+\d{4})\b",
    re.I,
)

# IDBI's "Customer Account Ledger Report" export repeats a per-page
# carry-forward block at every page break: a dashed rule, that page's own
# Credit/Debit subtotals, and the next page's re-stated running balance
# ("B/F Balance"), sandwiched between report/branch header lines. None of
# these start with a date, so `_is_row_start` never recognises them as
# their own row - they silently land in whatever transaction happens to
# still be "open" as bogus continuation instead, and its "Page Total
# Debit"/"B/F Balance" figures are exactly amount-shaped enough to be
# picked up as if they were that transaction's own amount/closing-balance,
# corrupting both. Filtering them out here (before `txs` gets built, same
# principle as every other bank parser's own noise stripping) keeps them
# from ever reaching a transaction's continuation at all.
_IDBI_NOISE_RE = re.compile(
    r"(^-{5,}$|^Page\s+Total\s+(?:Credit|Debit)\s*:|^B/F\s+Balance\s*:|"
    r"^REP31\s+Customer\s+Account\s+Ledger\s+Report|^Service\s+OutLet\s*:|"
    r"^Account\s+No\s*:|^Gl\s+Sub\s+Head\s+Code\s*:|^Peg\s+Review\s+date\s*:|"
    # The report's own final grand-total block, printed once after the
    # very last transaction with no further row to close it off: a
    # repeated column-header line, then the statement's own closing
    # balance and Total Credit/Debit figures. Left unfiltered, these are
    # the only noise lines in the whole export with nothing after them to
    # end the last transaction's continuation, so they get glued onto it
    # and its grand Total Credit/Debit are mistaken for that one row's own
    # amount and closing balance.
    r"^GL\.\s+Value\s+Tran\s+Id|^Date\s+Date\s+Number\s+Debit\s+Amount|"
    r"^Closing\s+Balance\s*:|^Total\s+Credit\s*:|^Total\s+Debit\s*:|"
    r"^Signature\s*:?$)",
    re.I,
)


def _is_row_start(line):
    return bool(DATE_START_RE.match(line) or ISO_DATETIME_START_RE.match(line))


def _date_matches(line):
    """All date-like tokens in `line`, in left-to-right order, regardless
    of whether they're the standard DD/MM/YYYY shape or the ISO quirk."""
    matches = list(DATE_TOKEN_RE.finditer(line)) + list(ISO_DATETIME_RE.finditer(line))
    matches.sort(key=lambda m: m.start())
    return matches


def _date_value(match):
    if match.re is ISO_DATETIME_RE:
        return match.group(1)  # already YYYY-MM-DD
    return parse_any_date(match.group(0))


def build_idbi_transactions(text, pdf_name, bank, st_from, st_to, opening_balance, seq_start):
    # IDBI's "STATEMENT OF ACCOUNT" export (a different template from the
    # "REP31 Customer Account Ledger Report" one - see `_IDBI_NOISE_RE`
    # above) ends with a "Statement Summary :" block: Dr/Cr counts and
    # Debit/Credit grand totals, immediately followed by a "Legends for
    # transactions..." glossary. There is no further transaction row after
    # it to end the last row's continuation, so - left in - its own
    # Debit/Credit total figures get glued on and mistaken for that last
    # transaction's own amount and closing balance. Truncating here, the
    # same way the ledger-report template's own tail is filtered line by
    # line above, removes it before it can ever reach a transaction.
    end_match = re.search(r"Statement\s+Summary\s*:", text, re.I)
    if end_match:
        text = text[: end_match.start()]

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    txs = []
    current = None

    for line in lines:
        if BOILER_RE.search(line) or _IDBI_NOISE_RE.search(line):
            continue
        if _is_row_start(line):
            if current:
                txs.append(current)
            current = {"line": line, "continuation": []}
        elif current:
            current["continuation"].append(line)
    if current:
        txs.append(current)

    records = []
    balance = opening_balance
    seq = seq_start

    for tx in txs:
        raw_text = " ".join([tx["line"]] + tx["continuation"])

        date_matches = _date_matches(tx["line"])
        if not date_matches:
            continue
        tx_date = _date_value(date_matches[0])
        value_date = _date_value(date_matches[1]) if len(date_matches) > 1 else tx_date

        amount_matches = list(AMOUNT_RE.finditer(raw_text))
        if len(amount_matches) < 2:
            continue

        closing = parse_amount(amount_matches[-1].group(0))
        amount = parse_amount(amount_matches[-2].group(0))

        debit = Decimal("0.00")
        credit = Decimal("0.00")

        upper = raw_text.upper()
        credit_words = ["CR", "REC", "RECEIPT", "DEPOSIT", "BY CASH", "CASH RECEIPT", "NEFT/", "UPI/CR"]
        debit_words = ["DR", "PAY", "WDL", "WITHDRAW", "CHARG", "GST", "POS", "DEBIT", "FT - DR"]

        if any(w in upper for w in credit_words) and not any(w in upper for w in [" DR", "/DR/", "FT - DR"]):
            credit = amount
        elif any(w in upper for w in debit_words):
            debit = amount
        else:
            credit = amount

        cut = date_matches[-1].end() if date_matches else 0
        narration = raw_text[cut:]
        for m in reversed(amount_matches):
            narration = narration.replace(m.group(0), " ", 1)
        narration = re.sub(r"(?:Dr|Cr)", " ", narration, flags=re.I)
        narration = re.sub(r"\s+", " ", narration).strip(" -")
        if not narration:
            narration = re.sub(r"\s+", " ", raw_text).strip()

        seq += 1
        records.append(
            {
                "seq": seq,
                "source_file": pdf_name,
                "statement_from": st_from,
                "statement_to": st_to,
                "date": tx_date,
                "value_date": value_date,
                "narration": narration[:500],
                "reference": "",
                "withdrawal": to_float(debit),
                "deposit": to_float(credit),
                "amount": to_float(credit if credit > 0 else debit),
                "closing_balance": to_float(closing),
                "bank": bank,
            }
        )

    if len(records) > 1 and records[0]["date"] > records[-1]["date"]:
        records.reverse()

    for i in range(len(records)):
        r = records[i]
        prev_bal = balance if i == 0 else Decimal(str(records[i - 1]["closing_balance"]))
        closing = Decimal(str(r["closing_balance"]))
        amount = Decimal(str(r["amount"]))
        if prev_bal is not None:
            delta = closing - prev_bal
            if money_close(delta, amount):
                r["deposit"] = to_float(amount)
                r["withdrawal"] = 0.0
            elif money_close(delta, -amount):
                r["withdrawal"] = to_float(amount)
                r["deposit"] = 0.0
            elif amount < 0:
                r["withdrawal"] = to_float(abs(amount))
                r["deposit"] = 0.0
                r["amount"] = to_float(abs(amount))
            elif delta >= 0:
                r["deposit"] = to_float(amount)
                r["withdrawal"] = 0.0
            else:
                r["withdrawal"] = to_float(amount)
                r["deposit"] = 0.0
        r["direction"] = "Receipt" if r["deposit"] > 0 else "Payment"
        balance = closing

    return records
