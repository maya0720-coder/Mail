import hashlib
import json
import re
import sqlite3
import uuid
import os
import getpass
from functools import wraps
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parent.parent
KST = timezone(timedelta(hours=9))
STATE_ORDER = ["미등록", "등록완료", "진행중", "대기", "완료", "취소"]
STATES = set(STATE_ORDER)
STATUS_ALIASES = {"확인필요": "미등록", "할일": "등록완료"}
GENERIC_REQUEST_PATTERNS = (
    re.compile(r"^(?:내용|관련건|해당건|상기(?:의)?내용)(?:확인|검토).{0,15}(?:부탁|요청)"),
    re.compile(r"^추가로?답변작성중문의사항이생겨.{0,50}확인요청"),
    re.compile(r"^혹시.{0,80}(?:필요하시다면|필요하시면).{0,20}(?:코멘트|연락)부탁"),
)


def generic_followup(text):
    normalized = re.sub(r"\s+", "", str(text or "")).casefold()
    return any(pattern.search(normalized) for pattern in GENERIC_REQUEST_PATTERNS)


def canonical_status(value):
    return STATUS_ALIASES.get(value, value)


def canonical_task(task):
    # Older automatic-task refresh events stored partial snapshots. Normalize
    # them before reports replay events so existing databases remain readable.
    normalized = {
        "id": None, "title": "(업무 제목 없음)", "project": "기타",
        "assignee": None, "due_date": None, "status": "등록완료",
        "created_at": None, "updated_at": None, "source_id": None,
        "manual": 0, "completed_at": None, "deleted_at": None,
        "summary": None, "auto_generated": 0, "read_at": None,
        "needs_reply": 0,
    }
    normalized.update(task or {})
    normalized["status"] = canonical_status(normalized.get("status") or "등록완료")
    return normalized


def now():
    return datetime.now(KST).isoformat(timespec="seconds")


def dump(value):
    return json.dumps(value, ensure_ascii=False)


def addresses(value):
    from email.utils import getaddresses
    if isinstance(value, list):
        value = ",".join(value)
    return {addr.lower() for _, addr in getaddresses([str(value or "")]) if "@" in addr}


def audited(role):
    """검색·보고 작업도 내용 대신 상태와 프롬프트 지문만 기록합니다."""
    def decorate(fn):
        @wraps(fn)
        def wrapped(self, *args, **kwargs):
            run_id = uuid.uuid4().hex
            text = "\n\n".join(p.read_text(encoding="utf-8") for p in
                                   [ROOT / "main_prompt", ROOT / "prompts/common.md", ROOT / "prompts" / (role + ".md")])
            trace = [{"role": role, "engine": self.cfg["engine"], "prompt_sha256": hashlib.sha256(text.encode()).hexdigest()}]
            with self.store.connect() as db:
                db.execute("INSERT INTO runs VALUES(?,?,NULL,?,?,NULL)", (run_id, now(), "실행중", dump(trace)))
            try:
                result = fn(self, *args, **kwargs)
            except Exception:
                with self.store.connect() as db:
                    db.execute("UPDATE runs SET ended_at=?,status=?,error=? WHERE id=?", (now(), "실패", "입력·설정·모델 상태를 확인하세요.", run_id))
                raise
            with self.store.connect() as db:
                db.execute("UPDATE runs SET ended_at=?,status=? WHERE id=?", (now(), "완료", run_id))
            return result
        return wrapped
    return decorate


def load_config(path=None):
    source = Path(path or ROOT / "config.json")
    cfg = json.loads((source if source.exists() else ROOT / "config.example.json").read_text(encoding="utf-8-sig"))
    if cfg.get("user_email") == "auto" or cfg.get("identity_mode") == "windows_ad":
        username = (os.environ.get("USERNAME") or getpass.getuser()).split("\\")[-1]
        if not re.fullmatch(r"[A-Za-z0-9._-]+", username):
            raise ValueError("Windows 계정명을 확인할 수 없습니다. user_email을 직접 지정하세요.")
        domain = cfg.get("email_domain", "golfzon.com")
        if not re.fullmatch(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}", domain):
            raise ValueError("이메일 도메인이 올바르지 않습니다.")
        cfg["user_email"] = username.lower() + "@" + domain.lower()
        cfg["windows_account"] = (os.environ.get("USERDOMAIN", "") + "\\" + username).strip("\\")
    if cfg.get("engine") not in {"rules", "ollama"}:
        raise ValueError("engine은 rules 또는 ollama여야 합니다.")
    if not addresses(cfg.get("user_email", "")) or len(addresses(cfg["user_email"])) != 1:
        raise ValueError("본인 이메일 주소를 설정하세요.")
    cfg["data_dir"] = str((ROOT / cfg.get("data_dir", "data")).resolve())
    return cfg


class Store:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS messages(
                  id TEXT PRIMARY KEY, thread_id TEXT, subject TEXT, sender TEXT,
                  recipients TEXT, sent_at TEXT, body TEXT, summary TEXT,
                  source TEXT, imported_at TEXT, analysis TEXT);
                CREATE TABLE IF NOT EXISTS tasks(
                  id TEXT PRIMARY KEY, title TEXT, project TEXT, assignee TEXT,
                  due_date TEXT, status TEXT, created_at TEXT, updated_at TEXT,
                  source_id TEXT REFERENCES messages(id), manual INTEGER DEFAULT 0,
                  summary TEXT, auto_generated INTEGER NOT NULL DEFAULT 0,
                  read_at TEXT, needs_reply INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS events(
                  seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
                  kind TEXT, before_json TEXT, after_json TEXT, source_id TEXT,
                  actor TEXT, at TEXT);
                CREATE TABLE IF NOT EXISTS proposals(
                  id TEXT PRIMARY KEY, source_id TEXT, action TEXT, target_id TEXT,
                  payload TEXT, reason TEXT, state TEXT DEFAULT '대기');
                CREATE TABLE IF NOT EXISTS runs(
                  id TEXT PRIMARY KEY, started_at TEXT, ended_at TEXT,
                  status TEXT, stages TEXT, error TEXT);
                CREATE TABLE IF NOT EXISTS reports(
                  id TEXT PRIMARY KEY, start_date TEXT, end_date TEXT,
                  cutoff TEXT, content TEXT);
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT);
            """)
            message_columns = {row["name"] for row in db.execute("PRAGMA table_info(messages)")}
            if "received_at" not in message_columns:
                db.execute("ALTER TABLE messages ADD COLUMN received_at TEXT")
            if "direction" not in message_columns:
                db.execute("ALTER TABLE messages ADD COLUMN direction TEXT NOT NULL DEFAULT 'inbound'")
            if "excluded" not in message_columns:
                db.execute("ALTER TABLE messages ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0")
            task_columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
            for column in ("completed_at", "deleted_at"):
                if column not in task_columns:
                    db.execute(f"ALTER TABLE tasks ADD COLUMN {column} TEXT")
            for column, declaration in (("summary", "TEXT"), ("auto_generated", "INTEGER NOT NULL DEFAULT 0"),
                                        ("read_at", "TEXT"), ("needs_reply", "INTEGER NOT NULL DEFAULT 0")):
                if column not in task_columns:
                    db.execute(f"ALTER TABLE tasks ADD COLUMN {column} {declaration}")
            for old, new in STATUS_ALIASES.items():
                db.execute("UPDATE tasks SET status=? WHERE status=?", (new, old))
            # 기존 완료 건은 실제 완료 전이 이벤트가 있을 때만 시각을 보완합니다.
            for task in db.execute("SELECT id FROM tasks WHERE status='완료' AND completed_at IS NULL").fetchall():
                completed_at = None
                for event in db.execute("SELECT * FROM events WHERE task_id=? ORDER BY seq", (task["id"],)):
                    before = json.loads(event["before_json"]) or {}
                    after = json.loads(event["after_json"])
                    if after.get("status") == "완료" and before.get("status") != "완료":
                        completed_at = event["at"]
                if completed_at:
                    db.execute("UPDATE tasks SET completed_at=? WHERE id=?", (completed_at, task["id"]))
            if "document" not in {row["name"] for row in db.execute("PRAGMA table_info(reports)")}:
                db.execute("ALTER TABLE reports ADD COLUMN document TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS ix_message_thread ON messages(thread_id)")
        self.apply_message_policy({})

    def apply_message_policy(self, cfg):
        from .policy import sender_excluded, noise_mail
        with self.connect() as db:
            for mail in db.execute("SELECT id,sender,subject,body,excluded FROM messages").fetchall():
                if (sender_excluded(mail["sender"], cfg) or noise_mail(mail["subject"], mail["body"], mail["sender"])) and not mail["excluded"]:
                    db.execute("UPDATE messages SET excluded=1 WHERE id=?", (mail["id"],))

    def bind_owner(self, email):
        with self.connect() as db:
            row = db.execute("SELECT value FROM metadata WHERE key='owner' ").fetchone()
            if row and row["value"] != email.lower():
                raise ValueError("이 데이터 폴더는 다른 계정에 연결되어 있습니다. data_dir을 분리하세요.")
            db.execute("INSERT OR IGNORE INTO metadata VALUES('owner',?)", (email.lower(),))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.folder / "mailbrain.sqlite3", timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def rows(self, sql, args=()):
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, args)]

    def state(self):
        from .topics import Topics
        return {
            "message_count": self.rows("SELECT count(*) AS n FROM messages WHERE excluded=0")[0]["n"],
            "excluded_message_count": self.rows("SELECT count(*) AS n FROM messages WHERE excluded=1")[0]["n"],
            "messages": self.rows("SELECT id,subject,sender,sent_at,received_at,summary,source FROM messages WHERE excluded=0 ORDER BY COALESCE(received_at,sent_at) DESC LIMIT 300"),
            "topics": Topics(self).summaries(),
            "tasks": self.rows("SELECT * FROM tasks ORDER BY updated_at DESC"),
            "proposals": self.rows("SELECT p.* FROM proposals p JOIN messages m ON p.source_id=m.id WHERE p.state='대기' AND m.excluded=0 ORDER BY p.rowid"),
            "events": self.rows("SELECT * FROM events ORDER BY seq DESC LIMIT 200"),
            "runs": self.rows("SELECT * FROM runs ORDER BY started_at DESC LIMIT 30"),
            "reports": self.rows("SELECT * FROM reports ORDER BY cutoff DESC LIMIT 20"),
        }

    def change(self, db, target, changes, actor, source_id=None, kind="변경"):
        old = dict(db.execute("SELECT * FROM tasks WHERE id=?", (target,)).fetchone())
        new = {**old, **changes, "updated_at": now()}
        new["status"] = canonical_status(new["status"])
        if new["status"] == "완료":
            new["completed_at"] = old.get("completed_at") if old["status"] == "완료" else new["updated_at"]
        else:
            new["completed_at"] = None
        db.execute("UPDATE tasks SET title=?,project=?,assignee=?,due_date=?,status=?,updated_at=?,manual=?,completed_at=?,deleted_at=? WHERE id=?",
                   (new["title"], new["project"], new["assignee"], new["due_date"], new["status"], new["updated_at"], new["manual"], new["completed_at"], new["deleted_at"], target))
        db.execute("INSERT INTO events(task_id,kind,before_json,after_json,source_id,actor,at) VALUES(?,?,?,?,?,?,?)",
                   (target, kind, dump(old), dump(new), source_id, actor, now()))

    def edit_task(self, task_id, changes):
        allowed = {"title", "project", "assignee", "due_date", "status"}
        if not changes or set(changes) - allowed:
            raise ValueError("변경 항목이 올바르지 않습니다.")
        if "status" in changes and changes["status"] not in STATES:
            raise ValueError("업무 상태가 올바르지 않습니다.")
        if "due_date" in changes and changes["due_date"]:
            date.fromisoformat(changes["due_date"])
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM tasks WHERE id=? AND deleted_at IS NULL", (task_id,)).fetchone():
                raise ValueError("업무를 찾지 못했습니다.")
            self.change(db, task_id, {**changes, "manual": 1}, "사용자", kind="사용자수정")

    def delete_task(self, task_id):
        with self.connect() as db:
            task = db.execute("SELECT * FROM tasks WHERE id=? AND deleted_at IS NULL", (task_id,)).fetchone()
            if not task:
                raise ValueError("삭제할 업무를 찾지 못했습니다.")
            self.change(db, task_id, {"deleted_at": now(), "manual": 1}, "사용자", kind="삭제")

    def restore_task(self, task_id):
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM tasks WHERE id=? AND deleted_at IS NOT NULL", (task_id,)).fetchone():
                raise ValueError("복원할 삭제 업무를 찾지 못했습니다.")
            self.change(db, task_id, {"deleted_at": None, "manual": 1}, "사용자", kind="복원")

    def list_tasks(self, status="", query="", project="", start="", end="", scope="active"):
        if status and status not in STATES:
            raise ValueError("업무 상태 필터가 올바르지 않습니다.")
        if scope not in {"active", "deleted"}:
            raise ValueError("목록 범위가 올바르지 않습니다.")
        if len(query) > 200 or len(project) > 200:
            raise ValueError("검색어는 200자 이하로 입력하세요.")
        for value in (start, end):
            if value:
                date.fromisoformat(value)
        if start and end and start > end:
            raise ValueError("완료일 시작은 종료일보다 늦을 수 없습니다.")
        conditions = ["deleted_at IS NULL" if scope == "active" else "deleted_at IS NOT NULL"]
        params = []
        for column, value in (("status", status), ("project", project)):
            if value:
                conditions.append(column + "=?")
                params.append(value)
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append("(title LIKE ? ESCAPE '\\' OR project LIKE ? ESCAPE '\\' OR assignee LIKE ? ESCAPE '\\')")
            params.extend(["%" + escaped + "%"] * 3)
        if start:
            conditions.append("substr(completed_at,1,10)>=?")
            params.append(start)
        if end:
            conditions.append("substr(completed_at,1,10)<=?")
            params.append(end)
        tasks = self.rows("SELECT * FROM tasks WHERE " + " AND ".join(conditions) + " ORDER BY updated_at DESC,id", params)
        # Attach existing thread topics to task rows so the UI can group work
        # by conversation and category instead of rendering unrelated flat rows.
        from .topics import Topics, task_title
        topic_by_message = {}
        for topic in Topics(self).build():
            for message in topic["messages"]:
                topic_by_message[message["id"]] = topic
        for task in tasks:
            topic = topic_by_message.get(task.get("source_id"))
            if topic:
                task["topic_id"] = topic["id"]
                task["topic_title"] = topic["title"]
                task["category"] = topic["category"]
                task["approval_status"] = topic["approval_status"]
                task["topic_message_count"] = topic["message_count"]
                task["topic_summary"] = topic["latest_summary"]
                task["thread_messages"] = [{key: message[key] for key in ("id", "subject", "sender", "sent_at", "received_at")}
                                            for message in topic["messages"]]
                task["display_title"] = task_title(topic["title"], topic["latest_summary"])
            else:
                task["topic_id"] = "task-" + str(task["id"])
                task["topic_title"] = task.get("title") or "제목 없는 업무"
                task["category"] = "상시업무"
                task["approval_status"] = None
                task["topic_message_count"] = 0
                task["topic_summary"] = task.get("summary") or ""
                task["thread_messages"] = []
                task["display_title"] = task_title(task["title"] or "제목 없는 업무")
            processed = task["status"] in {"완료", "취소"}
            if task["category"] == "전자결재":
                task["work_bucket"] = "전자결재 처리완료" if processed else "전자결재 처리미완료"
            elif processed:
                task["work_bucket"] = "처리완료"
            elif int(task.get("auto_generated") or 0) and not task.get("read_at"):
                task["work_bucket"] = "확인필요"
            else:
                task["work_bucket"] = "확인완료(미처리)"
        counts = {s: 0 for s in STATE_ORDER}
        scope_where = "deleted_at IS NULL" if scope == "active" else "deleted_at IS NOT NULL"
        for row in self.rows("SELECT status,count(*) AS n FROM tasks WHERE " + scope_where + " GROUP BY status"):
            counts[row["status"]] = row["n"]
        return {"tasks": tasks, "counts": counts, "filtered_count": len(tasks), "states": STATE_ORDER}

    def resolve(self, proposal_id, accept, target_id=None):
        with self.connect() as db:
            row = db.execute("SELECT * FROM proposals WHERE id=? AND state='대기'", (proposal_id,)).fetchone()
            if not row:
                raise ValueError("이미 처리했거나 존재하지 않는 확인 항목입니다.")
            if db.execute("SELECT excluded FROM messages WHERE id=?", (row["source_id"],)).fetchone()["excluded"]:
                raise ValueError("수집 제외 발신자의 제안은 업무에 반영하지 않습니다.")
            if accept:
                payload = json.loads(row["payload"])
                target = target_id or row["target_id"]
                if target:
                    if not db.execute("SELECT 1 FROM tasks WHERE id=? AND deleted_at IS NULL", (target,)).fetchone():
                        raise ValueError("대상 업무가 없습니다.")
                    changes = {}
                    if row["action"] == "CANCEL":
                        changes["status"] = "취소"
                    elif row["action"] == "COMPLETE":
                        changes["status"] = "완료"
                    else:
                        changes = {k: payload[k] for k in ("title", "project", "due_date", "assignee") if payload.get(k) is not None}
                    changes["manual"] = 1
                    self.change(db, target, changes, "사용자승인", row["source_id"], "메일반영")
                elif row["action"] == "CREATE":
                    automatic = db.execute("SELECT t.* FROM tasks t JOIN messages tm ON tm.id=t.source_id JOIN messages sm ON sm.thread_id=tm.thread_id WHERE sm.id=? AND t.auto_generated=1 AND t.deleted_at IS NULL ORDER BY t.created_at,t.id LIMIT 1", (row["source_id"],)).fetchone()
                    if automatic:
                        changes = {k: payload[k] for k in ("title", "project", "due_date", "assignee") if payload.get(k) is not None}
                        changes["status"] = "등록완료"
                        changes["manual"] = 1
                        self.change(db, automatic["id"], changes, "사용자승인", row["source_id"], "메일반영")
                    else:
                        self.create_task(db, payload, row["source_id"], confirmed=True)
                else:
                    raise ValueError("변경할 대상 업무를 선택하세요.")
            db.execute("UPDATE proposals SET state=? WHERE id=?", ("승인" if accept else "제외", proposal_id))

    def create_task(self, db, item, source, confirmed=False):
        timestamp = now()
        from .policy import reply_requested
        message = db.execute("SELECT subject,summary,body FROM messages WHERE id=?", (source,)).fetchone()
        task = dict(id=uuid.uuid4().hex[:12], title=item["title"], project=item.get("project") or "미분류",
                    assignee=item.get("assignee"), due_date=item.get("due_date"),
                    status="등록완료" if confirmed else "미등록", created_at=timestamp, updated_at=timestamp,
                    source_id=source, manual=int(confirmed), completed_at=None, deleted_at=None,
                    summary=message["summary"] if message else None, auto_generated=0,
                    read_at=timestamp if confirmed else None,
                    needs_reply=int(reply_requested(message["subject"], message["body"])) if message else 0)
        db.execute("INSERT INTO tasks(id,title,project,assignee,due_date,status,created_at,updated_at,source_id,manual,completed_at,deleted_at,summary,auto_generated,read_at,needs_reply) VALUES(:id,:title,:project,:assignee,:due_date,:status,:created_at,:updated_at,:source_id,:manual,:completed_at,:deleted_at,:summary,:auto_generated,:read_at,:needs_reply)", task)
        db.execute("INSERT INTO events(task_id,kind,before_json,after_json,source_id,actor,at) VALUES(?,?,?,?,?,?,?)",
                   (task["id"], "생성", "null", dump(task), source, "사용자승인" if confirmed else "분석기", timestamp))

    def create_auto_task(self, db, message, summary):
        from .policy import reply_requested
        timestamp = now()
        task = {"id": uuid.uuid4().hex[:12], "title": message["subject"].strip() or "제목 없는 메일",
                "project": "메일 요청", "assignee": None, "due_date": None, "status": "등록완료",
                "created_at": timestamp, "updated_at": timestamp, "source_id": message["id"],
                "manual": 0, "completed_at": None, "deleted_at": None,
                "summary": (summary or message["subject"])[:16000], "auto_generated": 1,
                "read_at": None, "needs_reply": int(reply_requested(message["subject"], message["body"]))}
        db.execute("INSERT INTO tasks(id,title,project,assignee,due_date,status,created_at,updated_at,source_id,manual,completed_at,deleted_at,summary,auto_generated,read_at,needs_reply) VALUES(:id,:title,:project,:assignee,:due_date,:status,:created_at,:updated_at,:source_id,:manual,:completed_at,:deleted_at,:summary,:auto_generated,:read_at,:needs_reply)", task)
        db.execute("INSERT INTO events(task_id,kind,before_json,after_json,source_id,actor,at) VALUES(?,?,?,?,?,?,?)",
                   (task["id"], "메일 자동 등록", "null", dump(task), message["id"], "자동 분류", timestamp))

    def mark_task_read(self, task_id):
        with self.connect() as db:
            cursor = db.execute("UPDATE tasks SET read_at=? WHERE id=? AND deleted_at IS NULL AND read_at IS NULL", (now(), task_id))
            if not cursor.rowcount and not db.execute("SELECT 1 FROM tasks WHERE id=? AND deleted_at IS NULL", (task_id,)).fetchone():
                raise ValueError("읽음 처리할 업무를 찾지 못했습니다.")

    def mark_task_unprocessed(self, task_id):
        with self.connect() as db:
            task = db.execute("SELECT * FROM tasks WHERE id=? AND deleted_at IS NULL", (task_id,)).fetchone()
            if not task:
                raise ValueError("미완료로 되돌릴 업무를 찾지 못했습니다.")
            if task["status"] not in {"완료", "취소"}:
                raise ValueError("이미 처리 미완료 상태입니다.")
            prior_status = "등록완료"
            for event in db.execute("SELECT before_json,after_json FROM events WHERE task_id=? ORDER BY seq DESC", (task_id,)):
                try:
                    before, after = json.loads(event["before_json"]), json.loads(event["after_json"])
                except (TypeError, json.JSONDecodeError):
                    continue
                if after and after.get("status") == task["status"] and before and before.get("status") not in {"완료", "취소"}:
                    prior_status = before["status"]
                    break
            self.change(db, task_id, {"status": prior_status, "manual": 1}, "사용자", kind="처리미완료")

    def refresh_auto_thread(self, db, thread_id, cfg, mark_unread=False):
        from .policy import task_recipient, reply_requested
        from .topics import clean_subject
        messages = [dict(row) for row in db.execute(
            "SELECT * FROM messages WHERE thread_id=? AND excluded=0 ORDER BY COALESCE(received_at,sent_at),id", (thread_id,))]
        if not messages:
            return False
        direct_messages = []
        for mail in messages:
            if mail.get("direction") == "outbound":
                continue
            try:
                recipients = json.loads(mail.get("recipients") or "{}")
            except (TypeError, json.JSONDecodeError):
                recipients = {}
            if not isinstance(recipients, dict):
                recipients = {}
            candidate = {"to": recipients.get("to", ""), "cc": recipients.get("cc", ""),
                         "subject": mail["subject"], "body": mail["body"]}
            candidate["analysis"] = mail.get("analysis")
            if task_recipient(candidate, cfg):
                direct_messages.append((mail, candidate))
        current = [dict(row) for row in db.execute(
            "SELECT t.* FROM tasks t JOIN messages m ON m.id=t.source_id "
            "WHERE m.thread_id=? AND t.auto_generated=1 AND t.deleted_at IS NULL ORDER BY t.created_at,t.id", (thread_id,))]
        if not direct_messages:
            stamp = now()
            archived = []
            for task in current:
                if task["manual"]:
                    continue
                before = dict(task)
                db.execute("UPDATE tasks SET deleted_at=?,updated_at=? WHERE id=?", (stamp, stamp, task["id"]))
                after = {**task, "deleted_at": stamp, "updated_at": stamp}
                db.execute("INSERT INTO events(task_id,kind,before_json,after_json,source_id,actor,at) VALUES(?,?,?,?,?,?,?)",
                           (task["id"], "업무 무관 메일에서 자동 업무 제외", dump(before), dump(after), task["source_id"], "자동 분류", stamp))
                archived.append(task["id"])
            return bool(archived)
        if not current:
            mail, candidate = direct_messages[-1]
            candidate["id"] = mail["id"]
            self.create_auto_task(db, candidate, mail["summary"])
            current = [dict(db.execute("SELECT * FROM tasks WHERE source_id=? AND auto_generated=1", (mail["id"],)).fetchone())]

        editable = [task for task in current if not task["manual"]]
        primary = editable[0] if editable else current[0]
        merge = [task for task in editable if task["id"] != primary["id"]]
        archived_auto = db.execute(
            "SELECT t.id FROM tasks t JOIN messages m ON m.id=t.source_id "
            "WHERE m.thread_id=? AND t.auto_generated=1 AND t.manual=0 AND t.deleted_at IS NOT NULL", (thread_id,)).fetchall()
        for archived in archived_auto:
            db.execute("UPDATE proposals SET target_id=? WHERE target_id=? AND state='대기'",
                       (primary["id"], archived["id"]))
        latest = messages[-1]

        # Keep every distinct action extracted from the thread, with its original evidence.
        actions = {}
        for mail in messages:
            try:
                analysis = json.loads(mail.get("analysis") or "{}")
            except (TypeError, json.JSONDecodeError):
                analysis = {}
            for item in analysis.get("tasks", []):
                if not isinstance(item, dict):
                    continue
                title, action, quote = item.get("title"), item.get("action"), item.get("evidence_quote")
                if not all(isinstance(value, str) and value.strip() for value in (title, action, quote)):
                    continue
                key = (action, re.sub(r"\s+", " ", title).strip().casefold())
                actions[key] = {"title": title.strip(), "action": action, "quote": quote.strip(),
                                "at": mail["received_at"] or mail["sent_at"]}

        mail_lines = []
        omitted_messages = max(0, len(messages) - 25)
        for mail in messages[-25:]:
            brief_text = re.sub(r"\s+", " ", mail.get("summary") or "").strip()
            mail_lines.append(f"- {(mail['received_at'] or mail['sent_at'])[:10]} · {clean_subject(mail['subject'])}: {brief_text[:320]}")
        action_lines = []
        labels = {"CREATE": "신규 요청", "UPDATE": "변경 요청", "CANCEL": "취소 요청", "COMPLETE": "완료 확인"}
        all_actions = list(actions.values())
        for item in all_actions[-40:]:
            action_lines.append(f"- [{labels.get(item['action'], item['action'])}] {item['title'][:180]} — {item['quote'][:260]}")
        omitted_actions = max(0, len(all_actions) - 40)
        parts = ["같은 메일 스레드의 업무 정리"]
        if action_lines:
            parts.extend(["", "메일에서 추출한 업무 요청", *action_lines])
            if omitted_actions:
                parts.append(f"- 중복 제거 후 요청 {omitted_actions}건은 원문에서 확인하세요.")
        parts.extend(["", "같은 스레드 메일별 요약", *mail_lines])
        if omitted_messages:
            parts.append(f"- 이전 메일 {omitted_messages}건은 원문에서 확인하세요.")
        summary = "\n".join(parts)[:16000]
        needs_reply = int(any(reply_requested(mail["subject"], mail["body"]) for mail in messages))
        read_at = None if mark_unread or any(task["read_at"] is None for task in current) else max(task["read_at"] for task in current)
        new_title = primary["title"] if primary["manual"] else clean_subject(latest["subject"])
        new_source = primary["source_id"] if primary["manual"] else latest["id"]
        changes = {"title": new_title, "source_id": new_source, "summary": summary,
                   "needs_reply": needs_reply, "read_at": read_at}
        changed = any(primary.get(key) != value for key, value in changes.items())
        if changed:
            stamp = now()
            before = {key: primary.get(key) for key in ("title", "source_id", "summary", "needs_reply", "read_at")}
            db.execute("UPDATE tasks SET title=?,source_id=?,summary=?,needs_reply=?,read_at=?,updated_at=? WHERE id=?",
                       (new_title, new_source, summary, needs_reply, read_at, stamp, primary["id"]))
            db.execute("INSERT INTO events(task_id,kind,before_json,after_json,source_id,actor,at) VALUES(?,?,?,?,?,?,?)",
                       (primary["id"], "메일 스레드 정보 갱신", dump(before), dump(changes), latest["id"], "자동 분류", stamp))
        for task in merge:
            stamp = now()
            before = dict(task)
            after = {**task, "deleted_at": stamp, "updated_at": stamp}
            db.execute("UPDATE proposals SET target_id=? WHERE target_id=? AND state='대기'",
                       (primary["id"], task["id"]))
            db.execute("UPDATE tasks SET deleted_at=?,updated_at=? WHERE id=?", (stamp, stamp, task["id"]))
            db.execute("INSERT INTO events(task_id,kind,before_json,after_json,source_id,actor,at) VALUES(?,?,?,?,?,?,?)",
                       (task["id"], "같은 스레드 업무 통합", dump(before), dump(after), latest["id"], "자동 분류", stamp))
        return changed or bool(merge)

    def backfill_auto_tasks(self, cfg):
        with self.connect() as db:
            thread_ids = [row["thread_id"] for row in db.execute(
                "SELECT DISTINCT thread_id FROM messages WHERE excluded=0 ORDER BY thread_id")]
            for thread_id in thread_ids:
                self.refresh_auto_thread(db, thread_id, cfg)
        return len(thread_ids)

    def compact_pending_proposals(self):
        """Collapse legacy line-by-line proposals into one review item per mail."""
        from .topics import task_title
        compacted = 0
        with self.connect() as db:
            sources = db.execute(
                "SELECT source_id FROM proposals WHERE state='대기' GROUP BY source_id HAVING count(*)>2"
            ).fetchall()
            for source in sources:
                rows = db.execute(
                    "SELECT p.* FROM proposals p WHERE p.source_id=? AND p.state='대기' ORDER BY p.rowid",
                    (source["source_id"],),
                ).fetchall()
                mail = db.execute("SELECT subject,summary,analysis FROM messages WHERE id=?", (source["source_id"],)).fetchone()
                if not mail or len(rows) <= 2:
                    continue
                payloads = []
                for row in rows:
                    try:
                        item = json.loads(row["payload"])
                    except (TypeError, json.JSONDecodeError):
                        item = {}
                    if isinstance(item, dict):
                        payloads.append((row, item))
                if len(payloads) <= 2:
                    continue
                try:
                    analysis = json.loads(mail["analysis"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    analysis = {}
                summary = (analysis.get("summary") if isinstance(analysis, dict) else None) or mail["summary"] or ""
                titles = list(dict.fromkeys(item.get("title", "") for _, item in payloads if item.get("title")))
                title = task_title(mail["subject"] or "제목 없는 메일", " ".join(titles))
                actions = {}
                for row, _ in payloads:
                    actions[row["action"]] = actions.get(row["action"], 0) + 1
                action = max(actions, key=actions.get)
                quotes = [item.get("evidence_quote", "") for _, item in payloads if item.get("evidence_quote")]
                base = next((item for _, item in payloads if item.get("evidence_quote")), payloads[0][1])
                merged = {**base, "title": title, "summary": summary,
                          "evidence_quote": quotes[0] if quotes else "",
                          "merged_candidate_count": len(payloads),
                          "merged_titles": titles[:30]}
                targets = {row["target_id"] for row, _ in payloads}
                target_id = next(iter(targets)) if len(targets) == 1 else None
                for row, _ in payloads:
                    db.execute("UPDATE proposals SET state='통합됨' WHERE id=?", (row["id"],))
                db.execute(
                    "INSERT INTO proposals(id,source_id,action,target_id,payload,reason,state) VALUES(?,?,?,?,?,?, '대기')",
                    (uuid.uuid4().hex[:12], source["source_id"], action, target_id, dump(merged),
                     f"기존 업무 후보 {len(payloads)}개를 메일 단위 요약 후보 하나로 통합했습니다."),
                )
                compacted += 1
        return compacted

    def archive_superseded_proposals(self):
        """Keep review proposals only for the newest inbound message in each thread."""
        with self.connect() as db:
            latest = db.execute("""
                SELECT id FROM (
                    SELECT id,thread_id,ROW_NUMBER() OVER (
                        PARTITION BY thread_id ORDER BY COALESCE(received_at,sent_at) DESC,id DESC
                    ) AS position
                    FROM messages WHERE excluded=0 AND direction='inbound'
                ) WHERE position=1
            """).fetchall()
            latest_ids = {row["id"] for row in latest}
            pending = db.execute("SELECT id,source_id FROM proposals WHERE state='대기'").fetchall()
            superseded = [row["id"] for row in pending if row["source_id"] not in latest_ids]
            db.executemany("UPDATE proposals SET state='이전 메일' WHERE id=?", ((identifier,) for identifier in superseded))
            return len(superseded)

    def archive_generic_proposals(self):
        archived = 0
        with self.connect() as db:
            for row in db.execute("SELECT id,payload FROM proposals WHERE state='대기'").fetchall():
                try:
                    payload = json.loads(row["payload"])
                except (TypeError, json.JSONDecodeError):
                    payload = {}
                if generic_followup(payload.get("evidence_quote", "")):
                    db.execute("UPDATE proposals SET state='불필요' WHERE id=?", (row["id"],))
                    archived += 1
        return archived


class Engine:
    def __init__(self, config):
        self.config = config
        self.trace = []

    def role(self, name, payload):
        paths = [ROOT / "main_prompt", ROOT / "prompts/common.md", ROOT / "prompts" / (name + ".md")]
        text = "\n\n".join(p.read_text(encoding="utf-8") for p in paths)
        self.trace.append({"role": name, "prompt_sha256": hashlib.sha256(text.encode()).hexdigest(), "engine": self.config["engine"]})
        if self.config["engine"] != "ollama":
            return None
        if len(dump(payload).encode()) > 120000:
            raise ValueError("모델 입력 한도 120KB를 초과했습니다. 수집 범위나 검색어를 줄이세요.")
        model = self.config.get("ollama_model")
        if not model:
            raise ValueError("ollama_model을 설정하세요.")
        req = Request("http://127.0.0.1:11434/api/chat", data=dump({
            "model": model, "stream": False, "format": "json", "options": {"temperature": 0},
            "messages": [{"role": "system", "content": text + "\n현재 지정된 역할만 수행하고 JSON 객체만 반환한다."},
                         {"role": "user", "content": dump(payload)}],
        }).encode(), headers={"Content-Type": "application/json"}, method="POST")
        # 시스템 프록시로 회사 메일이 전달되지 않도록 로컬 모델에 직접 연결합니다.
        with build_opener(ProxyHandler({})).open(req, timeout=180) as response:
            result = json.load(response)
        output = json.loads(result["message"]["content"])
        if not isinstance(output, dict):
            raise ValueError("모델은 JSON 객체를 반환해야 합니다. 규칙 모드로 자동 전환하지 않습니다.")
        return output


def normalize_message(message, cfg):
    required = {"id", "subject", "sender", "to", "sent_at", "body"}
    if not required <= message.keys():
        raise ValueError("메일 필수 필드가 없습니다.")
    if not all(isinstance(message[k], str) for k in required):
        raise ValueError("메일 필드는 문자열이어야 합니다.")
    mine = addresses(cfg["user_email"]) | addresses(cfg.get("aliases", []))
    direction = message.get("direction") or ("outbound" if addresses(message["sender"]) & mine else "inbound")
    if direction not in {"inbound", "outbound"}:
        raise ValueError("메일 방향 정보가 올바르지 않습니다.")
    recipients = addresses(message["to"]) | addresses(message.get("cc", ""))
    if direction == "inbound" and not mine & recipients:
        raise ValueError("본인이 수신자/참조자인 메일만 수집합니다.")
    if direction == "outbound" and not mine & addresses(message["sender"]):
        raise ValueError("본인이 보낸 회신만 수집합니다.")
    sent = datetime.fromisoformat(message["sent_at"])
    if sent.tzinfo is None:
        raise ValueError("메일 날짜에 시간대가 필요합니다.")
    if len(message["body"]) > 100000 or len(message["subject"]) > 1000:
        raise ValueError("메일 크기 제한을 초과했습니다.")
    if not message["id"].strip():
        raise ValueError("메일 식별자가 비어 있습니다.")
    received = message.get("received_at")
    if received:
        parsed = datetime.fromisoformat(received)
        if parsed.tzinfo is None:
            raise ValueError("수신 시각에 시간대가 필요합니다.")
        received = parsed.astimezone(KST).isoformat()
    return {**message, "direction": direction, "received_at": received, "sent_at": sent.astimezone(KST).isoformat(), "thread_id": message.get("thread_id") or message["id"]}


def rule_analysis(message, cfg):
    # 학습용 결정 규칙입니다. 자유 형식 메일에 대한 AI 분석을 가장하지 않습니다.
    from .topics import new_body
    body = new_body(message["body"])
    candidates = []
    project_match = re.search(r"\[([^\]]+)\]", message["subject"])
    project = project_match.group(1) if project_match else "미분류"
    for line in body.splitlines():
        line = line.strip()
        if not line or not re.search(r"해\s*주세요|바랍니다|부탁|요청|변경|수정|추가|삭제|취소|완료했습니다|완료되었습니다", line):
            continue
        if generic_followup(line):
            continue
        dates = re.findall(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)", line)
        action = "CANCEL" if "취소" in line else "COMPLETE" if re.search(r"완료했습니다|완료되었습니다", line) else "UPDATE" if "변경" in line else "CREATE"
        title_match = re.search(r"업무\s*:\s*([^;\n]+)", line)
        title = title_match.group(1).strip() if title_match else message["subject"]
        owners = {addr.lower() for addr in re.findall(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", line)}
        mine = addresses(cfg["user_email"]) | addresses(cfg.get("aliases", []))
        # To에 있어도 특정 담당자가 명시되지 않으면 검토 대상으로 남깁니다.
        assignee = next(iter(owners & mine), None)
        other = bool(owners and not owners & mine)
        if other:
            continue
        candidates.append({"title": title, "project": project, "assignee": assignee,
                           "due_date": dates[-1] if dates else None, "action": action,
                           "evidence_quote": line, "needs_confirmation": True})
    # 규칙 모드는 의미 요약 모델이 없으므로 메일 전체 요청을 하나의 검토 후보로 묶습니다.
    unique = list({(item["title"], item["action"], item["evidence_quote"]): item for item in candidates}.values())
    summary = " / ".join(dict.fromkeys(item["evidence_quote"] for item in unique))[:1200]
    if not unique:
        return {"summary": body[:500] or message["subject"], "tasks": []}
    actions = {}
    for item in unique:
        actions[item["action"]] = actions.get(item["action"], 0) + 1
    representative = dict(max(unique, key=lambda item: actions[item["action"]]))
    from .topics import task_title
    representative["title"] = task_title(message["subject"], " ".join(item["title"] for item in unique))
    representative["summary"] = summary
    representative["merged_candidate_count"] = len(unique)
    return {"summary": summary, "tasks": [representative], "omitted_candidate_count": 0}


def validate_analysis(analysis, message):
    if not isinstance(analysis, dict) or not isinstance(analysis.get("summary"), str) or not isinstance(analysis.get("tasks"), list):
        raise ValueError("분석 출력 스키마가 올바르지 않습니다.")
    if len(analysis["tasks"]) > 2:
        raise ValueError("메일 한 건당 업무 후보는 최대 2개입니다.")
    for item in analysis["tasks"]:
        if not isinstance(item, dict) or not isinstance(item.get("title"), str) or not item["title"].strip():
            raise ValueError("업무명이 필요합니다.")
        evidence = item.get("evidence_quote")
        if not isinstance(evidence, str) or not evidence.strip() or evidence not in message["body"]:
            raise ValueError("원문에 없는 근거 문구입니다.")
        if item.get("action") not in {"CREATE", "UPDATE", "CANCEL", "COMPLETE"}:
            raise ValueError("알 수 없는 업무 변경 유형입니다.")
        if item.get("due_date"):
            date.fromisoformat(item["due_date"])
        for key in ("project", "assignee"):
            if item.get(key) is not None and not isinstance(item[key], str):
                raise ValueError("분석 필드 형식이 올바르지 않습니다.")


class Pipeline:
    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store
        self.store.bind_owner(cfg["user_email"])
        self.store.apply_message_policy(cfg)
        self.store.backfill_auto_tasks(cfg)
        self.store.archive_generic_proposals()
        self.store.compact_pending_proposals()
        self.store.archive_superseded_proposals()

    def ingest(self, messages):
        engine = Engine(self.cfg)
        run_id = uuid.uuid4().hex
        counts = {"imported": 0, "duplicates": 0, "excluded": 0, "failed": 0, "errors": []}
        with self.store.connect() as db:
            db.execute("INSERT INTO runs VALUES(?,?,NULL,?,?,NULL)", (run_id, now(), "실행중", "[]"))
        try:
            for raw in messages:
                try:
                    from .policy import sender_excluded, noise_mail, task_recipient
                    if isinstance(raw, dict) and (sender_excluded(raw.get("sender"), self.cfg) or noise_mail(raw.get("subject"), raw.get("body"), raw.get("sender"))):
                        counts["excluded"] += 1
                        continue
                    message = normalize_message(raw, self.cfg)
                    existing = self.store.rows("SELECT body,received_at FROM messages WHERE id=?", (message["id"],))
                    recovering_body = False
                    if existing:
                        if existing[0]["body"] != message["body"]:
                            if existing[0]["body"].strip() or not message["body"].strip():
                                raise ValueError("A non-empty saved message body cannot be overwritten by a different body.")
                            recovering_body = True
                        if message.get("received_at") and not existing[0]["received_at"]:
                            with self.store.connect() as db:
                                db.execute("UPDATE messages SET received_at=? WHERE id=?", (message["received_at"], message["id"]))
                        if not recovering_body:
                            counts["duplicates"] += 1
                            continue
                    if message["direction"] == "outbound":
                        analysis = {"summary": "내가 보낸 회신입니다. 해결 내용은 RAG 업무 히스토리 근거로 저장합니다.", "tasks": []}
                    else:
                        analysis = engine.role("01_analyze", {"user_email": self.cfg["user_email"], "message": message})
                    if analysis is None:
                        analysis = rule_analysis(message, self.cfg)
                    validate_analysis(analysis, message)
                    verification = (engine.role("03_verify", {"message": message, "analysis": analysis})
                                    if message["direction"] == "inbound" else None)
                    if verification is not None and verification.get("verdict") != "PASS":
                        raise ValueError("AI 검증에서 확인 필요 판정입니다. 원문을 검토 후 다시 수집하세요.")
                    related = self.store.rows("SELECT DISTINCT t.* FROM tasks t JOIN messages m ON t.source_id=m.id WHERE m.thread_id=? AND t.deleted_at IS NULL", (message["thread_id"],))
                    # 연결 역할의 결과도 제안입니다. 대상 ID는 코드에서 범위를 검증합니다.
                    links = (engine.role("02_reconcile", {"analysis": analysis, "existing_tasks": related})
                             if message["direction"] == "inbound" else None)
                    link_list = links.get("links", []) if isinstance(links, dict) else []
                    with self.store.connect() as db:
                        if recovering_body:
                            db.execute("UPDATE messages SET thread_id=?,subject=?,sender=?,recipients=?,sent_at=?,body=?,summary=?,source=?,analysis=?,received_at=COALESCE(received_at,?),direction=? WHERE id=?", (
                                message["thread_id"], message["subject"], message["sender"],
                                dump({"to": message["to"], "cc": message.get("cc", "")}), message["sent_at"],
                                message["body"], analysis["summary"], message.get("source", "import"), dump(analysis), message.get("received_at"), message["direction"], message["id"]))
                        else:
                            db.execute("INSERT INTO messages(id,thread_id,subject,sender,recipients,sent_at,body,summary,source,imported_at,analysis,received_at,direction) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                                message["id"], message["thread_id"], message["subject"], message["sender"],
                                dump({"to": message["to"], "cc": message.get("cc", "")}), message["sent_at"],
                                message["body"], analysis["summary"], message.get("source", "import"), now(), dump(analysis), message.get("received_at"), message["direction"]))
                        if message["direction"] == "inbound":
                            db.execute("""UPDATE proposals SET state='이전 메일' WHERE state='대기' AND source_id IN
                                (SELECT id FROM messages WHERE thread_id=? AND id<>?)""",
                                       (message["thread_id"], message["id"]))
                        for index, item in enumerate(analysis["tasks"] if message["direction"] == "inbound" else []):
                            matches = [t for t in related if t["title"] == item["title"]]
                            target = matches[0]["id"] if len(matches) == 1 else None
                            if not target and item["action"] != "CREATE" and len(related) == 1:
                                target = related[0]["id"]
                            if index < len(link_list) and isinstance(link_list[index], dict):
                                suggested = link_list[index].get("target_task_id")
                                if suggested in {t["id"] for t in related}:
                                    target = suggested
                            action = item["action"]
                            if target and action == "CREATE":
                                action = "UPDATE"
                            reason = "사용자 승인 후 반영합니다."
                            if action != "CREATE" and not target:
                                reason = "연결할 기존 업무를 선택하세요."
                            db.execute("INSERT INTO proposals(id,source_id,action,target_id,payload,reason) VALUES(?,?,?,?,?,?)",
                                       (uuid.uuid4().hex[:12], message["id"], action, target,
                                        dump({**item, "summary": item.get("summary") or analysis["summary"]}), reason))
                        if message["direction"] == "inbound":
                            self.store.refresh_auto_thread(db, message["thread_id"], self.cfg,
                                                           mark_unread=task_recipient({**message, "cc": message.get("cc", ""),
                                                                                      "analysis": analysis}, self.cfg))
                    counts["imported"] += 1
                except (ValueError, TypeError, KeyError) as exc:
                    counts["failed"] += 1
                    counts["errors"].append({"position": counts["imported"] + counts["duplicates"] + counts["excluded"] + counts["failed"], "reason": str(exc)})
            self.store.archive_generic_proposals()
            self.wiki(engine)
            status = "일부실패" if counts["failed"] else "완료"
        except Exception:
            with self.store.connect() as db:
                db.execute("UPDATE runs SET ended_at=?,status=?,stages=?,error=? WHERE id=?", (now(), "실패", dump(engine.trace), "처리 중 예외. 설정과 로컬 모델 상태를 확인하세요.", run_id))
            raise
        with self.store.connect() as db:
            db.execute("UPDATE runs SET ended_at=?,status=?,stages=?,error=? WHERE id=?", (now(), status, dump(engine.trace), dump(counts["errors"]), run_id))
        return {**counts, "run_id": run_id, "engine": self.cfg["engine"]}

    @audited("07_history")
    def history(self, query="", start=None, end=None, topic_id=None):
        from .rag import HistoryRAG
        return HistoryRAG(self.cfg, self.store).search(query, start, end, topic_id)

    def wiki(self, engine=None):
        engine = engine or Engine(self.cfg)
        tasks = self.store.rows("SELECT * FROM tasks WHERE deleted_at IS NULL ORDER BY project,title")
        proposals = self.store.rows("SELECT p.source_id,p.action,p.reason FROM proposals p JOIN messages m ON p.source_id=m.id WHERE p.state='대기' AND m.excluded=0")
        commentary = engine.role("04_wiki", {"tasks": tasks, "pending": proposals})
        lines = ["# 프로젝트 업무 위키", "", "갱신 시각: " + now(), "", "현재 상태의 원본은 SQLite 업무 테이블입니다.", ""]
        for task in tasks:
            lines.append(f"- [{task['project']}] {task['title']} / {task['status']} / 기한 {task['due_date'] or '미정'} / 업무 {task['id']} / 메일 {task['source_id']}")
        lines.extend(["", "## 확인 대기", "", f"승인 대기 제안: {len(proposals)}건"])
        if commentary:
            lines.extend(["", "## AI 참고 해설 — 원문 검증 필요", "", dump(commentary)])
        folder = self.store.folder / "wiki"
        folder.mkdir(exist_ok=True)
        (folder / "projects.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    @audited("05_search")
    def search(self, query):
        if not isinstance(query, str) or not query.strip() or len(query) > 300:
            raise ValueError("검색어를 1~300자로 입력하세요.")
        terms = re.findall(r"[\w@.-]+", query)[:8]
        results, seen = [], set()
        # 첫 검색에서 찾은 스레드의 관련 메일까지 최대 2단계로 확장합니다.
        for term in terms:
            escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = "%" + escaped + "%"
            rows = self.store.rows("SELECT DISTINCT m.* FROM messages m LEFT JOIN tasks t ON m.id=t.source_id WHERE m.excluded=0 AND (m.subject LIKE ? ESCAPE '\\' OR m.body LIKE ? ESCAPE '\\' OR t.title LIKE ? ESCAPE '\\') LIMIT 30", (pattern, pattern, pattern))
            for row in rows:
                if row["id"] not in seen:
                    results.append(row)
                    seen.add(row["id"])
        for thread in list(dict.fromkeys(r["thread_id"] for r in results))[:10]:
            for row in self.store.rows("SELECT * FROM messages WHERE thread_id=? AND excluded=0 ORDER BY sent_at LIMIT 30", (thread,)):
                if row["id"] not in seen:
                    results.append(row)
                    seen.add(row["id"])
        results = sorted(results, key=lambda x: x["sent_at"])[-60:]
        task_ids = [t["id"] for t in self.store.rows("SELECT * FROM tasks") if t["source_id"] in seen]
        events = [e for e in self.store.rows("SELECT * FROM events ORDER BY seq") if e["source_id"] in seen or e["task_id"] in task_ids]
        answer = Engine(self.cfg).role("05_search", {"question": query, "messages": results, "events": events}) if results else None
        return {"messages": results, "events": events, "answer": answer, "mode": "키워드+스레드 2단계 검색", "notice": "검색 결과 원문을 확인하세요." if results else "현재 수집된 메일에서 찾지 못했습니다."}

    @audited("06_report")
    def report(self, start, end):
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        if first > last or (last - first).days > 366:
            raise ValueError("보고 기간은 시작일 이후 최대 366일로 설정하세요.")
        cutoff = now()
        if last > datetime.now(KST).date():
            raise ValueError("보고 종료일은 오늘 이후일 수 없습니다.")
        # 이벤트를 재생해 보고 종료 시점의 상태를 복원합니다.
        events = self.store.rows("SELECT * FROM events WHERE substr(at,1,10)<=? ORDER BY seq", (end,))
        snapshot = {}
        changes = []
        for event in events:
            previous = snapshot.get(event["task_id"], {})
            after = json.loads(event["after_json"]) or {}
            snapshot[event["task_id"]] = canonical_task({**previous, **after})
            if event["at"][:10] >= start:
                changes.append(event)
        snapshot = {key: task for key, task in snapshot.items() if not task.get("deleted_at")}
        completed_ids = set()
        for event in changes:
            if event["task_id"] not in snapshot:
                continue
            before = json.loads(event["before_json"]) or {}
            after = canonical_task({**snapshot[event["task_id"]], **(json.loads(event["after_json"]) or {})})
            if before.get("status") != "완료" and after["status"] == "완료" and snapshot[event["task_id"]]["status"] == "완료":
                completed_ids.add(event["task_id"])
        completed = [t for k, t in snapshot.items() if k in completed_ids]
        active = [t for t in snapshot.values() if t["status"] not in {"완료", "취소"}]
        overdue = [t for t in active if t["due_date"] and t["due_date"] < end]
        def listing(items):
            return [f"- {t['title']} — {t['status']}, 기한 {t['due_date'] or '미정'} [업무:{t['id']}] [메일:{t['source_id']}]" for t in items] or ["- 해당 없음"]
        lines = [f"# 주간 업무 보고 ({start} ~ {end})", "", "작성 시각: " + cutoff,
                 "", "집계 기준: 앱에 기록된 이벤트 시각(KST), 종료일 시점 상태 복원. 메일 발송일과 다를 수 있습니다.",
                 "", f"완료 {len(completed)}건 · 진행/대기 {len(active)}건 · 기한 경과 {len(overdue)}건", "", "## 완료 업무", *listing(completed),
                 "", "## 진행 중 및 이월 업무", *listing(active), "", "## 지연·위험", *listing(overdue),
                 "", "## 다음 주 계획", "- 미완료 업무의 기한과 우선순위를 검토해 확정하세요.", "", "## 기간 내 변경 이력"]
        lines += [f"- {e['at']}: {e['kind']} / 업무 {e['task_id']} / 근거 {e['source_id'] or '사용자 직접 확인'}" for e in changes] or ["- 해당 없음"]
        engine = Engine(self.cfg)
        narrative = engine.role("06_report", {"period": [start, end], "completed": completed, "active": active, "overdue": overdue, "changes": changes})
        if narrative:
            lines += ["", "## AI 보고 해설 — 확정 전 검토", dump(narrative)]
        content = "\n".join(lines) + "\n"
        report_id = uuid.uuid4().hex[:12]
        from .reporting import build_document, markdown, write_pdf
        document = build_document(self.store, self.cfg, start, end, snapshot, changes, len(completed))
        content = markdown(document) + "\n" + content
        folder = self.store.folder / "reports"
        folder.mkdir(exist_ok=True)
        write_pdf(document, folder / (report_id + ".pdf"))
        with self.store.connect() as db:
            db.execute("INSERT INTO reports(id,start_date,end_date,cutoff,content,document) VALUES(?,?,?,?,?,?)", (report_id, start, end, cutoff, content, dump(document)))
        (folder / f"{start}_{end}_{report_id}.md").write_text(content, encoding="utf-8")
        return {"id": report_id, "content": content, "document": document, "completed_count": len(completed), "active_count": len(active)}
