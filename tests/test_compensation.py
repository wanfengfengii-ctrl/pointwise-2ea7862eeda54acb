import json
import sqlite3
from contextlib import closing
from fractions import Fraction as Q

from exchange.envelope import (PieceOrigin, encode_envelope,
                               encode_versioned_envelope)
from exchange.issue import issue
from exchange.receive import RejectedPiece, receive
from reports.export import export_report
from reports.lookup import get_report
from reports.record import CompensationDecision, decode_report
from reports.store import ReportStore
from review.compare import sign as sign_pointwise
from review.compensation import sign_compensation
from review.endpoints import QuadraticEndpoint
from review.requests import InvalidComparisonRequest
from station.instruments import register_instrument
from station.revisions import InvalidRevision, import_revision
from tests.support import HandoverCase, IDENTITY, stage, table_counts


RECIPROCAL_NEG = (0, -1, 1, 0)
DOUBLE_RECIPROCAL_NEG = (0, -2, 1, 0)


class CompensationCase(HandoverCase):
    def freeze_permitted(self, instrument, stages, source, permission,
                         supersedes=None):
        evidence = {'source': source,
                    'records': {'primary': '原校验单首项', 'secondary': '原校验单后项'}}
        return import_revision(self.station, instrument, evidence, stages,
                               supersedes=supersedes, compensation_range=list(permission))


class TestCompensationFreeze(CompensationCase):
    def test_permission_freeze_and_correction_independence(self):
        first = self.freeze('NEW-B', [stage('inverse', RECIPROCAL_NEG, ['1/2', 2])],
                            '带许可初版')
        import_revision(self.station, 'NEW-B',
                        {'source': '独立许可单', 'records': {'primary': '许可正文'}},
                        [stage('inverse', RECIPROCAL_NEG, ['1/2', 2])],
                        supersedes=first, compensation_range=['0', '3'])
        self.assertIsNone(self.station.get(first).compensation_range)
        corrected = self.freeze('NEW-B', [stage('inverse', RECIPROCAL_NEG, ['1/2', 2])],
                                '无许可勘误', first)
        self.assertIsNone(self.station.get(corrected).compensation_range)
        names = ('instruments', 'calibration_evidence', 'revisions', 'revision_stages')
        baseline = table_counts(self.station_path, *names)
        with self.assertRaises(InvalidRevision):
            self.freeze_permitted('NEW-B', [stage('inverse', RECIPROCAL_NEG)], '逆序许可',
                                  ('3', '0'))
        with self.assertRaises(InvalidRevision):
            self.freeze_permitted('NEW-B', [stage('inverse', RECIPROCAL_NEG)], '坏端点许可',
                                  ('1/0', '1'))
        with self.assertRaises(InvalidRevision):
            self.freeze_permitted('NEW-B', [stage('inverse', RECIPROCAL_NEG)], '回滚许可',
                                  ('0',))
        self.assertEqual(table_counts(self.station_path, *names), baseline)

    def test_single_point_and_negative_permission(self):
        revision = self.freeze_permitted(
            'NEW-B', [stage('identity', IDENTITY, [0, 2])], '零许可单', ('0', '0'))
        self.assertEqual(self.station.get(revision).compensation_range, (Q(0), Q(0)))
        negative = self.freeze_permitted(
            'NEW-B', [stage('identity', IDENTITY, [0, 2])], '负许可单', ('-2', '-1/2'))
        self.assertEqual(self.station.get(negative).compensation_range, (Q(-2), Q(-1, 2)))

    def test_legacy_revision_keeps_v1_wire(self):
        revision = self.freeze('NEW-B', [stage('identity', IDENTITY)], '旧格式件')
        wire = json.loads(issue(self.station, revision).wire)
        self.assertEqual(wire['version'], 1)
        self.assertNotIn('compensation_range', json.loads(wire['body']))

    def test_correction_never_changes_the_prior_permission(self):
        first = self.freeze_permitted(
            'NEW-B', [stage('inverse', RECIPROCAL_NEG, ['1/2', 2])], '宽许可初版', ('0', '4'))
        corrected = self.freeze_permitted(
            'NEW-B', [stage('inverse', RECIPROCAL_NEG, ['1/2', 2])], '收紧勘误',
            ('0', '5/2'), supersedes=first)
        self.assertEqual(self.station.get(first).compensation_range, (Q(0), Q(4)))
        self.assertEqual(self.station.get(corrected).compensation_range, (Q(0), Q(5, 2)))
        dropped = self.freeze('NEW-B', [stage('inverse', RECIPROCAL_NEG, ['1/2', 2])],
                              '撤销许可勘误', corrected)
        self.assertIsNone(self.station.get(dropped).compensation_range)
        self.assertEqual(self.station.get(first).compensation_range, (Q(0), Q(4)))
        self.assertEqual(self.station.get(corrected).compensation_range, (Q(0), Q(5, 2)))

    def test_non_rational_permission_is_rejected_without_rows(self):
        names = ('instruments', 'calibration_evidence', 'revisions', 'revision_stages')
        baseline = table_counts(self.station_path, *names)
        for bad_permission in ((0.5, 1), (True, 1), (('0',),), ('0', 1, 2)):
            with self.subTest(bad=bad_permission), self.assertRaises(InvalidRevision):
                self.freeze_permitted('NEW-B', [stage('identity', IDENTITY)], '坏许可',
                                      bad_permission)
        self.assertEqual(table_counts(self.station_path, *names), baseline)


class TestCompensationExchange(CompensationCase):
    def _freeze_permitted(self, permission=('0', '3')):
        return self.freeze_permitted(
            'NEW-B', [stage('inverse', RECIPROCAL_NEG, ['1/2', 2])], '许可试车单', permission)

    def test_v2_issue_receive_and_receipt_permission(self):
        revision = self._freeze_permitted()
        wire = json.loads(issue(self.station, revision).wire)
        self.assertEqual(wire['version'], 2)
        body = json.loads(wire['body'])
        self.assertEqual(set(body), {'support', 'terminal', 'compensation_range'})
        self.assertEqual(body['compensation_range'], ['0', '3'])
        receipt_id = receive(self.receipts, json.dumps(wire), 'NEW-B')
        piece = self.receipts.get(receipt_id)
        self.assertEqual(piece.compensation_range, (Q(0), Q(3)))
        self.assertEqual(issue(self.station, revision).wire,
                         issue(self.station, revision).wire)

    def test_version_field_mixing_is_rejected(self):
        revision = self._freeze_permitted()
        wire = json.loads(issue(self.station, revision).wire)
        tampered = dict(wire)
        tampered['version'] = 1
        from exchange.envelope import canonical_json
        from hashlib import sha256
        payload = {'version': 1, 'origin': wire['origin'], 'body': wire['body']}
        tampered['digest'] = sha256(canonical_json(payload).encode()).hexdigest()
        with self.assertRaises(RejectedPiece):
            receive(self.receipts, canonical_json(tampered), 'NEW-B')
        self.assertEqual(self.receipt_count(), 0)

    def test_v2_without_range_is_rejected(self):
        revision = self._freeze_permitted()
        wire = json.loads(issue(self.station, revision).wire)
        body = json.loads(wire['body'])
        del body['compensation_range']
        bad = encode_versioned_envelope(PieceOrigin.from_data(wire['origin']),
                                        json.dumps(body), 2)
        with self.assertRaises(RejectedPiece):
            receive(self.receipts, bad, 'NEW-B')
        self.assertEqual(self.receipt_count(), 0)

    def test_v1_with_range_is_rejected(self):
        origin = PieceOrigin('NEW-B', 'bar', 1, '混版件')
        body = json.dumps({'terminal': ['1', '0', '0', '1'], 'support': [],
                           'compensation_range': ['0', '1']})
        with self.assertRaises(RejectedPiece):
            receive(self.receipts, encode_envelope(origin, body), 'NEW-B')
        self.assertEqual(self.receipt_count(), 0)

    def test_damaged_v2_range_is_rejected(self):
        revision = self._freeze_permitted()
        wire = json.loads(issue(self.station, revision).wire)
        body = json.loads(wire['body'])
        body['compensation_range'] = ['2', '0']
        bad = encode_versioned_envelope(PieceOrigin.from_data(wire['origin']), json.dumps(body), 2)
        with self.assertRaises(RejectedPiece):
            receive(self.receipts, bad, 'NEW-B')
        self.assertEqual(self.receipt_count(), 0)


class TestCompensationSigning(CompensationCase):
    def _setup_pieces(self):
        left_revision = import_revision(
            self.station, 'OLD-A',
            {'source': '旧表试车单', 'records': {'bench': '直读'}},
            [stage('read', IDENTITY, None, 'bench')])
        incident = import_revision(
            self.station, 'NEW-B',
            {'source': '负倒数试车单', 'records': {'bench': '末值为负倒数'}},
            [stage('inverse', RECIPROCAL_NEG, ['1/2', 2], 'bench')],
            compensation_range=['0', '3'])
        wide = import_revision(
            self.station, 'NEW-B',
            {'source': '宽许可试车单', 'records': {'bench': '末值为负二倍倒数'}},
            [stage('gain', DOUBLE_RECIPROCAL_NEG, [1, 2], 'bench')],
            compensation_range=['0', '4'])
        corrected = import_revision(
            self.station, 'NEW-B',
            {'source': '许可勘误单', 'records': {'bench': '许可收紧到 5/2'}},
            [stage('gain', DOUBLE_RECIPROCAL_NEG, [1, 2], 'bench')],
            supersedes=wide, compensation_range=['0', '5/2'])
        legacy = import_revision(
            self.station, 'NEW-B',
            {'source': '旧格式原单', 'records': {'bench': '仅登记换算'}},
            [stage('gain', DOUBLE_RECIPROCAL_NEG, [1, 2], 'bench')])
        left = receive(self.receipts, issue(self.station, left_revision).wire, 'OLD-A')
        ids = {key: receive(self.receipts, issue(self.station, revision).wire, 'NEW-B')
               for key, revision in (('incident', incident), ('wide', wide),
                                     ('corrected', corrected), ('legacy', legacy))}
        return left, ids

    def test_sample_decisions(self):
        left, ids = self._setup_pieces()
        self.detach_station()
        cases = [
            (ids['incident'], '1/2', 2, '1/5',
             CompensationDecision('no_feasible_offset', None)),
            (ids['incident'], '1/2', 2, '1/4',
             CompensationDecision('allowed', (Q(9, 4), Q(9, 4)))),
        ]
        for right, lo, hi, tol, expected in cases:
            with self.subTest(tol=tol):
                report = sign_compensation(self.receipts, self.reports, left, right, lo, hi, tol)
                self.assertEqual(report.snapshot.compensation, expected)
                self.assertEqual(report.snapshot.kind, 'window_compensation')
        root = QuadraticEndpoint(16, -8, -127, 1)
        interior = sign_compensation(self.receipts, self.reports, left, ids['wide'], 1, 2, '1/4')
        self.assertEqual(interior.snapshot.compensation,
                         CompensationDecision('allowed', (Q(11, 4), root)))
        restricted = sign_compensation(self.receipts, self.reports, left, ids['wide'],
                                       '3/2', 2, '1/4')
        self.assertEqual(restricted.snapshot.compensation,
                         CompensationDecision('allowed', (Q(11, 4), Q(37, 12))))
        tightened = sign_compensation(self.receipts, self.reports, left, ids['corrected'],
                                      1, 2, '1/4')
        self.assertEqual(tightened.snapshot.compensation,
                         CompensationDecision('no_feasible_offset', None))
        legacy = sign_compensation(self.receipts, self.reports, left, ids['legacy'], 1, 2, '1/4')
        self.assertEqual(legacy.snapshot.compensation,
                         CompensationDecision('missing_evidence', None))

    def test_single_point_window_and_zero_tolerance(self):
        left_revision = import_revision(
            self.station, 'OLD-A',
            {'source': '旧表试车单', 'records': {'bench': '直读'}},
            [stage('read', IDENTITY, [0, 2], 'bench')])
        right_revision = import_revision(
            self.station, 'NEW-B',
            {'source': '倒数许可单', 'records': {'bench': '负倒数，许可 0..3'}},
            [stage('inverse', RECIPROCAL_NEG, ['1/2', 2], 'bench')],
            compensation_range=['0', '3'])
        left = receive(self.receipts, issue(self.station, left_revision).wire, 'OLD-A')
        right = receive(self.receipts, issue(self.station, right_revision).wire, 'NEW-B')
        self.detach_station()
        # At x=1, d=2; with t=0 the only feasible offset is the single point 2.
        point = sign_compensation(self.receipts, self.reports, left, right, 1, 1, 0)
        self.assertEqual(point.snapshot.compensation,
                         CompensationDecision('allowed', (Q(2), Q(2))))
        exported = json.loads(export_report(self.reports, point.report_id))
        self.assertEqual(exported['compensation']['range'], ['2', '2'])
        # A point outside both supports is missing evidence.
        outside = sign_compensation(self.receipts, self.reports, left, right, 3, 3, 0)
        self.assertEqual(outside.snapshot.compensation.status, 'missing_evidence')

    def test_missing_evidence_hole(self):
        left_revision = import_revision(
            self.station, 'OLD-A',
            {'source': '双级复测单', 'records': {'bench': 'x=1 首级无读数'}},
            [stage('first', (1, 0, 1, -1), [0, 2], 'bench'),
             stage('second', (1, 0, 1, -1), None, 'bench')])
        right_revision = import_revision(
            self.station, 'NEW-B',
            {'source': '零修正对照单', 'records': {'bench': '直读且只准零补偿'}},
            [stage('read', IDENTITY, [0, 2], 'bench')],
            compensation_range=['0', '0'])
        left = receive(self.receipts, issue(self.station, left_revision).wire, 'OLD-A')
        right = receive(self.receipts, issue(self.station, right_revision).wire, 'NEW-B')
        self.detach_station()
        report = sign_compensation(self.receipts, self.reports, left, right, 0, 2, 0)
        self.assertEqual(report.snapshot.compensation,
                         CompensationDecision('missing_evidence', None))

    def test_invalid_requests_leave_no_records(self):
        left, ids = self._setup_pieces()
        self.detach_station()
        for arguments in ((left, 999, 0, 2, 0), (999, ids['wide'], 0, 2, 0),
                          (left, ids['wide'], 2, 0, 0), (left, ids['wide'], 0, 2, -1),
                          (left, ids['wide'], 0.5, 2, 0)):
            with self.subTest(arguments=arguments), self.assertRaises(InvalidComparisonRequest):
                sign_compensation(self.receipts, self.reports, *arguments)
        self.assertEqual(self.signed_counts(), (0, 0))

    def test_mixed_units_rejected(self):
        register_instrument(self.station_path, 'THIRD', '异单位表', 'psi')
        other = import_revision(
            self.station, 'THIRD',
            {'source': 'psi 原单', 'records': {'bench': '直读'}},
            [stage('read', IDENTITY, [0, 2], 'bench')],
            compensation_range=['0', '1'])
        left_revision = import_revision(
            self.station, 'OLD-A',
            {'source': 'bar 原单', 'records': {'bench': '直读'}},
            [stage('read', IDENTITY, [0, 2], 'bench')])
        left = receive(self.receipts, issue(self.station, left_revision).wire, 'OLD-A')
        right = receive(self.receipts, issue(self.station, other).wire, 'THIRD')
        self.detach_station()
        with self.assertRaises(InvalidComparisonRequest):
            sign_compensation(self.receipts, self.reports, left, right, 0, 2, 0)
        self.assertEqual(self.signed_counts(), (0, 0))

    def test_write_failure_rolls_back_report_and_receipt(self):
        left, ids = self._setup_pieces()
        self.detach_station()
        baseline = sign_compensation(self.receipts, self.reports, left, ids['wide'], 1, 2, '1/4')
        with closing(sqlite3.connect(self.center_path)) as connection, connection:
            connection.execute('''
                CREATE TRIGGER refuse_compensation_receipt BEFORE INSERT ON report_receipts
                BEGIN SELECT RAISE(ABORT, 'receipt write blocked'); END
            ''')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'receipt write blocked'):
            sign_compensation(self.receipts, self.reports, left, ids['wide'], 1, 2, 0)
        self.assertEqual(self.signed_counts(), (1, 1))
        reopened = ReportStore(self.center_path)
        self.assertIsNone(get_report(reopened, baseline.report_id + 1))
        with closing(sqlite3.connect(self.center_path)) as connection, connection:
            connection.execute('DROP TRIGGER refuse_compensation_receipt')
        retry = sign_compensation(self.receipts, reopened, left, ids['wide'], 1, 2, '1/4')
        self.assertEqual(retry.snapshot.compensation.status, 'allowed')
        self.assertEqual(self.signed_counts(), (2, 2))


class TestCompensationPersistence(CompensationCase):
    def test_export_decode_reopen_and_old_reports_coexist(self):
        from review.compare import sign as sign_pointwise
        left_revision = import_revision(
            self.station, 'OLD-A',
            {'source': '旧表试车单', 'records': {'bench': '直读'}},
            [stage('read', IDENTITY, None, 'bench')])
        right_revision = import_revision(
            self.station, 'NEW-B',
            {'source': '许可试车单', 'records': {'bench': '负倒数'}},
            [stage('inverse', RECIPROCAL_NEG, ['1/2', 2], 'bench')],
            compensation_range=['0', '3'])
        left = receive(self.receipts, issue(self.station, left_revision).wire, 'OLD-A')
        right = receive(self.receipts, issue(self.station, right_revision).wire, 'NEW-B')
        self.detach_station()
        pointwise = sign_pointwise(self.receipts, self.reports, left, right, '1/2', 2, 1)
        compensation = sign_compensation(self.receipts, self.reports, left, right,
                                         '1/2', 2, '1/4')
        self.assertNotEqual(pointwise.report_id, compensation.report_id)
        reopened = ReportStore(self.center_path)
        self.assertEqual(get_report(reopened, compensation.report_id), compensation)
        self.assertEqual(get_report(reopened, pointwise.report_id), pointwise)
        exported = json.loads(export_report(reopened, compensation.report_id))
        self.assertEqual(exported['kind'], 'window_compensation')
        self.assertNotIn('partition', exported)
        self.assertEqual(exported['compensation'],
                         {'status': 'allowed', 'range': ['9/4', '9/4']})
        self.assertEqual(exported['request']['receipts'], [left, right])
        self.assertEqual([origin['instrument'] for origin in exported['origins']],
                         ['OLD-A', 'NEW-B'])
        self.assertEqual(decode_report(export_report(reopened, compensation.report_id)),
                         compensation)
        old_export = json.loads(export_report(reopened, pointwise.report_id))
        self.assertIn('partition', old_export)
        self.assertNotIn('kind', old_export)
        self.assertIsNone(export_report(reopened, compensation.report_id + 100))

    def test_reopen_claims_original_number_after_correction(self):
        left_revision = import_revision(
            self.station, 'OLD-A',
            {'source': '旧表试车单', 'records': {'bench': '直读'}},
            [stage('read', IDENTITY, None, 'bench')])
        wide = import_revision(
            self.station, 'NEW-B',
            {'source': '宽许可单', 'records': {'bench': '负二倍倒数'}},
            [stage('gain', DOUBLE_RECIPROCAL_NEG, [1, 2], 'bench')],
            compensation_range=['0', '4'])
        left = receive(self.receipts, issue(self.station, left_revision).wire, 'OLD-A')
        right = receive(self.receipts, issue(self.station, wide).wire, 'NEW-B')
        self.detach_station()
        first = sign_compensation(self.receipts, self.reports, left, right, 1, 2, '1/4')
        first_token = first.receipt.token
        self.archive.rename(self.station_path)
        corrected = import_revision(
            self.station, 'NEW-B',
            {'source': '勘误许可单', 'records': {'bench': '许可收紧'}},
            [stage('gain', DOUBLE_RECIPROCAL_NEG, [1, 2], 'bench')],
            supersedes=wide, compensation_range=['0', '5/2'])
        new_receipt = receive(self.receipts, issue(self.station, corrected).wire, 'NEW-B')
        self.detach_station()
        second = sign_compensation(self.receipts, self.reports, left, new_receipt, 1, 2, '1/4')
        reopened = ReportStore(self.center_path)
        claimed = get_report(reopened, first.report_id)
        self.assertEqual(claimed, first)
        self.assertEqual(claimed.receipt.token, first_token)
        self.assertEqual(claimed.snapshot.compensation.status, 'allowed')
        self.assertEqual(second.snapshot.compensation.status, 'no_feasible_offset')
        self.assertEqual(self.signed_counts(), (2, 2))
