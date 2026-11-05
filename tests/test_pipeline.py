import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from mailbrain.core import Store, Pipeline, Engine, KST, normalize_message, rule_analysis, validate_analysis
from mailbrain.collectors import read_eml, origin
from mailbrain.demo import messages


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = {"user_email": "me@example.com", "aliases": [], "engine": "rules", "data_dir": self.temp.name}
        self.store = Store(self.temp.name)
        self.pipeline = Pipeline(self.cfg, self.store)
        self.mails = messages(self.cfg["user_email"])

    def ingest_one(self):
        return self.pipeline.ingest(self.mails[:1])

    def accept_one(self):
        self.ingest_one()
        proposal = self.store.state()["proposals"][0]
        self.store.resolve(proposal["id"], True)
        return self.store.state()["tasks"][0]

    def test_ingest_is_idempotent(self):
        first = self.pipeline.ingest(self.mails)
        second = self.pipeline.ingest(self.mails)
        self.assertEqual((first["imported"], second["duplicates"]), (5, 5))
        self.assertEqual(len(self.store.state()["messages"]), 5)
        self.assertEqual(len(self.store.state()["proposals"]), 3)

    def test_no_task_before_approval(self):
        self.ingest_one()
        self.assertEqual(self.store.state()["tasks"], [])
        self.assertEqual(len(self.store.state()["proposals"]), 1)

    def test_approval_records_event_atomically(self):
        task = self.accept_one()
        self.assertEqual(task["status"], "등록완료")
        self.assertEqual(len(self.store.state()["events"]), 1)
        proposal = self.store.rows("SELECT * FROM proposals")[0]
        with self.assertRaises(ValueError):
            self.store.resolve(proposal["id"], True)
        self.assertEqual(len(self.store.state()["tasks"]), 1)

    def test_reject_does_not_create_task(self):
        self.ingest_one()
        self.store.resolve(self.store.state()["proposals"][0]["id"], False)
        self.assertEqual(self.store.state()["tasks"], [])

    def test_other_recipient_rejected(self):
        mail = {**self.mails[0], "to": "someone@example.com"}
        result = self.pipeline.ingest([mail])
        self.assertEqual(result["failed"], 1)
        self.assertEqual(self.store.state()["messages"], [])

    def test_cc_not_automatically_assignee(self):
        result = rule_analysis(self.mails[3], self.cfg)
        self.assertEqual(result["tasks"], [])

    def test_unclear_assignee_is_null(self):
        mail = {**self.mails[0], "body": "관련 부서에서 업무: 문서 검토; 검토해주세요."}
        self.assertIsNone(rule_analysis(mail, self.cfg)["tasks"][0]["assignee"])

    def test_update_requires_approval_and_preserves_manual_value(self):
        task = self.accept_one()
        self.store.edit_task(task["id"], {"due_date": "2027-01-01"})
        self.pipeline.ingest([self.mails[1]])
        self.assertEqual(self.store.state()["tasks"][0]["due_date"], "2027-01-01")
        proposal = self.store.state()["proposals"][0]
        self.assertEqual(proposal["target_id"], task["id"])
        self.store.resolve(proposal["id"], True)
        self.assertNotEqual(self.store.state()["tasks"][0]["due_date"], "2027-01-01")
        self.assertEqual(len(self.store.state()["events"]), 3)

    def test_update_without_target_is_not_created(self):
        self.pipeline.ingest([self.mails[1]])
        with self.assertRaises(ValueError):
            self.store.resolve(self.store.state()["proposals"][0]["id"], True)
        self.assertEqual(self.store.state()["tasks"], [])

    def test_cancel_and_complete_proposals(self):
        task = self.accept_one()
        for index, body, status in [(7, "업무: 검토 자료 제출; 취소합니다.", "취소"), (8, "업무: 검토 자료 제출; 완료했습니다.", "완료")]:
            mail = {**self.mails[0], "id": str(index), "body": body}
            self.pipeline.ingest([mail])
            proposal = self.store.state()["proposals"][0]
            self.store.resolve(proposal["id"], True)
            self.assertEqual(self.store.state()["tasks"][0]["status"], status)

    def test_request_to_complete_is_not_complete(self):
        mail = {**self.mails[0], "body": "업무: 검토 자료 제출; 완료해주세요."}
        self.assertEqual(rule_analysis(mail, self.cfg)["tasks"][0]["action"], "CREATE")

    def test_fabricated_evidence_blocked(self):
        analysis = {"summary": "요약", "tasks": [{"title": "가짜 업무", "action": "CREATE", "evidence_quote": "원문에 없는 문장"}]}
        with patch.object(Engine, "role", return_value=analysis):
            result = self.ingest_one()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(self.store.state()["proposals"], [])

    def test_invalid_date_fails_validation(self):
        analysis = rule_analysis(self.mails[0], self.cfg)
        analysis["tasks"][0]["due_date"] = "2026-02-30"
        with self.assertRaises(ValueError):
            validate_analysis(analysis, self.mails[0])

    def test_same_id_changed_source_rejected(self):
        self.ingest_one()
        result = self.pipeline.ingest([{**self.mails[0], "body": "변경된 원문"}])
        self.assertEqual(result["failed"], 1)
        self.assertEqual(self.store.rows("SELECT body FROM messages")[0]["body"], self.mails[0]["body"])

    def test_search_expands_thread_and_events(self):
        self.accept_one()
        self.pipeline.ingest([self.mails[1]])
        result = self.pipeline.search("협력사")
        self.assertEqual(len(result["messages"]), 2)
        self.assertEqual(len(result["events"]), 1)
        self.assertIn("스레드", result["mode"])

    def test_search_not_found_and_sql_characters(self):
        self.ingest_one()
        self.assertEqual(self.pipeline.search("없는단어")["messages"], [])
        self.assertEqual(self.pipeline.search("zzz%' OR zzz=zzz")["messages"], [])

    def test_report_completion_deduplicated(self):
        task = self.accept_one()
        self.store.edit_task(task["id"], {"status": "완료"})
        self.store.edit_task(task["id"], {"status": "완료"})
        today = datetime.now(KST).date().isoformat()
        report = self.pipeline.report(today, today)
        self.assertEqual(report["completed_count"], 1)
        self.assertEqual(report["active_count"], 0)
        self.assertTrue(list((Path(self.temp.name) / "reports").glob("*.md")))

    def test_reopened_work_not_reported_completed(self):
        task = self.accept_one()
        self.store.edit_task(task["id"], {"status": "완료"})
        self.store.edit_task(task["id"], {"status": "진행중"})
        today = datetime.now(KST).date().isoformat()
        report = self.pipeline.report(today, today)
        self.assertEqual(report["completed_count"], 0)
        self.assertEqual(report["active_count"], 1)

    def test_historical_report_reconstructs_state(self):
        task = self.accept_one()
        self.store.edit_task(task["id"], {"status": "완료"})
        today = datetime.now(KST).date()
        prior = (today - timedelta(days=3)).isoformat()
        with self.store.connect() as db:
            db.execute("UPDATE events SET at=? WHERE kind='생성'", (prior + "T09:00:00+09:00",))
        report = self.pipeline.report(prior, prior)
        self.assertEqual(report["active_count"], 1)
        self.assertEqual(report["completed_count"], 0)

    def test_invalid_state_rolls_back(self):
        task = self.accept_one()
        with self.assertRaises(ValueError):
            self.store.edit_task(task["id"], {"status": "거짓상태"})
        self.assertEqual(len(self.store.state()["events"]), 1)

    def test_naive_mail_date_rejected(self):
        with self.assertRaises(ValueError):
            normalize_message({**self.mails[0], "sent_at": "2026-11-04T09:00:00"}, self.cfg)

    def test_eml_parses_korean_and_keeps_thread(self):
        from email.message import EmailMessage
        mail = EmailMessage()
        mail["Subject"] = "한글 제목"
        mail["From"] = "lead@example.com"
        mail["To"] = self.cfg["user_email"]
        mail["Date"] = "Wed, 04 Nov 2026 09:00:00 +0900"
        mail["Message-ID"] = "<reply@example.com>"
        mail["References"] = "<original@example.com>"
        mail.set_content("한글 본문")
        path = Path(self.temp.name) / "sample.eml"
        path.write_bytes(mail.as_bytes())
        parsed = read_eml(path)
        self.assertEqual(parsed["subject"], "한글 제목")
        self.assertEqual(parsed["thread_id"], "<original@example.com>")
        self.assertIn("한글 본문", parsed["body"])

    def test_model_failure_does_not_silently_fallback(self):
        with patch.object(Engine, "role", side_effect=ConnectionError("모델 연결 실패")):
            with self.assertRaises(ConnectionError):
                self.ingest_one()
        self.assertEqual(self.store.state()["runs"][0]["status"], "실패")
        self.assertEqual(self.store.state()["messages"], [])

    def test_prompt_roles_loaded_in_order(self):
        self.ingest_one()
        stages = json.loads(self.store.state()["runs"][0]["stages"])
        self.assertEqual([s["role"] for s in stages], ["01_analyze", "03_verify", "02_reconcile", "04_wiki"])
        self.assertTrue(all(len(s["prompt_sha256"]) == 64 for s in stages))

    def test_origin_is_exact(self):
        self.assertNotEqual(origin("https://mail.example.com.evil.test/inbox"), "https://mail.example.com")

    def test_data_folder_bound_to_one_account(self):
        self.ingest_one()
        with self.assertRaises(ValueError):
            Pipeline({**self.cfg, "user_email": "other@example.com"}, self.store)

    def test_large_request_mail_preserved_with_visible_limit_notice(self):
        body = "\n".join(f"업무: 자료 검토 {index}; 준비해주세요." for index in range(40))
        result = self.pipeline.ingest([{**self.mails[0], "body": body}])
        self.assertEqual(result["failed"], 0)
        mail = self.store.rows("SELECT * FROM messages")[0]
        self.assertEqual(mail["body"], body)
        self.assertIn("처음 30개", mail["summary"])
        self.assertEqual(len(self.store.state()["proposals"]), 30)


if __name__ == "__main__":
    unittest.main()
