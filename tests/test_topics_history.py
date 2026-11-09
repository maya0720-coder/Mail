import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from mailbrain.core import Store, Pipeline, KST, Engine
from mailbrain.demo import messages
from mailbrain.topics import Topics, clean_subject, new_body, CATEGORIES
from mailbrain.reporting import build_document, write_pdf, is_auth_notification


class TopicsHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = {"user_email":"me@example.com","aliases":[],"engine":"rules"}
        self.store = Store(self.temp.name)
        self.pipeline = Pipeline(self.cfg, self.store)
        self.mails = messages("me@example.com")

    def test_repeated_external_reply_prefix(self):
        self.assertEqual(clean_subject("[외부메일]Re: [외부메일]FW: QA 서버 시스템 카피 문의"), "QA 서버 시스템 카피 문의")

    def test_lease_schedule_is_categorized_as_finance_work(self):
        from mailbrain.topics import category, task_title
        self.assertEqual(category("재무팀 리스 스케쥴 변경 요청"), "재무/리스 업무")
        self.assertEqual(task_title("[재무팀] 리스 스케쥴 변경 요청의 건", "계약 변경으로 인한 스케쥴 조정"), "리스 스케줄 조정")

    def test_same_thread_one_topic_many_candidates(self):
        self.pipeline.ingest(self.mails[:2])
        groups = Topics(self.store).build()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["message_count"], 2)
        self.assertEqual(groups[0]["proposal_count"], 2)

    def test_subject_fallback_groups_broken_thread_ids(self):
        first = {**self.mails[0],"thread_id":"t1","subject":"QA 서버 시스템 카피 문의"}
        second = {**self.mails[1],"thread_id":"t2","subject":"[외부메일]Re: [외부메일]QA 서버 시스템 카피 문의"}
        self.pipeline.ingest([first, second])
        self.assertEqual(len(Topics(self.store).build()), 1)

    def test_short_generic_titles_not_merged(self):
        self.pipeline.ingest([{**self.mails[0],"thread_id":"a","subject":"공지"},{**self.mails[1],"thread_id":"b","subject":"공지"}])
        self.assertEqual(len(Topics(self.store).build()), 2)

    def test_received_time_overrides_sent_sort(self):
        today = datetime.now(KST)
        first = {**self.mails[0],"received_at":today.isoformat()}
        second = {**self.mails[2],"received_at":(today-timedelta(days=2)).isoformat(),"sent_at":(today+timedelta(days=1)).isoformat()}
        self.pipeline.ingest([second,first])
        self.assertEqual(Topics(self.store).build()[0]["messages"][0]["id"],first["id"])
        self.assertEqual(self.store.state()["messages"][0]["id"],first["id"])

    def test_old_mail_received_metadata_update_preserves_original(self):
        self.pipeline.ingest(self.mails[:1])
        original = self.store.rows("SELECT * FROM messages")[0]
        stamp = datetime.now(KST).isoformat()
        result = self.pipeline.ingest([{**self.mails[0],"received_at":stamp}])
        saved = self.store.rows("SELECT * FROM messages")[0]
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(saved["received_at"],stamp)
        self.assertEqual(saved["body"],original["body"])

    def test_quoted_old_body_excluded_only_from_summary(self):
        body = "새 일정 확인 바랍니다.\nFrom: old@example.com\n예전 일정 확인 바랍니다."
        self.pipeline.ingest([{**self.mails[0],"body":body}])
        group = Topics(self.store).build()[0]
        self.assertNotIn("예전 일정",group["latest_summary"])
        self.assertEqual(self.store.rows("SELECT body FROM messages")[0]["body"],body)

    def test_history_retrieves_mail_before_task_approval(self):
        self.pipeline.ingest(self.mails[:2])
        result = self.pipeline.history("협력사 지연 이유")
        self.assertGreater(result["evidence_count"],0)
        self.assertIn("RAG",result["mode"])
        self.assertIn("스레드",self.pipeline.search("협력사")["mode"])
        self.assertEqual(len(self.store.state()["tasks"]), 1)

    def test_sent_solution_is_history_evidence_not_a_received_report_item(self):
        today = datetime.now(KST).date().isoformat()
        incoming = {**self.mails[0], "thread_id": "report-reply", "received_at": today + "T09:00:00+09:00"}
        reply = {**self.mails[1], "id": "report-sent-reply", "thread_id": "report-reply",
                 "sender": self.cfg["user_email"], "to": "lead@example.com", "direction": "outbound",
                 "sent_at": today + "T10:00:00+09:00", "received_at": None,
                 "body": "요청한 자료 검토를 완료하고 결과를 회신했습니다."}
        self.pipeline.ingest([incoming, reply])
        report = self.pipeline.report(today, today)
        source_ids = [mail_id for row in report["document"]["rows"] for item in row["this_week"] for mail_id in item["source_ids"]]
        self.assertIn(incoming["id"], source_ids)
        self.assertNotIn(reply["id"], source_ids)

    def test_rag_synonym_copy_retrieves_korean(self):
        self.pipeline.ingest([{**self.mails[0],"subject":"QA 서버 시스템 카피 문의","body":"카피 일정을 다음 주로 변경합니다."}])
        result = self.pipeline.history("시스템 복제")
        self.assertGreater(result["evidence_count"],0)

    def test_history_dates_do_not_leak_expanded_neighbor(self):
        first={**self.mails[0],"received_at":"2026-09-01T10:00:00+09:00"}
        second={**self.mails[1],"received_at":"2026-10-01T10:00:00+09:00"}
        self.pipeline.ingest([first,second])
        result=self.pipeline.history("자료", "2026-09-01","2026-09-30")
        self.assertEqual([day["date"] for day in result["days"]],["2026-09-01"])

    def test_history_groups_events_by_actual_change_day(self):
        self.pipeline.ingest(self.mails[:1])
        proposal=self.store.state()["proposals"][0]
        self.store.resolve(proposal["id"],True)
        task=self.store.state()["tasks"][0]
        self.store.edit_task(task["id"],{"status":"완료"})
        result=self.pipeline.history("검토 자료")
        self.assertTrue(any(item["kind"]=="업무 변경" for day in result["days"] for item in day["items"]))

    def test_legacy_partial_auto_refresh_events_do_not_break_history_or_reports(self):
        self.pipeline.ingest(self.mails[:1])
        message = self.store.rows("SELECT * FROM messages")[0]
        with self.store.connect() as db:
            self.store.create_auto_task(db, message, "메일에서 확인한 업무")
            task = dict(db.execute("SELECT * FROM tasks").fetchone())
            partial = {key: task[key] for key in ("title", "source_id", "summary", "needs_reply", "read_at")}
            db.execute("INSERT INTO events(task_id,kind,before_json,after_json,source_id,actor,at) VALUES(?,?,?,?,?,?,?)",
                       (task["id"], "메일 스레드 정보 갱신", json.dumps({"title": task["title"]}),
                        json.dumps(partial), message["id"], "자동 분류", task["updated_at"]))
        history = self.pipeline.history("")
        self.assertGreater(history["evidence_count"], 0)
        today = datetime.now(KST).date().isoformat()
        report = self.pipeline.report(today, today)
        self.assertEqual(report["document"]["end"], today)

    def test_topic_filter_and_no_result(self):
        self.pipeline.ingest(self.mails)
        topic=Topics(self.store).build()[0]
        result=self.pipeline.history("",topic_id=topic["id"])
        self.assertTrue(all(item["topic_id"]==topic["id"] for day in result["days"] for item in day["items"]))
        self.assertEqual(self.pipeline.history("아무관련없는단어XYZ")['evidence_count'],0)

    def test_rag_index_is_reused_and_invalidated(self):
        self.pipeline.ingest(self.mails[:1])
        self.pipeline.history()
        first=self.store.rows("SELECT value FROM metadata WHERE key='rag_stamp'")[0]["value"]
        self.pipeline.history()
        self.assertEqual(self.store.rows("SELECT value FROM metadata WHERE key='rag_stamp'")[0]["value"],first)
        self.pipeline.ingest(self.mails[1:2])
        self.pipeline.history()
        self.assertNotEqual(self.store.rows("SELECT value FROM metadata WHERE key='rag_stamp'")[0]["value"],first)

    def test_report_matches_four_column_template_without_fake_completion(self):
        self.pipeline.ingest(self.mails)
        today=datetime.now(KST).date().isoformat()
        report=self.pipeline.report(today,today)
        doc=report["document"]
        self.assertEqual(doc["columns"],["구분","업무 내용 (금주)","차주","비고"])
        self.assertEqual([r["category"] for r in doc["rows"]],CATEGORIES)
        self.assertEqual(report["completed_count"],0)
        self.assertTrue(any(r["this_week"] for r in doc["rows"]))
        self.assertTrue((Path(self.temp.name)/"reports"/(report["id"]+".pdf")).read_bytes().startswith(b'%PDF'))

    def test_report_does_not_include_future_mail(self):
        today=datetime.now(KST).date()
        older=(today-timedelta(days=5)).isoformat()
        self.pipeline.ingest([{**self.mails[0],"received_at":older+'T09:00:00+09:00'},self.mails[2]])
        report=self.pipeline.report(older,older)
        titles=[i["title"] for row in report["document"]["rows"] for i in row["this_week"]]
        self.assertTrue(any('알파' in title for title in titles))
        self.assertFalse(any('베타' in title for title in titles))

    def test_report_excludes_auth_code_mail_but_keeps_business_login_issue(self):
        self.pipeline.ingest([{**self.mails[0],"subject":"임시 ChatGPT 로그인 코드","body":"인증 코드 123456"},{**self.mails[2],"subject":"SAP 로그인 오류 조치","body":"접속 오류 확인 바랍니다."}])
        today=datetime.now(KST).date().isoformat()
        result=self.pipeline.report(today,today)
        self.assertNotIn('123456',result['content'])
        self.assertIn('SAP 로그인 오류 조치',result['content'])
        self.assertEqual(len(self.store.state()['messages']),2)

    def test_llm_history_rejects_invented_citation(self):
        self.pipeline.ingest(self.mails[:1])
        cfg={**self.cfg,'engine':'ollama','ollama_model':'test-only'}
        pipeline=Pipeline(cfg,self.store)
        with patch.object(Engine,'role',return_value={'summary':'요약','facts':[{'text':'사실','citations':['invented-source']}]}):
            with self.assertRaises(ValueError):
                pipeline.history('자료')

    def test_auth_notification_languages_and_business_exception(self):
        for subject in ('Your temporary ChatGPT login code','ChatGPT 비밀번호 재설정용 임시 코드','Claude의 보안 링크가 도착했습니다','인증 코드 안내','New sign-in to your account'):
            self.assertTrue(is_auth_notification(subject))
        self.assertFalse(is_auth_notification('SAP 로그인 오류 조치'))


if __name__=='__main__':
    unittest.main()
