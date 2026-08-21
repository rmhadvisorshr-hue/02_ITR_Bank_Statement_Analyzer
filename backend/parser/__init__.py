"""Bank-specific statement parsers.

Each module in this package owns the row-parsing logic for one bank's
layout. `parser_selector.py` is the only place that decides which module
handles a given statement - individual parsers should not need to know
about each other.

Adding support for a new bank layout should mean adding a new
`<bank>_parser.py` module (see `generic_parser.py` for the shared
helpers/regexes to build on), not patching `generic_parser.py` with
bank-specific branches.
"""
