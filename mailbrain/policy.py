"""메일 수집·조회에 공통 적용하는 발신자 제외 규칙."""
import json
import re
from email.utils import getaddresses

DEFAULT_EXCLUDED_SENDERS = {"no-reply@teams.mail.microsoft"}
# 내용과 관계없이 전자결재로 분류하는 발신자. 설정의 approval_senders로 추가할 수 있습니다.
DEFAULT_APPROVAL_SENDERS = {"batchjobserv@golfzon.com"}
# 회신/전달 접두어 뒤의 [결재 진행]·[결재 완료] 제목 접두어
APPROVAL_PREFIX = re.compile(
    r"^\s*(?:(?:re|fw|fwd|회신|전달)\s*(?:\[\d+\])?\s*[:：]\s*|\[\s*(?:외부\s*메일|external)\s*\]\s*)*\[\s*결재\s*(?:진행|완료)\s*\]",
    re.I)
BIRTHDAY = re.compile(r"생일\s*(?:을|을\s*진심으로)?\s*축하|생신\s*(?:을)?\s*축하|happy\s*birthday|birthday\s*(?:wish|greeting|celebration)", re.I)
SIGN_IN_SUBJECT = re.compile(r"로그인|sign[\s-]?in|log[\s-]?in|보안\s*링크|인증\s*링크|본인\s*(?:인증|확인)|magic\s*link|verify|verification|일회용|one[\s-]?time", re.I)
SIGN_IN_LINK = re.compile(r"링크|link|https?://", re.I)
BUSINESS_ISSUE = re.compile(r"SAP|ERP|오류|장애|이슈|개선|연동|권한\s*신청|error|issue", re.I)


def sender_excluded(sender, cfg=None):
    configured = (cfg or {}).get("excluded_senders", [])
    if isinstance(configured, str):
        configured = [configured]
    blocked = DEFAULT_EXCLUDED_SENDERS | {str(value).strip().lower() for value in configured}
    actual = {address.strip().lower() for _, address in getaddresses([str(sender or "")])}
    # Display-form sender strings may contain a blocked token and the actual
    # mailbox. Exclude only when every parsed mailbox is blocked.
    return bool(actual) and actual.issubset(blocked)


def mailbox(value):
    return {address.strip().lower() for _, address in getaddresses([str(value or "")]) if "@" in address}


def approval_senders(cfg=None):
    configured = (cfg or {}).get("approval_senders", [])
    if isinstance(configured, str):
        configured = [configured]
    return DEFAULT_APPROVAL_SENDERS | {str(value).strip().lower() for value in configured}


def approval_mail(subject, body="", sender="", cfg=None):
    """[결재 진행]·[결재 완료] 접두어나 결재 시스템 발신자는 다른 규칙보다 먼저 전자결재로 봅니다."""
    if APPROVAL_PREFIX.match(str(subject or "")):
        return True
    if mailbox(sender) & approval_senders(cfg):
        return True
    return approval_kind(subject, body)


def personal_notice(subject, body=""):
    """생일 축하·로그인 보안 링크처럼 업무가 아닌 개인 알림 메일인지 판단합니다."""
    subject, head = str(subject or ""), str(body or "")[:600]
    if BIRTHDAY.search(subject) or BIRTHDAY.search(head):
        return True
    return bool(SIGN_IN_SUBJECT.search(subject) and SIGN_IN_LINK.search(subject + " " + head)
                and not BUSINESS_ISSUE.search(subject))


def approval_kind(subject, body=""):
    text = f"{subject or ''} {body or ''}".lower()
    approval_words = ("전자결재", "결재", "결재선", "승인 요청", "결재 요청", "결재 완료", "결재 진행", "approval", "approval request", "approved", "approval status")
    return any(word in text for word in approval_words)


def noise_mail(subject, body="", sender="", cfg=None):
    """Hide obvious marketing and low-value automated notices from work views."""
    text = f"{subject or ''} {body or ''}".lower()
    if approval_mail(subject, body, sender, cfg):
        return False
    promotional = ("광고", "프로모션", "특가", "할인 쿠폰", "뉴스레터", "구독 해지", "수신 거부", "newsletter", "unsubscribe", "promotion", "special offer", "sale", "coupon", "marketing")
    if any(word in text for word in promotional):
        return True
    notification = ("자동 알림", "시스템 알림", "단순 알림", "notification:", "automated notification", "no-reply notification", "읽음 확인", "배달 확인", "전달 확인", "부재중 자동 회신", "자동 회신")
    return any(word in text for word in notification)


def direct_recipient(message, cfg):
    def parsed(value):
        if isinstance(value, list):
            value = ",".join(str(item) for item in value)
        return {addr.lower() for _, addr in getaddresses([str(value or "")]) if "@" in addr}
    mine = parsed(cfg.get("user_email", "")) | parsed(cfg.get("aliases", []))
    return bool(mine & parsed(message.get("to", "")))


def work_related(message):
    """Keep explicit action items or mail with clear business context in My Work."""
    analysis = message.get("analysis") or {}
    if isinstance(analysis, str):
        try:
            analysis = json.loads(analysis)
        except (TypeError, json.JSONDecodeError):
            analysis = {}
    tasks = analysis.get("tasks", []) if isinstance(analysis, dict) else []
    if any(isinstance(item, dict)
           and item.get("action") in {"CREATE", "UPDATE", "CANCEL", "COMPLETE"}
           and isinstance(item.get("title"), str) and item["title"].strip()
           and isinstance(item.get("evidence_quote"), str) and item["evidence_quote"].strip()
           for item in tasks):
        return True
    text = f"{message.get('subject', '')} {message.get('body', '')}".casefold()
    business_signals = (
        "업무", "프로젝트", "sap", "erp", "리스", "계약", "스케줄", "스케쥴", "일정 조정",
        "회의", "거래처", "전자결재", "승인 요청", "서버", "시스템", "개발", "장애", "오류",
        "견적", "발주", "전표", "정산", "계정 권한", "유지보수", "배포", "검토 요청",
        "자료 요청", "변경 요청", "조치 요청", "업무 요청", "business", "project", "invoice",
        "contract", "incident", "deployment", "maintenance", "purchase order",
    )
    return any(signal in text for signal in business_signals)


def task_recipient(message, cfg):
    """To 메일은 업무 관련성이 있어야 하며, CC는 실행 요청 근거까지 있어야 합니다."""
    if not work_related(message):
        return False
    if direct_recipient(message, cfg):
        return True

    def parsed(value):
        if isinstance(value, list):
            value = ",".join(str(item) for item in value)
        return {addr.lower() for _, addr in getaddresses([str(value or "")]) if "@" in addr}

    mine = parsed(cfg.get("user_email", "")) | parsed(cfg.get("aliases", []))
    if not mine & parsed(message.get("cc", "")):
        return False
    analysis = message.get("analysis") or {}
    if isinstance(analysis, str):
        try:
            analysis = json.loads(analysis)
        except (TypeError, json.JSONDecodeError):
            return False
    tasks = analysis.get("tasks", []) if isinstance(analysis, dict) else []
    return any(isinstance(item, dict)
               and item.get("action") in {"CREATE", "UPDATE", "CANCEL", "COMPLETE"}
               and isinstance(item.get("title"), str) and item["title"].strip()
               and isinstance(item.get("evidence_quote"), str) and item["evidence_quote"].strip()
               for item in tasks)


def reply_requested(subject, body=""):
    text = f"{subject or ''} {body or ''}".lower()
    return any(word in text for word in ("회신 부탁", "회신 바랍니다", "회신해 주세요", "회신해주세요", "답변 부탁", "답변 바랍니다", "답변해 주세요", "답변해주세요", "답장 부탁", "확인 후 알려", "의견 부탁", "please reply", "please respond", "please confirm", "let me know"))
