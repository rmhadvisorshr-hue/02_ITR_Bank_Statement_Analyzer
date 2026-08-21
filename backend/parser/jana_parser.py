"""Jana Small Finance Bank statement parser.

Jana's table prints the Debit/Credit pair in the opposite column order
to every other bank the generic engine was built against, so it needs
the `swap_debit_credit` flag on the shared engine. This used to be a
`if bank == "Jana Small Finance Bank"` branch inside
`generic_parser.build_transactions` itself; moved out here so the
generic module stays bank-agnostic.
"""

from .generic_parser import build_transactions


def build_jana_transactions(text, pdf_name, bank, st_from, st_to, opening_balance, seq_start):
    return build_transactions(
        text, pdf_name, bank, st_from, st_to, opening_balance, seq_start, swap_debit_credit=True
    )
