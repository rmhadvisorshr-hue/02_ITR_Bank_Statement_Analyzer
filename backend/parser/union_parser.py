"""Union Bank of India statement parser.

No Union Bank sample was in the failing-PDF batch this module was
written against, so this currently delegates straight to the generic
date-anchored engine (byte-for-byte the same result the old
`else: build_transactions(...)` fallback in extract_bank.py produced
for any bank it didn't special-case).

Kept as its own module - rather than folded into the generic fallback
- so a future Union Bank-specific layout fix lands here without
touching `generic_parser.py` or risking any other bank.
"""

from .generic_parser import build_transactions


def build_union_transactions(text, pdf_name, bank, st_from, st_to, opening_balance, seq_start):
    return build_transactions(text, pdf_name, bank, st_from, st_to, opening_balance, seq_start)
