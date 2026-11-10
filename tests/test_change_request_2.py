"""2차 개선 요구사항(work_mgmt_change_request_2.md) 검증."""
import tempfile
import unittest
from datetime import datetime

from mailbrain.core import Store, Pipeline, KST
from mailbrain.demo import messages
from mailbrain.policy import approval_mail, personal_notice, noise_mail
from mailbrain.reporting import nominal, summary_points, build_document
from mailbrain.topics import Topics


class PolicyRuleTests(unittest.TestCase):
    def test_approval_prefix_and_sender_rules(self):
        self.assertTrue(approval_mail('[결재 진행] 출장비 정산', ''))
        self.assertTrue(approval_mail('RE: [결재 완료] 법인카드 사용 내역', ''))
        self.assertTrue(approval_mail('배치 작업 결과', '처리 내역 안내', 'Batch <BatchJobServ@golfzon.com>'))
        self.assertTrue(approval_mail('배치 작업 결과', '', 'robot@example.com', {'approval_senders': ['robot@example.com']}))
        self.assertFalse(approval_mail('주간 회의 일정', '회의실 예약 확인', 'colleague@example.com'))

    def test_approval_rule_wins_over_notification_noise(self):
        self.assertFalse(noise_mail('시스템 알림', '자동 알림입니다', 'batchjobserv@golfzon.com'))
        self.assertTrue(noise_mail('시스템 알림', '자동 알림입니다', 'someone@example.com'))

    def test_personal_notice_rules(self):
        self.assertTrue(personal_notice('홍길동님의 생일을 축하합니다!', ''))
        self.assertTrue(personal_notice('Happy Birthday', 'Have a great day'))
        self.assertTrue(personal_notice('로그인을 위한 보안 링크', '아래 링크를 눌러 로그인하세요.'))
        self.assertTrue(personal_notice('Your sign-in link', 'Click the link to sign in'))
        self.assertFalse(personal_notice('SAP 로그인 오류 조치 요청', '로그인 링크 접속 시 오류가 납니다.'))
        self.assertFalse(personal_notice('리스 계약 검토 요청', '계약서를 검토해 주세요.'))


class ReportTextTests(unittest.TestCase):
    def test_few_shot_examples(self):
        self.assertEqual(nominal('a 작업을 처리해주세요'), 'a 작업 진행 요청')
        self.assertEqual(nominal('b 작업을 처리해주세요'), 'b 작업 진행 요청')
        self.assertEqual(nominal('서버 점검을 부탁드립니다'), '서버 점검 진행 요청')

    def test_noun_endings(self):
        self.assertEqual(nominal('자료를 검토해 주시기 바랍니다.'), '자료 검토 요청')
        self.assertEqual(nominal('배포를 완료했습니다.'), '배포 완료')
        self.assertEqual(nominal('추가 검토가 필요합니다'), '추가 검토 필요')
        self.assertEqual(nominal('내일 배포할 예정입니다'), '내일 배포 예정')
        self.assertEqual(nominal('번거로우시겠지만 계약서 회신 부탁드립니다.'), '계약서 회신 요청')

    def test_subject_and_greeting_removed(self):
        points = summary_points('안녕하세요. 재무팀 홍길동입니다.\n리스 계약 건 관련 문의\n계약서를 검토해 주세요. 감사합니다.',
                                subjects=['RE: 리스 계약 건 관련 문의'])
        self.assertEqual(points, ['계약서 검토 요청'])
        self.assertEqual(summary_points('담당자님, 업무: 검토 자료 제출; 2026-11-12까지 준비해주세요.'), ['2026-11-12까지 준비 요청'])
        self.assertEqual(summary_points('홍길동 매니저님, 자료 검토 부탁드립니다. me@example.com'), ['자료 검토 요청'])

    def test_auto_thread_summary_uses_quotes_not_subjects(self):
        summary = ('같은 메일 스레드의 업무 정리\n\n메일에서 추출한 업무 요청\n- [신규 요청] 서버 점검 — 서버 점검을 부탁드립니다\n\n'
                   '같은 스레드 메일별 요약\n- 2026-11-10 · 서버 점검 요청의 건: 안녕하세요. 서버 점검을 부탁드립니다')
        points = summary_points(summary)
        self.assertEqual(points, ['서버 점검 진행 요청'])
        self.assertFalse(any('요청의 건' in p or '안녕하세요' in p or '스레드' in p for p in points))


class ScreenRuleTests(unittest.TestCase):
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

    def test_batch_sender_topic_is_approval_and_notices_hidden_from_work(self):
        self.mail('batch', '배치 작업 결과 전달', 'me@example.com 업무: 배치 결과 확인; 확인해주세요.', 'batchjobserv@golfzon.com')
        self.mail('birthday', '생일 축하드립니다', 'me@example.com 업무: 생일 축하 카드 확인; 확인해주세요. 생일을 축하합니다.')
        self.mail('normal', '[운영] 서버 점검', 'me@example.com 업무: 서버 점검; 서버 점검을 부탁드립니다.')
        topics = {t['messages'][0]['id']: t for t in Topics(self.store).build()}
        self.assertEqual(topics['batch']['category'], '전자결재')
        self.assertTrue(topics['birthday']['notice'])
        self.assertFalse(topics['normal']['notice'])
        state = self.store.state()
        # 수집한 메일에는 남고, 내 업무와 상단 숫자에서는 빠집니다.
        self.assertIn('birthday', [m['id'] for m in state['messages']])
        listed = self.store.list_tasks()['tasks']
        self.assertNotIn('birthday', [t['source_id'] for t in listed])
        self.assertEqual(state['work_counts']['open'], len([t for t in listed if t['work_stage'] != '처리완료']))
        batch_tasks = [t for t in listed if t['source_id'] == 'batch']
        self.assertTrue(batch_tasks)
        self.assertTrue(all(t['work_kind'] == '전자결재' for t in batch_tasks))

    def test_group_filters(self):
        self.mail('one', '[운영] 서버 점검', 'me@example.com 업무: 서버 점검; 서버 점검을 부탁드립니다.')
        self.mail('two', '[운영] 백업 확인', 'me@example.com 업무: 백업 확인; 확인해주세요.')
        self.store.resolve_topics([t['id'] for t in Topics(self.store).build()], True)
        task = self.store.list_tasks()['tasks'][0]
        self.store.task_action(task['id'], 'start')
        self.store.task_action(task['id'], 'complete')
        self.assertEqual([t['id'] for t in self.store.list_tasks(group='done')['tasks']], [task['id']])
        self.assertNotIn(task['id'], [t['id'] for t in self.store.list_tasks(group='open')['tasks']])
        self.assertEqual(self.store.state()['work_counts']['done'], 1)
        with self.assertRaises(ValueError):
            self.store.list_tasks(group='bad')

    def test_bulk_resolve_by_topic(self):
        self.mail('one', '[운영] 서버 점검', 'me@example.com 업무: 서버 점검; 서버 점검을 부탁드립니다.')
        self.mail('two', '[운영] 백업 확인', 'me@example.com 업무: 백업 확인; 확인해주세요.')
        topics = [t['id'] for t in Topics(self.store).build() if t['proposal_count']]
        self.assertEqual(len(topics), 2)
        result = self.store.resolve_topics(topics + ['missing'], True)
        self.assertEqual(result['done'], 2)
        self.assertEqual([f['topic_id'] for f in result['failed']], ['missing'])
        self.assertEqual(self.store.state()['proposals'], [])
        stages = {t['source_id']: t['work_stage'] for t in self.store.list_tasks()['tasks']}
        self.assertEqual(stages, {'one': '확인완료', 'two': '확인완료'})
        with self.assertRaises(ValueError):
            self.store.resolve_topics([], True)

    def test_report_excludes_notice_and_uses_noun_points(self):
        self.mail('birthday', '생일 축하드립니다', 'me@example.com 업무: 생일 카드 확인; 확인해주세요. 생일을 축하합니다.')
        self.mail('normal', '[운영] 서버 점검', '안녕하세요.\nme@example.com 업무: 서버 점검; 서버 점검을 부탁드립니다.\n감사합니다.')
        self.store.resolve_topics([t['id'] for t in Topics(self.store).build() if t['proposal_count']], True)
        today = datetime.now(KST).date().isoformat()
        tasks = {t['id']: t for t in self.store.rows('SELECT * FROM tasks')}
        events = self.store.rows('SELECT * FROM events')
        document = build_document(self.store, self.cfg, today, today, tasks, events, 0)
        items = [item for row in document['rows'] for item in row['this_week']]
        self.assertEqual(len(items), 1)
        self.assertNotIn('생일', items[0]['title'])
        self.assertEqual(items[0]['points'], ['서버 점검 진행 요청'])
        plans = [i['points'][0] for row in document['rows'] for i in row['next_week']]
        self.assertEqual(plans, ['서버 점검 착수 예정'])
        for item in items + [i for row in document['rows'] for i in row['next_week']]:
            for point in item['points']:
                self.assertNotRegex(point, r'안녕하세요|감사합니다|주세요|부탁드립니다|\[운영\]')


if __name__ == '__main__':
    unittest.main()
