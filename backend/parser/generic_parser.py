"""Shared regexes/helpers plus the generic (bank-agnostic) row parser.

This module is the fallback used for any bank that doesn't have its own
dedicated `<bank>_parser.py`. Bank-specific parsers import the helpers
below rather than duplicating them; they should NOT need to edit
`build_transactions` itself to support a new layout - if the generic
engine can't handle a bank's table shape, that's the signal to write a
new parser module instead of special-casing it here.
"""

import datetime as dt
import re
from collections import Counter
from decimal import Decimal


AMOUNT_RE = re.compile(r"-?\d{1,3}(?:,\d{2,3})*(?:\.\d{1,2})-?|-?\d+\.\d{1,2}-?")
DATE_TOKEN_RE = re.compile(
    r"\b(?:\d{2}[-/.]\d{2}[-/.]\d{4}|\d{2}[-/.]\d{2}[-/.]\d{2}|\d{2}[-/][A-Za-z]{3}[-/]\d{4}|\d{2}\s+[A-Za-z]{3}\s+\d{4})\b",
    re.I,
)
DATE_START_RE = re.compile(
    r"^\s*(?:\d+\s+)?'?(?:\d{2}[-/.]\d{2}[-/.]\d{4}|\d{2}[-/.]\d{2}[-/.]\d{2}|\d{2}[-/][A-Za-z]{3}[-/]\d{4}|\d{2}\s+[A-Za-z]{3}\s+\d{4})\b",
    re.I,
)
BOILER_RE = re.compile(
    r"(Page\s+\d+|PageNo|Registered Office|This is a computer generated|"
    r"Generated On|STATEMENT OF ACCOUNT$|Transaction Value Date Particulars|"
    r"Date Narration|TRANS DATE VALUE DATE|Sr\.?No\.?|Opening Balance Total Debit|"
    r"Legends for transactions|Never share your OTP|For clarification kindly|"
    # Genuine decorative banners wrap a whole line in asterisks at each
    # edge (e.g. "*************27 pages printed. End of Report*************"
    # or a masked "CKYC NO : 200910676*****" trailer). Anchoring to the
    # very start/end of the (already-stripped) line - rather than allowing
    # a `[^\n]*` gap anywhere between two asterisk runs - is deliberate:
    # a masked card number embedded INSIDE an ordinary transaction
    # narration, e.g. "ANNUAL_CARDFEE_652266******5393_APR25 257.92 0.00",
    # has real digits before AND after its run of asterisks, so it can no
    # longer satisfy this pattern by splitting that single run in two. The
    # old un-anchored version did exactly that and silently dropped every
    # such transaction row - a routine, common shape (any card-fee/POS
    # narration that mentions a masked card number), not a rare edge case.
    r"^\*{2,}|\*{2,}$|Unless constituent notifies|"
    r"do not require any authentication|Please do not accept any manual entries|"
    r"is a system generated statement|does not require any signature|"
    r"Toll free no\.? of our call center|Helpline No)",
    re.I,
)


def parse_amount(raw):
    raw = raw.replace(",", "").strip()
    if raw in ("", "-"):
        return Decimal("0.00")
    negative = raw.startswith("-") or raw.endswith("-")
    raw = raw.strip("-")
    value = Decimal(raw)
    return -value if negative else value


def parse_balance_from_match(match, text):
    value = parse_amount(match.group(0))
    suffix = text[match.end() : match.end() + 2].lower()
    if suffix == "dr" and value > 0:
        return -value
    return value


def to_float(value):
    return float(value)


def parse_any_date(raw):
    raw = raw.strip("' ")
    for fmt in (
        "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%y", "%d/%m/%y", "%d.%m.%y",
        "%d-%b-%Y", "%d/%b/%Y", "%d %b %Y",
        # ICICI's retail savings-account statement spells its own cover-page
        # period out in full ("for the period April 1, 2025 - March 31,
        # 2026") rather than the DD-MM-YYYY/DD-Mon-YYYY shapes every other
        # bank uses there - a format none of the patterns above can read.
        "%B %d, %Y", "%B %d %Y",
    ):
        try:
            return dt.datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def money_close(a, b, tolerance=Decimal("0.05")):
    return abs(a - b) <= tolerance


def normalize_period_date(raw):
    raw = raw.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    # Try the text exactly as captured first - needed for the full
    # month-name shape ("April 1, 2025") above, which the blind
    # space-to-hyphen rewrite below would otherwise mangle into
    # "April-1,-2025" and fail to parse entirely.
    direct = parse_any_date(raw)
    if direct:
        return direct
    return parse_any_date(raw.replace(" ", "-"))


def find_period(text):
    patterns = [
        r"From\s*:?\s*(\d{2}/\d{2}/\d{4})\s+To\s*:?\s*(\d{2}/\d{2}/\d{4})",
        r"PERIOD\s*:?\s*(\d{2}[-/]\d{2}[-/]\d{4})\s+(?:to|To|-)\s+(\d{2}[-/]\d{2}[-/]\d{4})",
        r"STATEMENT PERIOD\s*:?\s*(\d{4}-\d{2}-\d{2})\s+TO\s+(\d{4}-\d{2}-\d{2})",
        r"from\s+(\d{2}[-/][A-Za-z]{3}[-/]\d{4})\s+to\s+(\d{2}[-/][A-Za-z]{3}[-/]\d{4})",
        r"period\s+of\s+(\d{2}[-/]\d{2}[-/]\d{4})\s+to\s+(\d{2}[-/]\d{2}[-/]\d{4})",
        r"FOR THE PERIOD FROM\s+(\d{4}-\d{2}-\d{2})\s+TO\s+(\d{4}-\d{2}-\d{2})",
        # Bank of India's "Detailed Statement" export: "Transaction Date
        # from: 01-04-2025 to: 31-03-2026" - the leading "Transaction Date"
        # label rules out matching the statement's own *generation* date
        # ("Date: 30/05/2026") printed just above it, which the unlabelled
        # any-date-in-text fallback further down otherwise picks up as a
        # bogus period boundary.
        r"Transaction\s+Date\s+from\s*:?\s*(\d{2}-\d{2}-\d{4})\s+to\s*:?\s*(\d{2}-\d{2}-\d{4})",
        # ICICI's retail savings-account statement: "...in INR for the
        # period April 1, 2025 - March 31, 2026" - a full month name, not
        # the DD-Mon-YYYY/DD/MM/YYYY shapes every other pattern here reads.
        r"period\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})\s*-\s*([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
        # SBI's "STATEMENT OF ACCOUNT" export: "Statement From : 01-04-2025
        # to 31-03-2026" - plain numeric DD-MM-YYYY on both ends, no
        # separator character between them for the other numeric pattern
        # above to anchor on other than the word "to".
        r"Statement\s+From\s*:?\s*(\d{2}[-/]\d{2}[-/]\d{4})\s+to\s+(\d{2}[-/]\d{2}[-/]\d{4})",
        # Canara Bank's own export: "Searched By From 01 Apr 2025 To 31 Mar
        # 2026" - space-separated "DD Mon YYYY" on both ends joined by the
        # word "To", not the hyphen the last fallback pattern below needs.
        r"From\s+(\d{2}\s+[A-Za-z]{3}\s+\d{4})\s+To\s+(\d{2}\s+[A-Za-z]{3}\s+\d{4})",
        r"(\d{2}\s+[A-Za-z]{3}\s+\d{4})\s*-\s*(\d{2}\s+[A-Za-z]{3}\s+\d{4})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return normalize_period_date(match.group(1)), normalize_period_date(match.group(2))
    dates = [parse_any_date(m.group(0)) for m in DATE_TOKEN_RE.finditer(text)]
    dates = [d for d in dates if d]
    return (min(dates), max(dates)) if dates else ("", "")


def extract_opening_balance(text):
    match = re.search(r"Opening Balance\s*:?\s*(-?[\d,]+\.\d{2})(Cr|Dr)?", text, re.I)
    if match:
        value = parse_amount(match.group(1))
        if (match.group(2) or "").lower() == "dr":
            value = -value
        return value
    match = re.search(r"Opening Balance[^\d]+(-?[\d,]+\.\d{2})(Cr|Dr)?", text, re.I)
    if match:
        value = parse_amount(match.group(1))
        if (match.group(2) or "").lower() == "dr":
            value = -value
        return value
    match = re.search(r"Opening Balance\s+(-?[\d,]+\.\d{2})", text, re.I)
    if match:
        return parse_amount(match.group(1))
    match = re.search(r"\bB/F\s+(-?[\d,]+\.\d{2})(Cr|Dr)?", text, re.I)
    if match:
        value = parse_amount(match.group(1))
        if (match.group(2) or "").lower() == "dr":
            value = -value
        return value
    return None


def extract_grand_totals(text):
    match = re.search(
        r"Grand\s+Total\s*:?\s*([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+(-?[\d,]+\.\d{2})(Cr|Dr)?",
        text,
        re.I,
    )
    if not match:
        return None
    closing = parse_amount(match.group(3))
    if (match.group(4) or "").lower() == "dr" and closing > 0:
        closing = -closing
    return {
        "debits": parse_amount(match.group(1)),
        "credits": parse_amount(match.group(2)),
        "closing_balance": closing,
    }


def extract_account_details(text):
    name = ""
    # Bank of India's "Detailed Statement" export prints the holder's name
    # and address label on the same physical line ("Account holder name:
    # VAIBHAV ... Account holder address: MANOR ROAD ..."), so the capture
    # has to stop at "Account holder address" itself rather than running to
    # end of line the way every other bank's single-field name line allows.
    boi_holder_match = re.search(
        r"Account\s+holder\s+name\s*:?\s*(.+?)(?=\s+Account\s+holder\s+address\s*:|\n|$)", text, re.I
    )
    if boi_holder_match:
        name = boi_holder_match.group(1).strip()

    name_match = None if name else re.search(
        r"(?:A/C\s*Name|A/c\s*Name|Account\s*Name|Customer\s*Name)\s*:?\s*([^\n]+)", text, re.I
    )
    if name_match:
        name = name_match.group(1).strip()
    if not name:
        city_match = re.search(r"City\s*:\s*[^\n]+\n([^\n]+)", text, re.I)
        if city_match:
            candidate = city_match.group(1).strip()
            if not any(k in candidate.upper() for k in ["STATE", "PHONE", "EMAIL", "ZIP", "PIN", "NOMIN", "CUSTID", "CITY", "BRANCH"]):
                name = candidate
        if not name:
            ms_match = re.search(r"\bM/[Ss]\.\s*([A-Za-z0-9\s&.,'-]+)", text[:2000])
            if ms_match:
                name = "M/S. " + ms_match.group(1).strip()
            else:
                ms_match2 = re.search(r"\bM/S\s+([A-Za-z0-9\s&.,'-]+)", text[:2000])
                if ms_match2:
                    name = "M/S " + ms_match2.group(1).strip()
        if not name and "Statement of Account No" in text[:2000]:
            first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
            if first_line and not re.search(r"statement|account|customer|ifsc|micr", first_line, re.I):
                name = first_line
        if not name:
            kotak_match = re.search(r"(?:\d{2}\s+[A-Za-z]{3}\s+\d{4}\s*-\s*\d{2}\s+[A-Za-z]{3}\s+\d{4})\n([^\n]+)", text, re.I)
            if kotak_match:
                candidate = kotak_match.group(1).strip()
                if not re.search(r"account|statement|page", candidate, re.I):
                    name = candidate
        if not name:
            # ICICI / similar "Wealth Management" statements print the holder
            # as "MR.<NAME>" directly under the account-summary heading with
            # no explicit "Account Name:" label anywhere in the document.
            title_match = re.search(r"^(?:MR|MRS|MS|M/S)\.?\s*[A-Z][A-Z .]{3,60}$", text[:3000], re.M)
            if title_match:
                name = title_match.group(0).strip()
        if not name:
            # ICICI's retail "Statement of Transactions" export prints the
            # holder name with no title prefix at all, sharing its own
            # line with "Your Base Branch:" (e.g. "RODNEY SEBASTIAN DSA
            # Your Base Branch: ICICI BANK LIMITED,").
            branch_match = re.search(r"^([A-Z][A-Z .]{3,60}?)\s+Your Base Branch\s*:", text[:3000], re.M)
            if branch_match:
                name = branch_match.group(1).strip()

    if name:
        name = name.split("Address :")[0].strip()
        name = name.split("Joint Holders :")[0].strip()
        name = name.split("JOINTHOLDERS:")[0].strip()
        name = re.sub(r"\s+", " ", name).strip()

    acc_match = re.search(
        r"(?:Statement\s+of\s+Account\s+No|Statement\s+of\s+Account|Account\s*No\.?|AccountNo|Account\s*Number|A/C\s*Number|A/C\s*No\.?|A/c\s*No\.?|A/c\s*Number)\s*:?\s*(\d+)",
        text,
        re.I
    )
    account_no = ""
    if acc_match:
        account_no = acc_match.group(1).strip()
    else:
        digits = re.findall(r"\b\d{9,18}\b", text[:2000])
        if digits:
            for d in digits:
                if d in text[:2000]:
                    account_no = d
                    break

    return name, account_no


def build_transactions(
    text,
    pdf_name,
    bank,
    st_from,
    st_to,
    opening_balance,
    seq_start,
    amount_search_scope="line",
    swap_debit_credit=False,
    invert_delta_sign=False,
):
    """Generic date-anchored, line-based transaction parser.

    `amount_search_scope`:
      - "line" (default): only look for the withdrawal/deposit/balance
        numbers on the same physical line as the date (this is the
        original, long-standing behaviour - kept as the default so
        every bank currently relying on the generic fallback keeps
        working exactly as before).
      - "full": also look across the wrapped continuation lines that
        belong to the same transaction. Needed by banks (e.g. ICICI)
        whose long narrations wrap onto their own line(s) *before* the
        deposit/withdrawal/balance columns, which would otherwise land
        the amounts outside the single line this parser used to check.

    `invert_delta_sign`: some current/overdraft-account statements
    print a *drawn* balance - one that reflects how much the customer
    owes the bank, not how much money is in the account - so a
    withdrawal makes the printed balance go UP and a deposit makes it
    go DOWN, the opposite of every ordinary savings-account statement.
    Set True for that layout: every parsed balance gets negated once,
    up front, which both fixes the debit/credit reconciliation and
    makes the `closing_balance` written out for each row read as
    normal signed account equity (negative while overdrawn) instead of
    the raw always-positive drawn figure the statement itself prints.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    txs = []
    current = None
    pending = []

    for line in lines:
        if BOILER_RE.search(line):
            continue
        if DATE_START_RE.match(line):
            if current:
                txs.append(current)
            current = {"line": line, "continuation": list(pending)}
            pending = []
        elif current:
            current["continuation"].append(line)
        else:
            # Some statements print narration immediately before the dated numeric row.
            if not any(word in line.upper() for word in ["STATEMENT", "ACCOUNT", "ADDRESS", "BRANCH", "IFSC"]):
                pending = (pending + [line])[-3:]
    if current:
        txs.append(current)

    # Every reconciliation below carries `balance` forward from one `tx` to
    # the next and reads `closing - balance` as *this* row's own signed
    # movement - which is only true if `txs` arrives oldest-first. Some
    # banks (e.g. Jana Small Finance's own net-banking export) print
    # newest-first instead: walking that order feeds a LATER transaction's
    # closing balance in as this row's "opening" balance, so the delta no
    # longer measures this row's own movement at all. Most rows still
    # resolve correctly anyway (the position-based fallback further down
    # doesn't depend on delta), but whenever that wrong delta happens to
    # coincidentally match this row's own amount by magnitude, it silently
    # flips a genuine debit into the deposit column or vice versa - a real,
    # if infrequent, corruption. Detecting the printed direction from the
    # first vs last row's own date and reversing `txs` here - only for this
    # internal walk; every caller re-sorts its final records by date anyway
    # - restores chronological order regardless of which way the source
    # PDF lists its rows.
    first_tx_date = next(
        (parse_any_date(m.group(0)) for t in txs if (m := DATE_TOKEN_RE.search(t["line"]))),
        "",
    )
    last_tx_date = next(
        (parse_any_date(m.group(0)) for t in reversed(txs) if (m := DATE_TOKEN_RE.search(t["line"]))),
        "",
    )
    if first_tx_date and last_tx_date and first_tx_date > last_tx_date:
        txs = list(reversed(txs))

    records = []
    balance = opening_balance
    seq = seq_start
    for tx in txs:
        line = tx["line"]
        date_matches = list(DATE_TOKEN_RE.finditer(line))
        if not date_matches:
            continue
        tx_date = parse_any_date(date_matches[0].group(0))
        value_date = parse_any_date(date_matches[1].group(0)) if len(date_matches) > 1 else tx_date

        # Search for amounts only in the text AFTER the date token(s), never
        # in the Sl.No/date prefix itself. Dot-separated dates (ICICI's
        # "03.04.2025") satisfy AMOUNT_RE's own "digits.digits" shape - e.g.
        # "03.04" out of "03.04.2025" - so searching the raw line (which is
        # exactly what the old code did) silently injects a bogus ~3-rupee
        # amount into every dotted-date transaction, corrupting its real
        # debit/credit classification. No bank in this codebase ever prints
        # a real amount before its own date, so cutting the date out first
        # is safe for every existing layout and fixes this for free.
        cut = date_matches[-1].end()
        line_after_date = line[cut:]
        line_amount_matches = list(AMOUNT_RE.finditer(line_after_date))
        # Prefer the date's own line whenever it already carries at least
        # a flow amount and a balance: that is the real, unambiguous table
        # row, and every layout observed so far never needs more than
        # that. Only reach into the wrapped continuation lines - which
        # exist purely for "full" scope's original purpose of recovering
        # a layout that pushes its amounts onto them entirely - when the
        # date line itself doesn't have enough numbers, since continuation
        # text routinely contains numeric-looking fragments that are NOT
        # additional amount columns: a dotted reference date embedded in
        # narration ("BCB/01.09.2025"), or a fee/GST breakdown
        # ("chgRs5.00GSTRs0.90"). Counting either of those as a 3rd or 4th
        # "amount" corrupts the parsed closing balance (silently replaced
        # by whichever of those fragments happens to be the LAST regex
        # match) and, as a knock-on effect, causes the debit/credit
        # resolver's own-both-columns fallback to fire, writing a bogus
        # value into whichever column - Withdrawal or Deposit - the real
        # transaction never touched.
        if amount_search_scope == "line" or len(line_amount_matches) >= 2:
            amount_source = line_after_date
            amount_matches = line_amount_matches
        else:
            amount_source = " ".join([line_after_date] + tx["continuation"])
            amount_matches = list(AMOUNT_RE.finditer(amount_source))
        if len(amount_matches) < 2:
            continue

        closing = parse_balance_from_match(amount_matches[-1], amount_source)
        if invert_delta_sign:
            # This layout prints a *drawn* balance (how much the
            # customer owes the bank), not account equity - it goes UP
            # on a withdrawal and DOWN on a deposit. Negating it here,
            # once, converts it to normal signed equity (negative when
            # overdrawn) so every downstream comparison, the running
            # `balance` carried into the next row, and the
            # `closing_balance` written out for this row are all
            # correct without needing special-cased comparison logic
            # anywhere else.
            closing = -closing
        nums = [parse_amount(m.group(0)) for m in amount_matches]
        debit = Decimal("0.00")
        credit = Decimal("0.00")
        resolved = False

        # Prefer balance-delta reconciliation over trusting nums[-3]/nums[-2]
        # by raw position whenever a running balance is available: it's
        # immune to stray numeric fragments elsewhere in a wrapped/
        # full-scope narration - an embedded fee breakdown like
        # "chgRs5.00GSTRs0.90", or a dotted clearance-date reference like
        # "01.09.2025" that itself partially matches AMOUNT_RE - either of
        # which would otherwise get counted as an extra debit/credit column
        # and silently invert or corrupt the real amount. nums[-2] (the
        # number immediately before the balance) is the transaction amount
        # in every layout observed in this codebase, so it's tried first;
        # only fall back to position when delta reconciliation can't settle
        # it at all (no prior balance yet, or a row that genuinely prints
        # non-zero values in both the debit and credit column).
        delta = None if balance is None else closing - balance
        if delta is not None:
            amount = nums[-2]
            if money_close(delta, amount):
                credit = amount
                resolved = True
            elif money_close(delta, -amount):
                debit = amount
                resolved = True
            elif len(nums) >= 3:
                maybe_debit, maybe_credit = nums[-3], nums[-2]
                if money_close(delta, maybe_credit - maybe_debit):
                    if swap_debit_credit:
                        debit, credit = maybe_credit, maybe_debit
                    else:
                        debit, credit = maybe_debit, maybe_credit
                    resolved = True

        if not resolved and len(nums) >= 3:
            maybe_debit, maybe_credit = nums[-3], nums[-2]
            if maybe_debit != 0 or maybe_credit != 0:
                if swap_debit_credit:
                    debit, credit = maybe_credit, maybe_debit
                else:
                    debit, credit = maybe_debit, maybe_credit
                resolved = True

        if not resolved:
            amount = nums[-2]
            upper = " ".join([line] + tx["continuation"]).upper()
            # "CR", "DR" and "REC" are bare 2-3 letter tokens that are
            # only safe to match as whole words - as plain substrings
            # they collide with ordinary narration text ("MICR", a
            # "CREDIT CARD" bill *payment*, "ADDRESS", "RECHARGE") and
            # would silently flip that transaction's direction. This
            # fallback only ever runs when the running-balance delta
            # couldn't settle it (e.g. the very first row of a
            # statement with no printed opening balance), but that's
            # exactly the row most Indian bank statements start with,
            # so getting it wrong here is not a rare edge case.
            #
            # "PAY" and "POS" have the exact same bare-substring problem
            # and were missed by that reasoning: "PAY" is a substring of
            # "Paym"/"Payment" - the default UPI purpose note apps fill
            # in on CREDIT narrations just as often as debits (e.g. SBI's
            # own "UPI/CR/.../Paym") - and "POS" is a substring of
            # "DEPOSIT" itself ("de-POS-it"), which is also literally in
            # `credit_words` below. Left as plain substrings, a routine
            # "CASH DEPOSIT ..." or UPI-credit-with-"Paym" narration would
            # set both has_credit_word AND has_debit_word, and the
            # has_debit_word branch below wins - silently turning a
            # deposit into a withdrawal. Matching them as whole words the
            # same way CR/DR/REC already are fixes both collisions.
            credit_words = ["RECEIPT", "DEPOSIT", "BY CASH", "CASH RECEIPT", "NEFT/", "UPI/CR"]
            debit_words = ["WDL", "WITHDRAW", "CHARG", "GST", "DEBIT", "FT - DR"]
            has_credit_word = any(w in upper for w in credit_words) or bool(re.search(r"\b(?:CR|REC)\b", upper))
            has_debit_word = (
                any(w in upper for w in debit_words)
                or bool(re.search(r"\b(?:DR|PAY|POS)\b", upper))
            )
            if has_credit_word and not (has_debit_word or any(w in upper for w in [" DR", "/DR/", "FT - DR"])):
                credit = amount
            elif has_debit_word:
                debit = amount
            else:
                credit = amount

        raw_text = " ".join([line] + tx["continuation"])
        narration = raw_text[cut:]
        for m in reversed(amount_matches):
            narration = narration.replace(m.group(0), " ", 1)
        narration = re.sub(r"\b(?:Dr|Cr)\b", " ", narration, flags=re.I)
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
                "direction": "Receipt" if credit > 0 else "Payment",
                "withdrawal": to_float(debit),
                "deposit": to_float(credit),
                "amount": to_float(credit if credit > 0 else debit),
                "closing_balance": to_float(closing),
                "bank": bank,
            }
        )
        balance = closing
    return records


FD_INTEREST_RE = re.compile(r"\b(?:INT|INTEREST)\b", re.I)
FD_RE = re.compile(
    r"\b(?:"
    r"F\.?\s*D\.?|FDR|FIXED\s*DEPOSIT|TERM\s*DEPOSIT|TDR|"
    r"FD\s*(?:NO|A/C|AC|ACCOUNT|OPEN|CLOS(?:E|URE|ING)?|MATUR(?:ITY|ED)?|"
    r"RENEW(?:AL|ED)?|BOOK(?:ING|ED)?|DEP(?:OSIT)?|INT(?:EREST)?|RECEIPT|LIEN)"
    r")\b",
    re.I,
)
MUTUAL_FUND_RE = re.compile(r"\b(?:MUTUAL\s+FUND|MF|SIP|REDEMPTION|CAP\s+FUND)\b", re.I)
LIC_RE = re.compile(r"\b(?:LIC|LIFE\s+INSURANCE)\b", re.I)
PF_RE = re.compile(
    r"\b(?:"
    r"PUBLIC\s+PROVIDENT\s+FUND|PROVIDENT\s+FUND|EPF|"
    r"PPF\s*(?:A/C|AC|ACCOUNT|CONTRIBUTION|DEPOSIT|INTEREST|INT|SUBSCRIPTION|"
    r"TRANSFER|MATURITY|WITHDRAWAL|LOAN)|"
    r"PF\s*(?:CONTRIBUTION|INTEREST|TRANSFER|CLAIM|WITHDRAWAL|SETTLEMENT|"
    r"EMPLOYEE|EMPLOYER)"
    r")\b",
    re.I,
)
SALARY_RE = re.compile(r"\b(?:SALARY|PAYROLL|PENSION)\b", re.I)


def classify(records):
    def has_salary_marker(record):
        return bool(SALARY_RE.search(record["narration"]))

    def has_split_salary_context(index):
        record = records[index]
        if record["direction"] != "Receipt" or record["amount"] < 10000:
            return False
        for neighbor_index in (index - 1, index + 1):
            if neighbor_index < 0 or neighbor_index >= len(records):
                continue
            neighbor = records[neighbor_index]
            if (
                neighbor.get("date") == record.get("date")
                and neighbor.get("direction") == "Payment"
                and neighbor.get("amount", 0) <= 1000
                and has_salary_marker(neighbor)
            ):
                return True
        return False

    for index, record in enumerate(records):
        narration = record["narration"]
        compact = re.sub(r"\s+", "", narration.upper())
        amount_abs = abs(record["amount"])
        categories = []
        if any(k in compact for k in ["CASHDEPOSIT", "CASHRECEIPT", "BYCASH", "CASHDEP", "CASH"]):
            categories.append("Cash")
        if "UPI" in compact:
            categories.append("UPI")
        if "IMPS" in compact:
            categories.append("IMPS")
        if LIC_RE.search(narration):
            categories.append("LIC")
        if record["direction"] == "Payment" and any(k in compact for k in ["EMI", "ECS", "ACHDR", "NACH", "LOAN", "INSTALMENT", "INSTALLMENT"]):
            categories.append("EMI")
        if record["direction"] == "Receipt" and (has_salary_marker(record) or has_split_salary_context(index)):
            categories.append("Salary")
        is_mutual_fund = bool(MUTUAL_FUND_RE.search(narration))
        is_fd = bool(FD_RE.search(narration)) and not is_mutual_fund
        if is_fd and FD_INTEREST_RE.search(narration):
            categories.append("FD interest")
        elif is_fd:
            categories.append("FD Deposit/Withdrawal")
        if is_mutual_fund:
            categories.append("Mutual Fund / Investment")
        if PF_RE.search(narration):
            categories.append("PPF/PF interest/contribution")
        if amount_abs >= 100000:
            categories.append("High Transaction")
        record["categories"] = "; ".join(dict.fromkeys(categories)) if categories else "Other"

    def recurring_key(narration):
        upper = narration.upper()
        for token in re.split(r"[/|-]", upper):
            token = re.sub(r"[^A-Z ]", "", token).strip()
            if len(token) >= 8 and token not in {"PAYMENT", "TRANSFER", "CHARGES"}:
                return token[:40]
        upper = re.sub(r"\d+", "#", upper)
        upper = re.sub(r"\s+", " ", upper).strip()
        return upper[:50]

    counts = Counter(f"{recurring_key(r['narration'])}|{r['direction']}" for r in records)
    for record in records:
        key = f"{recurring_key(record['narration'])}|{record['direction']}"
        record["recurring_key"] = key.split("|", 1)[0]
        record["is_recurring"] = "Yes" if counts[key] >= 2 else "No"
        if record["is_recurring"] == "Yes" and "Recurring Transaction" not in record["categories"]:
            record["categories"] = (
                f"{record['categories']}; Recurring Transaction"
                if record["categories"] != "Other"
                else "Recurring Transaction"
            )
