"""V1-parity commercial rules: tariffs, billing, invoices and client outages.

Ported from the frozen V1 `monitoring_board.reporting` modules, which depend
only on their own dataclasses and the standard library: no database, no Flask,
no provider. Parity is the requirement, so these are copies under V2 ownership
rather than reinterpretations, and every one of them is pinned by a golden test
that runs both implementations over the same inputs.
"""
