"""원문을 바꾸지 않는 토픽·스레드 읽기 모델."""
import hashlib
import json
import re

from .policy import personal_notice


def clean_subject(subject):
    value = subject.strip()
    prefix = r"^(?:\s*\[(?:외부\s*메일|external|re|fw|fwd)\]\s*|\s*(?:re|fw|fwd|회신|전달)\s*(?:\[\d+\])?\s*[:：]\s*)"
    while True:
        result = re.sub(prefix, "", value, flags=re.I)
        if result == value:
            break
        value = result
    return re.sub(r"\s+", " ", value).strip() or "(제목 없음)"


def new_body(body):
    value = re.split(r"(?im)^\s*(?:On .+wrote:|보낸\s*사람\s*:|From\s*:|[-_]{3,}\s*(?:Original Message|원본 메시지)|>)", body)[0]
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    return "\n".join(lines)


def brief(body, limit=260):
    lines = [s for s in new_body(body).splitlines() if not re.fullmatch(r"(?:안녕하세요[.!]?|감사합니다[.!]?)", s)]
    return "\n".join(lines)[:limit] or "새 본문 요약 없음 · 원문을 확인하세요."


CATEGORIES = ["SAP ERP 개선 과제", "SAP ERP 이슈 및 조치", "SAP ERP 운영 유지보수 관리", "AI 데이터 업무 지원", "IFRS18 업무 지원", "펌뱅킹", "상시업무"]
CATEGORIES.append("재무/리스 업무")
CATEGORIES.append("전자결재")


def category(text, sender="", cfg=None):
    value = text.lower()
    from .policy import approval_mail
    if approval_mail(text, "", sender, cfg):
        return "전자결재"
    if "ifrs" in value:
        return CATEGORIES[4]
    if any(word in value for word in ("펌뱅킹", "fcm", "은행", "송금")):
        return CATEGORIES[5]
    if "리스" in value and any(word in value for word in ("스케줄", "스케쥴", "계약", "임대")):
        return CATEGORIES[7]
    if re.search(r"\bai\b|데이터 솔루션|인공지능", value):
        return CATEGORIES[3]
    if any(word in value for word in ("유지보수", "노츠", "보안 패치", "rfc 계정", "취약")):
        return CATEGORIES[2]
    if any(word in value for word in ("개선", "예산", "고도화")):
        return CATEGORIES[0]
    if any(word in value for word in ("sap", "erp", "서버", "카피", "전표")):
        return CATEGORIES[1]
    return CATEGORIES[6]


def task_title(subject, group_text=""):
    """메일 제목을 업무 목록에서 빠르게 알아볼 짧은 이름으로 정리합니다."""
    title = re.sub(r"^\s*\[[^\]]+\]\s*", "", clean_subject(subject))
    title = re.sub(r"\s*(?:요청(?:의\s*건|\s*건)?|문의(?:의\s*건|\s*건)?|협조(?:의\s*건|\s*요청)?|의\s*건)\s*$", "", title, flags=re.I).strip(" -:：")
    text = f"{subject} {group_text}".lower()
    if "리스" in text and any(word in text for word in ("스케줄", "스케쥴")):
        return "리스 스케줄 조정"
    if len(title) > 72:
        return title[:69].rstrip() + "…"
    return title or clean_subject(subject)


class Topics:
    def __init__(self, store):
        self.store = store

    def build(self):
        mails = self.store.rows("SELECT * FROM messages WHERE excluded=0 ORDER BY COALESCE(received_at,sent_at) DESC,id")
        parent = {m["id"]: m["id"] for m in mails}
        def find(key):
            while parent[key] != key:
                parent[key] = parent[parent[key]]
                key = parent[key]
            return key
        def union(a, b):
            parent[find(a)] = find(b)
        threads, subjects = {}, {}
        for mail in mails:
            thread = mail["thread_id"]
            if thread in threads:
                union(mail["id"], threads[thread])
            threads[thread] = mail["id"]
            subject = clean_subject(mail["subject"]).casefold()
            # 짧은 공통 제목(공지, 안내 등)은 다른 대화를 합치지 않습니다.
            if len(re.sub(r"\s", "", subject)) >= 6:
                if subject in subjects:
                    union(mail["id"], subjects[subject])
                subjects[subject] = mail["id"]
        buckets = {}
        for mail in mails:
            buckets.setdefault(find(mail["id"]), []).append(mail)
        proposals = self.store.rows("SELECT * FROM proposals WHERE state='대기' ORDER BY rowid")
        by_mail = {}
        for proposal in proposals:
            by_mail.setdefault(proposal["source_id"], []).append(proposal)
        result = []
        for group in buckets.values():
            group.sort(key=lambda m: (m["received_at"] or m["sent_at"], m["id"]), reverse=True)
            title = clean_subject(group[0]["subject"])
            candidate_mail = next((m for m in group if m.get("direction", "inbound") == "inbound"), None)
            key = min(clean_subject(m["subject"]).casefold() for m in group)
            identity = "topic-" + hashlib.sha256((key + min(m["thread_id"] for m in group)).encode()).hexdigest()[:16]
            children = []
            for mail in group:
                children.append({"id": mail["id"], "subject": mail["subject"], "sender": mail["sender"],
                                 "sent_at": mail["sent_at"], "received_at": mail["received_at"] or mail["sent_at"],
                                 "direction": mail.get("direction", "inbound"),
                                 "date_basis": "수신일" if mail["received_at"] else "발송일 대체",
                                 "summary": (mail.get("summary") or brief(mail["body"])),
                                 "proposals": by_mail.get(mail["id"], []) if candidate_mail and mail["id"] == candidate_mail["id"] else []})
            latest = group[0]
            latest_text_mail = next((m for m in group if m.get("direction", "inbound") == "inbound"), latest)
            group_text = latest_text_mail["subject"] + " " + brief(new_body(latest_text_mail["body"]))
            policy_cfg = getattr(self.store, "policy_cfg", {})
            topic_category = category(group_text, latest_text_mail.get("sender", ""), policy_cfg)
            is_approval = topic_category == "전자결재"
            # 생일 축하·로그인 보안 링크 메일은 [수집한 메일]에만 남기고 업무 화면에서는 뺍니다.
            notice = not is_approval and personal_notice(latest_text_mail["subject"], new_body(latest_text_mail["body"]))
            lowered = group_text.lower()
            approval_status = ("결재 완료" if any(word in lowered for word in ("결재 완료", "결재완료", "승인 완료", "승인완료", "결재가 완료", "최종 승인", "approved", "approval completed")) else "진행 알림") if is_approval else None
            main_summary = next((m["summary"] for m in children if candidate_mail and m["id"] == candidate_mail["id"]), children[0]["summary"])
            result.append({"id": identity, "title": title, "category": topic_category, "approval_status": approval_status, "notice": notice, "date_basis": children[0]["date_basis"],
                           "latest_at": children[0]["received_at"], "latest_summary": main_summary,
                           "message_count": len(group), "proposal_count": sum(len(m["proposals"]) for m in children),
                           "messages": children})
        return sorted(result, key=lambda g: (g["latest_at"], g["id"]), reverse=True)

    def summaries(self):
        return [{k: v for k, v in g.items() if k != "messages"} for g in self.build()]

    def get(self, topic_id):
        return next((g for g in self.build() if g["id"] == topic_id), None)
