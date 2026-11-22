from fractions import Fraction
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from bench.trial import trial
from exchange.issue import issue
from exchange.receipts import ReceiptStore
from exchange.receive import receive
from reports.export import export_report
from reports.lookup import get_report
from reports.record import decode_report
from reports.store import ReportStore
from review.compare import sign
from review.compensation import sign_compensation
from review.endpoints import encode_endpoint
from station.instruments import register_instrument
from station.revision_store import RevisionStore
from station.revisions import import_revision


def main() -> None:
    sample_path = Path(__file__).resolve().parent / 'samples' / 'calibrations.json'
    sample = json.loads(sample_path.read_text(encoding='utf-8'))
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        station = RevisionStore(root / 'station.sqlite')
        center = root / 'center.sqlite'
        archive = root / 'station.offline'
        receipts, reports = ReceiptStore(center), ReportStore(center)
        station.initialize()
        receipts.initialize()
        reports.initialize()
        for instrument in sample['instruments']:
            register_instrument(station.path, **instrument)

        revisions = {}

        def intake(specification):
            instrument = specification['instrument']
            parent = specification.get('supersedes')
            revision = import_revision(station, instrument, specification['evidence'], specification['stages'],
                                       supersedes=None if parent is None else revisions[parent])
            revisions[specification['key']] = revision
            return receive(receipts, issue(station, revision).wire, instrument)

        old_spec, new_spec, corrected_spec = sample['revisions']
        old_receipt, new_receipt = intake(old_spec), intake(new_spec)
        endpoints = [trial(station, revisions['old'], setting).value for setting in (0, 2)]
        assert endpoints == [Fraction(2), Fraction(6)]
        station.path.rename(archive)
        assert not station.path.exists()
        request = sample['comparison']
        first = sign(receipts, reports, old_receipt, new_receipt, **request)
        archive.rename(station.path)
        corrected = intake(corrected_spec)
        station.path.rename(archive)
        updated = sign(receipts, reports, old_receipt, corrected, **request)

        reopened = ReportStore(center)
        assert get_report(reopened, first.report_id) == first
        assert get_report(reopened, updated.report_id) == updated
        assert not station.path.exists()
        first_export = json.loads(export_report(reopened, first.report_id))
        updated_export = json.loads(export_report(reopened, updated.report_id))
        expected_first = {'released': [['0', '2', True, True]], 'over_tolerance': [], 'no_common_evidence': []}
        expected_updated = {'released': [], 'over_tolerance': [['0', '2', True, True]], 'no_common_evidence': []}
        assert first_export['partition'] == expected_first
        assert updated_export['partition'] == expected_updated
        assert first_export['request']['receipts'] == [old_receipt, new_receipt]
        assert updated_export['request']['receipts'] == [old_receipt, corrected]
        print(json.dumps({'bench_outputs': [str(value) for value in endpoints],
                          'first_report': first_export, 'corrected_report': updated_export}, ensure_ascii=False))
        print('POINTWISE_SCENARIO ' + json.dumps({
            'case_id': 'v4',
            'input': {'request': request, 'old': ['x+1', '2t'], 'replacement': '2x+3'},
            'expected': expected_first, 'actual': first_export['partition'],
            'downstream_expected': {'old': expected_first, 'correction': expected_updated},
            'downstream_actual': {'old': first_export['partition'], 'correction': updated_export['partition']},
            'code_paths': ['bench/trial.py', 'exchange/issue.py', 'review/compare.py', 'reports/export.py'],
            'oracle': '两端和整窗旧表末值为 2x+2，初版常差 1，勘误后常差 2。',
        }, ensure_ascii=False))
        run_compensation_demo(root, archive)


def _compensation_object(decision) -> dict:
    if decision.range is None:
        return {'status': decision.status, 'range': None}
    return {'status': decision.status,
            'range': [encode_endpoint(decision.range[0]), encode_endpoint(decision.range[1])]}


def run_compensation_demo(root: Path, archive: Path) -> None:
    sample_path = Path(__file__).resolve().parent / 'samples' / 'compensation.json'
    sample = json.loads(sample_path.read_text(encoding='utf-8'))
    station = RevisionStore(root / 'comp-station.sqlite')
    center = root / 'comp-center.sqlite'
    comp_archive = root / 'comp-station.offline'
    receipts, reports = ReceiptStore(center), ReportStore(center)
    station.initialize()
    receipts.initialize()
    reports.initialize()
    for instrument in sample['instruments']:
        register_instrument(station.path, **instrument)

    revisions, receipt_ids = {}, {}
    for specification in sample['revisions']:
        instrument = specification['instrument']
        parent = specification.get('supersedes')
        revision = import_revision(
            station, instrument, specification['evidence'], specification['stages'],
            supersedes=None if parent is None else revisions[parent],
            compensation_range=specification.get('compensation_range'))
        revisions[specification['key']] = revision
        wire = issue(station, revision).wire
        receipt_ids[specification['key']] = receive(receipts, wire, instrument)

    # 关站：站库移开后，中心只凭双实收号核定整窗补偿。
    station.path.rename(comp_archive)
    assert not station.path.exists()
    signed_cases = []
    initial_report_id = None
    for case in sample['cases']:
        signed = sign_compensation(
            receipts, reports, receipt_ids[case['left']], receipt_ids[case['right']],
            case['window'][0], case['window'][1], case['tolerance'])
        actual = _compensation_object(signed.snapshot.compensation)
        assert actual == case['expected'], (case['name'], actual, case['expected'])
        if case['name'] == 'interior_extremum':
            initial_report_id = signed.report_id
        signed_cases.append({'name': case['name'], 'compensation': actual,
                             'report_id': signed.report_id,
                             'receipts': list(signed.snapshot.request.receipt_ids)})
    assert initial_report_id is not None

    # 重开中心库，按初版报告号复领，不重新核定；再演示导出与解码。
    reopened = ReportStore(center)
    claimed = get_report(reopened, initial_report_id)
    exported = json.loads(export_report(reopened, initial_report_id))
    decoded = decode_report(export_report(reopened, initial_report_id))
    assert claimed == decoded
    assert exported['kind'] == 'window_compensation'
    assert exported['compensation'] == _compensation_object(claimed.snapshot.compensation)

    # 勘误版许可收紧，独立签为无整窗补偿；初版原号结论保持不变。
    corrected = next(case for case in signed_cases if case['name'] == 'tightened_permission')
    assert corrected['compensation'] == {'status': 'no_feasible_offset', 'range': None}
    legacy = next(case for case in signed_cases if case['name'] == 'legacy_right')
    assert legacy['compensation'] == {'status': 'missing_evidence', 'range': None}

    print(json.dumps({'closed_station_signings': signed_cases,
                      'reopened_claim': {'report_id': initial_report_id,
                                         'compensation': exported['compensation'],
                                         'origins': exported['origins'],
                                         'request': exported['request']}},
                     ensure_ascii=False))
    print('POINTWISE_SCENARIO ' + json.dumps({
        'case_id': 'compensation.window',
        'input': {'sample': 'samples/compensation.json', 'cases': len(sample['cases'])},
        'expected': {case['name']: case['expected'] for case in sample['cases']},
        'actual': {case['name']: case['compensation'] for case in signed_cases},
        'downstream_expected': {'reopened_report_id': initial_report_id,
                                'errata': 'no_feasible_offset', 'legacy': 'missing_evidence'},
        'downstream_actual': {'reopened_report_id': claimed.report_id,
                              'errata': corrected['compensation']['status'],
                              'legacy': legacy['compensation']['status']},
        'code_paths': ['station/revisions.py', 'exchange/issue.py', 'exchange/receive.py',
                       'review/compensation.py', 'reports/export.py'],
        'oracle': '全窗可行补偿为 [max(d)-t, min(d)+t] 与右件许可的闭区间交，d 为两末值差。',
    }, ensure_ascii=False))
    # 站库保持离线，中心账自足。
    assert not station.path.exists()
    comp_archive.rename(station.path)


if __name__ == '__main__':
    main()
