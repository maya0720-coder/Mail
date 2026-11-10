"""3차 개선 요구사항(work_mgmt_change_request_3.md) 검증."""
import tempfile
import unittest
from datetime import datetime

from mailbrain.core import Store, Pipeline, KST
from mailbrain.demo import messages
from mailbrain.reporting import report_title, sap_note_numbers, summary_points, build_document, markdown
from mailbrain.topics import Topics


class ReportCleanupTests(unittest.TestCase):
    def test_title_cleanup_example(self):
        self.assertEqual(report_title('RE: [외부메일]Re: [골프존]시스템 카피 일정 확인 요청 — Subject: [외부메일…'), '시스템 카피 일정 확인')
        self.assertEqual(report_title('FW: [SAP] 전표 오류 문의의 건'), '전표 오류')
        self.assertEqual(report_title('사건 정리'), '사건 정리')

    def test_points_are_not_truncated(self):
        long = '이번 주 결산 마감 일정과 관련하여 각 부서별 미결 전표와 증빙 자료를 모두 취합해서 금요일까지 회신해 주세요.'
        points = summary_points(long)
        self.assertFalse(any(p.endswith('…') for p in points))
        self.assertTrue(points[0].startswith('이번 주 결산 마감 일정'))
        self.assertTrue(points[0].endswith('금요일까지 회신 요청'))

    def test_points_drop_subject_fragments(self):
        points = summary_points('[외부메일] 서버 점검을 부탁드립니다 — Subject: [외부메일] 서버 점검')
        self.assertEqual(points, ['서버 점검 진행 요청'])

    def test_sap_note_numbers(self):
        mails = [{'subject': 'SAP Note 3456789 released', 'body': 'Security Note #3456790, Note: 3456789'},
                 {'subject': 'SAP Notes 3401234', 'body': ''}]
        self.assertEqual(sap_note_numbers(mails), ['3401234', '3456789', '3456790'])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = {"user_email": "me@example.com", "aliases": [], "engine": "rules"}
        self.store = Store(self.temp.name)
        self.pipeline = Pipeline(self.cfg, self.store)
        self.base = messages('me@example.com')[0]

    def mail(self, identifier, subject, body, sender=None):
        mail = {**self.base, 'id': identifier, 'thread_id': identifier, 'subject': subject, 'body': body}
        if sender:
            mail['sender'] = sender
        self.pipeline.ingest([mail])

    def accept_all(self):
        self.store.resolve_topics([t['id'] for t in Topics(self.store).build() if t['proposal_count']], True)

    def test_advance_moves_each_task_one_stage(self):
        self.mail('one', '[운영] 서버 점검', 'me@example.com 업무: 서버 점검; 서버 점검을 부탁드립니다.')
        self.mail('two', '[운영] 백업 확인', 'me@example.com 업무: 백업 확인; 확인해주세요.')
        self.accept_all()
        ids = [t['id'] for t in self.store.list_tasks()['tasks']]
        for expected in ('진행중', '처리완료'):
            result = self.store.advance_tasks(ids)
            self.assertEqual((result['done'], result['failed']), (2, []))
            self.assertEqual({t['work_stage'] for t in self.store.list_tasks()['tasks']}, {expected})
        result = self.store.advance_tasks(ids)
        self.assertEqual(result['done'], 0)
        self.assertEqual(len(result['failed']), 2)
        kinds = [e['kind'] for e in self.store.rows('SELECT kind FROM events WHERE task_id=? ORDER BY seq', (ids[0],))]
        self.assertIn('진행', kinds)
        self.assertIn('처리완료', kinds)
        with self.assertRaises(ValueError):
            self.store.advance_tasks([])

    def test_sap_notes_merged_and_notes_column_without_links(self):
        self.mail('sap1', 'SAP Note 3456789 released', 'me@example.com 업무: SAP Note 3456789; 검토해주세요.', 'notification-service@sap.com')
        self.mail('sap2', 'SAP Note 3401234 released', 'me@example.com 업무: SAP Note 3401234; 검토해주세요.', 'notification-service@sap.com')
        self.mail('normal', 'RE: [외부메일]Re: [골프존]시스템 카피 일정 확인 요청', 'me@example.com 업무: 시스템 카피; 일정 확인 부탁드립니다.')
        self.accept_all()
        today = datetime.now(KST).date().isoformat()
        tasks = {t['id']: t for t in self.store.rows('SELECT * FROM tasks')}
        document = build_document(self.store, self.cfg, today, today, tasks, self.store.rows('SELECT * FROM events'), 0)
        items = [item for row in document['rows'] for item in row['this_week']]
        sap = [item for item in items if item['title'] == 'SAP Notes 검토']
        self.assertEqual(len(sap), 1)
        self.assertEqual(sap[0]['points'], ['3401234, 3456789'])
        self.assertEqual(len(sap[0]['task_ids']), 2)
        self.assertIn('시스템 카피 일정 확인', [item['title'] for item in items])
        self.assertFalse(any('SAP Note 3' in item['title'] for item in items))
        content = markdown(document)
        for source in ('sap1', 'sap2', 'normal'):
            self.assertNotIn(source, content)


if __name__ == '__main__':
    unittest.main()
