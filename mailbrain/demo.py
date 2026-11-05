from datetime import datetime, timedelta
from .core import KST


def messages(email):
    today = datetime.now(KST).date()
    due = (today + timedelta(days=2)).isoformat()
    changed = (today + timedelta(days=5)).isoformat()
    stamp = datetime.now(KST).replace(hour=9, minute=0, second=0).isoformat()
    prefix = "demo:" + today.isoformat()
    return [
        {"id": prefix + ":1", "thread_id": prefix + ":A", "subject": "[알파] 검토 자료 요청", "sender": "lead@example.com", "to": email, "sent_at": stamp,
         "body": f"{email} 담당자님, 업무: 검토 자료 제출; {due}까지 준비해주세요.", "source": "demo"},
        {"id": prefix + ":2", "thread_id": prefix + ":A", "subject": "[알파] 제출 일정 변경", "sender": "lead@example.com", "to": email, "sent_at": stamp,
         "body": f"협력사 자료 지연으로 변경합니다. 업무: 검토 자료 제출; 기한을 {changed}로 변경합니다.", "source": "demo"},
        {"id": prefix + ":3", "thread_id": prefix + ":B", "subject": "[베타] 회의 자료", "sender": "manager@example.com", "to": email, "sent_at": stamp,
         "body": f"{email} 담당자님, 업무: 회의 안건 정리; {due}까지 정리해주세요.", "source": "demo"},
        {"id": prefix + ":4", "thread_id": prefix + ":C", "subject": "[참고] 다른 담당자 업무", "sender": "manager@example.com", "to": "other@example.com", "cc": email, "sent_at": stamp,
         "body": "other@example.com 담당자님, 업무: 서버 점검; 내일까지 수행해주세요.", "source": "demo"},
        {"id": prefix + ":5", "thread_id": prefix + ":D", "subject": "이번 주 안내", "sender": "news@example.com", "to": email, "sent_at": stamp,
         "body": "금요일 점심시간 사내 행사를 안내드립니다.", "source": "demo"},
    ]
