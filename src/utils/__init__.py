"""Shared utility helpers."""
from decimal import Decimal


def format_decimal_compact(value: Decimal) -> str:
    """Format a finite Decimal without insignificant trailing zeroes."""
    if not value.is_finite():
        return str(value)
    return format(value.normalize(), "f")
