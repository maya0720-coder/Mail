import tempfile
import types
import unittest
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from mailbrain.collectors import collection_window, outlook_filter, outlook, outlook_probe, _outlook_account, _outlook_body, watch_outlook_new_mail
from mailbrain.core import KST, Pipeline, Store, load_config, ROOT
from mailbrain.sync import SyncManager
from mailbrain.demo import messages


class Collection:
    def __init__(self, entries):
        self.entries = entries
        self.Count = len(entries)
        self.filters = []
        self.sorts = []

    def Item(self, index):
        return self.entries[index - 1]

    def Restrict(self, condition):
        self.filters.append(condition)
        return self

    def Sort(self, key, descending):
        self.sorts.append((key, descending))


class Mail:
    Class = 43
    def __init__(self, identifier, received, recipient="me@example.com"):
        self.EntryID = identifier
        self.PropertyAccessor = types.SimpleNamespace(GetProperty=lambda _: identifier)
        self.ReceivedTime = received
        self.SentOn = received
        self.ConversationID = "thread-1"
        self.Subject = "검토 요청"
        self.Sender = types.SimpleNamespace(Type="SMTP", Address="lead@example.com")
        self.Recipients = Collection([types.SimpleNamespace(Type=1, AddressEntry=types.SimpleNamespace(Type="SMTP", Address=recipient))])
        self.body_reads = 0

    @property
    def Body(self):
        self.body_reads += 1
        return "me@example.com 업무: 자료 검토; 준비해주세요."


class OutlookTests(unittest.TestCase):
    def setUp(self):
        self.clock = datetime(2026, 11, 4, 15, 0, tzinfo=KST)
        self.cfg = {"user_email": "me@example.com", "aliases": [], "engine": "rules"}

    def test_default_is_rolling_30_days(self):
        lower, upper = collection_window(clock=self.clock)
        self.assertEqual(upper - lower, timedelta(days=30))
        self.assertEqual(upper, self.clock)

    def test_end_date_is_included(self):
        lower, upper = collection_window("2026-09-01", "2026-09-30", self.clock)
        self.assertEqual(lower.day, 1)
        self.assertEqual(upper, datetime(2026, 10, 1, tzinfo=KST))

    def test_today_capped_at_now(self):
        _, upper = collection_window("2026-11-01", "2026-11-04", self.clock)
        self.assertEqual(upper, self.clock)

    def test_rejects_invalid_ranges(self):
        for first, last in [("2026-09-01", None), (None, "2026-10-01"), ("2026-10-02", "2026-10-01"), ("2025-01-01", "2026-10-01"), ("2026-11-01", "2026-12-01")]:
            with self.subTest(first=first, last=last), self.assertRaises(ValueError):
                collection_window(first, last, self.clock)

    def test_filter_converts_to_utc(self):
        lower, upper = collection_window("2026-09-01", "2026-09-30", self.clock)
        query = outlook_filter(lower, upper)
        self.assertIn("2026-08-31 15:00:00", query)
        self.assertIn("2026-09-30 15:00:00", query)
        self.assertIn("urn:schemas:httpmail:datereceived", query)

    def test_outlook_html_body_is_used_when_plain_body_is_empty(self):
        item = types.SimpleNamespace(Body="", HTMLBody="<html><style>hidden</style><p>메일 내용</p><br>확인</html>")
        self.assertEqual(_outlook_body(item), "메일 내용\n확인")

    def test_new_mail_event_notifies_sync_callback(self):
        client = types.SimpleNamespace(Dispatch=MagicMock(return_value=object()))
        client.WithEvents = lambda app, event_class: event_class()
        win32com = types.ModuleType("win32com")
        win32com.client = client
        called = threading.Event()
        with patch.dict("sys.modules", {"win32com": win32com, "win32com.client": client}):
            app, sink = watch_outlook_new_mail(called.set)
            sink.OnNewMailEx("new-entry-id")
        self.assertTrue(called.is_set())
        client.Dispatch.assert_called_once_with("Outlook.Application")

    def test_missing_pywin32_errors_explain_install_requirement(self):
        with patch.dict("sys.modules", {"pythoncom": None, "win32com": None, "win32com.client": None}):
            with self.assertRaisesRegex(ValueError, "requirements.txt"):
                outlook_probe(self.cfg)
            with self.assertRaisesRegex(ValueError, "requirements.txt"):
                watch_outlook_new_mail(lambda: None)

    def test_outlook_com_failure_includes_hresult(self):
        error = RuntimeError("COM activation failed")
        error.hresult = 0x80070520
        client = types.SimpleNamespace(Dispatch=MagicMock(side_effect=error))
        win32com = types.ModuleType("win32com")
        win32com.client = client
        with patch.dict("sys.modules", {"win32com": win32com, "win32com.client": client}):
            with self.assertRaisesRegex(ValueError, "HRESULT 0x80070520"):
                from mailbrain.collectors import _outlook_namespace
                _outlook_namespace()

    def test_only_matching_account_delivery_store(self):
        other = types.SimpleNamespace(SmtpAddress="other@example.com", DeliveryStore=MagicMock())
        mine = types.SimpleNamespace(SmtpAddress="me@example.com", DeliveryStore=MagicMock())
        ns = types.SimpleNamespace(Accounts=Collection([other, mine]))
        self.assertIs(_outlook_account(ns, self.cfg), mine)
        other.DeliveryStore.GetDefaultFolder.assert_not_called()
        with self.assertRaises(ValueError):
            _outlook_account(ns, {"user_email": "missing@example.com"})

    def test_filter_before_body_and_skip_known_outside_other(self):
        lower, upper = collection_window(clock=self.clock)
        valid = Mail("new", upper - timedelta(days=1))
        known = Mail("known", upper - timedelta(days=2))
        outside = Mail("old", lower - timedelta(days=1))
        other = Mail("other", upper - timedelta(days=3), "other@example.com")
        items = Collection([valid, known, outside, other])
        folder = types.SimpleNamespace(Items=items)
        delivery = types.SimpleNamespace(GetDefaultFolder=lambda _: folder)
        ns = types.SimpleNamespace(Accounts=Collection([types.SimpleNamespace(SmtpAddress="me@example.com", DeliveryStore=delivery)]))
        com = types.SimpleNamespace(CoInitialize=MagicMock(), CoUninitialize=MagicMock())
        with patch.dict("sys.modules", {"pythoncom": com}), patch("mailbrain.collectors._outlook_namespace", return_value=ns), patch("mailbrain.collectors.collection_window", return_value=(lower, upper)):
            result = list(outlook(self.cfg, known_ids={"known"}))
        self.assertEqual([m["id"] for m in result], ["new"])
        self.assertEqual((known.body_reads, outside.body_reads, other.body_reads), (0, 0, 0))
        self.assertEqual(valid.body_reads, 1)
        self.assertEqual(len(items.filters), 1)
        self.assertEqual(items.sorts, [("[ReceivedTime]", False)])
        com.CoInitialize.assert_called_once()
        com.CoUninitialize.assert_called_once()

    def test_ad_identity_uses_requested_domain(self):
        with patch.dict("os.environ", {"USERNAME": "maya0720", "USERDOMAIN": "GFZ"}):
            cfg = load_config(ROOT / "config.example.json")
        self.assertEqual(cfg["user_email"], "maya0720@golfzon.com")

    def test_teams_sender_excluded_before_body_read(self):
        lower,upper=collection_window(clock=self.clock)
        mail=Mail('teams-message',upper-timedelta(days=1))
        mail.Sender.Address='no-reply@teams.mail.microsoft'
        items=Collection([mail])
        ns=types.SimpleNamespace(Accounts=Collection([types.SimpleNamespace(SmtpAddress='me@example.com',DeliveryStore=types.SimpleNamespace(GetDefaultFolder=lambda _:types.SimpleNamespace(Items=items)))]))
        com=types.SimpleNamespace(CoInitialize=MagicMock(),CoUninitialize=MagicMock())
        with patch.dict('sys.modules',{'pythoncom':com}),patch('mailbrain.collectors._outlook_namespace',return_value=ns),patch('mailbrain.collectors.collection_window',return_value=(lower,upper)):
            self.assertEqual(list(outlook(self.cfg)),[])
        self.assertEqual(mail.body_reads,0)

    def test_sync_records_counts_without_exposing_body(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            pipeline = Pipeline(self.cfg, store)
            manager = SyncManager(self.cfg, store, pipeline)
            probe = {"connected": True, "email": "me@example.com", "folder": "받은 편지함", "method": "Classic Outlook"}
            with patch("mailbrain.sync.outlook_probe", return_value=probe), patch("mailbrain.sync.outlook", return_value=iter(messages("me@example.com")[:1])):
                manager.execute(None, None, False)
            status = manager.snapshot()
            self.assertEqual(status["imported"], 1)
            self.assertTrue(status["connected"])
            self.assertNotIn("body", status)

    def test_sync_refuses_overlapping_request(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            manager = SyncManager(self.cfg, store, Pipeline(self.cfg, store))
            manager.submit()
            with self.assertRaises(ValueError):
                manager.submit("2026-09-01", "2026-09-30")

    def test_auto_sync_starts_on_app_start(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            cfg = {**self.cfg, "outlook_auto_sync": True}
            manager = SyncManager(cfg, store, Pipeline(cfg, store))
            called = threading.Event()
            with patch.object(manager, "execute", side_effect=lambda *args: called.set()):
                manager.start()
                try:
                    self.assertTrue(called.wait(3))
                finally:
                    manager.close()

    def test_new_mail_event_queues_followup_sync(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            cfg = {**self.cfg, "outlook_auto_sync": True}
            manager = SyncManager(cfg, store, Pipeline(cfg, store))
            registered = threading.Event()
            callback = {}
            calls = []
            first_call = threading.Event()
            enough_calls = threading.Event()

            def watch(on_new_mail):
                callback["notify"] = on_new_mail
                registered.set()
                app = object()
                return app, object()

            com = types.SimpleNamespace(CoInitialize=MagicMock(), CoUninitialize=MagicMock(), PumpWaitingMessages=MagicMock())
            def execute(*args):
                calls.append(args)
                first_call.set()
                if len(calls) >= 2:
                    enough_calls.set()

            with patch.dict("sys.modules", {"pythoncom": com}), patch("mailbrain.collectors.watch_outlook_new_mail", side_effect=watch), patch.object(manager, "execute", side_effect=execute):
                manager.start()
                try:
                    self.assertTrue(registered.wait(2))
                    self.assertTrue(first_call.wait(2))
                    callback["notify"]()
                    self.assertTrue(enough_calls.wait(2))
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(manager.snapshot()["event_watch"], "connected")
                finally:
                    manager.close()


if __name__ == "__main__":
    unittest.main()
