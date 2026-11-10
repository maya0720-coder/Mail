"""사용자 제공 ERP 주간보고의 4열 구조를 따르는 출력기."""
from datetime import date, timedelta
from html import escape
from pathlib import Path
from urllib.parse import quote

from .topics import Topics, CATEGORIES, category
from .policy import personal_notice
import re
import uuid


def report_excerpt(text):
    text = re.sub(r"<?https?://\S+>?", "", text)
    text = re.sub(r"((?:인증\s*코드|verification\s*code|log.?in\s*code)[^\d\n]{0,30})\d{4,8}\b", r"\1[인증 정보 생략]", text, flags=re.I)
    lines = [line.strip() for line in text.splitlines() if line.strip() and not re.search(r"안녕하세요|감사합니다|드림$|^dear\b|^hello\b|best regard|^https?://", line, re.I)]
    important = [line for line in lines if re.search(r"일정|변경|요청|완료|검토|오류|테스트|진행|예정|확인|지원|배포|update|resolve", line, re.I)]
    return " ".join((important or lines)[:3])[:160] or "세부 내용은 근거 메일 확인"


def is_auth_notification(subject):
    authentication = re.search(r"인증|로그인|보안\s*링크|비밀번호|verification|log.?in|sign.in|password|one.time", subject, re.I)
    business_issue = re.search(r"\bSAP\b|\bERP\b|오류|장애|이슈|개선|연동|\berror\b|\bissue\b", subject, re.I)
    return bool(authentication and not business_issue)


# 보고서 문장에서 빼는 인사말·서명·자기소개
GREETING = re.compile(
    r"안녕하(?:세요|십니까)|수고\s*(?:많으십니다|하십니다|많으셨습니다)|고생\s*많으십니다|감사(?:합니다|드립니다|드려요)|고맙습니다"
    r"|좋은\s*하루|(?:드림|올림)$|^dear\b|^hello\b|^hi\b|best\s*regards?|^regards|^thanks?\b|^thank\s*you",
    re.I)
SELF_INTRO = re.compile(r"(?:팀|파트|센터|그룹|본부|실|부)\s*\S{0,10}\s*[가-힣]{2,4}\s*(?:입니다|이에요)$"
                        r"|(?:매니저|대리|과장|차장|부장|사원|주임|책임|선임|수석|프로|팀장|실장|담당자?)\s*(?:입니다|이에요)$")
FILLER = re.compile(r"^(?:다름이\s*아니(?:라|오라)\s*|번거로우시겠지만\s*|바쁘시겠지만\s*|바쁘신\s*(?:와중에|중에)\s*|죄송하지만\s*|죄송합니다만\s*"
                    r"|혹시\s*|아무쪼록\s*|가능하시면\s*|가능하다면\s*|(?:아래|하기|다음)와\s*같이\s*|(?:아래|하기)\s*내용\s*)+")
# 명사형으로 바꿀 때 쓰는 업무 행위. 활용형(보내·알려)은 명사로 바꿉니다.
ACTION_STEMS = ("처리", "진행", "검토", "확인", "회신", "답변", "공유", "제출", "작성", "승인", "결재", "수정", "반영", "등록",
                "송부", "전달", "조치", "지원", "협조", "점검", "준비", "배포", "적용", "변경", "삭제", "추가", "요청", "안내",
                "보내", "알려", "완료", "테스트", "분석", "정리", "업데이트", "취소")
ACTION_NOUN = {"처리": "진행", "보내": "송부", "알려": "회신"}
# '~ 요청'이 바로 붙는 행위. 그 밖의 명사 뒤에는 '진행 요청'을 붙입니다(예: 서버 점검 진행 요청).
DIRECT_REQUEST = {"검토", "확인", "회신", "답변", "공유", "제출", "승인", "결재", "송부", "협조", "피드백", "의견", "작성",
                  "수정", "반영", "조치", "지원", "전달", "등록", "삭제", "추가", "변경", "안내"}


def stem(name):
    return f"(?P<{name}>" + "|".join(sorted(ACTION_STEMS, key=len, reverse=True)) + ")"


REQUEST_TAIL = (r"(?:\s*(?:해|하여|해서|하여서))?\s*(?:주세요|주십시오|주시기\s*바랍니다|주시면\s*감사하겠습니다|주시길\s*바랍니다"
                r"|주실\s*수\s*있(?:을까요|나요|으신가요)|줄\s*수\s*있(?:을까요|나요))"
                r"|\s*부탁(?:드립니다|드려요|드리겠습니다|합니다|해요)|\s*요청(?:드립니다|드려요|합니다)|\s*(?:해\s*)?바랍니다")
REQUEST = re.compile(rf"^(?P<head>.*?)\s*{stem('verb')}?(?:{REQUEST_TAIL})\??$")
DONE = re.compile(rf"^(?P<head>.*?)\s*{stem('verb')}\s*(?:을|를|이|가)?\s*(?:하였|했|되었|됐|완료하였|완료했|완료되었|완료됐)(?:습니다|어요|음|다)$")
NEED = re.compile(rf"^(?P<head>.*?)\s*{stem('verb')}?\s*(?:이|가)?\s*필요(?:합니다|해요|함|해\s*보입니다|할\s*것\s*같습니다)$")
PLAN = re.compile(rf"^(?P<head>.*?)\s*{stem('verb')}?\s*(?:(?:할|될|하실)\s*(?:예정|계획)(?:입니다|이에요|임)?|하겠습니다|할게요)$")
DOING = re.compile(rf"^(?P<head>.*?)\s*{stem('verb')}?\s*(?:하고\s*있습니다|중입니다|중에\s*있습니다|중이에요|중임)$")
OFFER = re.compile(rf"^(?P<head>.*?)\s*{stem('verb')}\s*(?:드립니다|드려요|합니다|해요)$")


def strip_particle(head):
    head = re.sub(r"\s+", " ", head or "").strip(" ,.;:~-")
    return re.sub(r"(?<=[가-힣A-Za-z0-9)])\s*(?:을|를|은|는|도|에\s*대해(?:서)?|에\s*대한)\s*(?:좀|꼭|빠르게|조속히|신속히)?$", "", head).strip()


def phrase(head, verb, suffix):
    verb = ACTION_NOUN.get(verb, verb) if verb else ""
    return " ".join(part for part in (strip_particle(head), verb, suffix) if part)


def nominal(sentence):
    """구어체·요청형 문장을 보고서용 명사형('~ 진행 요청', '~ 완료', '~ 검토 필요')으로 바꿉니다."""
    text = re.sub(r"\s+", " ", str(sentence or "")).strip()
    text = FILLER.sub("", text).strip()
    text = re.sub(r"[.!~。]+$", "", text).strip()
    if not text:
        return ""
    match = REQUEST.match(text)
    if match:
        head, verb = strip_particle(match["head"]), match["verb"]
        if verb in (None, "처리", "진행", "요청"):
            last = head.split(" ")[-1] if head else ""
            return phrase(head, None, "요청" if last in DIRECT_REQUEST else "진행 요청")
        return phrase(head, verb, "요청")
    match = DONE.match(text)
    if match:
        verb = match["verb"]
        return phrase(match["head"], None if verb in ("처리", "진행", "완료") else verb, "완료")
    for pattern, suffix, default in ((NEED, "필요", None), (PLAN, "예정", None), (DOING, "중", "진행"), (OFFER, "", None)):
        match = pattern.match(text)
        if match:
            return phrase(match["head"], match["verb"] or default, suffix)
    return re.sub(r"\s*(?:입니다|이에요|예요|합니다|됩니다|드립니다)$", "", text)


def report_sentences(text, subjects=()):
    """메일 요약을 문장 단위로 나누고 메일 제목·인사말·자동 정리 머리말을 뺍니다."""
    from .topics import clean_subject
    def key(value):
        return re.sub(r"\s+", "", clean_subject(value)).casefold()
    text = re.sub(r"<?https?://\S+>?", "", str(text or ""))
    text = re.sub(r"((?:인증\s*코드|verification\s*code|log.?in\s*code)[^\d\n]{0,30})\d{4,8}\b", r"\1[인증 정보 생략]", text, flags=re.I)
    known = {key(s) for s in subjects if s}
    quotes, briefs, plain = [], [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or re.fullmatch(r"같은 메일 스레드의 업무 정리|메일에서 추출한 업무 요청|같은 스레드 메일별 요약", line):
            continue
        if re.match(r"^-\s*(?:이전 메일|중복 제거 후)", line):
            continue
        # 자동 정리의 '- [신규 요청] 업무명 — 원문 근거'는 원문 근거만 씁니다.
        action = re.match(r"^-\s*\[[^\]]+\]\s*.*?\s—\s(?P<quote>.+)$", line)
        if action:
            quotes.append(action["quote"])
            continue
        # '- 2026-11-10 · 메일 제목: 요약'은 메일 제목을 빼고 요약만 씁니다.
        dated = re.match(r"^-\s*\d{4}-\d{2}-\d{2}\s*·\s*(?P<subject>.+?):\s(?P<brief>.*)$", line)
        if dated:
            known.add(key(dated["subject"]))
            briefs.append(dated["brief"])
            continue
        plain.append(line)
    pieces = []
    for line in quotes or plain or briefs:
        if re.match(r"^\s*(?:제목|subject|re|fw|fwd)\s*[:：]", line, re.I):
            continue
        for part in re.split(r"(?<=[.!?。])\s+|(?<=다\.)\s*|\s+/\s+|\s*[•▶■]\s*", line):
            part = re.sub(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", "", part or "")
            # 규칙 분석용 '업무: 이름;' 표기는 이름 대신 뒤의 실제 요청 문장만 씁니다.
            part = re.sub(r"^\s*(?:(?:[^\s,]{1,15}\s+){0,2}[^\s,]{1,15}님\s*[,，]?\s*)?(?:업무\s*:\s*[^;]+;\s*)?", "", part.strip())
            part = re.sub(r"^\s*[-·*]\s*", "", part).strip(" -•·:;")
            if not part or GREETING.search(part) or SELF_INTRO.search(part) or key(part) in known:
                continue
            pieces.append(part)
    return pieces


def summary_points(text, limit=3, width=60, subjects=()):
    """서술형 메일 요약을 보고서용 짧은 명사형 요약 항목으로 바꿉니다(메일 제목·인사말 제외)."""
    pieces = report_sentences(text, subjects)
    important = [p for p in pieces if re.search(r"일정|변경|요청|완료|검토|오류|테스트|진행|예정|확인|지원|배포|처리|결재|부탁|주세요|바랍니다|필요|update|resolve", p, re.I)]
    points = []
    for piece in important or pieces:
        piece = nominal(piece)
        if not piece:
            continue
        short = piece if len(piece) <= width else piece[:width - 1].rstrip() + "…"
        if short not in points:
            points.append(short)
        if len(points) >= limit:
            break
    return points or ["세부 내용은 근거 메일 확인"]


def new_item(title, points, start, end, source_ids, task_ids, done=False):
    return {"id": uuid.uuid4().hex[:10], "title": title, "points": points, "start": start, "end": end,
            "source_ids": [s for s in source_ids if s], "task_ids": task_ids, "done": done}


def build_document(store, cfg, start, end, snapshot, changes, completed_count):
    from .core import work_kind, work_stage, work_confirmed
    from .topics import task_title
    topics = store.topic_by_message()
    current = {row["id"]: row for row in store.rows("SELECT id,read_at FROM tasks")}
    rows = {name: {"category": name, "this_week": [], "next_week": [], "notes": []} for name in CATEGORIES}
    end_date = date.fromisoformat(end)
    next_start, next_end = (end_date + timedelta(days=1)).isoformat(), (end_date + timedelta(days=7)).isoformat()
    changed_ids = {event["task_id"] for event in changes}
    topic_limit = max(1, min(30, int(cfg.get("report_topics_per_category", 8))))
    for task in sorted(snapshot.values(), key=lambda t: t.get("updated_at") or "", reverse=True):
        task = dict(task)
        # 확인 이벤트가 없던 이전 버전 데이터는 현재 확인 시각이 보고 종료일 이전일 때만 인정합니다.
        stored = (current.get(task["id"]) or {}).get("read_at")
        if not task.get("read_at") and stored and stored[:10] <= end:
            task["read_at"] = stored
        topic = topics.get(task.get("source_id"))
        category_name = topic["category"] if topic else category(f"{task.get('project') or ''} {task.get('title') or ''}")
        kind = work_kind(category_name)
        stage = work_stage(task, kind)
        # 생일 축하·로그인 보안 링크 같은 개인 알림 업무는 보고서에 넣지 않습니다.
        if (topic and topic.get("notice")) or (not topic and personal_notice(task.get("title"), task.get("summary"))):
            continue
        if not work_confirmed(task, kind) or is_auth_notification(task.get("title") or ""):
            continue
        if task["id"] not in changed_ids and stage != "진행중":
            continue
        done = stage == "처리완료"
        source_ids = [m["id"] for m in topic["messages"] if m.get("direction", "inbound") == "inbound"][:5] if topic else [task.get("source_id")]
        title = task_title(task.get("title") or "(업무 제목 없음)", task.get("summary") or "")
        subjects = [task.get("title")] + [m.get("subject") for m in (topic or {}).get("messages", [])]
        points = summary_points(task.get("summary") or (topic or {}).get("latest_summary") or task.get("title"), subjects=subjects)
        period_end = (task.get("completed_at") or "")[:10] if done else task.get("due_date")
        row = rows.setdefault(category_name, {"category": category_name, "this_week": [], "next_week": [], "notes": []})
        row["this_week"].append(new_item(title, points, (task.get("created_at") or "")[:10] or None, period_end or None,
                                         source_ids, [task["id"]], done))
        if not done:
            due = task.get("due_date")
            # 차주 계획도 명사형으로 씁니다(예: '서버 점검 계속 진행', '서버 점검 착수 예정').
            core = re.sub(r"\s*(?:진행\s*)?(?:요청|필요|예정|중|완료)$", "", points[0]).strip() or title
            plan = core + (" 계속 진행" if stage == "진행중" else " 착수 예정")
            row["next_week"].append(new_item(title, [plan] + ([f"기한 {due}"] if due else []), next_start,
                                             due if due and next_start <= due <= next_end else next_end,
                                             source_ids, [task["id"]]))
    for row in rows.values():
        row["omitted"] = len(row["this_week"][topic_limit:])
        row["this_week"] = row["this_week"][:topic_limit]
        row["next_week"] = row["next_week"][:topic_limit]
        if row["omitted"]:
            row["notes"].append(f"그 외 {row['omitted']}개 업무는 내 업무에서 조회")
    return {"title": "ERP_" + end_date.strftime("%y%m%d"), "start": start, "end": end,
            "columns": ["구분", "업무 내용 (금주)", "차주", "비고"], "rows": list(rows.values()),
            "basis": "내 업무에서 확정된 업무(일반업무 확인완료 이상, 전자결재 처리완료)만 반영합니다. 차주 계획은 금주 업무를 바탕으로 만든 초안이며 항목별로 수정·삭제할 수 있습니다.",
            "completed_count": completed_count, "port": int(cfg.get("port", 18765))}


def period_text(item):
    if not item.get("start") and not item.get("end"):
        return "기간 미정"
    return f"{item.get('start') or '미정'} ~ {item.get('end') or '미정'}"


def markdown(document):
    lines = ["# " + document["title"], "", f"보고 기간: {document['start']} ~ {document['end']}", "", document["basis"], "",
             "| 구분 | 업무 내용 (금주) | 차주 | 비고 |", "|---|---|---|---|"]
    for row in document["rows"]:
        def items(values):
            return "<br>".join((f"**{v['title']}** ({period_text(v)})<br>" + "<br>".join("- " + p for p in v["points"])).replace("|", "\\|").replace("\n", " ") for v in values) or "해당 없음"
        refs = list(dict.fromkeys(s for item in row["this_week"] + row["next_week"] for s in item["source_ids"]))
        lines.append("| " + " | ".join([row["category"], items(row["this_week"]), items(row["next_week"]), "<br>".join(row["notes"] + refs)]) + " |")
    if document.get("narrative"):
        lines += ["", "## AI 보고 해설 — 확정 전 검토", document["narrative"]]
    return "\n".join(lines) + "\n"


def write_pdf(document, output):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, LongTable, TableStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    font, bold = "HYSMyeongJo-Medium", "HYSMyeongJo-Medium"
    regular = Path("C:/Windows/Fonts/malgun.ttf")
    strong = Path("C:/Windows/Fonts/malgunbd.ttf")
    if regular.exists():
        if "ReportKorean" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont("ReportKorean", str(regular)))
            pdfmetrics.registerFont(TTFont("ReportKoreanBold", str(strong if strong.exists() else regular)))
        font, bold = "ReportKorean", "ReportKoreanBold"
    elif font not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(font))
    body = ParagraphStyle("body", fontName=font, fontSize=8.2, leading=12, wordWrap="CJK", spaceAfter=7)
    heading = ParagraphStyle("heading", fontName=bold, fontSize=12, leading=17, alignment=1)
    title = ParagraphStyle("title", fontName=bold, fontSize=20, leading=28, spaceAfter=14)
    small = ParagraphStyle("small", fontName=font, fontSize=7, leading=10, wordWrap="CJK")
    def para(text, style=body):
        return Paragraph(escape(str(text)).replace("\n", "<br/>"), style)
    def cell(items, offset=1):
        flow = []
        for index, item in enumerate(items, offset):
            flow += [Paragraph(f"<b>{index}. {escape(item['title'])}</b>", body), para(period_text(item), small),
                     para("\n".join("- " + point for point in item["points"]))]
        return flow or [para("해당 없음")]
    data = [[para(column, heading) for column in document["columns"]]]
    # 토픽별로 행을 나눠 긴 분류에서도 행과 표 머리글이 겹치지 않게 합니다.
    for row in document["rows"]:
        count = max(1, len(row["this_week"]), len(row["next_week"]))
        for index in range(count):
            current = row["this_week"][index:index+1]
            following = row["next_week"][index:index+1]
            refs = list(dict.fromkeys(s for item in current + following for s in item["source_ids"]))
            notes = [para(note) for note in row["notes"]] if index == 0 else []
            for number, source in enumerate(refs[:3], 1):
                url = f"http://127.0.0.1:{document['port']}/#mail=" + quote(source, safe="")
                notes.append(Paragraph(f'<link href="{escape(url, quote=True)}" color="#2166b8">관련 메일 {number}</link>', body))
            if len(refs) > 3:
                notes.append(para(f"동일 토픽의 근거 메일 총 {len(refs)}건. 나머지는 앱에서 펼쳐 확인", small))
            data.append([para(row["category"] if index == 0 else "(계속)"), cell(current,index+1), cell(following,index+1), notes or [para("-")]])
    table = LongTable(data, colWidths=[52, 145, 145, 161], repeatRows=1, splitByRow=1, splitInRow=0, hAlign="LEFT")
    table.setStyle(TableStyle([("GRID",(0,0),(-1,-1),.5,colors.HexColor("#bec7d4")),("BACKGROUND",(0,0),(-1,0),colors.HexColor("#f0f2f5")),("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),5),("RIGHTPADDING",(0,0),(-1,-1),5),("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6)]))
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(output), pagesize=A4, leftMargin=46,rightMargin=46,topMargin=43,bottomMargin=38,title=document["title"],author="메일 브레인")
    def footer(canvas, doc):
        canvas.setFont(font, 7)
        canvas.setFillColor(colors.HexColor("#718096"))
        canvas.drawString(46, 24, f"{document['start']} - {document['end']} / 메일·업무 근거 기반 초안")
        canvas.drawRightString(A4[0]-46,24,str(doc.page))
    doc.build([para(document["title"],title),para(f"보고 기간: {document['start']} ~ {document['end']}",small),para(document["basis"],small),Spacer(1,14),table],onFirstPage=footer,onLaterPages=footer)
    return output
