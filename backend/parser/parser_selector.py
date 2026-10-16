"""Bank detection + parser dispatch.

`extract_bank.py` should only ever need two things from this module:

    bank = detect_bank(text, filename)
    build = get_transaction_builder(bank)
    records = build(text, pdf_name, bank, st_from, st_to, opening_balance, seq_start)

Everything else (HDFC's loan-schedule vs. statement distinction, and
OCR'd-text handling) is orchestration that depends on more than just
the bank name, so it stays in extract_bank.py's process_single_pdf.
"""

import pathlib
import re

from .generic_parser import build_transactions
from .idbi_parser import build_idbi_transactions
from .kotak_parser import build_kotak_transactions
from .icici_parser import build_icici_transactions
from .sbi_parser import build_sbi_transactions
from .axis_parser import build_axis_transactions
from .union_parser import build_union_transactions
from .jana_parser import build_jana_transactions
from .pnb_parser import build_pnb_transactions
from .canara_parser import build_canara_transactions
from .kvb_parser import build_kvb_transactions
from .bob_parser import build_bob_transactions
from .boi_parser import build_boi_transactions
from .bassein_parser import build_bassein_transactions


# Indian bank IFSC prefixes (the first 4 letters of any IFSC code, e.g.
# "HDFC0000408" -> "HDFC"). A statement's OWN account IFSC is almost
# always printed once, next to a literal "IFSC" label near the branch/
# account header - unlike bare bank-name words, which show up constantly
# in transaction narrations for the *counterparty's* bank (a UPI handle
# like "...@OKAXIS-UTIB0000000-..." or "...KOTAKPAYOUT-..." says nothing
# about whose statement this is). Checking the labelled IFSC first lets
# real self-identification win over those narration false-positives.
_IFSC_PREFIX_TO_BANK = {
    "HDFC": "HDFC Bank",
    "KKBK": "Kotak Mahindra Bank",
    "ICIC": "ICICI Bank",
    "UTIB": "Axis Bank",
    "SBIN": "State Bank of India",
    "IBKL": "IDBI Bank",
    "IDFB": "IDFC First Bank",
    "FDRL": "Federal Bank",
    "UBIN": "Union Bank",
    "PUNB": "Punjab National Bank",
    "BKID": "Bank of India",
    "BARB": "Bank of Baroda",
    "MAHB": "Bank of Maharashtra",
    "DCBL": "DCB Bank",
    "JSFB": "Jana Small Finance Bank",
    "CNRB": "Canara Bank",
    "KVBL": "Karur Vysya Bank",
    "BACB": "Bassein Catholic Co-op Bank",
}
# The gap between the word "IFSC" and its actual code varies by bank -
# some print no separator at all ("IFSC UBIN0561380", "IFSC Code
# CNRB0004503"), others a colon ("IFSC : BACB0000018"), others a longer
# label with a colon ("IFSC Code / MICR Code : BACB0000006 / 400238006").
# Restricting the gap to only label-shaped characters (letters, slashes,
# whitespace, colons - never digits or other punctuation) lets one
# pattern cover every such label wording without needing a hardcoded
# variant per bank, while staying too narrow to ever bridge across into
# unrelated text elsewhere in the document.
_LABELLED_IFSC_RE = re.compile(r"IFSC[A-Za-z/\s:]{0,30}?([A-Z]{4})\s*0[A-Z0-9]{6}", re.I)


def _bank_from_labelled_ifsc(text):
    match = _LABELLED_IFSC_RE.search(text)
    if not match:
        return None
    return _IFSC_PREFIX_TO_BANK.get(match.group(1).upper())


def _find(phrase_pattern, scope_upper):
    """Start offset of `phrase_pattern`'s first match in `scope_upper`,
    or None - excluding a match embedded in a "/"-delimited UPI/NEFT
    reference chain (e.g. "UPI/sanjay.../UPI/HDFC BANK 20.00",
    "...IT/HDFC BANK/560434504755/ICI62a").

    Every genuine bank-name false-positive found in practice - a
    transaction whose counterparty happens to be some *other* bank -
    turned out to be embedded in exactly this slash-separated narration
    shape, whereas a statement's own letterhead/footer never is. This is
    a cheap, general guard against that whole failure class rather than
    a fix for one specific bank name.
    """
    match = re.search(rf"(?<!/){phrase_pattern}", scope_upper)
    return match.start() if match else None


# Candidate self-identification phrase(s) per bank, in no particular
# order - `_match_bank_phrases` below picks whichever bank's phrase
# actually starts earliest in the scanned text, not whichever is listed
# first here. Kept as a flat list (rather than one `if/elif` per bank)
# specifically so ordering in this file can never again decide the
# outcome - see that function's own docstring for why that mattered.
_BANK_PHRASE_CANDIDATES = [
    ("IDBI Bank", ["IDBI BANK"]),
    ("Kotak Mahindra Bank", ["KOTAK MAHINDRA"]),
    ("Jana Small Finance Bank", ["JANA SMALL"]),
    ("Axis Bank", ["AXIS BANK"]),
    ("HDFC Bank", ["HDFC BANK"]),
    ("ICICI Bank", [r"\bICICI\s+BANK\b"]),
    ("Bank of Baroda", ["BANK OF BARODA"]),
    ("IDFC First Bank", ["IDFC FIRST"]),
    ("DCB Bank", ["DCB BANK"]),
    ("Federal Bank", ["FEDERAL BANK"]),
    ("Punjab National Bank", ["PUNJAB NATIONAL BANK"]),
    ("Union Bank", ["UNION BANK"]),
    ("State Bank of India", ["STATE BANK OF INDIA", r"\bSBI\b"]),
    ("Canara Bank", ["CANARA BANK"]),
    ("Karur Vysya Bank", ["KARUR VYSYA"]),
    ("Saraswat Bank", ["SARASWAT"]),
    ("Bank of Maharashtra", ["BANK OF MAHARASHTRA"]),
    ("Vasai Vikas Bank", ["VASAI VIKAS"]),
    ("Bassein Catholic Co-op Bank", ["BASSEIN CATHOLIC"]),
]


def _match_bank_phrases(scope_upper):
    """Identify a statement's own bank from among every candidate whose
    self-identification phrase appears in `scope_upper`, picking
    whichever one starts *earliest* in the text.

    This used to be a fixed if/elif chain that returned on the first
    candidate whose phrase existed *anywhere* in scope, regardless of
    where. A statement inevitably mentions other banks too - a UPI/NEFT
    counterparty's bank, a beneficiary name in an NEFT/RTGS narration -
    and whichever of those happened to be checked earlier in the list
    won even when the statement's own, genuine letterhead was sitting
    right at the top of the very same text. In practice this mis-tagged
    HDFC statements as Kotak Mahindra Bank whenever a transaction named
    "Kotak Mahindra Bank" as a counterparty without a "/" immediately in
    front of it (the one shape `_find`'s guard excludes), since "KOTAK
    MAHINDRA" was checked before "HDFC BANK" here - not because Kotak's
    name appeared earlier in the statement, only because it was earlier
    in this list. Comparing actual text position instead of list
    position fixes that whole class of false positive at once, for
    every bank pairing, rather than reordering one pair at a time.

    Deliberately excludes the bare "KOTAK" fallback - that one is weak
    enough (a UPI counterparty mention is enough to trigger it) that
    it's only ever tried once, as an absolute last resort, in
    `detect_bank` itself.
    """
    best_bank = None
    best_pos = None
    for bank, patterns in _BANK_PHRASE_CANDIDATES:
        positions = [p for p in (_find(pattern, scope_upper) for pattern in patterns) if p is not None]
        if not positions:
            continue
        pos = min(positions)
        if best_pos is None or pos < best_pos:
            best_bank, best_pos = bank, pos

    # "Bank of India" is a real, distinct bank, but its name is also a
    # literal substring of "State Bank of India" and (via the ubiquitous
    # regulator-disclaimer boilerplate almost every Indian bank statement
    # carries) "Reserve Bank of India" - the lookbehinds here keep it
    # from ever matching *inside* either of those two phrases, so it can
    # compete purely on position like every other candidate above
    # without needing those two ruled out first.
    boi_match = re.search(r"(?<!/)(?<!STATE )(?<!RESERVE )BANK OF INDIA", scope_upper)
    if boi_match and (best_pos is None or boi_match.start() < best_pos):
        best_bank, best_pos = "Bank of India", boi_match.start()

    return best_bank


def detect_bank(text, filename):
    filename_upper = pathlib.Path(filename).name.upper()

    if "KOTAK" in filename_upper: return "Kotak Mahindra Bank"
    if "HDFC" in filename_upper: return "HDFC Bank"
    if "AXIS" in filename_upper: return "Axis Bank"
    if "ICICI" in filename_upper: return "ICICI Bank"
    if "IDBI" in filename_upper: return "IDBI Bank"
    if "IDFC" in filename_upper: return "IDFC First Bank"
    if "FEDERAL" in filename_upper: return "Federal Bank"
    if "UNION" in filename_upper: return "Union Bank"
    if "SBI" in filename_upper or "STATE BANK" in filename_upper: return "State Bank of India"
    if "PNB" in filename_upper: return "Punjab National Bank"
    if "JANA" in filename_upper: return "Jana Small Finance Bank"
    if "CANARA" in filename_upper: return "Canara Bank"
    if "KARUR" in filename_upper or "KVB" in filename_upper: return "Karur Vysya Bank"

    ifsc_bank = _bank_from_labelled_ifsc(text)
    if ifsc_bank:
        return ifsc_bank

    # Try the document's own letterhead/header zone first - a genuine
    # statement always names its bank near the top. Only fall back to
    # scanning the *whole* document if that comes up empty: a long,
    # many-page statement is close to guaranteed to mention several
    # *other* banks by name somewhere in its transaction narrations
    # (UPI/NEFT counterparties), so searching the full text first would
    # make those false-positives the norm rather than the exception.
    header_bank = _match_bank_phrases(text[:5000].upper())
    if header_bank:
        return header_bank

    whole_doc_bank = _match_bank_phrases(text.upper())
    if whole_doc_bank:
        return whole_doc_bank

    # No bare "KOTAK" (or similar) last resort here on purpose: a mention
    # that weak is exactly as likely to be a UPI/NEFT counterparty as the
    # statement's own bank, and the generic date-anchored engine handles
    # an unrecognised layout perfectly well without a (possibly wrong)
    # bank label forcing it through the wrong bank-specific parser.
    return "Unknown Bank"


_BUILDERS = {
    # HDFC Bank is intentionally absent here: extract_bank.py picks
    # between build_hdfc_transactions / build_hdfc_loan_transactions /
    # the OCR adapter itself, since that choice depends on more than
    # just the bank name (loan schedule vs. statement, OCR'd vs. not),
    # and build_hdfc_transactions takes an extra `issues` argument the
    # other builders here don't.
    "Kotak Mahindra Bank": build_kotak_transactions,
    "IDBI Bank": build_idbi_transactions,
    "ICICI Bank": build_icici_transactions,
    "State Bank of India": build_sbi_transactions,
    "Axis Bank": build_axis_transactions,
    "Union Bank": build_union_transactions,
    "Jana Small Finance Bank": build_jana_transactions,
    "Punjab National Bank": build_pnb_transactions,
    "Canara Bank": build_canara_transactions,
    "Karur Vysya Bank": build_kvb_transactions,
    "Bank of Baroda": build_bob_transactions,
    "Bank of India": build_boi_transactions,
    "Bassein Catholic Co-op Bank": build_bassein_transactions,
}


def get_transaction_builder(bank):
    """Return the row-parsing function for `bank`.

    Falls back to the generic date-anchored engine for any bank without
    a dedicated module - this is the same fallback extract_bank.py's
    old if/elif chain used to fall through to.
    """
    return _BUILDERS.get(bank, build_transactions)
