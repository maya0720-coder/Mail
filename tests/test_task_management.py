import json
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from mailbrain.core import Store, Pipeline, KST, Engine
from mailbrain.demo import messages
from mailbrain.policy import sender_excluded
from mailbrain.topics import Topics


class TaskManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg={"user_email":"me@example.com","aliases":[],"engine":"rules"}
        self.store=Store(self.temp.name)
        self.pipeline=Pipeline(self.cfg,self.store)
        self.mail=messages('me@example.com')[0]

    def task(self, identifier='one', project='프로젝트A', title='자료 검토'):
        mail={**self.mail,'id':identifier,'thread_id':identifier,'subject':'['+project+'] '+title,'body':f'me@example.com 업무: {title}; 준비해주세요.'}
        self.pipeline.ingest([mail])
        proposal=next(p for p in self.store.state()['proposals'] if p['source_id']==identifier)
        self.store.resolve(proposal['id'],True)
        return next(t for t in self.store.state()['tasks'] if t['source_id']==identifier)

    def test_sender_matching_uses_actual_case_insensitive_address(self):
        self.assertTrue(sender_excluded('Microsoft Teams <NO-REPLY@TEAMS.MAIL.MICROSOFT>'))
        self.assertFalse(sender_excluded('no-reply@teams.mail.microsoft <allowed@example.com>'))
        self.assertFalse(sender_excluded('no-reply@teams.mail.microsoft.other.test'))

    def test_excluded_ingest_does_not_call_model_or_save_body(self):
        with patch.object(Engine,'role',side_effect=AssertionError('excluded mail reached model')):
            # 위키 역할은 별도 실행되므로 위키만 차단 없이 둡니다.
            with patch.object(self.pipeline,'wiki'):
                result=self.pipeline.ingest([{**self.mail,'sender':'no-reply@teams.mail.microsoft'}])
        self.assertEqual(result['excluded'],1)
        self.assertEqual(result['failed'],0)
        self.assertEqual(self.store.state()['message_count'],0)

    def test_legacy_teams_mail_hidden_without_deleting_original(self):
        self.pipeline.ingest([self.mail])
        proposal=self.store.state()['proposals'][0]
        with self.store.connect() as db:
            db.execute('UPDATE messages SET sender=?',('Teams <no-reply@teams.mail.microsoft>',))
        self.store.apply_message_policy(self.cfg)
        self.assertEqual(self.store.state()['messages'],[])
        self.assertEqual(self.store.state()['proposals'],[])
        self.assertEqual(Topics(self.store).build(),[])
        self.assertEqual(self.pipeline.search('자료')['messages'],[])
        self.assertEqual(self.pipeline.history('자료')['evidence_count'],0)
        self.assertEqual(self.store.rows('SELECT body FROM messages')[0]['body'],self.mail['body'])
        with self.assertRaises(ValueError):
            self.store.resolve(proposal['id'],True)

    def test_initial_status_and_all_requested_states(self):
        task=self.task()
        self.assertEqual(task['status'],'등록완료')
        for status in ('미등록','등록완료','진행중','대기','완료','취소'):
            self.store.edit_task(task['id'],{'status':status})
            self.assertEqual(self.store.list_tasks(status=status)['filtered_count'],1)

    def test_backfill_removes_existing_auto_task_when_mail_is_unrelated(self):
        self.pipeline.ingest([self.mail])
        task = next(task for task in self.store.state()["tasks"] if task["auto_generated"] == 1)
        self.assertEqual(task["manual"], 0)
        with self.store.connect() as db:
            db.execute("UPDATE messages SET subject=?,body=?,analysis=? WHERE id=?",
                       ("주말 영화 추천", "주말에 시간 되면 영화 보러 갈래?", json.dumps({"summary": "개인 메일", "tasks": []}), task["source_id"]))
        self.store.backfill_auto_tasks(self.cfg)
        self.assertEqual(self.store.list_tasks()["tasks"], [])
        hidden = self.store.list_tasks(scope="deleted")["tasks"]
        self.assertEqual(len(hidden), 1)
        self.assertIsNotNone(hidden[0]["deleted_at"])
        self.assertEqual(self.store.rows("SELECT kind FROM events ORDER BY seq DESC LIMIT 1")[0]["kind"], "업무 무관 메일에서 자동 업무 제외")

    def test_completion_time_is_idempotent_and_reopening_preserves_history(self):
        task=self.task()
        with patch('mailbrain.core.now',return_value='2026-10-03T10:00:00+09:00'):
            self.store.edit_task(task['id'],{'status':'완료'})
        with patch('mailbrain.core.now',return_value='2026-10-04T10:00:00+09:00'):
            self.store.edit_task(task['id'],{'status':'완료'})
        self.assertEqual(self.store.list_tasks()['tasks'][0]['completed_at'],'2026-10-03T10:00:00+09:00')
        self.store.edit_task(task['id'],{'status':'진행중'})
        self.assertIsNone(self.store.list_tasks()['tasks'][0]['completed_at'])
        history=self.store.rows('SELECT * FROM events WHERE task_id=? ORDER BY seq',(task['id'],))
        self.assertTrue(any(json.loads(e['after_json']).get('completed_at')=='2026-10-03T10:00:00+09:00' for e in history))

    def test_delete_restore_keeps_status_completion_and_original(self):
        task=self.task()
        self.store.edit_task(task['id'],{'status':'완료'})
        original=self.store.list_tasks()['tasks'][0]
        self.store.delete_task(task['id'])
        self.assertEqual(self.store.list_tasks()['tasks'],[])
        deleted=self.store.list_tasks(scope='deleted')['tasks'][0]
        self.assertEqual(self.store.list_tasks(scope='deleted')['counts']['완료'],1)
        self.assertEqual(deleted['status'],'완료')
        self.assertEqual(deleted['completed_at'],original['completed_at'])
        self.assertEqual(self.store.state()['message_count'],1)
        with self.assertRaises(ValueError):
            self.store.edit_task(task['id'],{'status':'진행중'})
        self.store.restore_task(task['id'])
        self.assertEqual(self.store.list_tasks()['tasks'][0]['status'],'완료')
        self.assertEqual([e['kind'] for e in self.store.rows('SELECT kind FROM events ORDER BY seq')][-2:],['삭제','복원'])

    def test_combined_filters_status_project_text_and_completion_date(self):
        one=self.task('one','프로젝트A','자료 검토')
        two=self.task('two','프로젝트B','자료 작성')
        self.task('three','프로젝트A','계약 검토')
        for task,stamp in [(one,'2026-10-03T10:00:00+09:00'),(two,'2026-10-10T10:00:00+09:00')]:
            with patch('mailbrain.core.now',return_value=stamp):
                self.store.edit_task(task['id'],{'status':'완료'})
        result=self.store.list_tasks(status='완료',query='자료',project='프로젝트A',start='2026-10-01',end='2026-10-05')
        self.assertEqual([t['id'] for t in result['tasks']],[one['id']])
        self.assertEqual(result['counts']['완료'],2)
        self.assertEqual(self.store.list_tasks(query='%')['tasks'],[])
        self.assertEqual(self.store.list_tasks(query="' OR 1=1 --")['tasks'],[])

    def test_invalid_filter_ranges_and_states(self):
        for arguments in ({'status':'아무거나'},{'scope':'all'},{'start':'2026-11-02','end':'2026-11-01'},{'start':'wrong'}):
            with self.assertRaises(ValueError):self.store.list_tasks(**arguments)

    def test_deleted_target_cannot_receive_new_mail_update(self):
        task=self.task()
        self.store.delete_task(task['id'])
        self.pipeline.ingest([{**self.mail,'id':'update','thread_id':'one','body':'업무: 자료 검토; 기한 변경 바랍니다.'}])
        proposal=self.store.state()['proposals'][0]
        self.assertIsNone(proposal['target_id'])
        with self.assertRaises(ValueError):self.store.resolve(proposal['id'],True,task['id'])

    def test_schema_migration_renames_old_states_and_recovers_completion_event(self):
        task=self.task()
        with self.store.connect() as db:db.execute("UPDATE tasks SET status='할일' WHERE id=?",(task['id'],))
        reopened=Store(self.temp.name)
        self.assertEqual(reopened.list_tasks()['tasks'][0]['status'],'등록완료')
        reopened.edit_task(task['id'],{'status':'완료'})
        stamp=reopened.list_tasks()['tasks'][0]['completed_at']
        with reopened.connect() as db:db.execute('UPDATE tasks SET completed_at=NULL WHERE id=?',(task['id'],))
        migrated=Store(self.temp.name)
        self.assertEqual(migrated.list_tasks()['tasks'][0]['completed_at'],stamp)

    def test_report_excludes_deleted_task_and_recovers_past_snapshot(self):
        today=datetime.now(KST).date()
        prior=(today-timedelta(days=2)).isoformat()
        with patch('mailbrain.core.now',return_value=prior+'T09:00:00+09:00'):
            task=self.task()
        self.store.delete_task(task['id'])
        historical=self.pipeline.report(prior,prior)
        current=self.pipeline.report(today.isoformat(),today.isoformat())
        self.assertEqual(historical['active_count'],1)
        self.assertEqual(current['active_count'],0)


    def stage(self, task_id):
        task = next(t for t in self.store.list_tasks()['tasks'] if t['id'] == task_id)
        return task['work_kind'], task['work_stage'], task['actions']

    def test_general_work_follows_four_stage_buttons(self):
        mail = {**self.mail, 'id': 'auto', 'thread_id': 'auto', 'subject': '[프로젝트A] 자료 검토 요청',
                'body': '자료 검토 부탁드립니다. 업무: 자료 검토; 준비해주세요.'}
        self.pipeline.ingest([mail])
        task = next(t for t in self.store.list_tasks()['tasks'] if t['source_id'] == 'auto')
        self.assertEqual(self.stage(task['id']), ('일반업무', '미확인', ['confirm']))
        with self.assertRaises(ValueError):
            self.store.task_action(task['id'], 'complete')
        for action, expected, buttons in (('confirm', '확인완료', ['start']), ('start', '진행중', ['complete']),
                                          ('complete', '처리완료', ['reopen']), ('reopen', '진행중', ['complete'])):
            self.store.task_action(task['id'], action)
            self.assertEqual(self.stage(task['id']), ('일반업무', expected, buttons))
        kinds = [e['kind'] for e in self.store.rows("SELECT kind FROM events WHERE task_id=? ORDER BY seq", (task['id'],))]
        self.assertEqual(kinds[-4:], ['확인', '진행', '처리완료', '재진행'])
        self.assertIsNone(self.store.list_tasks()['tasks'][0]['completed_at'])

    def test_approval_work_has_two_stages_and_reopen_returns_to_pending(self):
        task = self.task('approval', '전자결재', '구매 결재 요청')
        self.assertEqual(self.stage(task['id']), ('전자결재', '미처리', ['complete']))
        with self.assertRaises(ValueError):
            self.store.task_action(task['id'], 'start')
        self.store.task_action(task['id'], 'complete')
        self.assertEqual(self.stage(task['id']), ('전자결재', '처리완료', ['reopen']))
        self.store.task_action(task['id'], 'reopen')
        self.assertEqual(self.stage(task['id']), ('전자결재', '미처리', ['complete']))
        result = self.store.list_tasks(kind='전자결재', stage='미처리')
        self.assertEqual([t['id'] for t in result['tasks']], [task['id']])
        self.assertEqual(result['stage_counts']['전자결재']['미처리'], 1)

    def test_resolve_without_target_assigns_confirmed_stage(self):
        task = self.task()
        self.assertEqual(self.stage(task['id'])[:2], ('일반업무', '확인완료'))

    def test_report_items_can_be_edited_and_deleted(self):
        task = self.task()
        self.store.task_action(task['id'], 'start')
        today = datetime.now(KST).date().isoformat()
        report = self.pipeline.report(today, today)
        items = [i for row in report['document']['rows'] for i in row['this_week']]
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertTrue(item['start'])
        edited = self.pipeline.edit_report_item(report['id'], item['id'],
                                                {'title': '자료 검토 정리', 'points': '- 초안 작성\n- 검토 의견 반영',
                                                 'start': '2026-11-02', 'end': '2026-11-06'})
        saved = [i for row in edited['document']['rows'] for i in row['this_week']][0]
        self.assertEqual((saved['title'], saved['points'], saved['start'], saved['end']),
                         ('자료 검토 정리', ['초안 작성', '검토 의견 반영'], '2026-11-02', '2026-11-06'))
        self.assertIn('2026-11-02 ~ 2026-11-06', edited['content'])
        with self.assertRaises(ValueError):
            self.pipeline.edit_report_item(report['id'], item['id'], {'start': '2026-11-09', 'end': '2026-11-01'})
        with self.assertRaises(ValueError):
            self.pipeline.edit_report_item(report['id'], item['id'], {'status': '완료'})
        next_item = [i for row in edited['document']['rows'] for i in row['next_week']][0]
        self.pipeline.edit_report_item(report['id'], next_item['id'], delete=True)
        stored = json.loads(self.store.rows("SELECT document FROM reports WHERE id=?", (report['id'],))[0]['document'])
        self.assertFalse(any(row['next_week'] for row in stored['rows']))
        self.assertTrue(any(row['this_week'] for row in stored['rows']))

if __name__=='__main__':unittest.main()
