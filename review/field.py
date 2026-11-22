"""Exact arithmetic in a quadratic field Q(sqrt(D)) over the rationals.

An element is ``p + q*sqrt(D)`` with rational ``p`` and ``q`` and a fixed
non-square integer ``D > 0``.  Rational values are the special case ``q == 0``,
which may also move between fields freely.
"""
from dataclasses import dataclass
from fractions import Fraction
from functools import cmp_to_key
from math import isqrt

from review.endpoints import Endpoint, QuadraticEndpoint, compare_endpoints, real_quadratic_roots


def _square_part(discriminant: int) -> tuple[int, int]:
    """Split D into (square factor s, remaining nonsquare d) with D = s^2 * d."""
    square = 1
    remainder = discriminant
    factor = 2
    while factor * factor <= remainder:
        twice = factor * factor
        while remainder % twice == 0:
            remainder //= twice
            square *= factor
        factor += 1 if factor == 2 else 2
    return square, remainder


@dataclass(frozen=True)
class QuadraticNumber:
    p: Fraction
    q: Fraction
    D: int

    def __post_init__(self) -> None:
        object.__setattr__(self, 'p', Fraction(self.p))
        object.__setattr__(self, 'q', Fraction(self.q))
        if type(self.D) is not int or self.D <= 0 or isqrt(self.D) ** 2 == self.D:
            raise ValueError('D must be a positive nonsquare integer')

    @classmethod
    def rational(cls, value, D: int) -> 'QuadraticNumber':
        return cls(Fraction(value), Fraction(0), D)

    @classmethod
    def from_endpoint(cls, point: QuadraticEndpoint) -> 'QuadraticNumber':
        # alpha = (-b + branch*sqrt(discriminant)) / (2 a), then normalize the
        # irrational part onto the smallest nonsquare field discriminant.
        square, base_d = _square_part(point.discriminant)
        return cls(Fraction(-point.b, 2 * point.a),
                   Fraction(point.branch * square, 2 * point.a), base_d)

    @property
    def is_rational(self) -> bool:
        return self.q == 0

    def _field(self, other: 'QuadraticNumber') -> int:
        if self.is_rational:
            return other.D
        if other.is_rational:
            return self.D
        if self.D != other.D:
            raise ValueError('quadratic numbers must share a field')
        return self.D

    def __add__(self, other: 'QuadraticNumber') -> 'QuadraticNumber':
        D = self._field(other)
        return QuadraticNumber(self.p + other.p, self.q + other.q, D)

    def __sub__(self, other: 'QuadraticNumber') -> 'QuadraticNumber':
        D = self._field(other)
        return QuadraticNumber(self.p - other.p, self.q - other.q, D)

    def __neg__(self) -> 'QuadraticNumber':
        return QuadraticNumber(-self.p, -self.q, self.D)

    def __mul__(self, other: 'QuadraticNumber') -> 'QuadraticNumber':
        D = self._field(other)
        return QuadraticNumber(self.p * other.p + self.q * other.q * D,
                               self.p * other.q + self.q * other.p, D)

    def __truediv__(self, other: 'QuadraticNumber') -> 'QuadraticNumber':
        D = self._field(other)
        norm = other.p * other.p - other.q * other.q * D
        if not norm:
            raise ZeroDivisionError('division by a zero quadratic number')
        conjugate = QuadraticNumber(other.p, -other.q, D)
        numerator = self * conjugate
        return QuadraticNumber(numerator.p / norm, numerator.q / norm, D)

    def to_endpoint(self) -> Endpoint:
        if self.is_rational:
            return self.p
        # Build (x - p)^2 = q^2 D, i.e. x^2 - 2p x + (p^2 - q^2 D) = 0, and
        # let real_quadratic_roots reduce it to the primitive endpoint.
        roots = real_quadratic_roots(Fraction(1), -2 * self.p,
                                     self.p * self.p - self.q * self.q * self.D)
        irrational = [root for root in roots if isinstance(root, QuadraticEndpoint)]
        if len(irrational) != 2:
            raise ValueError('quadratic value did not reduce to algebraic endpoints')
        smaller, larger = sorted(irrational, key=cmp_to_key(compare_endpoints))
        return larger if self.q > 0 else smaller
