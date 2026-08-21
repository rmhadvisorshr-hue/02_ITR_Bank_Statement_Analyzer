"""OCR adapter for photographed passbook ledgers.

Statements like "BANK STATEMENT.pdf" / "BANK STATEMENT 2.pdf" aren't a
modern bank export at all - they're phone photos of a physical passbook,
with columns literally titled `Date | Particulars | Cheque No. |
Withdrawals | Deposits | Balance | Intl.`. Two things make this
fundamentally different from every other parser in this codebase:

1. Withdrawals and Deposits are two separate columns, distinguished only
   by *physical position* on the page - there's no "Dr"/"Cr" keyword or
   sign to key off, the way every text-based statement in `parser/`
   has. Once OCR flattens a page into one stream of text (which is what
   `pytesseract.image_to_string` / the old `extract_text_with_ocr` does),
   that positional information is gone for good - there is no regex that
   recovers it. This adapter instead consumes `image_to_data`'s per-word
   bounding boxes (`ocr.extract_words_with_boxes`) and reconstructs each
   row from *where* a number sits on the page, not what it says.

2. It's a photo, not a scan - narrations routinely wrap across several
   physical lines with no amount on them at all (a continuation row has
   no Date/Withdrawals/Deposits/Balance, just more Particulars text).

The row/column-reconstruction logic below is written as plain functions
over word-position dictionaries so it can be unit-tested without a
working Tesseract install (see the bottom of this file's companion
tests) - the OCR call itself is a thin, separately-swappable layer.
"""

import re
import sys
import pathlib
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.resolve()))
from parser.generic_parser import parse_amount, parse_any_date, to_float  # noqa: E402


AMOUNT_TOKEN_RE = re.compile(r"^-?\d{1,3}(?:,\d{2,3})*\.\d{2}$|^-?\d+\.\d{2}$")
DATE_TOKEN_RE = re.compile(r"^\d{1,2}[-/]\d{1,2}[-/]\d{2,4}$")
CRDR_TOKEN_RE = re.compile(r"^(Cr|Dr)\.?$", re.I)

# Header label -> canonical column name. Matched word-by-word (not as an
# exact phrase) since OCR frequently splits "Cheque No." into two tokens,
# or drops the period.
_HEADER_KEYWORDS = {
    "date": "date",
    "particulars": "particulars",
    "cheque": "cheque_no",
    "chq": "cheque_no",
    "withdrawals": "withdrawals",
    "withdrawal": "withdrawals",
    "deposits": "deposits",
    "deposit": "deposits",
    "balance": "balance",
    "intl": "intl",
    "int": "intl",
}

# Last-resort column layout (fraction of page width) if no header row
# could be found on any page at all - approximates the standard Indian
# bank-passbook ledger proportions. Used only as a fallback, and always
# surfaced via `issues` so it isn't mistaken for a confirmed layout.
_FALLBACK_COLUMN_FRACTIONS = {
    "date": 0.00,
    "particulars": 0.15,
    "cheque_no": 0.52,
    "withdrawals": 0.62,
    "deposits": 0.76,
    "balance": 0.88,
}


def _clean_header_word(word):
    return re.sub(r"[^a-z]", "", word.lower())


def detect_columns(words):
    """Find each ledger column's x-anchor from a page's header row.

    Returns {column_name: x_left} for whichever columns were recognised,
    or {} if this page has no header row at all (e.g. a continuation
    page in a multi-page photo set).
    """
    anchors = {}
    for word in words:
        key = _HEADER_KEYWORDS.get(_clean_header_word(word["text"]))
        if key and key not in anchors:
            anchors[key] = word["left"]
    # A lone "date" or "balance" match isn't a real header row (those
    # words can plausibly show up elsewhere); require at least three
    # recognised columns before trusting this as the header.
    if len(anchors) < 3:
        return {}
    return anchors


def _fallback_columns(page_width):
    return {name: frac * page_width for name, frac in _FALLBACK_COLUMN_FRACTIONS.items()}


def _cluster_rows(words, y_tolerance_ratio=0.6):
    """Group words into visual rows by vertical position, then sort each
    row left-to-right. Row order follows the words' own top-to-bottom
    order rather than assuming any fixed row height."""
    if not words:
        return []
    heights = sorted(w["height"] for w in words if w["height"] > 0)
    median_height = heights[len(heights) // 2] if heights else 12
    tolerance = max(4, median_height * y_tolerance_ratio)

    sorted_words = sorted(words, key=lambda w: (w["top"], w["left"]))
    rows = []
    current_row = []
    current_center = None
    for word in sorted_words:
        center = word["top"] + word["height"] / 2
        if current_center is None or abs(center - current_center) <= tolerance:
            current_row.append(word)
            centers = [w["top"] + w["height"] / 2 for w in current_row]
            current_center = sum(centers) / len(centers)
        else:
            rows.append(sorted(current_row, key=lambda w: w["left"]))
            current_row = [word]
            current_center = center
    if current_row:
        rows.append(sorted(current_row, key=lambda w: w["left"]))
    return rows


def _column_boundaries(anchors):
    """Turn {column: x_anchor} into sorted (column, left_bound, right_bound)
    ranges, splitting the space between adjacent columns at the midpoint."""
    ordered = sorted(anchors.items(), key=lambda kv: kv[1])
    boundaries = []
    for i, (name, x) in enumerate(ordered):
        left_bound = x if i == 0 else (ordered[i - 1][1] + x) / 2
        right_bound = float("inf") if i == len(ordered) - 1 else (x + ordered[i + 1][1]) / 2
        boundaries.append((name, left_bound, right_bound))
    return boundaries

# columns whose OCR misreads (accidental commas, decimals) shouldn't
# leak into narration text
_AMOUNT_COLUMNS = {"withdrawals", "deposits", "balance"}


def _assign_row_to_columns(row_words, boundaries):
    buckets = {name: [] for name, _, _ in boundaries}
    buckets.setdefault("particulars", [])
    for word in row_words:
        center = word["left"] + word["width"] / 2
        target = None
        for name, left_bound, right_bound in boundaries:
            if left_bound <= center < right_bound:
                target = name
                break
        if target is None:
            target = boundaries[-1][0] if boundaries else "particulars"
        buckets.setdefault(target, []).append(word["text"])
    return {name: " ".join(vals) for name, vals in buckets.items()}


def _first_amount(text):
    for token in text.split():
        token = token.strip(",")
        if AMOUNT_TOKEN_RE.match(token):
            return parse_amount(token)
    return None


def _balance_with_sign(text):
    amount = _first_amount(text)
    if amount is None:
        return None
    is_dr = bool(re.search(r"\bDr\.?\b", text, re.I))
    if is_dr and amount > 0:
        amount = -amount
    return amount


def _money_close(a, b, tolerance=Decimal("0.05")):
    return abs(a - b) <= tolerance


def build_passbook_transactions(pages_words, pdf_name, bank, st_from, st_to, opening_balance, seq_start, issues):
    records = []
    seq = seq_start
    balance = opening_balance
    # Narration seen before the *first* transaction has been emitted - once
    # a transaction exists, any later no-date/no-amount row is a wrap of
    # its Particulars text (printed below the row it belongs to, since a
    # passbook's ledger cell overflows downward), so it's appended to
    # `records[-1]` directly instead of being held for "next".
    leading_particulars = []
    column_anchors = None
    used_fallback_layout = False

    for page_words in pages_words:
        if not page_words:
            continue

        if column_anchors is None:
            column_anchors = detect_columns(page_words)
            if not column_anchors:
                page_width = max((w["left"] + w["width"] for w in page_words), default=1000)
                column_anchors = _fallback_columns(page_width)
                used_fallback_layout = True

        boundaries = _column_boundaries(column_anchors)
        header_top = None
        for word in page_words:
            if _clean_header_word(word["text"]) in _HEADER_KEYWORDS:
                header_top = word["top"] + word["height"]
                break

        rows = _cluster_rows(page_words)
        for row_words in rows:
            if header_top is not None and all(w["top"] < header_top for w in row_words):
                continue  # the header row itself

            cols = _assign_row_to_columns(row_words, boundaries)
            date_text = cols.get("date", "")
            date_match = next((t for t in date_text.split() if DATE_TOKEN_RE.match(t)), None)

            withdrawal = _first_amount(cols.get("withdrawals", ""))
            deposit = _first_amount(cols.get("deposits", ""))
            balance_val = _balance_with_sign(cols.get("balance", ""))
            particulars = cols.get("particulars", "").strip()
            if cols.get("cheque_no", "").strip():
                particulars = f"{particulars} [Chq {cols['cheque_no'].strip()}]".strip()

            has_amount = withdrawal is not None or deposit is not None or balance_val is not None
            if date_match is None and not has_amount:
                # Pure narration continuation - no date, no numbers on this
                # visual row. It's the tail of whichever row's Particulars
                # text is still overflowing, which is always the row
                # *above* it (a passbook cell wraps downward, never up).
                if particulars:
                    if records:
                        records[-1]["narration"] = f"{records[-1]['narration']} {particulars}".strip()[:500]
                    else:
                        leading_particulars.append(particulars)
                continue

            if date_match is None:
                # Rare: a row has amounts but the date OCR'd badly enough
                # to miss the pattern entirely. Still worth keeping the
                # transaction rather than silently dropping real money
                # movement; flag it so it's easy to find and fix by hand.
                issues.append({
                    "file": pdf_name,
                    "issue": "passbook row has an amount but no recognisable date (likely an OCR misread) - date left blank, please verify",
                    "particulars": particulars,
                })

            full_particulars = " ".join([p for p in (leading_particulars + [particulars]) if p]).strip()
            leading_particulars = []

            tx_date = parse_any_date(date_match) if date_match else ""

            if balance_val is None:
                # Can't reconcile this row against a running balance at
                # all - safer to surface it than to guess.
                issues.append({
                    "file": pdf_name,
                    "issue": "passbook row's Balance column could not be read - transaction skipped, please verify from the original photo",
                    "particulars": full_particulars,
                })
                continue

            # Withdrawal/Deposit are read straight off their own column, so
            # (unlike every other parser in this codebase) direction never
            # has to be guessed from a keyword or a balance delta. The
            # delta is still worth checking, though: if it doesn't
            # reconcile, that's a strong signal one of this row's digits
            # was OCR-misread, worth flagging even though it doesn't change
            # which column the money came from.
            if balance is not None:
                expected_delta = (deposit or Decimal("0.00")) - (withdrawal or Decimal("0.00"))
                actual_delta = balance_val - balance
                if not _money_close(actual_delta, expected_delta):
                    issues.append({
                        "file": pdf_name,
                        "issue": (
                            f"passbook row's balance ({balance_val}) doesn't reconcile with the previous "
                            f"balance ({balance}) plus this row's deposit/withdrawal ({expected_delta}) - "
                            "likely an OCR misread digit somewhere on this row, please verify"
                        ),
                        "particulars": full_particulars,
                        "date": tx_date,
                    })

            seq += 1
            records.append({
                "seq": seq,
                "source_file": pdf_name,
                "statement_from": st_from,
                "statement_to": st_to,
                "date": tx_date,
                "value_date": tx_date,
                "narration": full_particulars[:500],
                "reference": "",
                "direction": "Receipt" if (deposit or Decimal("0")) > 0 else "Payment",
                "withdrawal": to_float(withdrawal) if withdrawal is not None else 0.0,
                "deposit": to_float(deposit) if deposit is not None else 0.0,
                "amount": to_float(deposit if deposit else (withdrawal or Decimal("0.00"))),
                "closing_balance": to_float(balance_val),
                "bank": bank,
            })
            balance = balance_val

    if used_fallback_layout:
        issues.append({
            "file": pdf_name,
            "issue": "no column header row was recognised anywhere in this passbook photo; used an approximate standard-layout column split instead of the statement's actual measured columns - amounts and withdrawal/deposit direction should be spot-checked",
        })

    return records
