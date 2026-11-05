"""원문을 바꾸지 않는 토픽·스레드 읽기 모델."""
import hashlib
import json
import re


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


def category(text):
    value = text.lower()
    if "ifrs" in value:
        return CATEGORIES[4]
    if any(word in value for word in ("펌뱅킹", "fcm", "은행", "송금")):
        return CATEGORIES[5]
    if re.search(r"\bai\b|데이터 솔루션|인공지능", value):
        return CATEGORIES[3]
    if any(word in value for word in ("유지보수", "노츠", "보안 패치", "rfc 계정", "취약")):
        return CATEGORIES[2]
    if any(word in value for word in ("개선", "예산", "고도화")):
        return CATEGORIES[0]
    if any(word in value for word in ("sap", "erp", "서버", "카피", "전표")):
        return CATEGORIES[1]
    return CATEGORIES[6]


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
            key = min(clean_subject(m["subject"]).casefold() for m in group)
            identity = "topic-" + hashlib.sha256((key + min(m["thread_id"] for m in group)).encode()).hexdigest()[:16]
            children = []
            for mail in group:
                children.append({"id": mail["id"], "subject": mail["subject"], "sender": mail["sender"],
                                 "sent_at": mail["sent_at"], "received_at": mail["received_at"] or mail["sent_at"],
                                 "date_basis": "수신일" if mail["received_at"] else "발송일 대체",
                                 "summary": brief(mail["body"]), "proposals": by_mail.get(mail["id"], [])})
            result.append({"id": identity, "title": title, "category": category(title), "date_basis": children[0]["date_basis"],
                           "latest_at": children[0]["received_at"], "latest_summary": children[0]["summary"],
                           "message_count": len(group), "proposal_count": sum(len(m["proposals"]) for m in children),
                           "messages": children})
        return sorted(result, key=lambda g: (g["latest_at"], g["id"]), reverse=True)

    def summaries(self):
        return [{k: v for k, v in g.items() if k != "messages"} for g in self.build()]

    def get(self, topic_id):
        return next((g for g in self.build() if g["id"] == topic_id), None)
