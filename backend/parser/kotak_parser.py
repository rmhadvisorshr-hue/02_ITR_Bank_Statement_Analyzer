"""Kotak Mahindra Bank statement parser.

Kotak's own export has (at least) two different row shapes seen in the
wild:

  Layout A (older template) - the row starts with the serial number
  immediately followed by the date:
      "1  03 May 2025  Some narration ...  4,970.96  34,298.87"

  Layout B (current-account "Account Statement" template, e.g.
  "KOTAK APRIL 25 BS.pdf") - the "Sl. No." cell is narrow enough that
  pdfplumber emits it as its own standalone line, and the Date column
  is physically positioned *after* Amount/Dr-Cr, so it lands in the
  middle of the row's text, not at the start:
      "1"
      "KOTAKPAYOUT-0792089A0152929-030525FOS2512378319858  4,970.96  CR  03/05/2025  34,298.87  CR"

  A row-start matcher anchored on "<serial> <date>" (Layout A) never
  matches Layout B at all, so every single transaction in a Layout-B
  statement was silently dropped (0 rows parsed, whole file fails).
  This module tries Layout A first (unchanged, so any statement that
  already worked keeps working byte-for-byte) and falls back to a
  Layout-B parser - anchored on the "<amount> <CR|DR> <date> <balance>
  <CR|DR>" tail that Layout B always prints at the end of a row -
  only when Layout A finds nothing.
"""

import re
from decimal import Decimal

from .generic_parser import (
    BOILER_RE,
    AMOUNT_RE,
    parse_amount,
    parse_any_date,
    money_close,
    to_float,
)

_LAYOUT_A_DATE_RE = re.compile(r"^\d+\s+(\d{2}\s+[A-Za-z]{3}\s+\d{4}|\d{2}/\d{2}/\d{4})")
_SERIAL_ONLY_RE = re.compile(r"^\d{1,5}$")
# The "Sl. No. | Date | Description | ..." column header repeats on every
# page of a Layout-B statement; without filtering it, whichever
# transaction happens to start right after a page break would swallow it
# into its narration.
_LAYOUT_B_HEADER_RE = re.compile(
    r"^Sl\.?\s*No\.?\s+Date\s+Description\b|"
    r"^(?:Statement of Transactions|Account Statement|Account Branch|Cust\.?\s*Reln\.?\s*No)\b",
    re.I,
)
_LAYOUT_B_TAIL_RE = re.compile(
    r"(?P<amount>-?\d{1,3}(?:,\d{2,3})*\.\d{2})\s*(?P<sign>CR|DR)\s+"
    r"(?P<date>\d{2}[-/]\d{2}[-/]\d{4}|\d{2}\s+[A-Za-z]{3}\s+\d{4})\s+"
    r"(?P<balance>-?\d{1,3}(?:,\d{2,3})*\.\d{2})\s*(?P<bsign>CR|DR)\s*$",
    re.I,
)
# Layout C - another "Account Statement" template variant where pdfplumber's
# y-position-based line grouping splits each row differently again: the
# amount/Dr-Cr/balance/Dr-Cr tail lands on its OWN line (no date on it at
# all), and the "<serial> <date>" pair that belongs to it prints on the
# *next* line, e.g.:
#     KOTAKPAYOUT- FOS2512378319858  4,970.96 CR  34,298.87 CR
#     1  03/05/2025
#     0792089A0152929-030525
# - i.e. identical to Layout B's tail except there's no date baked into it.
_LAYOUT_C_TAIL_RE = re.compile(
    r"(?P<amount>-?\d{1,3}(?:,\d{2,3})*\.\d{2})\s*(?P<sign>CR|DR)\s+"
    r"(?P<balance>-?\d{1,3}(?:,\d{2,3})*\.\d{2})\s*(?P<bsign>CR|DR)\s*$",
    re.I,
)
_LAYOUT_C_SERIAL_DATE_RE = re.compile(
    r"^\d+\s+(\d{2}[-/]\d{2}[-/]\d{4}|\d{2}\s+[A-Za-z]{3}\s+\d{4})\s*$"
)


def _build_layout_a(lines, pdf_name, bank, st_from, st_to, opening_balance, seq_start):
    txs = []
    current = None
    for line in lines:
        if BOILER_RE.search(line):
            continue
        match = _LAYOUT_A_DATE_RE.match(line)
        if match:
            if current:
                txs.append(current)
            current = {"line": line, "date": match.group(1), "continuation": []}
        elif current:
            current["continuation"].append(line)
    if current:
        txs.append(current)

    records = []
    balance = opening_balance
    seq = seq_start
    for tx in txs:
        line = tx["line"]
        amount_matches = list(AMOUNT_RE.finditer(line))
        if len(amount_matches) < 2:
            continue
        closing = parse_amount(amount_matches[-1].group(0))
        printed_amount = parse_amount(amount_matches[-2].group(0))
        if balance is None:
            balance = closing
        delta = closing - balance
        withdrawal = Decimal("0.00")
        deposit = Decimal("0.00")
        if money_close(delta, printed_amount):
            deposit = printed_amount
        elif money_close(delta, -printed_amount):
            withdrawal = printed_amount
        elif printed_amount < 0:
            withdrawal = abs(printed_amount)
        elif delta >= 0:
            deposit = printed_amount
        else:
            withdrawal = printed_amount

        raw_text = " ".join([line] + tx["continuation"])
        date_match = re.search(r"\d{2}\s+[A-Za-z]{3}\s+\d{4}|\d{2}/\d{2}/\d{4}", raw_text)
        cut = date_match.end() if date_match else 0
        narration = raw_text[cut:]
        for m in reversed(amount_matches):
            narration = narration.replace(m.group(0), " ", 1)
        narration = re.sub(r"\s+", " ", narration).strip(" -")
        narration = re.sub(r"^\d+\s+", "", narration)

        seq += 1
        records.append({
            "seq": seq,
            "source_file": pdf_name,
            "statement_from": st_from,
            "statement_to": st_to,
            "date": parse_any_date(tx["date"]),
            "value_date": parse_any_date(tx["date"]),
            "narration": narration[:500],
            "reference": "",
            "direction": "Receipt" if deposit > 0 else "Payment",
            "withdrawal": to_float(withdrawal),
            "deposit": to_float(deposit),
            "amount": to_float(deposit if deposit > 0 else withdrawal),
            "closing_balance": to_float(closing),
            "bank": bank,
        })
        balance = closing
    return records


def _build_layout_b(lines, pdf_name, bank, st_from, st_to, opening_balance, seq_start):
    records = []
    seq = seq_start
    buffer_lines = []

    def flush(buffer_lines, tail_match):
        nonlocal seq
        date = parse_any_date(tail_match.group("date"))
        amount = parse_amount(tail_match.group("amount"))
        balance = parse_amount(tail_match.group("balance"))
        if tail_match.group("bsign").upper() == "DR" and balance > 0:
            balance = -balance
        deposit = amount if tail_match.group("sign").upper() == "CR" else Decimal("0.00")
        withdrawal = amount if tail_match.group("sign").upper() == "DR" else Decimal("0.00")

        narration = " ".join(buffer_lines)
        narration = re.sub(r"\s+", " ", narration).strip(" -")

        seq += 1
        records.append({
            "seq": seq,
            "source_file": pdf_name,
            "statement_from": st_from,
            "statement_to": st_to,
            "date": date,
            "value_date": date,
            "narration": narration[:500],
            "reference": "",
            "direction": "Receipt" if deposit > 0 else "Payment",
            "withdrawal": to_float(withdrawal),
            "deposit": to_float(deposit),
            "amount": to_float(deposit if deposit > 0 else withdrawal),
            "closing_balance": to_float(balance),
            "bank": bank,
        })

    for line in lines:
        if BOILER_RE.search(line) or _SERIAL_ONLY_RE.match(line) or _LAYOUT_B_HEADER_RE.search(line):
            # Boilerplate, a standalone "Sl. No." cell that pdfplumber
            # split onto its own line, or the repeating column header -
            # none of these carry narration content.
            continue
        tail_match = _LAYOUT_B_TAIL_RE.search(line)
        if tail_match:
            buffer_lines.append(line[: tail_match.start()])
            flush(buffer_lines, tail_match)
            buffer_lines = []
        else:
            buffer_lines.append(line)
    return records


def _build_layout_c(lines, pdf_name, bank, st_from, st_to, opening_balance, seq_start):
    # First pass: group lines into one block per tail-line, keeping every
    # other line (whether it precedes or - as with the "<serial> <date>"
    # line and any further description wrap - follows the tail) as part of
    # that block, since Layout C's date line sits *in the middle* of a
    # transaction's narration rather than at either end of it.
    blocks = []
    current = None
    for line in lines:
        if BOILER_RE.search(line) or _SERIAL_ONLY_RE.match(line) or _LAYOUT_B_HEADER_RE.search(line):
            continue
        tail_match = _LAYOUT_C_TAIL_RE.search(line)
        if tail_match:
            if current:
                blocks.append(current)
            current = {"prefix": line[: tail_match.start()], "tail": tail_match, "rest": []}
        elif current:
            current["rest"].append(line)
    if current:
        blocks.append(current)

    records = []
    seq = seq_start
    for block in blocks:
        date_str = None
        narration_parts = [block["prefix"]]
        for line in block["rest"]:
            if date_str is None:
                serial_match = _LAYOUT_C_SERIAL_DATE_RE.match(line)
                if serial_match:
                    date_str = serial_match.group(1)
                    continue
            narration_parts.append(line)
        if date_str is None:
            # No "<serial> <date>" line ever showed up for this block - it
            # can't be dated, so it's dropped rather than guessed at.
            continue

        tail = block["tail"]
        date = parse_any_date(date_str)
        amount = parse_amount(tail.group("amount"))
        balance = parse_amount(tail.group("balance"))
        if tail.group("bsign").upper() == "DR" and balance > 0:
            balance = -balance
        deposit = amount if tail.group("sign").upper() == "CR" else Decimal("0.00")
        withdrawal = amount if tail.group("sign").upper() == "DR" else Decimal("0.00")

        narration = " ".join(narration_parts)
        narration = re.sub(r"\s+", " ", narration).strip(" -")

        seq += 1
        records.append({
            "seq": seq,
            "source_file": pdf_name,
            "statement_from": st_from,
            "statement_to": st_to,
            "date": date,
            "value_date": date,
            "narration": narration[:500],
            "reference": "",
            "direction": "Receipt" if deposit > 0 else "Payment",
            "withdrawal": to_float(withdrawal),
            "deposit": to_float(deposit),
            "amount": to_float(deposit if deposit > 0 else withdrawal),
            "closing_balance": to_float(balance),
            "bank": bank,
        })
    return records


def build_kotak_transactions(text, pdf_name, bank, st_from, st_to, opening_balance, seq_start):
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    records = _build_layout_a(lines, pdf_name, bank, st_from, st_to, opening_balance, seq_start)
    if records:
        return records

    records = _build_layout_b(lines, pdf_name, bank, st_from, st_to, opening_balance, seq_start)
    if records:
        return records

    return _build_layout_c(lines, pdf_name, bank, st_from, st_to, opening_balance, seq_start)
