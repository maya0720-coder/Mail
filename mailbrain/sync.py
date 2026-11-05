"""Outlook 수집 전용 STA 작업 스레드와 최근 30일 자동 수집."""
import queue
import threading
import time
from datetime import datetime, timedelta

from .core import KST, now, dump
from .collectors import outlook, outlook_probe, collection_window


class SyncManager:
    def __init__(self, cfg, store, pipeline):
        self.cfg, self.store, self.pipeline = cfg, store, pipeline
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.jobs = queue.Queue(maxsize=1)
        self.new_mail_event = threading.Event()
        self.thread = None
        self.state = {"phase": "대기", "connected": False, "busy": False, "error": None,
                      "email": cfg["user_email"], "scanned": 0, "matched": 0,
                      "imported": 0, "failed": 0, "last_finished": None, "next_sync": None,
                      "event_watch": "starting" if cfg.get("outlook_auto_sync", False) else "disabled",
                      "watch_error": None,
                      "auto_enabled": bool(cfg.get("outlook_auto_sync", False)),
                      "interval_minutes": max(1, int(cfg.get("outlook_sync_minutes", 10))),
                      "range_start": None, "range_end": None}

    def update(self, **values):
        with self.lock:
            self.state.update(values)

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def start(self):
        self.thread = threading.Thread(target=self.worker, name="outlook-sta-worker", daemon=True)
        self.thread.start()
        if self.state["auto_enabled"]:
            self.submit()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3)

    def submit(self, start=None, end=None, probe=False):
        if not probe:
            collection_window(start, end)
        with self.lock:
            if self.state["busy"]:
                raise ValueError("Outlook 작업이 진행 중입니다. 완료 후 다시 수집하세요.")
            self.state.update(busy=True, phase="연결 확인 중" if probe else "수집 준비 중", error=None)
        try:
            self.jobs.put_nowait((start, end, probe))
        except queue.Full:
            self.update(busy=False)
            raise ValueError("이미 대기 중인 수집 요청이 있습니다.")
        return self.snapshot()

    def worker(self):
        next_due = None
        interval = self.state["interval_minutes"] * 60
        pythoncom = application = event_sink = None
        com_initialized = False
        if self.state["auto_enabled"]:
            try:
                import pythoncom as com
                com.CoInitialize()
                pythoncom, com_initialized = com, True
                from .collectors import watch_outlook_new_mail
                application, event_sink = watch_outlook_new_mail(self.new_mail_event.set)
                self.update(event_watch="connected", watch_error=None)
            except Exception as exc:
                self.update(event_watch="unavailable", watch_error=str(exc))
        try:
            while not self.stop_event.is_set():
                if pythoncom is not None:
                    pythoncom.PumpWaitingMessages()
                if self.state["auto_enabled"] and self.new_mail_event.is_set() and not self.state["busy"]:
                    self.new_mail_event.clear()
                    try:
                        self.submit()
                    except ValueError:
                        self.new_mail_event.set()
                if self.state["auto_enabled"] and next_due is not None and time.monotonic() >= next_due:
                    try:
                        self.submit()
                        next_due = None
                    except ValueError:
                        pass
                try:
                    job = self.jobs.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    self.execute(*job)
                except Exception as exc:
                    self.update(phase="연결/수집 실패", error=str(exc), last_finished=now())
                finally:
                    self.update(busy=False)
                    self.jobs.task_done()
                    if self.state["auto_enabled"]:
                        next_due = time.monotonic() + interval
                        self.update(next_sync=(datetime.now(KST) + timedelta(seconds=interval)).isoformat(timespec="seconds"))
        finally:
            event_sink = None
            application = None
            if com_initialized:
                pythoncom.CoUninitialize()

    def execute(self, start, end, probe):
        import uuid
        run_id = uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute("INSERT INTO runs VALUES(?,?,NULL,?,?,NULL)",
                       (run_id, now(), "연결 확인" if probe else "Outlook 수집중", dump([{"role": "outlook_collect", "period": [start, end]}])))
        try:
            try:
                details = outlook_probe(self.cfg)
            except Exception:
                self.update(connected=False)
                raise
            self.update(connected=True, phase="연결됨", **{k: v for k, v in details.items() if k != "connected"})
            if probe:
                self.update(last_finished=now())
            else:
                lower, upper = collection_window(start, end)
                self.update(phase="메일 수집 중", scanned=0, matched=0, imported=0, failed=0,
                            range_start=lower.isoformat(), range_end=upper.isoformat())
                # Revisit rows whose original collection produced no body so the
                # collector can recover content from Outlook's HTMLBody.
                known = {row["id"] for row in self.store.rows("SELECT id FROM messages WHERE length(trim(body)) > 0")}
                counts = {"imported": 0, "failed": 0}

                def progress(**values):
                    if self.stop_event.is_set():
                        raise InterruptedError("앱 종료로 수집을 중단했습니다. 다음 실행에서 중복 없이 재개합니다.")
                    self.update(**values)

                def ingest(batch):
                    result = self.pipeline.ingest(batch)
                    counts["imported"] += result["imported"]
                    counts["failed"] += result["failed"]
                    self.update(**counts)

                batch = []
                def metadata_update(identifier, received_at):
                    with self.store.connect() as db:
                        db.execute("UPDATE messages SET received_at=? WHERE id=? AND received_at IS NULL", (received_at, identifier))
                for message in outlook(self.cfg, start, end, known, progress, metadata_update):
                    batch.append(message)
                    if len(batch) >= 20:
                        ingest(batch)
                        batch = []
                if batch:
                    ingest(batch)
                self.update(phase="일부 메일 분석 실패" if counts["failed"] else "수집 완료", last_finished=now())
            with self.store.connect() as db:
                db.execute("UPDATE runs SET ended_at=?,status=? WHERE id=?", (now(), "완료", run_id))
        except Exception as exc:
            with self.store.connect() as db:
                db.execute("UPDATE runs SET ended_at=?,status=?,error=? WHERE id=?", (now(), "실패", str(exc), run_id))
            raise
