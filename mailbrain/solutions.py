"""원문과 업무 상태를 변경하지 않는, 근거가 연결된 해결 초안."""
import hashlib
import json
import re

from .core import Engine, ROOT, audited, dump, now
from .topics import Topics, new_body


UNSAFE_MAIL_TEXT = re.compile(r"(?i)password|credential|ignore.{0,24}(instruction|command)|run.{0,16}command|비밀번호|명령.{0,16}실행|지침.{0,16}무시")


def safe_quote(value):
    if UNSAFE_MAIL_TEXT.search(value or ""):
        return "[민감 정보 또는 지시형 문구 생략]"
    return value


class Solutions:
    LIMIT = 12

    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS solutions(
                id TEXT PRIMARY KEY, target_kind TEXT, target_id TEXT,
                fingerprint TEXT, created_at TEXT, document TEXT,
                UNIQUE(target_kind,target_id,fingerprint))""")

    def context(self, topic_id=None, task_id=None):
        if bool(topic_id) == bool(task_id):
            raise ValueError("토픽 또는 업무 하나를 선택하세요.")
        task = None
        topics = Topics(self.store)
        if topic_id:
            group = topics.get(topic_id)
            if not group:
                raise ValueError("토픽이 없거나 수집 제외 대상입니다.")
            ids = {m["id"] for m in group["messages"]}
            title, kind, target = group["title"], "topic", topic_id
            source_id = None
        else:
            tasks = self.store.rows("SELECT * FROM tasks WHERE id=? AND deleted_at IS NULL", (task_id,))
            if not tasks:
                raise ValueError("현재 업무 목록에서 업무를 선택하세요.")
            task = tasks[0]
            source_id = task["source_id"]
            ids = {source_id} | {e["source_id"] for e in self.store.rows(
                "SELECT source_id FROM events WHERE task_id=? AND source_id IS NOT NULL", (task_id,))}
            # 같은 제목의 무관한 업무를 검색하지 않고 실제 출처의 대화만 확장합니다.
            linked = [g for g in topics.build() if any(m["id"] in ids for m in g["messages"])]
            ids |= {m["id"] for g in linked for m in g["messages"]}
            title, kind, target = task["title"], "task", task_id
        visible = [m for m in self.store.rows(
            "SELECT * FROM messages WHERE excluded=0 ORDER BY COALESCE(received_at,sent_at) DESC,id DESC") if m["id"] in ids]
        if not visible:
            raise ValueError("분석할 수 있는 메일 근거가 없습니다. 제외된 메일은 솔루션에 사용하지 않습니다.")
        selected = visible[:self.LIMIT]
        origin = next((m for m in visible if m["id"] == source_id), None)
        if origin and origin not in selected:
            selected[-1] = origin
        evidence = []
        action_items = {}
        for m in selected:
            body = new_body(m["body"]) or m["body"]
            evidence.append({"id": m["id"], "subject": m["subject"], "sender": m["sender"],
                             "at": m["received_at"] or m["sent_at"], "body": body[:2200],
                             "truncated": len(body) > 2200})
            try:
                analysis = json.loads(m.get("analysis") or "{}")
            except (TypeError, json.JSONDecodeError):
                analysis = {}
            for item in analysis.get("tasks", []):
                if not isinstance(item, dict):
                    continue
                item_title, action, quote = item.get("title"), item.get("action"), item.get("evidence_quote")
                if not all(isinstance(value, str) and value.strip() for value in (item_title, action, quote)):
                    continue
                if UNSAFE_MAIL_TEXT.search(quote):
                    continue
                if quote not in body:
                    continue
                key = (action, re.sub(r"\s+", " ", item_title).strip().casefold())
                action_items[key] = {"title": item_title.strip(), "action": action, "quote": quote.strip(),
                                     "evidence_id": m["id"]}
        return {"title": title, "target_kind": kind, "target_id": target, "task": task,
                "evidence": evidence, "action_items": list(action_items.values()),
                "task_summary": task.get("summary", "") if task else "", "total_messages": len(visible),
                "scope": f"관련 메일 {len(visible)}건 중 최근 최대 {self.LIMIT}건과 최초 업무 근거를 포함해 {len(evidence)}건 검토 · 메일별 새 본문 최대 2,200자"}

    @staticmethod
    def rules(context):
        evidence = context["evidence"]
        text = "\n".join(m["subject"] + "\n" + m["body"] for m in evidence).lower()
        items = context.get("action_items") or []
        if "리스" in text and any(word in text for word in ("스케줄", "스케쥴")):
            item = next((candidate for candidate in items if any(word in candidate["quote"] for word in ("스케줄", "스케쥴", "계약 변경"))), None)
            message_id = item["evidence_id"] if item else evidence[0]["id"]
            quote = safe_quote(item["quote"][:240]) if item else safe_quote(evidence[0]["body"][:240])
            return {"summary": "메일 스레드를 ‘리스 스케줄 조정’ 업무로 분류했습니다. 요청된 계약 조건과 대상 레코드를 확인한 뒤 변경안을 검토하세요.",
                    "steps": [{"text": f"SE16에서 대상 리스 스케줄 데이터를 조회하고, 계약 변경 내용과 적용 기간·금액을 대조해 요청된 값으로 조정하는 방안을 검토합니다. 실제 반영 전 승인된 SAP 변경 절차와 검증·복구 방법을 확인하세요. 근거: “{quote}”",
                               "evidence_ids": [message_id]}],
                    "questions": ["요청한 계약 변경의 적용 기준일과 변경 대상 레코드가 확정됐나요?", "SAP 변경 권한과 승인 절차, 변경 후 검증 기준을 확인했나요?"],
                    "cautions": ["SE16 조회 결과와 계약 근거를 대조한 뒤 승인된 변경 절차로 반영하세요.",
                                 "메일만으로는 실제 변경·승인 여부를 확인할 수 없습니다."]}
        problem_terms = ("오류", "장애", "실패", "에러", "불가", "문제", "원인", "중단", "error", "incident", "failed")
        issue_items = [item for item in items if any(term in item["quote"].lower() for term in problem_terms)]
        is_problem = bool(issue_items) or any(term in text for term in problem_terms)
        steps = []
        if items:
            selected = (issue_items or items)[:5]
            for item in selected:
                title, quote, action = item["title"][:120], safe_quote(item["quote"][:240]), item["action"]
                if is_problem:
                    instruction = (f"‘{title}’ 문제를 메일 근거 문구대로 재현하고, 발생 시각·대상 사용자·화면/처리 건·오류 원문을 기록합니다. 근거: “{quote}”")
                elif action == "CANCEL":
                    instruction = f"‘{title}’ 취소 요청의 대상과 영향 범위를 요청자에게 확인한 뒤, 확인된 범위만 취소하고 관련 업무와 일정을 정리합니다. 근거: “{quote}”"
                elif action == "COMPLETE":
                    instruction = f"‘{title}’의 완료 산출물과 검증 결과를 확인하고, 근거가 확인된 뒤에만 완료로 처리합니다. 근거: “{quote}”"
                elif action == "UPDATE":
                    instruction = f"‘{title}’의 현재 값과 요청된 변경 값을 비교하고, 적용 대상·영향 범위·완료 기준을 확인한 뒤 변경합니다. 근거: “{quote}”"
                else:
                    instruction = f"메일에서 요청한 ‘{title}’을 실행 항목으로 나누고 담당자·완료 기준·필요 자료를 확인해 진행합니다. 근거: “{quote}”"
                steps.append({"text": instruction, "evidence_ids": [item["evidence_id"]]})
            labels = {"CREATE": "신규 요청", "UPDATE": "변경 요청", "CANCEL": "취소 요청", "COMPLETE": "완료 확인"}
            request_names = list(dict.fromkeys(f"{labels.get(item['action'], '요청')} {item['title'][:55]}" for item in selected[:3]))
            summary = (f"{context['title']} 메일에서 추출한 요청을 근거 문구와 연결했습니다: " + "; ".join(request_names)
                       + (" 문제 재현과 원인 확인을 먼저 진행하세요." if is_problem else "."))[:560]
        else:
            request_lines = []
            request_markers = ("해주세요", "해 주세", "바랍니다", "부탁", "요청드립니다", "진행해", "확인해", "please", "kindly")
            for mail in evidence:
                for line in mail["body"].splitlines():
                    value = line.strip()
                    if UNSAFE_MAIL_TEXT.search(value):
                        continue
                    if len(value) > 12 and any(marker in value.lower() for marker in request_markers):
                        request_lines.append((value[:360], mail["id"]))
            if request_lines:
                for quote, message_id in request_lines[:3]:
                    steps.append({"text": f"메일의 요청 문구를 기준으로 실제 대상·완료 결과·기한을 확인하고 진행 여부를 기록합니다. 근거: “{quote}”",
                                  "evidence_ids": [message_id]})
                summary = "업무 후보가 명시적으로 추출되지는 않아, 메일 본문에서 요청 문장을 찾아 실행 확인 항목으로 정리했습니다."
            else:
                mail = evidence[0]
                quote = safe_quote(mail["body"].strip()[:240]) or mail["subject"][:120]
                subject = mail["subject"][:120]
                steps = [{"text": f"메일 ‘{subject}’의 내용을 요청자와 확인해 원하는 결과·담당자·기한을 확정한 뒤 업무로 등록합니다. 근거: “{quote}”",
                          "evidence_ids": [mail["id"]]}]
                summary = f"메일 ‘{subject}’에서 확정 가능한 실행 요청을 찾지 못했습니다. 원문 내용을 확인해 업무 범위를 먼저 합의하세요."
            is_problem = is_problem or any(term in text for term in problem_terms)
            if is_problem and len(steps) < 5:
                refs = [mail["id"] for mail in evidence[:3]]
                steps.extend([
                    {"text": "문제가 발생한 시간·사용자·업무 단계와 재현 절차를 적고, 정확한 오류 문구와 관련 로그를 확보합니다.", "evidence_ids": refs},
                    {"text": "같은 스레드에서 정상 동작한 조건과 이후 변경사항을 비교해 원인 후보를 좁힙니다. 설정·권한·기준정보·연계 중 무엇이 달라졌는지 확인합니다.", "evidence_ids": refs},
                    {"text": "수정안은 테스트 환경에서 재현 건으로 검증하고, 영향 범위와 원복 방법을 확인한 뒤 요청자에게 결과를 회신합니다.", "evidence_ids": refs},
                ][:5-len(steps)])
        questions = (["같은 오류를 재현할 수 있는 계정·처리 건·발생 시각은 무엇인가요?", "최근 변경사항과 오류 로그를 확인했나요?"]
                     if is_problem else ["메일에서 추출한 요청의 담당자·기한·완료 기준이 현재도 유효한가요?", "스레드의 최신 회신이 앞선 요청을 변경하거나 취소했나요?"])
        return {"summary": summary, "steps": steps[:5], "questions": questions,
                "cautions": ["메일의 요청 문구와 저장된 업무 후보를 바탕으로 만든 제안입니다. 적용 전 최신 회신과 실제 상태를 확인하세요.",
                             "해결 여부·승인 여부·시스템 변경 결과를 확인된 사실처럼 가정하지 않았습니다."]}

    @staticmethod
    def validate(result, evidence):
        def text(value, limit):
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise ValueError("솔루션 문장 형식이 올바르지 않습니다.")
            return value.strip()
        known = {m["id"] for m in evidence}
        if not isinstance(result, dict):
            raise ValueError("솔루션은 객체여야 합니다.")
        clean = {"summary": text(result.get("summary"), 600), "steps": []}
        steps = result.get("steps")
        if not isinstance(steps, list) or not 1 <= len(steps) <= 5:
            raise ValueError("제안은 1~5개여야 합니다.")
        for step in steps:
            if not isinstance(step, dict):
                raise ValueError("제안 형식이 올바르지 않습니다.")
            refs = step.get("evidence_ids")
            if not isinstance(refs, list) or not refs or not all(isinstance(r, str) and r in known for r in refs):
                raise ValueError("존재하지 않는 메일을 근거로 사용했습니다.")
            clean["steps"].append({"text": text(step.get("text"), 800), "evidence_ids": list(dict.fromkeys(refs))})
        for key in ("questions", "cautions"):
            values = result.get(key)
            if not isinstance(values, list) or len(values) > 4:
                raise ValueError("확인 항목 형식이 올바르지 않습니다.")
            clean[key] = [text(v, 500) for v in values]
        return clean

    @audited("07_solution")
    def generate(self, topic_id=None, task_id=None, regenerate=False):
        if not isinstance(regenerate, bool):
            raise ValueError("재검토 여부는 참/거짓이어야 합니다.")
        context = self.context(topic_id, task_id)
        prompt = "\n".join((ROOT / p).read_text(encoding="utf-8") for p in
                           ("main_prompt", "prompts/common.md", "prompts/07_solution.md"))
        fingerprint = hashlib.sha256(dump({"context": context, "engine": self.cfg["engine"],
            "model": self.cfg.get("ollama_model"), "prompt": prompt, "version": 2}).encode()).hexdigest()
        params = (context["target_kind"], context["target_id"], fingerprint)
        existing = self.store.rows("SELECT * FROM solutions WHERE target_kind=? AND target_id=? AND fingerprint=?", params)
        if existing and not regenerate:
            return {**json.loads(existing[0]["document"]), "cached": True}
        mode, fallback = "메일 기반 검토 가이드", False
        result = self.rules(context)
        if self.cfg["engine"] == "ollama":
            try:
                result = self.validate(Engine(self.cfg).role("07_solution", context), context["evidence"])
                mode = "로컬 AI 해결 초안"
            except Exception:
                result, fallback = self.rules(context), True
        # 규칙 결과에도 동일한 검증을 적용합니다. 사실은 원문 인용만 제공합니다.
        result = self.validate(result, context["evidence"])
        result.update({"title": context["title"], "mode": mode, "fallback": fallback,
                       "created_at": now(), "scope": context["scope"], "cached": False,
                       "notice": "검토용 제안입니다. 업무 상태 변경·메일 발송·시스템 작업은 실행하지 않습니다.",
                       "evidence": [{k: m[k] for k in ("id", "subject", "sender", "at", "truncated")} |
                                    {"quote": m["body"][:320]} for m in context["evidence"]]})
        ident = hashlib.sha256(dump(params).encode()).hexdigest()[:24]
        with self.store.connect() as db:
            db.execute("INSERT INTO solutions VALUES(?,?,?,?,?,?) ON CONFLICT(target_kind,target_id,fingerprint) DO UPDATE SET created_at=excluded.created_at,document=excluded.document",
                       (ident, *params, result["created_at"], dump(result)))
        return result
