"""원문과 업무 상태를 변경하지 않는, 근거가 연결된 해결 초안."""
import hashlib
import json

from .core import Engine, ROOT, audited, dump, now
from .topics import Topics, new_body


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
        for m in selected:
            body = new_body(m["body"]) or m["body"]
            evidence.append({"id": m["id"], "subject": m["subject"], "sender": m["sender"],
                             "at": m["received_at"] or m["sent_at"], "body": body[:2200],
                             "truncated": len(body) > 2200})
        return {"title": title, "target_kind": kind, "target_id": target, "task": task,
                "evidence": evidence, "total_messages": len(visible),
                "scope": f"관련 메일 {len(visible)}건 중 최근 최대 {self.LIMIT}건과 최초 업무 근거를 포함해 {len(evidence)}건 검토 · 메일별 새 본문 최대 2,200자"}

    @staticmethod
    def rules(context):
        evidence = context["evidence"]
        text = "\n".join(m["subject"] + "\n" + m["body"] for m in evidence).lower()
        # 조건별 권고는 실제 확인된 원인이나 실행 결과가 아니라 검토 절차입니다.
        scenarios = [
            (("카피", "복제", "백업", "복구"), "서버 복제·복구 범위와 작업 창부터 합의하는 방안을 권합니다.",
             ["복제 원본·대상, 작업 범위와 서비스 중단 허용 시간을 담당자에게 확인하세요.",
              "개인정보·접속정보 마스킹, 백업 및 되돌리기 절차를 검토한 뒤 테스트 계획을 작성하세요.",
              "승인된 점검 환경에서 연결·데이터 정합성을 검증하고 결과를 공유하는 방안을 제안합니다."]),
            (("오류", "장애", "실패", "에러", "접속 불가"), "영향 범위와 재현 근거를 먼저 확보한 뒤 원인을 좁히는 방안을 권합니다.",
             ["발생 시각·영향 사용자·환경·재현 조건을 확인하고 관련 로그를 확보하세요.",
              "최근 변경사항과 정상 동작 조건을 비교해 원인 가설을 분리하세요.",
              "복구 방안과 되돌리기 절차를 검토하고 담당자 승인 후 검증하는 방안을 제안합니다."]),
            (("기한", "일정", "지연", "까지"), "최신 일정과 선행 작업을 맞춘 뒤 진행 순서를 정리하는 방안을 권합니다.",
             ["최신 메일의 일정이 이전 합의를 대체하는지 요청자에게 확인하세요.",
              "지연 원인과 필요한 입력 자료를 확인하고 선행 작업별 담당·완료 기준을 합의하세요.",
              "확인된 일정과 산출물을 회신 초안으로 정리한 뒤 업무 기한에 반영할지 검토하세요."]),
            (("자료", "검토", "회의", "보고"), "요청 산출물과 검토 기준을 먼저 맞추는 방안을 권합니다.",
             ["필요한 자료의 범위·양식·최신 버전과 수신 대상을 확인하세요.",
              "메일의 요청사항을 검토 항목으로 나누고 누락된 자료를 목록으로 정리하세요.",
              "검토 결과와 남은 질문을 구분해 회의 안건 또는 회신 초안으로 작성하세요."]),
        ]
        chosen = next((row for row in scenarios if any(w in text for w in row[0])), None)
        summary, steps = chosen[1:] if chosen else (
            "요청 범위와 완료 기준을 확인한 뒤 실행 항목으로 나누는 방안을 권합니다.",
            ["최신 요청과 앞선 대화의 변경사항을 비교해 현재 유효한 요청을 확인하세요.",
             "담당·기한·산출물과 선행 작업을 요청자에게 확인하세요.",
             "확인된 내용을 실행 순서로 정리하고 결과 검증 기준을 합의하세요."])
        # 선택된 절차를 유발한 메일을 근거로 연결합니다.
        related = [m["id"] for m in evidence if chosen and any(w in (m["subject"]+m["body"]).lower() for w in chosen[0])][:3]
        refs = related or [evidence[0]["id"]]
        return {"summary": summary, "steps": [{"text": s, "evidence_ids": refs} for s in steps],
                "questions": ["현재 유효한 담당자·기한·완료 기준은 합의되었나요?", "후속 메일이나 첨부파일에 추가 조건이 있나요?"],
                "cautions": ["메일 내용으로 고른 검토 절차이며 실제 원인·승인 여부·실행 결과는 확인되지 않았습니다.",
                             "첨부파일·외부 링크·운영 시스템은 열거나 실행하지 않았습니다."]}

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
            "model": self.cfg.get("ollama_model"), "prompt": prompt, "version": 1}).encode()).hexdigest()
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
