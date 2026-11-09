"""사용자가 접근 가능한 본인 메일만 읽는 선택형 수집기."""
import hashlib
import json
from datetime import datetime, date, time, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from .core import KST, addresses, normalize_message
from .policy import sender_excluded


class TextHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "div", "br", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _outlook_body(item):
    """Return Outlook's plain body, falling back to text extracted from HTML."""
    body = str(item.Body or "")
    if body.strip():
        return body
    html = str(item.HTMLBody or "")
    if not html.strip():
        return body
    parser = TextHTML()
    parser.feed(html)
    return "".join(parser.parts).strip()


def watch_outlook_new_mail(callback):
    """Subscribe to Outlook's NewMailEx event; caller must pump COM messages."""
    try:
        import win32com.client
    except ImportError as exc:
        raise ValueError("실시간 Outlook 알림에는 pywin32가 필요합니다. requirements.txt를 설치한 Python 환경으로 앱을 실행하세요.") from exc

    class OutlookEvents:
        def OnNewMailEx(self, entry_ids):
            callback()

    application = win32com.client.Dispatch("Outlook.Application")
    sink = win32com.client.WithEvents(application, OutlookEvents)
    return application, sink


def read_eml(path):
    raw = Path(path).read_bytes()
    mail = BytesParser(policy=policy.default).parsebytes(raw)
    part = mail.get_body(preferencelist=("plain", "html"))
    content = part.get_content() if part else ""
    if part and part.get_content_type() == "text/html":
        parser = TextHTML()
        parser.feed(content)
        content = "".join(parser.parts)
    sent = parsedate_to_datetime(str(mail["Date"]))
    if sent.tzinfo is None:
        raise ValueError("EML 날짜에 시간대가 없습니다.")
    identifier = str(mail.get("Message-ID") or hashlib.sha256(raw).hexdigest())
    references = str(mail.get("References") or mail.get("In-Reply-To") or identifier).split()
    return {"id": identifier, "thread_id": references[0], "subject": str(mail.get("Subject", "")),
            "sender": str(mail.get("From", "")), "to": str(mail.get("To", "")), "cc": str(mail.get("Cc", "")),
            "sent_at": sent.isoformat(), "body": content, "source": "eml"}


def read_json(path):
    result = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(result, list):
        raise ValueError("JSON 최상위는 메일 배열이어야 합니다.")
    return result


def collection_window(start=None, end=None, clock=None):
    """미지정은 정확히 현재부터 30일 전. 지정 날짜의 종료일은 포함합니다."""
    clock = clock or datetime.now(KST)
    if (start is None) != (end is None):
        raise ValueError("이전 메일 수집은 시작일과 종료일을 모두 지정하세요.")
    if start is None:
        return clock - timedelta(days=30), clock
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last:
        raise ValueError("수집 시작일은 종료일보다 늦을 수 없습니다.")
    if last > clock.date():
        raise ValueError("수집 종료일은 오늘 이후일 수 없습니다.")
    if (last - first).days >= 366:
        raise ValueError("한 번에 최대 366일을 지정하세요. 더 짧은 기간으로 나눠 수집할 수 있습니다.")
    lower = datetime.combine(first, time.min, KST)
    upper = min(datetime.combine(last + timedelta(days=1), time.min, KST), clock)
    return lower, upper


def outlook_filter(lower, upper, field='"urn:schemas:httpmail:datereceived"'):
    # DASL 날짜는 UTC. Outlook의 지역별 날짜 문자열 해석 차이를 피합니다.
    stamp = lambda value: value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return f"@SQL={field} >= '{stamp(lower)}' AND {field} < '{stamp(upper)}'"


def _outlook_namespace():
    try:
        import win32com.client
    except ImportError as exc:
        raise ValueError("Classic Outlook 수집에는 pywin32가 필요합니다.") from exc
    try:
        return win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
    except Exception as exc:
        hresult = getattr(exc, "hresult", None)
        code = f" (HRESULT 0x{hresult & 0xFFFFFFFF:08X})" if isinstance(hresult, int) else ""
        raise ValueError(f"Classic Outlook에 연결하지 못했습니다{code}. Outlook과 이 앱을 같은 일반 사용자 Windows 세션에서 실행하고, 관리자 권한이나 서비스 세션을 섞지 마세요.") from exc


def _outlook_account(namespace, cfg):
    expected = cfg["user_email"].lower()
    for index in range(1, namespace.Accounts.Count + 1):
        candidate = namespace.Accounts.Item(index)
        if str(candidate.SmtpAddress).lower() == expected:
            return candidate
    raise ValueError(f"Outlook 프로필에서 {expected} 계정을 찾지 못했습니다. Outlook에서 해당 계정 설정을 완료하세요.")


def outlook_probe(cfg):
    try:
        import pythoncom
    except ImportError as exc:
        raise ValueError("Outlook 수집에는 pywin32가 필요합니다. requirements.txt를 설치한 Python 환경으로 앱을 실행하세요.") from exc
    pythoncom.CoInitialize()
    try:
        namespace = _outlook_namespace()
        account = _outlook_account(namespace, cfg)
        folder = account.DeliveryStore.GetDefaultFolder(6)
        return {"connected": True, "email": str(account.SmtpAddress), "folder": str(folder.Name), "method": "Classic Outlook"}
    finally:
        pythoncom.CoUninitialize()


def outlook(cfg, start=None, end=None, known_ids=None, progress=None, metadata_update=None, folder_type=6):
    """본인 받은편지함(6) 또는 보낸 편지함(5)을 기간별로 수집합니다."""
    lower, upper = collection_window(start, end)
    try:
        import pythoncom
    except ImportError as exc:
        raise ValueError("Outlook 수집에는 pywin32가 필요합니다.") from exc
    pythoncom.CoInitialize()
    try:
        namespace = _outlook_namespace()
        account = _outlook_account(namespace, cfg)
        sent_folder = folder_type == 5
        if folder_type not in (5, 6):
            raise ValueError("지원하지 않는 Outlook 메일함입니다.")
        folder = account.DeliveryStore.GetDefaultFolder(folder_type)
        date_field = '"urn:schemas:httpmail:date"' if sent_folder else '"urn:schemas:httpmail:datereceived"'
        items = folder.Items.Restrict(outlook_filter(lower, upper, date_field))
        items.Sort("[SentOn]" if sent_folder else "[ReceivedTime]", False)
        total = items.Count
        known = known_ids or set()

        def smtp(entry):
            if entry is None:
                return ""
            if entry.Type == "EX":
                user = entry.GetExchangeUser()
                if user:
                    return str(user.PrimarySmtpAddress)
                try:
                    return str(entry.PropertyAccessor.GetProperty("http://schemas.microsoft.com/mapi/proptag/0x39FE001E"))
                except Exception:
                    return ""
            return str(entry.Address)

        for index in range(1, total + 1):
            if progress:
                progress(scanned=index, matched=total)
            item = items.Item(index)
            if item.Class != 43:
                continue
            try:
                identifier = item.PropertyAccessor.GetProperty("http://schemas.microsoft.com/mapi/proptag/0x1035001F")
            except Exception:
                identifier = item.EntryID
            identifier = str(identifier or item.EntryID)
            # Outlook에서 범위 필터 후에도 경계 시각을 코드에서 한 번 더 확인합니다.
            received = item.ReceivedTime if not sent_folder else None
            timestamp = item.SentOn if sent_folder else received
            if timestamp.tzinfo is None:
                timestamp = timestamp.astimezone()
            if not lower <= timestamp < upper:
                continue
            if identifier in known:
                if metadata_update and not sent_folder:
                    metadata_update(identifier, received.astimezone(KST).isoformat())
                continue
            sender = str(account.SmtpAddress) if sent_folder else smtp(item.Sender)
            if sender_excluded(sender, cfg):
                continue
            to, cc = [], []
            for recipient_index in range(1, item.Recipients.Count + 1):
                recipient = item.Recipients.Item(recipient_index)
                address = smtp(recipient.AddressEntry)
                (to if recipient.Type == 1 else cc).append(address)
            mine = addresses(cfg["user_email"]) | addresses(cfg.get("aliases", []))
            if not sent_folder and not (addresses(to) | addresses(cc)) & mine:
                continue
            message = {"id": identifier, "thread_id": str(item.ConversationID or identifier),
                       "subject": str(item.Subject or "(제목 없음)"), "sender": sender,
                       "to": ",".join(to), "cc": ",".join(cc), "sent_at": timestamp.isoformat(),
                       "received_at": None if sent_folder else received.astimezone(KST).isoformat(),
                       "direction": "outbound" if sent_folder else "inbound",
                       "body": _outlook_body(item), "source": "outlook-classic-sent" if sent_folder else "outlook-classic"}
            yield normalize_message(message, cfg)
    finally:
        # COM 참조는 생성한 스레드에서만 사용하고 해제합니다.
        pythoncom.CoUninitialize()


def origin(url):
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def groupware(cfg):
    """설정된 페이지의 현재 목록만 수집. 로그인과 MFA는 사용자가 수행합니다."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ValueError("그룹웨어 수집에는 playwright와 Chromium 설치가 필요합니다.") from exc
    settings = cfg["groupware"]
    allowed = settings["allowed_origin"].rstrip("/")
    if urlsplit(allowed).scheme != "https" or origin(settings["url"]) != allowed:
        raise ValueError("그룹웨어 URL과 HTTPS allowed_origin이 일치해야 합니다.")
    profile = Path(cfg["data_dir"]) / "browser-profile"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch_persistent_context(str(profile), headless=False, viewport={"width": 1400, "height": 1000})
        try:
            page = browser.pages[0] if browser.pages else browser.new_page()
            page.goto(settings["url"], wait_until="domcontentloaded")
            input("전용 브라우저에서 본인 로그인/MFA를 마치고 받은편지함을 연 뒤 여기서 Enter를 누르세요: ")

            def guard():
                if origin(page.url) != allowed:
                    raise ValueError("허용한 그룹웨어 주소가 아닙니다.")
                locator = page.locator(settings["account_selector"])
                if locator.count() != 1:
                    raise ValueError("현재 로그인 계정을 유일하게 확인할 수 없습니다. account_selector를 확인하세요.")
                attr = settings.get("account_attribute")
                value = locator.get_attribute(attr) if attr else locator.inner_text()
                # 표시 이름이 아닌 실제 SMTP 주소의 정확한 일치를 요구합니다.
                if addresses(value) != addresses(cfg["user_email"]):
                    raise ValueError("현재 로그인 계정이 설정한 본인 이메일과 일치하지 않습니다.")

            guard()
            limit = max(1, min(int(cfg.get("collection_limit", 50)), 500))
            rows = page.locator(settings["row_selector"])
            count = min(rows.count(), limit)
            if not count:
                raise ValueError("수집할 메일 행이 없습니다. row_selector를 확인하세요.")
            result = []
            for index in range(count):
                guard()
                row = page.locator(settings["row_selector"]).nth(index)
                identifier = row.get_attribute(settings["id_attribute"])
                thread = row.get_attribute(settings.get("thread_attribute", "data-thread-id"))
                if not identifier:
                    raise ValueError("안정적인 메시지 ID가 없습니다. id_attribute를 설정하세요.")
                if settings.get("open_messages", False):
                    row.click()
                    page.locator(settings["body_selector"]).wait_for(state="visible")
                    guard()
                    container = page
                else:
                    container = row

                def text(key, optional=False):
                    selector = settings.get(key)
                    if not selector and optional:
                        return ""
                    node = container.locator(selector)
                    if optional and node.count() == 0:
                        return ""
                    if node.count() != 1:
                        raise ValueError(f"{key} 선택자는 메일당 한 요소여야 합니다.")
                    return node.inner_text().strip()

                date_node = container.locator(settings["date_selector"])
                attribute = settings.get("date_attribute")
                timestamp = date_node.get_attribute(attribute) if attribute else date_node.inner_text().strip()
                message = {"id": "web:" + hashlib.sha256((allowed + ":" + identifier).encode()).hexdigest(),
                           "thread_id": "web:" + hashlib.sha256((allowed + ":" + str(thread or identifier)).encode()).hexdigest(), "subject": text("subject_selector"),
                           "sender": text("sender_selector"), "to": text("to_selector"), "cc": text("cc_selector", True),
                           "sent_at": timestamp, "body": text("body_selector"), "source": "groupware"}
                result.append(normalize_message(message, cfg))
                if settings.get("open_messages", False) and settings.get("back_selector"):
                    page.locator(settings["back_selector"]).click()
                    page.locator(settings["row_selector"]).first.wait_for(state="visible")
            return result
        finally:
            browser.close()
