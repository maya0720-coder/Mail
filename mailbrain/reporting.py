"""사용자 제공 ERP 주간보고의 4열 구조를 따르는 출력기."""
from datetime import date, timedelta
from html import escape
from pathlib import Path
from urllib.parse import quote

from .topics import Topics, CATEGORIES, category
import re


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


def build_document(store, cfg, start, end, snapshot, changes, completed_count):
    topics = Topics(store).build()
    rows = {name: {"category": name, "this_week": [], "next_week": [], "notes": []} for name in CATEGORIES}
    end_date = date.fromisoformat(end)
    mail_topics = {}
    for topic in topics:
        for message in topic["messages"]:
            mail_topics[message["id"]] = topic
        messages = [m for m in topic["messages"] if start <= m["received_at"][:10] <= end and not is_auth_notification(m["subject"])]
        if not messages:
            continue
        newest = messages[0]
        rows[topic["category"]]["this_week"].append({"title": topic["title"], "text": newest["summary"],
            "status": "수신 내용 · 업무 확정 전", "date": newest["received_at"][:10], "source_ids": [m["id"] for m in messages], "task_ids": []})
    topic_limit = max(1, min(30, int(cfg.get("report_topics_per_category", 8))))
    for row in rows.values():
        row["total_mail_topics"] = len(row["this_week"])
        row["omitted_topics"] = row["this_week"][topic_limit:]
        row["this_week"] = row["this_week"][:topic_limit]
        for item in row["this_week"]:
            item["text"] = report_excerpt(item["text"])
        if row["omitted_topics"]:
            row["notes"].append(f"그 외 {len(row['omitted_topics'])}개 토픽은 업무 히스토리에서 조회")
    changed_ids = {event["task_id"] for event in changes}
    for task in snapshot.values():
        topic = mail_topics.get(task["source_id"])
        row = rows[topic["category"] if topic else category(task["project"] + " " + task["title"])]
        item = {"title": task["title"], "text": f"상태: {task['status']} / 기한: {task['due_date'] or '미정'}",
                "status": task["status"], "source_ids": [task["source_id"]], "task_ids": [task["id"]], "date": task["updated_at"][:10]}
        if task["id"] in changed_ids:
            row["this_week"].append(item)
        if task["status"] not in {"완료", "취소"} and task["due_date"] and end < task["due_date"] <= (end_date + timedelta(days=7)).isoformat():
            row["next_week"].append(item)
    for row in rows.values():
        if not row["next_week"] and row["this_week"]:
            row["notes"].append("차주 계획은 확인 후 확정 필요")
    return {"title": "ERP_" + end_date.strftime("%y%m%d"), "start": start, "end": end,
            "columns": ["구분", "업무 내용 (금주)", "차주", "비고"], "rows": list(rows.values()),
            "basis": f"분류별 최근 {topic_limit}개 메일 토픽 요약 + 승인된 업무 이력. 수신 내용은 성과가 아니며, 완료는 승인 상태로만 집계합니다.",
            "completed_count": completed_count, "port": int(cfg.get("port", 18765))}


def markdown(document):
    lines = ["# " + document["title"], "", f"보고 기간: {document['start']} ~ {document['end']}", "", document["basis"], "",
             "| 구분 | 업무 내용 (금주) | 차주 | 비고 |", "|---|---|---|---|"]
    for row in document["rows"]:
        def items(values):
            return "<br>".join((v["title"] + " / " + v["status"] + " / " + v["text"]).replace("|", "\\|").replace("\n", " ") for v in values) or "해당 없음"
        refs = list(dict.fromkeys(s for item in row["this_week"] + row["next_week"] for s in item["source_ids"]))
        lines.append("| " + " | ".join([row["category"], items(row["this_week"]), items(row["next_week"]), "<br>".join(row["notes"] + refs)]) + " |")
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
            flow += [Paragraph(f"<b>{index}. {escape(item['title'])}</b>", body), para(item["date"] + " / " + item["status"], small), para(item["text"])]
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
