"""메일 수집·조회에 공통 적용하는 발신자 제외 규칙."""
from email.utils import getaddresses

DEFAULT_EXCLUDED_SENDERS = {"no-reply@teams.mail.microsoft"}


def sender_excluded(sender, cfg=None):
    configured = (cfg or {}).get("excluded_senders", [])
    if isinstance(configured, str):
        configured = [configured]
    blocked = DEFAULT_EXCLUDED_SENDERS | {str(value).strip().lower() for value in configured}
    actual = {address.strip().lower() for _, address in getaddresses([str(sender or "")])}
    return bool(actual & blocked)
