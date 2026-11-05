import json
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

from .core import ROOT, dump
from .demo import messages
from .sync import SyncManager


class LocalHTTPServer(HTTPServer):
    # Windows에서 같은 포트의 이전 서버와 겹쳐 실행되는 것을 막습니다.
    allow_reuse_address = False


def serve(cfg, store, pipeline):
    token = secrets.token_urlsafe(32)
    port = int(cfg.get("port", 18765))
    sync = SyncManager(cfg, store, pipeline)
    from .solutions import Solutions
    solutions = Solutions(cfg, store)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # 검색어·메일 식별자를 액세스 로그에 출력하지 않습니다.

        def reply(self, payload, status=200, kind="application/json; charset=utf-8"):
            body = payload if isinstance(payload, bytes) else payload.encode() if isinstance(payload, str) else dump(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def host_ok(self):
            return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

        def do_GET(self):
            if not self.host_ok():
                return self.reply({"error": "허용하지 않는 호스트입니다."}, 403)
            route = urlparse(self.path)
            if route.path == "/":
                html = (ROOT / "static/index.html").read_text(encoding="utf-8").replace("__CSRF_TOKEN__", token)
                return self.reply(html, kind="text/html; charset=utf-8")
            if route.path == "/app.css":
                return self.reply((ROOT / "static/app.css").read_text(encoding="utf-8"), kind="text/css; charset=utf-8")
            if route.path == "/app.js":
                return self.reply((ROOT / "static/app.js").read_text(encoding="utf-8"), kind="text/javascript; charset=utf-8")
            if route.path == "/topics.js":
                return self.reply((ROOT / "static/topics.js").read_text(encoding="utf-8"), kind="text/javascript; charset=utf-8")
            if route.path == "/tasks.js":
                return self.reply((ROOT / "static/tasks.js").read_text(encoding="utf-8"), kind="text/javascript; charset=utf-8")
            if route.path == "/solutions.js":
                return self.reply((ROOT / "static/solutions.js").read_text(encoding="utf-8"), kind="text/javascript; charset=utf-8")
            if self.headers.get("X-App-Token") != token:
                return self.reply({"error": "앱 화면을 새로고침하세요."}, 403)
            if route.path == "/api/state":
                return self.reply({**store.state(), "engine": cfg["engine"], "user_email": cfg["user_email"], "windows_account": cfg.get("windows_account"), "sync": sync.snapshot()})
            if route.path == "/api/sync":
                return self.reply(sync.snapshot())
            if route.path == "/api/tasks":
                params = parse_qs(route.query)
                try:
                    filters = {key: params.get(key, [default])[0] for key, default in (("status",""),("query",""),("project",""),("start",""),("end",""),("scope","active"))}
                    return self.reply(store.list_tasks(**filters))
                except ValueError as exc:
                    return self.reply({"error": str(exc)}, 400)
            if route.path == "/api/topic":
                from .topics import Topics
                identifier = parse_qs(route.query).get("id", [""])[0]
                topic = Topics(store).get(identifier)
                return self.reply(topic if topic else {"error": "토픽이 없습니다."}, 200 if topic else 404)
            if route.path == "/api/report-file":
                params = parse_qs(route.query)
                identifier = params.get("id", [""])[0]
                rows = store.rows("SELECT * FROM reports WHERE id=?", (identifier,))
                if not rows:
                    return self.reply({"error": "보고서가 없습니다."}, 404)
                file = store.folder / "reports" / (rows[0]["id"] + ".pdf")
                if not file.exists():
                    return self.reply({"error": "이 보고서는 PDF 생성 이전 버전입니다. 다시 생성하세요."}, 404)
                return self.reply(file.read_bytes(), kind="application/pdf")
            if route.path == "/api/message":
                ident = parse_qs(route.query).get("id", [""])[0]
                rows = store.rows("SELECT * FROM messages WHERE id=?", (ident,))
                return self.reply(rows[0] if rows else {"error": "메일이 없습니다."}, 200 if rows else 404)
            return self.reply({"error": "없는 경로입니다."}, 404)

        def do_POST(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 5_000_000:
                    return self.reply({"error": "요청 크기 제한은 5MB입니다."}, 400)
                raw_body = self.rfile.read(size)
            except ValueError:
                return self.reply({"error": "요청 크기 형식이 올바르지 않습니다."}, 400)
            if not self.host_ok() or self.headers.get("X-App-Token") != token:
                return self.reply({"error": "잘못된 앱 요청입니다."}, 403)
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
                return self.reply({"error": "외부 출처 요청을 거부했습니다."}, 403)
            try:
                data = json.loads(raw_body)
                if not isinstance(data, dict):
                    raise ValueError("JSON 객체가 필요합니다.")
                if self.path == "/api/demo":
                    result = pipeline.ingest(messages(cfg["user_email"]))
                elif self.path == "/api/sync":
                    result = sync.submit(data.get("start"), data.get("end"))
                elif self.path == "/api/outlook/probe":
                    result = sync.submit(probe=True)
                elif self.path == "/api/import":
                    if not isinstance(data.get("messages"), list) or len(data["messages"]) > 500:
                        raise ValueError("최대 500개 메일 배열이 필요합니다.")
                    result = pipeline.ingest(data["messages"])
                elif self.path == "/api/task":
                    store.edit_task(data["id"], data["changes"])
                    pipeline.wiki()
                    result = {"ok": True}
                elif self.path == "/api/solution":
                    result = solutions.generate(data.get("topic_id"), data.get("task_id"), data.get("regenerate", False))
                elif self.path == "/api/task/delete":
                    store.delete_task(data["id"])
                    pipeline.wiki()
                    result = {"ok": True}
                elif self.path == "/api/task/restore":
                    store.restore_task(data["id"])
                    pipeline.wiki()
                    result = {"ok": True}
                elif self.path == "/api/resolve":
                    if not isinstance(data.get("accept"), bool):
                        raise ValueError("승인 여부는 참/거짓이어야 합니다.")
                    store.resolve(data["id"], data["accept"], data.get("target_id"))
                    pipeline.wiki()
                    result = {"ok": True}
                elif self.path == "/api/search":
                    result = pipeline.search(data["query"])
                elif self.path == "/api/history":
                    result = pipeline.history(data.get("query", ""), data.get("start") or None, data.get("end") or None, data.get("topic_id") or None)
                elif self.path == "/api/report":
                    result = pipeline.report(data["start"], data["end"])
                else:
                    return self.reply({"error": "없는 경로입니다."}, 404)
                return self.reply(result)
            except (ValueError, KeyError, TypeError) as exc:
                return self.reply({"error": str(exc)}, 400)
            except Exception as exc:
                return self.reply({"error": "처리에 실패했습니다. 로컬 모델과 설정을 확인하세요.", "type": type(exc).__name__}, 500)

    server = LocalHTTPServer(("127.0.0.1", port), Handler)
    sync.start()
    print(f"메일 브레인: http://127.0.0.1:{port}  / 종료: Ctrl+C")
    print(f"분석 모드: {cfg['engine']} / 로컬 사용자: {cfg['user_email']}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        sync.close()
        server.server_close()
