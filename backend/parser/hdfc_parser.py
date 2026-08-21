"""HDFC Bank statement parser.

Handles two distinct HDFC document types seen in practice:
  1. Regular savings/current account statements
     ("Date Narration Chq./Ref.No. Value Dt Withdrawal Amt. Deposit Amt. Closing Balance").
  2. Personal Loan EMI schedules ("Instl No Due Date Instl Amt Principal
     Interestl O/s Principal") - these aren't account statements at all,
     they're amortisation tables, so they get their own row grammar.

This module is a straight relocation of the logic that used to live in
extract_bank.py - behaviour is unchanged.
"""

import re
import datetime as dt
from decimal import Decimal

from .generic_parser import (
    BOILER_RE,
    parse_amount,
    parse_any_date,
    money_close,
    to_float,
)

HDFC_DATE_START_RE = re.compile(r"^(\d{2}/\d{2}/\d{2})\s+(.*)$")
HDFC_VALUE_DATE_RE = re.compile(r"\b\d{2}/\d{2}/\d{2}\b")
HDFC_AMOUNT_RE = re.compile(r"(?<!\w)-?\d{1,3}(?:,\d{3})*\.\d{2}-?")
HDFC_REF_RE = re.compile(r"^(?P<narration>.+?)\s+(?P<reference>(?:[A-Z]{2,}\d[A-Z0-9]*|\d{10,}|[A-Z0-9]{12,}|CDT\d{8,}))$")
HDFC_NOISE_RE = re.compile(
    r"(HDFC BANK LIMITED|\*Closing balance includes|Contents of this statement|"
    r"Page\s*No\s*\.?:|Account Branch\s*:|Registered Office Address|"
    r"State account branch GSTN|HDFC Bank GSTIN|https://www\.hdfcbank\.com|Generated On:|"
    r"Cust\s*ID\s*:|A/C\s*Open\s*Date\s*:|Nomination\s*:|Account\s*No\s*:|"
    r"Address\s*:|Email\s*:|Phone\s*:|Phone\s+no\.?\s*:|JOINT HOLDERS|OD Limit|Currency\s*:|"
    r"From\s*:|To\s*:|Date\s+Narration|Chq\./Ref\.No\.|Value\s+Dt|"
    r"Withdrawal\s+Amt\.|Deposit\s+Amt\.|Closing\s+Balance|STATEMENT\s+SUMMARY|"
    r"\bCity\s*:|\bBranch\s+Code\s*:|\bM/S\.|\bC/O\b|\bthe address on this statement\b|"
    r"\bthis statement\b|\bnot require signature\b|"
    r"Account\s+Status\s*:|Account\s+Type\s*:)",
    re.I,
)
# The customer name/address lines HDFC repeats in the account-info box on
# every page (e.g. "MRS NAYNA VASANT HARIYA" / "FLAT NO 302 GANESH KRUPA
# BULIDING" / branch lines like "GURU NANAK NAGAR,AMBADI ROAD,") have no
# fixed wording - each statement carries a different customer's and
# branch's own address. Hardcoding one customer's street name here (as a
# previous fix did for "GURU NANAK"/"MAHAVIR DHAM"/"NALLASOPARA"/"VASAI
# (WEST)") only ever matches that one account and silently leaves every
# other customer's address to fall through as narration continuation text -
# see PAGE_FOOTER_START_RE below for the general, content-independent fix.
# A courtesy-titled name line ("MRS X Y Z", "MR X Y", "M/S ABC ENTERPRISES")
# is still recognisable by shape alone, so it's kept as a narrow
# whole-line pattern for the (rarer) case where that name line isn't
# already covered by the page-footer zone.
HDFC_NAME_LINE_RE = re.compile(r"^(?:MRS?|MS|M/S\.?|SHRI|SMT|DR)\.?\s+[A-Z][A-Z .&']*$")
# HDFC repeats the whole "Account Branch / customer name & address / Cust
# ID / IFSC / ..." box around every page break, stamped with a "Page No
# .: N" marker. Everything from that marker up to the next real
# transaction row is that box - regardless of the customer's own address
# text, which is why this is a *zone*, not another line-content pattern
# to enumerate. See build_hdfc_transactions.
PAGE_FOOTER_START_RE = re.compile(r"Page\s*No\s*\.?\s*:", re.I)


def parse_hdfc_date(raw):
    try:
        return dt.datetime.strptime(raw, "%d/%m/%y").date().isoformat()
    except ValueError:
        return parse_any_date(raw)


def is_hdfc_noise(line):
    if HDFC_NOISE_RE.search(line) or BOILER_RE.search(line) or HDFC_NAME_LINE_RE.match(line):
        return True
    upper = line.upper()
    profile_tokens = [
        "PALGHAR",
        "MAHARASHTRA",
        "CUST ID",
        "NOMINATION",
        "REGISTERED",
        "A/C OPEN DATE",
        "IFSC",
        "MICR",
    ]
    return any(token in upper for token in profile_tokens) and not HDFC_DATE_START_RE.match(line)


def split_hdfc_narration_reference(rest):
    rest = re.sub(r"\s+", " ", rest).strip()
    rest = re.sub(r"[\s.-]+$", "", rest)
    match = HDFC_REF_RE.match(rest)
    if match:
        return match.group("narration").strip(), match.group("reference").strip()

    parts = rest.rsplit(" ", 1)
    if len(parts) == 2 and re.fullmatch(r"[A-Z0-9]{10,}", parts[1]):
        return parts[0].strip(), parts[1].strip()
    return rest, ""


def build_hdfc_transactions(text, pdf_name, bank, st_from, st_to, opening_balance, seq_start, issues):
    start = text.find("Date Narration")
    end_match = re.search(r"STATEMENT\s*SUMMARY\s*:-", text, re.I)
    end = end_match.start() if end_match else len(text)
    block = text[start:end] if start != -1 else text[:end]

    txs = []
    current = None
    # True while walking the repeated "Account Branch / customer name &
    # address / Cust ID / IFSC / ..." box HDFC stamps around every page
    # break. Its content is different per statement (a different
    # customer's, possibly different branch's, address each time), so it
    # can't be matched by wording the way the rest of is_hdfc_noise's
    # boilerplate can - instead this tracks *position*: everything from a
    # "Page No .: N" marker up to the next real transaction row is that
    # box, no matter what it says. Without this, whichever address lines
    # is_hdfc_noise doesn't happen to recognise fall through as
    # "continuation" text and get appended onto the narration of
    # whichever transaction was last open - silently corrupting the
    # narration (though not the amounts - see the start_line-preferring
    # amount/date search below, which never reads this far) of the last
    # row on every page.
    in_page_footer = False
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("Date Narration"):
            continue
        match = HDFC_DATE_START_RE.match(line)
        if match:
            in_page_footer = False
            if current:
                txs.append(current)
            current = {"start_line": line, "date": match.group(1), "cont": []}
            continue
        if PAGE_FOOTER_START_RE.search(line):
            in_page_footer = True
        if in_page_footer or is_hdfc_noise(line):
            continue
        if current:
            current["cont"].append(line)
    if current:
        txs.append(current)

    records = []
    balance = opening_balance
    seq = seq_start
    for tx in txs:
        start_line = tx["start_line"]
        full_line = start_line + " " + " ".join(tx["cont"])

        # Prefer the value date and amounts found on the row's own physical
        # line: HDFC always prints Ref/Value Dt/Withdrawal Amt/Deposit Amt/
        # Closing Balance there in one go. Only reach into the wrapped
        # narration continuation when that line doesn't already have
        # enough numbers - continuation text is free-form UPI/merchant
        # reference text that can itself contain a numeric-looking
        # fragment (e.g. a VPA tail like "9.06106501@INDUS...") that is NOT
        # an additional amount column. Counting one as a genuine 3rd number
        # fabricates a "both withdrawal and deposit populated" row out of
        # thin air and silently replaces the real closing balance with
        # whatever that fragment happens to look like.
        line_dates = list(HDFC_VALUE_DATE_RE.finditer(start_line))
        line_amount_matches = (
            list(HDFC_AMOUNT_RE.finditer(start_line[line_dates[-1].end() :]))
            if len(line_dates) >= 2
            else []
        )

        if len(line_dates) >= 2 and len(line_amount_matches) >= 2:
            all_dates = line_dates
            amount_matches = line_amount_matches
        else:
            all_dates = list(HDFC_VALUE_DATE_RE.finditer(full_line))
            if len(all_dates) < 2:
                issues.append({"file": pdf_name, "issue": "could not find HDFC value date", "line": full_line})
                continue
            amount_matches = list(HDFC_AMOUNT_RE.finditer(full_line[all_dates[-1].end() :]))

        value_date = all_dates[-1].group(0)
        # `amount_matches` may have been found against the narrower
        # `start_line` slice above, but its offsets stay valid against
        # `trailing` too: `start_line` is a strict prefix of `full_line`, so
        # slicing both from the same `all_dates[-1].end()` offset yields one
        # string that is itself a prefix of the other.
        trailing = full_line[all_dates[-1].end() :]
        if len(amount_matches) < 2:
            issues.append({"file": pdf_name, "issue": "could not find HDFC amounts", "line": full_line})
            continue

        closing = parse_amount(amount_matches[-1].group(0))
        # If the PDF never states an explicit opening balance, `balance`
        # arrives as None for the very first transaction we process. Seeding
        # it from this row's own closing balance (so every later row has a
        # real previous balance to diff against) makes `delta` trivially 0
        # for *this* row - which used to fall straight into the
        # `delta >= 0 -> deposit` branch below and silently mislabel a
        # genuine debit as a credit, with nothing anywhere to say so.
        opening_was_unknown = balance is None
        if balance is None:
            balance = closing

        rest = full_line[len(tx["date"]) : all_dates[-1].start()].strip()
        narration, reference = split_hdfc_narration_reference(rest)

        after_amounts = trailing[amount_matches[-1].end():].strip()
        full_narration = re.sub(r"\s+", " ", f"{narration} {after_amounts}".strip()).strip(" .-")

        if len(amount_matches) >= 3:
            withdrawal = parse_amount(amount_matches[-3].group(0))
            deposit = parse_amount(amount_matches[-2].group(0))
        else:
            printed_amount = parse_amount(amount_matches[-2].group(0))
            # HDFC sometimes prints this single amount with a leading minus
            # sign to denote a same-day reversal (e.g. a POS charge and its
            # refund sharing one reference number: "2.00" then "-2.00"). That
            # sign is NOT a reliable debit/credit indicator on its own - a
            # negative print can correspond to either a reversed debit
            # (balance falls) or a reversed credit (balance rises,
            # cancelling an earlier debit) depending on what it's undoing.
            # The running balance is ground truth, so always classify off
            # the actual balance movement and use this only as a magnitude.
            magnitude = abs(printed_amount)
            delta = closing - balance
            if opening_was_unknown:
                # delta is meaningless here (balance was just seeded from
                # this same row's closing balance) - the PDF gives us no
                # running balance to verify direction against, so fall back
                # to the printed sign (negative -> reversal/debit) and say
                # so explicitly rather than presenting it as a confident
                # classification.
                if printed_amount < 0:
                    withdrawal = magnitude
                    deposit = Decimal("0.00")
                else:
                    deposit = magnitude
                    withdrawal = Decimal("0.00")
                issues.append({
                    "file": pdf_name,
                    "issue": "opening balance not stated in PDF; first transaction's debit/credit direction could not be verified against a running balance and was guessed from the printed sign - please confirm manually",
                    "line": full_line,
                })
            elif money_close(delta, magnitude):
                deposit = magnitude
                withdrawal = Decimal("0.00")
            elif money_close(delta, -magnitude):
                withdrawal = magnitude
                deposit = Decimal("0.00")
            elif delta >= 0:
                deposit = magnitude
                withdrawal = Decimal("0.00")
            else:
                withdrawal = magnitude
                deposit = Decimal("0.00")

        seq += 1
        records.append(
            {
                "seq": seq,
                "source_file": pdf_name,
                "statement_from": st_from,
                "statement_to": st_to,
                "date": parse_hdfc_date(tx["date"]),
                "value_date": parse_hdfc_date(value_date),
                "narration": full_narration[:500],
                "reference": reference,
                "direction": "Receipt" if deposit > 0 else "Payment",
                "withdrawal": to_float(withdrawal),
                "deposit": to_float(deposit),
                "amount": to_float(deposit if deposit > 0 else withdrawal),
                "closing_balance": to_float(closing),
                "bank": bank,
            }
        )
        balance = closing
    return records


def build_hdfc_loan_transactions(text, pdf_name, bank, st_from, st_to, opening_balance, seq_start):
    records = []
    seq = seq_start
    for line in text.splitlines():
        line = line.strip()
        match = re.match(r'^(\d+)\s+(\d{2}/\d{2}/\d{4})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})', line)
        if match:
            inst_no, date, inst_amt, principal, interest, os_principal = match.groups()
            seq += 1
            records.append({
                'seq': seq,
                'source_file': pdf_name,
                'statement_from': st_from,
                'statement_to': st_to,
                'date': parse_any_date(date),
                'value_date': parse_any_date(date),
                'narration': f'EMI Payment (Principal: {principal}, Interest: {interest})',
                'reference': str(inst_no),
                'direction': 'Payment',
                'withdrawal': to_float(parse_amount(inst_amt)),
                'deposit': 0.0,
                'amount': to_float(parse_amount(inst_amt)),
                'closing_balance': to_float(parse_amount(os_principal)),
                'bank': bank,
            })
    return records
