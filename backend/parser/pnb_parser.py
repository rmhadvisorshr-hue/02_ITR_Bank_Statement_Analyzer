"""Punjab National Bank statement parser.

Table shape: `Date | Instrument ID | Amount(INR) | Type (DR/CR) | Balance |
Remarks`, with the row's own Date/Amount/Type/Balance always on one
physical line and the (often long UPI) Remarks wrapping onto the next
1-2 lines with no leading date of their own:

    04/03/2026 80.0 DR 3670.97 UPI/DR/119482308510/JAGATPAL/YESB/paytmq
    r28100505/

Direction is printed explicitly as DR/CR so - unlike the generic
engine - it never needs to be guessed from a running-balance delta.
"""

import re
from decimal import Decimal

from .generic_parser import BOILER_RE, parse_amount, parse_any_date, to_float

_PNB_ROW_RE = re.compile(
    r"^(\d{2}[-/]\d{2}[-/]\d{4})\s+"
    r"(-?[\d,]+\.\d{1,2})\s+"
    r"(DR|CR)\s+"
    r"(-?[\d,]+\.\d{1,2})\s*"
    r"(.*)$",
    re.I,
)


def build_pnb_transactions(text, pdf_name, bank, st_from, st_to, opening_balance, seq_start):
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    txs = []
    current = None
    for line in lines:
        if BOILER_RE.search(line):
            continue
        match = _PNB_ROW_RE.match(line)
        if match:
            if current:
                txs.append(current)
            current = {"match": match, "continuation": []}
        elif current:
            current["continuation"].append(line)
    if current:
        txs.append(current)

    records = []
    seq = seq_start
    for tx in txs:
        match = tx["match"]
        date, amount_raw, sign, balance_raw, narration_tail = match.groups()

        amount = parse_amount(amount_raw)
        balance = parse_amount(balance_raw)
        is_credit = sign.upper() == "CR"
        withdrawal = Decimal("0.00") if is_credit else amount
        deposit = amount if is_credit else Decimal("0.00")

        narration = " ".join([narration_tail] + tx["continuation"])
        narration = re.sub(r"\s+", " ", narration).strip(" -")

        seq += 1
        records.append(
            {
                "seq": seq,
                "source_file": pdf_name,
                "statement_from": st_from,
                "statement_to": st_to,
                "date": parse_any_date(date),
                "value_date": parse_any_date(date),
                "narration": narration[:500],
                "reference": "",
                "direction": "Receipt" if is_credit else "Payment",
                "withdrawal": to_float(withdrawal),
                "deposit": to_float(deposit),
                "amount": to_float(amount),
                "closing_balance": to_float(balance),
                "bank": bank,
            }
        )
    return records
