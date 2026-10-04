"""Email normalization helpers.

Email addresses are case-insensitive for local-part matching in this product.
Every write path storing a recipient email must run it through ``normalize_email``
so duplicate detection and suppression lookups behave consistently.
"""

from __future__ import annotations


def normalize_email(value: str) -> str:
    """Return a normalized, comparable email address."""
    return value.strip().lower()