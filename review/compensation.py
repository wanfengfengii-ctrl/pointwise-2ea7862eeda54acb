from fractions import Fraction
from functools import cmp_to_key

from exchange.calibration_body import CalibrationBody
from exchange.receipts import ReceiptStore
from reports.publish import publish_compensation
from reports.record import CompensationDecision, SignedReport
from reports.store import ReportStore
from review.compare import _covers
from review.endpoints import (Endpoint, QuadraticEndpoint, compare_endpoints,
                              rational_between, real_quadratic_roots)
from review.field import QuadraticNumber
from review.requests import ComparisonRequest, InvalidComparisonRequest, validate_request


def _window_has_common_evidence(request: ComparisonRequest, left: CalibrationBody,
                                right: CalibrationBody) -> bool:
    if not left.support or not right.support:
        return False
    cuts = {request.lower, request.upper}
    for body in (left, right):
        for part in body.support:
            for edge in (part.lower, part.upper):
                if edge is not None and request.lower <= edge <= request.upper:
                    cuts.add(edge)
    ordered = sorted(cuts)

    def covered(point: Fraction) -> bool:
        return _covers(left, point) and _covers(right, point)

    for index, point in enumerate(ordered):
        if not covered(point):
            return False
        if index + 1 < len(ordered):
            if not covered(rational_between(point, ordered[index + 1])):
                return False
    return True


def _product(a: Fraction, b: Fraction, c: Fraction, d: Fraction):
    return a * c, a * d + b * c, b * d


def _difference_polynomials(left, right):
    """N, M with d(x) = left(x) - right(x) = N(x) / M(x)."""
    left_term = _product(left.a, left.b, right.c, right.d)
    right_term = _product(right.a, right.b, left.c, left.d)
    numerator = tuple(a - b for a, b in zip(left_term, right_term))
    denominator = _product(left.c, left.d, right.c, right.d)
    return numerator, denominator


def _convolve(left: tuple, right: tuple) -> tuple:
    # Coefficients stay ordered from highest degree to the constant term.
    degree = len(left) + len(right) - 2
    result = [Fraction(0)] * (degree + 1)
    for i, a in enumerate(left):
        for j, b in enumerate(right):
            result[i + j] += a * b
    return tuple(result)


def _derivative(polynomial: tuple) -> tuple:
    derived = tuple(coefficient * (len(polynomial) - 1 - index)
                    for index, coefficient in enumerate(polynomial[:-1]))
    return derived or (Fraction(0),)


def _stationary_polynomial(numerator: tuple, denominator: tuple) -> tuple:
    # N'M - NM' = 0 collects the interior extrema of N/M.
    stationary = tuple(a - b for a, b in zip(_convolve(_derivative(numerator), denominator),
                                             _convolve(numerator, _derivative(denominator))))
    padded = (Fraction(0),) * (3 - len(stationary)) + stationary
    return padded[-3:]


def _eval_rational_polynomial(polynomial: tuple, point: Fraction) -> Fraction:
    value = Fraction(0)
    for coefficient in polynomial:
        value = value * point + coefficient
    return value


def _eval_field_polynomial(polynomial: tuple, point: QuadraticEndpoint) -> QuadraticNumber:
    field_D = point.discriminant
    value = QuadraticNumber.rational(0, field_D)
    for coefficient in polynomial:
        value = value * QuadraticNumber.from_endpoint(point) + QuadraticNumber.rational(
            coefficient, field_D)
    return value


def _difference_values(request: ComparisonRequest, left: CalibrationBody,
                       right: CalibrationBody) -> list:
    """Exact d(x) at every candidate extremum point inside the window."""
    numerator, denominator = _difference_polynomials(left.terminal, right.terminal)
    candidates: list[Endpoint] = [request.lower, request.upper]
    stationary = _stationary_polynomial(numerator, denominator)
    for root in real_quadratic_roots(*stationary):
        if compare_endpoints(request.lower, root) <= 0 and compare_endpoints(
                root, request.upper) <= 0:
            candidates.append(root)
    values = []
    for point in candidates:
        if isinstance(point, QuadraticEndpoint):
            ratio = (_eval_field_polynomial(numerator, point)
                     / _eval_field_polynomial(denominator, point))
        else:
            ratio = (_eval_rational_polynomial(numerator, point)
                     / _eval_rational_polynomial(denominator, point))
        values.append(ratio)
    return values


def _as_endpoint(value) -> Endpoint:
    return value.to_endpoint() if isinstance(value, QuadraticNumber) else value


def _quadratic_plus_rational(point: QuadraticEndpoint, amount: Fraction) -> Endpoint:
    # alpha is a root of a x^2 + b x + c = 0; alpha + r is a root of
    # a y^2 + (b - 2 a r) y + (a r^2 - b r + c) = 0.
    a, b, c = point.a, point.b, point.c
    shifted = (a, b - 2 * a * amount, a * amount * amount - b * amount + c)
    roots = sorted(real_quadratic_roots(*shifted), key=cmp_to_key(compare_endpoints))
    return roots[1] if point.branch == 1 else roots[0]


def _shift_endpoint(point: Endpoint, amount: Fraction) -> Endpoint:
    if isinstance(point, Fraction):
        return point + amount
    return _quadratic_plus_rational(point, amount)


def evaluate_compensation(request: ComparisonRequest, left: CalibrationBody,
                          right: CalibrationBody) -> CompensationDecision:
    if right.compensation_range is None:
        return CompensationDecision('missing_evidence', None)
    if not _window_has_common_evidence(request, left, right):
        return CompensationDecision('missing_evidence', None)
    values = _difference_values(request, left, right)
    ordered = sorted((_as_endpoint(value) for value in values), key=cmp_to_key(compare_endpoints))
    minimum, maximum = ordered[0], ordered[-1]
    # Every window point requires d(x)-t <= k <= d(x)+t simultaneously, so the
    # whole-window feasible interval is [max d - t, min d + t].
    feasible_lower = _shift_endpoint(maximum, -request.tolerance)
    feasible_upper = _shift_endpoint(minimum, request.tolerance)
    permission_lower, permission_upper = right.compensation_range
    lower_pair = sorted((feasible_lower, permission_lower), key=cmp_to_key(compare_endpoints))
    upper_pair = sorted((feasible_upper, permission_upper), key=cmp_to_key(compare_endpoints))
    final_lower, final_upper = lower_pair[1], upper_pair[0]
    if compare_endpoints(final_lower, final_upper) > 0:
        return CompensationDecision('no_feasible_offset', None)
    return CompensationDecision('allowed', (final_lower, final_upper))


def sign_compensation(receipts: ReceiptStore, reports: ReportStore, left_receipt: int,
                      right_receipt: int, lower, upper, tolerance) -> SignedReport:
    request = validate_request(left_receipt, right_receipt, lower, upper, tolerance)
    if receipts.path.resolve() != reports.path.resolve():
        raise InvalidComparisonRequest('receipt and report stores must use the same center database')
    try:
        left = receipts.get(request.left_receipt)
        right = receipts.get(request.right_receipt)
    except KeyError as error:
        raise InvalidComparisonRequest(f'unknown received piece {error.args[0]}') from error
    if left.origin.unit != right.origin.unit:
        raise InvalidComparisonRequest('received pieces must use the same unit')
    decision = evaluate_compensation(request, left.body, right.body)
    return publish_compensation(reports, request, decision, left, right)
