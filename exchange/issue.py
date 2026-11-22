from collections.abc import Iterator
from dataclasses import dataclass
from fractions import Fraction

from bench.fractional import FractionalMap, compose_maps
from bench.stages import CalibrationStage
from bench.trial import run_trial
from exchange.calibration_body import CalibrationBody, SupportInterval, encode_body
from exchange.envelope import PieceOrigin, encode_envelope
from station.revision_store import RevisionStore


@dataclass(frozen=True)
class IssuedPiece:
    origin: PieceOrigin
    wire: str


def _add_zero(cuts: set[Fraction], coefficient: Fraction, constant: Fraction) -> None:
    if coefficient:
        cuts.add(-constant / coefficient)


def _value_relation_and_cuts(stages: tuple[CalibrationStage, ...]) -> tuple[FractionalMap | None, set[Fraction]]:
    prefix = FractionalMap(1, 0, 0, 1)
    cuts: set[Fraction] = set()
    for stage in stages:
        a, b, c, d = prefix.coefficients
        conversion = stage.conversion
        _add_zero(cuts, c, d)
        _add_zero(cuts, conversion.c * a + conversion.d * c,
                  conversion.c * b + conversion.d * d)
        limits = stage.input_range
        if limits.lower is not None:
            for edge in (limits.lower, limits.upper):
                _add_zero(cuts, a - edge * c, b - edge * d)
        try:
            prefix = compose_maps(conversion, prefix)
        except ZeroDivisionError:
            return None, set()
    return prefix, cuts


def _open_sample(lower: Fraction | None, upper: Fraction | None) -> Fraction:
    if lower is None:
        return Fraction(0) if upper is None else upper - 1
    if upper is None:
        return lower + 1
    return (lower + upper) / 2


def _support_atoms(cuts: set[Fraction]) -> Iterator[tuple[SupportInterval, Fraction]]:
    edges = [None, *sorted(cuts), None]
    for lower, upper in zip(edges, edges[1:]):
        if lower is not None:
            yield SupportInterval(lower, lower, True, True), lower
        yield SupportInterval(lower, upper), _open_sample(lower, upper)


def _calibration_body(stages: tuple[CalibrationStage, ...]) -> CalibrationBody:
    terminal, cuts = _value_relation_and_cuts(stages)
    if terminal is None:
        return CalibrationBody(None, ())
    support: list[SupportInterval] = []
    # Away from the cuts, every prefix denominator and every pulled-back
    # stage boundary has constant sign. Run the actual chain on each cell.
    for atom, sample in _support_atoms(cuts):
        if not run_trial(stages, sample).completed:
            continue
        if support and support[-1].upper == atom.lower and (
            support[-1].upper_closed or atom.lower_closed
        ):
            previous = support.pop()
            atom = SupportInterval(previous.lower, atom.upper, previous.lower_closed, atom.upper_closed)
        support.append(atom)
    if not support:
        return CalibrationBody(None, ())
    return CalibrationBody(terminal, tuple(support))


def issue(store: RevisionStore, revision_id: int) -> IssuedPiece:
    revision = store.get(revision_id)
    origin = PieceOrigin(revision.instrument.instrument_id, revision.instrument.unit,
                         revision.revision_id, revision.evidence.source)
    body = _calibration_body(revision.stages)
    return IssuedPiece(origin, encode_envelope(origin, encode_body(body)))
