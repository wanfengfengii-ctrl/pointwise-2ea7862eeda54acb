from dataclasses import dataclass
import re

from exchange.envelope import PieceOrigin, canonical_json, read_json, valid_number
from review.endpoints import decode_endpoint, encode_endpoint
from review.partition import DecisionInterval, DecisionPartition
from review.requests import ComparisonRequest, validate_request


COMPENSATION_STATUSES = ('allowed', 'missing_evidence', 'no_feasible_offset')


@dataclass(frozen=True)
class CompensationDecision:
    status: str
    range: tuple | None

    def __post_init__(self) -> None:
        if self.status not in COMPENSATION_STATUSES:
            raise ValueError('unknown compensation status')
        if self.status == 'allowed':
            if not isinstance(self.range, tuple) or len(self.range) != 2:
                raise ValueError('an allowed compensation requires a closed endpoint pair')
        elif self.range is not None:
            raise ValueError('only allowed compensation carries a range')


@dataclass(frozen=True)
class ReportSnapshot:
    request: ComparisonRequest
    origins: tuple[PieceOrigin, PieceOrigin]
    partition: DecisionPartition | None = None
    kind: str = 'pointwise_comparison'
    compensation: CompensationDecision | None = None

    def __post_init__(self) -> None:
        if self.kind == 'pointwise_comparison':
            if self.partition is None or self.compensation is not None:
                raise ValueError('pointwise snapshots carry a partition')
        elif self.kind == 'window_compensation':
            if self.compensation is None or self.partition is not None:
                raise ValueError('compensation snapshots carry a compensation decision')
        else:
            raise ValueError('unknown report kind')


@dataclass(frozen=True)
class ReportReceipt:
    report_id: int
    token: str

    def __post_init__(self) -> None:
        if not valid_number(self.report_id) or not isinstance(self.token, str) or not re.fullmatch('[0-9a-f]{64}', self.token):
            raise ValueError('invalid report receipt')


@dataclass(frozen=True)
class SignedReport:
    snapshot: ReportSnapshot
    receipt: ReportReceipt

    @property
    def report_id(self) -> int:
        return self.receipt.report_id


def _interval_data(interval: DecisionInterval) -> list:
    return [encode_endpoint(interval.lower), encode_endpoint(interval.upper),
            interval.lower_closed, interval.upper_closed]


def _interval_from_data(raw: object) -> DecisionInterval:
    if not isinstance(raw, list) or len(raw) != 4:
        raise ValueError('invalid interval in report snapshot')
    lower, upper, lower_closed, upper_closed = raw
    return DecisionInterval(decode_endpoint(lower), decode_endpoint(upper), lower_closed, upper_closed)


def _request_data(request: ComparisonRequest) -> dict:
    return {'receipts': list(request.receipt_ids),
            'window': [str(request.lower), str(request.upper)],
            'tolerance': str(request.tolerance)}


def _compensation_data(decision: CompensationDecision) -> dict:
    if decision.status != 'allowed':
        return {'status': decision.status, 'range': None}
    lower, upper = decision.range
    return {'status': decision.status,
            'range': [encode_endpoint(lower), encode_endpoint(upper)]}


def snapshot_data(snapshot: ReportSnapshot) -> dict:
    data = {
        'request': _request_data(snapshot.request),
        'origins': [origin.as_data() for origin in snapshot.origins],
    }
    if snapshot.kind == 'window_compensation':
        data['kind'] = 'window_compensation'
        data['compensation'] = _compensation_data(snapshot.compensation)
    else:
        partition = snapshot.partition
        data['partition'] = {
            'released': [_interval_data(interval) for interval in partition.released],
            'over_tolerance': [_interval_data(interval) for interval in partition.over_tolerance],
            'no_common_evidence': [_interval_data(interval) for interval in partition.no_common_evidence],
        }
    return data


def _fields(raw: object, *names: str) -> dict:
    if not isinstance(raw, dict) or set(raw) != set(names):
        raise ValueError('invalid report snapshot fields')
    return raw


def _pair(raw: object) -> list:
    if not isinstance(raw, list) or len(raw) != 2:
        raise ValueError('expected an ordered pair in report snapshot')
    return raw


def _intervals(raw: object) -> tuple[DecisionInterval, ...]:
    if not isinstance(raw, list):
        raise ValueError('expected an interval group')
    return tuple(_interval_from_data(part) for part in raw)


def _request_from_data(raw: object) -> ComparisonRequest:
    requested = _fields(raw, 'receipts', 'window', 'tolerance')
    left, right = _pair(requested['receipts'])
    lower, upper = _pair(requested['window'])
    return validate_request(left, right, lower, upper, requested['tolerance'])


def _compensation_from_data(raw: object) -> CompensationDecision:
    fields = _fields(raw, 'status', 'range')
    status = fields['status']
    if status not in COMPENSATION_STATUSES:
        raise ValueError('unknown compensation status')
    if status != 'allowed':
        if fields['range'] is not None:
            raise ValueError('range must be null outside allowed compensation')
        return CompensationDecision(status, None)
    endpoints = _pair(fields['range'])
    return CompensationDecision('allowed', (decode_endpoint(endpoints[0]),
                                            decode_endpoint(endpoints[1])))


def snapshot_from_data(data: object) -> ReportSnapshot:
    if not isinstance(data, dict):
        raise ValueError('invalid report snapshot')
    kind = data.get('kind', 'pointwise_comparison')
    if kind == 'window_compensation':
        raw = _fields(data, 'request', 'origins', 'kind', 'compensation', 'receipt') \
            if 'receipt' in data else _fields(data, 'request', 'origins', 'kind', 'compensation')
        request = _request_from_data(raw['request'])
        first, second = _pair(raw['origins'])
        origins = (PieceOrigin.from_data(first), PieceOrigin.from_data(second))
        decision = _compensation_from_data(raw['compensation'])
        return ReportSnapshot(request, origins, None, 'window_compensation', decision)
    if 'partition' not in data:
        raise ValueError('invalid report snapshot fields')
    raw = _fields(data, 'request', 'origins', 'partition', 'receipt') \
        if 'receipt' in data else _fields(data, 'request', 'origins', 'partition')
    request = _request_from_data(raw['request'])
    first, second = _pair(raw['origins'])
    origins = (PieceOrigin.from_data(first), PieceOrigin.from_data(second))
    groups = _fields(raw['partition'], 'released', 'over_tolerance', 'no_common_evidence')
    partition = DecisionPartition(_intervals(groups['released']), _intervals(groups['over_tolerance']),
                                  _intervals(groups['no_common_evidence']))
    return ReportSnapshot(request, origins, partition)


def encode_report(report: SignedReport) -> str:
    data = snapshot_data(report.snapshot)
    data['receipt'] = {'report_id': report.report_id, 'token': report.receipt.token}
    return canonical_json(data)


def decode_report(text: str) -> SignedReport:
    data = read_json(text)
    if not isinstance(data, dict) or 'receipt' not in data:
        raise ValueError('invalid exported report')
    receipt = _fields(data['receipt'], 'report_id', 'token')
    snapshot = snapshot_from_data(data)
    return SignedReport(snapshot, ReportReceipt(receipt['report_id'], receipt['token']))
