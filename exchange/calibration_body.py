from dataclasses import dataclass
from fractions import Fraction

from bench.fractional import FractionalMap, parse_map, parse_rational
from exchange.envelope import canonical_json, read_json
from station.compensation import parse_compensation_range


TerminalRelation = FractionalMap


@dataclass(frozen=True)
class SupportInterval:
    lower: Fraction | None
    upper: Fraction | None
    lower_closed: bool = False
    upper_closed: bool = False

    def __post_init__(self) -> None:
        if type(self.lower_closed) is not bool or type(self.upper_closed) is not bool:
            raise ValueError('support endpoint flags must be booleans')
        for name, closed in (('lower', self.lower_closed), ('upper', self.upper_closed)):
            value = getattr(self, name)
            if value is None:
                if closed:
                    raise ValueError('an unbounded endpoint must be open')
            else:
                object.__setattr__(self, name, parse_rational(value))
        if self.lower is not None and self.upper is not None:
            if self.lower > self.upper or (self.lower == self.upper and not (
                self.lower_closed and self.upper_closed
            )):
                raise ValueError('support interval is empty or reversed')

    def contains(self, value: Fraction) -> bool:
        left = self.lower is None or value > self.lower or (self.lower_closed and value == self.lower)
        right = self.upper is None or value < self.upper or (self.upper_closed and value == self.upper)
        return left and right


@dataclass(frozen=True)
class CalibrationBody:
    terminal: TerminalRelation | None
    support: tuple[SupportInterval, ...]
    compensation_range: tuple[Fraction, Fraction] | None = None

    def __post_init__(self) -> None:
        if bool(self.support) != (self.terminal is not None):
            raise ValueError('a terminal relation is required exactly for nonempty support')
        for previous, interval in zip(self.support, self.support[1:]):
            if previous.upper is None or interval.lower is None or previous.upper > interval.lower:
                raise ValueError('support components overlap or are out of order')
            if previous.upper == interval.lower and (previous.upper_closed or interval.lower_closed):
                raise ValueError('touching support components must be merged')
        if self.terminal is not None:
            pole = self.terminal.pole
            if pole is not None and any(interval.contains(pole) for interval in self.support):
                raise ValueError('support contains a terminal pole')
        if self.compensation_range is not None:
            parse_compensation_range(self.compensation_range)

    def supports(self, setting) -> bool:
        value = parse_rational(setting)
        return any(interval.contains(value) for interval in self.support)


def _range_data(permission: tuple[Fraction, Fraction] | None) -> list[str] | None:
    if permission is None:
        return None
    return [str(permission[0]), str(permission[1])]


def encode_body(body: CalibrationBody, version: int = 1) -> str:
    relation = None if body.terminal is None else [str(value) for value in body.terminal.coefficients]
    support = [
        [None if interval.lower is None else str(interval.lower),
         None if interval.upper is None else str(interval.upper),
         interval.lower_closed, interval.upper_closed]
        for interval in body.support
    ]
    data = {'terminal': relation, 'support': support}
    if version == 2:
        if body.compensation_range is None:
            raise ValueError('a V2 body requires a compensation range')
        data['compensation_range'] = _range_data(body.compensation_range)
    elif version != 1:
        raise ValueError('unsupported envelope version')
    return canonical_json(data)


def decode_body(encoded: str, version: int | None = None) -> CalibrationBody:
    raw = read_json(encoded)
    if not isinstance(raw, dict):
        raise ValueError('invalid calibration body')
    base_fields = {'terminal', 'support'}
    if set(raw) not in (base_fields, {*base_fields, 'compensation_range'}):
        raise ValueError('invalid calibration body fields')
    has_range = 'compensation_range' in raw
    if version == 1 and has_range:
        raise ValueError('V1 body must not carry a compensation range')
    if version == 2 and not has_range:
        raise ValueError('V2 body requires a compensation range')
    if version is not None and version not in (1, 2):
        raise ValueError('unsupported envelope version')
    terminal = raw['terminal']
    if terminal is not None:
        if not isinstance(terminal, list) or any(type(value) is not str for value in terminal):
            raise ValueError('terminal coefficients must be rational strings')
        terminal = parse_map(terminal)
    if not isinstance(raw['support'], list):
        raise ValueError('support must be a list')
    intervals = []
    for part in raw['support']:
        if not isinstance(part, list) or len(part) != 4:
            raise ValueError('each support component needs two endpoints and two flags')
        lower, upper, lower_closed, upper_closed = part
        if any(value is not None and type(value) is not str for value in (lower, upper)):
            raise ValueError('finite support endpoints must be rational strings')
        intervals.append(SupportInterval(lower, upper, lower_closed, upper_closed))
    permission = None
    if has_range:
        try:
            permission = parse_compensation_range(raw['compensation_range'])
        except ValueError as error:
            raise ValueError('invalid V2 compensation range') from error
    return CalibrationBody(terminal, tuple(intervals), permission)
