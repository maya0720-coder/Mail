"""로컬 청크 색인 + BM25 검색 + 근거 요약/선택적 LLM 생성."""
import hashlib
import json
import re
from collections import defaultdict
from datetime import date

from .core import dump, Engine, canonical_status
from .topics import Topics, new_body

STOP = {"언제", "어떻게", "왜", "알려줘", "알려주세요", "찾아줘", "확인해줘", "내역", "히스토리", "업무", "내용", "대한", "관련", "있어", "있나요"}
SYNONYMS = [("카피", "복제", "복사", "copy"), ("지연", "연기", "늦어", "늦은"), ("완료", "배포", "완료됨")]


def query_terms(query):
    words = re.findall(r"[a-zA-Z0-9가-힣]+", query.lower())
    terms = []
    for word in words:
        if word in STOP:
            continue
        terms.append(word)
        if re.search(r"[가-힣]", word):
            terms.extend(word[i:i+2] for i in range(len(word)-1))
    for group in SYNONYMS:
        if any(term in query.lower() for term in group):
            terms.extend(group)
    return list(dict.fromkeys(terms))[:60]


def tokens(text):
    result = []
    for word in re.findall(r"[a-zA-Z0-9가-힣]+", text.lower()):
        result.append(word)
        if re.search(r"[가-힣]", word):
            result.extend(word[i:i+2] for i in range(len(word)-1))
    return " ".join(result)


def chunks(text):
    # 겹침 100자. 인용된 과거 본문은 새 이벤트로 중복 색인하지 않습니다.
    text = new_body(text)
    return [text[i:i+900] for i in range(0, len(text), 800)] or [""]


class HistoryRAG:
    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store

    def index(self):
        groups = Topics(self.store).build()
        mapping = {m["id"]: g for g in groups for m in g["messages"]}
        messages = self.store.rows("SELECT * FROM messages WHERE excluded=0")
        events = self.store.rows("SELECT * FROM events ORDER BY seq")
        stamp = hashlib.sha256(("rag-v3:" + dump([(m["id"], m["received_at"], m["summary"]) for m in messages] + [(e["seq"], e["at"]) for e in events])).encode()).hexdigest()
        with self.store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS rag_chunks(id INTEGER PRIMARY KEY, message_id TEXT, event_seq INTEGER, topic_id TEXT, day TEXT, title TEXT, content TEXT, source_time TEXT);
                CREATE VIRTUAL TABLE IF NOT EXISTS rag_fts USING fts5(terms);
            """)
            current = db.execute("SELECT value FROM metadata WHERE key='rag_stamp'").fetchone()
            if current and current["value"] == stamp:
                return groups
            db.execute("DELETE FROM rag_chunks")
            db.execute("DELETE FROM rag_fts")
            def insert(message_id, event_seq, topic_id, day, title, content, source_time):
                cursor = db.execute("INSERT INTO rag_chunks(message_id,event_seq,topic_id,day,title,content,source_time) VALUES(?,?,?,?,?,?,?)", (message_id,event_seq,topic_id,day,title,content,source_time))
                db.execute("INSERT INTO rag_fts(rowid,terms) VALUES(?,?)", (cursor.lastrowid, tokens(title + " " + title + " " + content)))
            for mail in messages:
                group = mapping[mail["id"]]
                timestamp = mail["received_at"] or mail["sent_at"]
                for part in chunks(mail["body"]):
                    insert(mail["id"], None, group["id"], timestamp[:10], group["title"], part, timestamp)
            for event in events:
                after = json.loads(event["after_json"])
                before = json.loads(event["before_json"]) or {}
                source = event["source_id"] or after.get("source_id")
                topic = mapping.get(source, {}).get("id", "")
                content = f"{event['kind']}: {after['title']} / 상태 {canonical_status(before.get('status','없음'))} → {canonical_status(after['status'])} / 기한 {before.get('due_date') or '미정'} → {after.get('due_date') or '미정'} / 확인 주체 {event['actor']}"
                insert(source, event["seq"], topic, event["at"][:10], after["title"], content, event["at"])
            db.execute("INSERT INTO metadata(key,value) VALUES('rag_stamp',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (stamp,))
        return groups

    def search(self, query="", start=None, end=None, topic_id=None):
        if not isinstance(query, str) or len(query) > 300:
            raise ValueError("질문은 300자 이내로 입력하세요.")
        for value in (start, end):
            if value:
                date.fromisoformat(value)
        if start and end and start > end:
            raise ValueError("시작일은 종료일보다 늦을 수 없습니다.")
        groups = self.index()
        group_by_id = {g["id"]: g for g in groups}
        conditions, params = [], []
        for condition, value in [("c.day>=?", start), ("c.day<=?", end), ("c.topic_id=?", topic_id)]:
            if value:
                conditions.append(condition)
                params.append(value)
        where = " AND ".join(conditions) or "1=1"
        terms = query_terms(query)
        if query.strip() and not terms:
            hits = []
        elif terms:
            expression = " OR ".join('"' + term.replace('"','""') + '"' for term in terms)
            hits = self.store.rows(f"SELECT c.*,bm25(rag_fts) AS rank FROM rag_fts JOIN rag_chunks c ON c.id=rag_fts.rowid WHERE rag_fts MATCH ? AND {where} ORDER BY rank LIMIT 40", [expression, *params])
        else:
            hits = self.store.rows(f"SELECT c.*,0 AS rank FROM rag_chunks c WHERE {where} AND c.id IN (SELECT MIN(id) FROM rag_chunks GROUP BY message_id,event_seq) ORDER BY c.source_time DESC LIMIT 120", params)
        hit_topics = list(dict.fromkeys(h["topic_id"] for h in hits if h["topic_id"]))[:8]
        # 같은 토픽의 전후 메일을 추가해 일정 변경의 흐름을 연결합니다. 날짜 경계를 유지합니다.
        if terms and hit_topics:
            marks = ",".join("?" for _ in hit_topics)
            related = self.store.rows(f"SELECT c.*,0 AS rank FROM rag_chunks c WHERE c.topic_id IN ({marks}) AND {where} AND c.id IN (SELECT MIN(id) FROM rag_chunks GROUP BY message_id,event_seq) ORDER BY c.source_time DESC LIMIT 160", [*hit_topics, *params])
            hit_ids = {h["id"] for h in hits}
            hits.extend(h for h in related if h["id"] not in hit_ids)
        # 한 메일/이벤트당 가장 적합한 청크 한 개를 보여줍니다.
        unique = {}
        for hit in hits:
            key = (hit["message_id"], hit["event_seq"])
            if key not in unique:
                unique[key] = hit
        evidence = list(unique.values())[:120]
        citation_ids = set()
        daily = defaultdict(list)
        for hit in evidence:
            citation = "E" + str(hit["event_seq"]) if hit["event_seq"] is not None else "M" + str(hit["id"])
            citation_ids.add(citation)
            item = {"citation": citation, "message_id": hit["message_id"], "event_seq": hit["event_seq"],
                    "topic_id": hit["topic_id"], "topic": group_by_id.get(hit["topic_id"], {}).get("title", hit["title"]),
                    "time": hit["source_time"], "kind": "업무 변경" if hit["event_seq"] is not None else "메일 수신",
                    "quote": hit["content"][:900]}
            daily[hit["day"]].append(item)
        days = [{"date": day, "items": sorted(items, key=lambda x: x["time"], reverse=True)} for day, items in sorted(daily.items(), reverse=True)]
        # 추출형 모드도 답변은 검색된 근거에서만 구성합니다. 자유 추론은 하지 않습니다.
        answer = {"summary": f"{len(daily)}개 날짜에서 {len(evidence)}건의 근거를 찾았습니다." if evidence else "현재 수집된 자료에서 근거를 찾지 못했습니다.",
                  "facts": [{"text": h["content"][:220], "citations": ["E"+str(h["event_seq"]) if h["event_seq"] is not None else "M"+str(h["id"])]} for h in evidence[:6]],
                  "unknowns": ["수신 메일은 업무 수행 완료의 증명이 아닙니다. 완료 여부는 승인된 업무 이력을 확인하세요."] if evidence else []}
        generation = "근거 추출형"
        if query.strip() and evidence and self.cfg["engine"] == "ollama":
            generated = Engine(self.cfg).role("07_history", {"question": query, "evidence": [item for day in days for item in day["items"]][:35]})
            if not isinstance(generated.get("summary"), str) or not isinstance(generated.get("facts"), list):
                raise ValueError("RAG 답변 형식이 올바르지 않습니다.")
            for fact in generated["facts"]:
                if not isinstance(fact.get("text"), str) or not fact.get("citations") or not set(fact["citations"]) <= citation_ids:
                    raise ValueError("RAG 답변이 검색 근거에 없는 출처를 인용했습니다.")
            answer, generation = generated, "로컬 Ollama 생성형"
        return {"query": query, "answer": answer, "days": days, "evidence_count": len(evidence),
                "mode": "RAG · 한국어 부분어절 BM25 + 토픽 확장", "generation": generation,
                "topics": [{"id":g["id"],"title":g["title"]} for g in groups],
                "date_basis": "메일 수신일(기존 미확인 자료는 발송일 대체), 업무 변경일", "limit": 120}
