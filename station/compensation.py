from fractions import Fraction

from bench.fractional import parse_rational


CompensationRange = tuple[Fraction, Fraction]


def parse_compensation_range(raw: object) -> CompensationRange | None:
    """Validate a bench-approved closed rational permission interval."""
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise ValueError('compensation range requires two ordered rational endpoints')
    lower, upper = (parse_rational(value) for value in raw)
    if lower > upper:
        raise ValueError('compensation range endpoints are reversed')
    return lower, upper
