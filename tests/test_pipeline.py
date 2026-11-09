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
        self.assertEqual(len(self.store.state()["proposals"]), 2)

    def test_direct_recipient_gets_auto_task_before_proposal_approval(self):
        self.ingest_one()
        self.assertEqual(len(self.store.state()["tasks"]), 1)
        self.assertEqual(self.store.state()["tasks"][0]["auto_generated"], 1)
        self.assertEqual(len(self.store.state()["proposals"]), 1)

    def test_non_business_direct_mail_is_saved_but_not_added_to_my_work(self):
        unrelated = {**self.mails[0], "id": "personal-note", "thread_id": "personal-note",
                     "subject": "주말 영화 추천", "body": "이번 주말 시간 되면 같이 보러 갈래?"}
        self.pipeline.ingest([unrelated])
        self.assertEqual(self.store.state()["message_count"], 1)
        self.assertEqual(self.store.state()["tasks"], [])
        self.assertEqual(self.store.state()["proposals"], [])

    def test_outgoing_solution_reply_is_saved_for_rag_without_creating_work(self):
        incoming = {**self.mails[0], "thread_id": "reply-thread"}
        reply = {**self.mails[1], "id": "my-final-reply", "thread_id": "reply-thread",
                 "sender": self.cfg["user_email"], "to": "lead@example.com", "cc": "",
                 "direction": "outbound", "body": "리스 스케줄 조정 완료. SE16에서 대상 데이터를 확인하고 승인된 절차로 반영했습니다."}
        self.pipeline.ingest([incoming, reply])
        saved = self.store.rows("SELECT direction,body FROM messages WHERE id='my-final-reply'")[0]
        self.assertEqual(saved["direction"], "outbound")
        self.assertIn("SE16", saved["body"])
        self.assertFalse(any(task["source_id"] == reply["id"] for task in self.store.state()["tasks"]))
        self.assertFalse(any(item["source_id"] == reply["id"] for item in self.store.state()["proposals"]))
        result = self.pipeline.history("SE16 리스 스케줄 조정")
        citations = [item for day in result["days"] for item in day["items"]]
        self.assertTrue(any(item["message_id"] == reply["id"] and item["kind"] == "내 회신 · 해결 이력" for item in citations))

    def test_approval_records_event_atomically(self):
        self.ingest_one()
        prior_events = len(self.store.state()["events"])
        proposal = self.store.state()["proposals"][0]
        self.store.resolve(proposal["id"], True)
        task = self.store.state()["tasks"][0]
        self.assertEqual(task["status"], "등록완료")
        self.assertEqual(len(self.store.state()["events"]), prior_events + 1)
        proposal = self.store.rows("SELECT * FROM proposals")[0]
        with self.assertRaises(ValueError):
            self.store.resolve(proposal["id"], True)
        self.assertEqual(len(self.store.state()["tasks"]), 1)

    def test_reject_does_not_create_task(self):
        self.ingest_one()
        self.store.resolve(self.store.state()["proposals"][0]["id"], False)
        self.assertEqual(len(self.store.state()["tasks"]), 1)
        self.assertEqual(self.store.state()["tasks"][0]["auto_generated"], 1)

    def test_other_recipient_rejected(self):
        mail = {**self.mails[0], "to": "someone@example.com"}
        result = self.pipeline.ingest([mail])
        self.assertEqual(result["failed"], 1)
        self.assertEqual(self.store.state()["messages"], [])

    def test_cc_not_automatically_assignee(self):
        result = rule_analysis(self.mails[3], self.cfg)
        self.assertEqual(result["tasks"], [])

    def test_actionable_cc_mail_is_added_to_my_work_but_cc_notice_is_not(self):
        assigned = {**self.mails[0], "id": "cc-action", "thread_id": "cc-action",
                    "to": "manager@example.com", "cc": self.cfg["user_email"]}
        self.pipeline.ingest([assigned])
        task = next(task for task in self.store.state()["tasks"] if task["source_id"] == assigned["id"])
        self.assertEqual(task["auto_generated"], 1)
        self.assertIsNone(task["read_at"])

        notice = {**self.mails[3], "id": "cc-notice", "thread_id": "cc-notice"}
        self.pipeline.ingest([notice])
        self.assertFalse(any(task["source_id"] == notice["id"] for task in self.store.state()["tasks"]))

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
        prior_events = len(self.store.state()["events"])
        self.store.resolve(proposal["id"], True)
        self.assertNotEqual(self.store.state()["tasks"][0]["due_date"], "2027-01-01")
        self.assertEqual(len(self.store.state()["events"]), prior_events + 1)

    def test_update_without_target_is_not_created(self):
        self.pipeline.ingest([self.mails[1]])
        with self.assertRaises(ValueError):
            self.store.resolve(self.store.state()["proposals"][0]["id"], True)
        self.assertEqual(len(self.store.state()["tasks"]), 1)
        self.assertEqual(self.store.state()["tasks"][0]["auto_generated"], 1)

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

    def test_empty_saved_body_is_recovered_on_reimport(self):
        original = {**self.mails[0], "body": ""}
        self.pipeline.ingest([original])
        recovered = {**self.mails[0], "body": "복구된 메일 본문"}
        result = self.pipeline.ingest([recovered])
        self.assertEqual(result["imported"], 1)
        self.assertEqual(self.store.rows("SELECT body FROM messages WHERE id=?", (recovered["id"],))[0]["body"], recovered["body"])

    def test_search_expands_thread_and_events(self):
        self.accept_one()
        self.pipeline.ingest([self.mails[1]])
        result = self.pipeline.search("협력사")
        self.assertEqual(len(result["messages"]), 2)
        self.assertGreaterEqual(len(result["events"]), 1)
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
            db.execute("UPDATE events SET at=? WHERE task_id=? AND seq=(SELECT MIN(seq) FROM events WHERE task_id=?)",
                       (prior + "T09:00:00+09:00", task["id"], task["id"]))
        report = self.pipeline.report(prior, prior)
        self.assertEqual(report["active_count"], 1)
        self.assertEqual(report["completed_count"], 0)

    def test_invalid_state_rolls_back(self):
        task = self.accept_one()
        prior_events = len(self.store.state()["events"])
        with self.assertRaises(ValueError):
            self.store.edit_task(task["id"], {"status": "거짓상태"})
        self.assertEqual(len(self.store.state()["events"]), prior_events)

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

    def test_large_request_mail_preserved_as_one_summary_task(self):
        body = "\n".join(f"업무: 자료 검토 {index}; 준비해주세요." for index in range(40))
        result = self.pipeline.ingest([{**self.mails[0], "body": body}])
        self.assertEqual(result["failed"], 0)
        mail = self.store.rows("SELECT * FROM messages")[0]
        self.assertEqual(mail["body"], body)
        self.assertEqual(len(self.store.state()["proposals"]), 1)
        payload = json.loads(self.store.state()["proposals"][0]["payload"])
        self.assertEqual(payload["merged_candidate_count"], 40)
        self.assertIn("자료 검토 0", payload["summary"])

    def test_legacy_pending_candidates_are_compacted_without_losing_history(self):
        self.ingest_one()
        mail = self.store.rows("SELECT id FROM messages")[0]
        for index in range(3):
            payload = {"title": f"분리된 요청 {index}", "project": "프로젝트", "action": "CREATE",
                       "evidence_quote": self.mails[0]["body"].splitlines()[0]}
            with self.store.connect() as db:
                db.execute("INSERT INTO proposals(id,source_id,action,payload) VALUES(?,?,?,?)",
                           (f"legacy-{index}", mail["id"], "CREATE", json.dumps(payload, ensure_ascii=False)))
        Pipeline(self.cfg, self.store)
        pending = self.store.state()["proposals"]
        self.assertEqual(sum(p["source_id"] == mail["id"] for p in pending), 1)
        compacted = next(p for p in pending if p["source_id"] == mail["id"])
        self.assertEqual(json.loads(compacted["payload"])["merged_candidate_count"], 4)
        archived = self.store.rows("SELECT state FROM proposals WHERE source_id=? AND state='통합됨'", (mail["id"],))
        self.assertEqual(len(archived), 4)

    def test_generic_confirmation_proposal_is_archived_on_startup(self):
        self.ingest_one()
        mail = self.store.rows("SELECT id FROM messages")[0]
        with self.store.connect() as db:
            db.execute("INSERT INTO proposals(id,source_id,action,payload) VALUES(?,?,?,?)",
                       ("generic-confirmation", mail["id"], "CREATE",
                        json.dumps({"title": "확인", "evidence_quote": "내용 확인부탁드립니다."}, ensure_ascii=False)))
        Pipeline(self.cfg, self.store)
        self.assertEqual(self.store.rows("SELECT state FROM proposals WHERE id='generic-confirmation'")[0]["state"], "불필요")
        self.assertFalse(any(p["id"] == "generic-confirmation" for p in self.store.state()["proposals"]))


if __name__ == "__main__":
    unittest.main()
