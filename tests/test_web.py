import json
import re
import socket
import tempfile
import threading
import unittest
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError
from unittest.mock import patch
from http.server import ThreadingHTTPServer

from mailbrain.core import Store, Pipeline
from mailbrain.web import serve


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            cls.port = sock.getsockname()[1]
        cfg = {"user_email": "me@example.com", "aliases": [], "engine": "rules", "port": cls.port}
        cls.store = Store(cls.temp.name)
        cls.ready = threading.Event()

        def factory(*args, **kwargs):
            cls.server = ThreadingHTTPServer(*args, **kwargs)
            cls.ready.set()
            return cls.server

        def target():
            with patch("mailbrain.web.LocalHTTPServer", side_effect=factory):
                serve(cfg, cls.store, Pipeline(cfg, cls.store))

        cls.thread = threading.Thread(target=target, daemon=True)
        cls.thread.start()
        if not cls.ready.wait(5):
            raise RuntimeError("테스트 서버가 시작되지 않았습니다.")
        cls.url = f"http://127.0.0.1:{cls.port}"
        cls.opener = build_opener(ProxyHandler({}))
        with cls.opener.open(cls.url, timeout=5) as response:
            cls.html = response.read().decode()
        cls.token = re.search(r"const TOKEN='([^']+)'", cls.html).group(1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(5)
        cls.temp.cleanup()

    def request(self, path, data=None, headers=None):
        req = Request(self.url + path, data=json.dumps(data).encode() if data is not None else None,
                      headers=headers or {})
        return self.opener.open(req, timeout=5)

    def test_api_requires_token(self):
        with self.assertRaises(HTTPError) as error:
            self.request("/api/state")
        self.assertEqual(error.exception.code, 403)

    def test_health_identifies_same_app_without_revealing_account(self):
        from mailbrain.launcher import running_instance
        with self.request('/healthz') as response:health=json.load(response)
        self.assertEqual(health['app'],'mailbrain')
        self.assertTrue(running_instance(self.port,health))
        self.assertFalse(running_instance(self.port,{**health,'instance':'different'}))
        self.assertNotIn('me@example.com',json.dumps(health))

    def test_solution_does_not_block_other_screen_requests(self):
        from mailbrain.solutions import Solutions
        entered,release=threading.Event(),threading.Event()
        failures=[]
        def slow(*args,**kwargs):
            entered.set()
            release.wait(4)
            return {'ok':True}
        def call():
            try:
                with self.request('/api/solution',{}, {'X-App-Token':self.token}) as response:json.load(response)
            except Exception as exc:failures.append(exc)
        with patch.object(Solutions,'generate',side_effect=slow):
            thread=threading.Thread(target=call)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                with self.request('/api/state',headers={'X-App-Token':self.token}) as response:
                    self.assertIn('tasks',json.load(response))
                self.assertFalse(release.is_set())
            finally:
                release.set()
                thread.join(5)
        self.assertEqual(failures,[])

    def test_host_boundary(self):
        with self.assertRaises(HTTPError) as error:
            self.request("/", headers={"Host": "evil.example"})
        self.assertEqual(error.exception.code, 403)

    def test_public_assets_and_sync_status(self):
        self.assertIn('id="task-list"', self.html)
        for path, kind in [("/app.css", "text/css"), ("/app.js", "text/javascript"), ("/topics.js", "text/javascript"), ("/tasks.js", "text/javascript"), ("/solutions.js", "text/javascript")]:
            with self.request(path) as response:
                self.assertIn(kind, response.headers["Content-Type"])
                self.assertGreater(len(response.read()), 100)
        with self.request("/api/sync", headers={"X-App-Token": self.token}) as response:
            status = json.load(response)
        self.assertFalse(status["auto_enabled"])

    def test_collection_range_requires_both_dates(self):
        with self.assertRaises(HTTPError) as error:
            self.request("/api/sync", {"start": "2026-09-01"}, {"X-App-Token": self.token})
        self.assertEqual(error.exception.code, 400)

    def test_cross_origin_write_rejected(self):
        with self.assertRaises(HTTPError) as error:
            self.request("/api/demo", {}, {"X-App-Token": self.token, "Origin": "https://evil.example"})
        self.assertEqual(error.exception.code, 403)

    def test_valid_demo_and_invalid_input(self):
        with self.request("/api/demo", {}, {"X-App-Token": self.token}) as response:
            result = json.load(response)
        self.assertEqual(result["imported"]+result["duplicates"], 5)
        with self.request("/api/state", headers={"X-App-Token": self.token}) as response:
            state = json.load(response)
        self.assertEqual(len(state["proposals"]), 2)
        with self.assertRaises(HTTPError) as error:
            self.request("/api/import", {"messages": "잘못된 형식"}, {"X-App-Token": self.token})
        self.assertEqual(error.exception.code, 400)

    def test_topic_and_daily_history_endpoints(self):
        headers={"X-App-Token":self.token}
        with self.request('/api/demo',{},headers) as response:
            json.load(response)
        with self.request('/api/state',headers=headers) as response:
            state=json.load(response)
        group=next(g for g in state['topics'] if g['message_count']==2)
        with self.request('/api/topic?id='+group['id'],headers=headers) as response:
            detail=json.load(response)
        self.assertEqual(len(detail['messages']),2)
        with self.request('/api/history',{'query':'협력사'},headers) as response:
            history=json.load(response)
        self.assertGreater(history['evidence_count'],0)
        self.assertIn('RAG',history['mode'])

    def test_auto_task_list_filters_and_history_work_together(self):
        from urllib.parse import urlencode
        headers={"X-App-Token":self.token}
        with self.request('/api/demo',{},headers) as response:json.load(response)
        with self.request('/api/tasks',headers=headers) as response:all_tasks=json.load(response)
        auto_tasks=[task for task in all_tasks['tasks'] if task['auto_generated']]
        self.assertTrue(auto_tasks)
        self.assertTrue(all(task['summary'] for task in auto_tasks))
        self.assertTrue(all(task['topic_summary'] and task['thread_messages'] for task in auto_tasks))
        status=all_tasks['states'][0]
        query=urlencode({'status':status})
        with self.request('/api/tasks?'+query,headers=headers) as response:filtered=json.load(response)
        self.assertEqual(filtered['filtered_count'],len(filtered['tasks']))
        with self.request('/api/history',{'query':''},headers) as response:history=json.load(response)
        self.assertIn('evidence_count',history)

    def test_report_pdf_endpoint_and_unknown_id(self):
        from datetime import datetime
        from mailbrain.core import KST
        today=datetime.now(KST).date().isoformat()
        headers={"X-App-Token":self.token}
        with self.request('/api/report',{'start':today,'end':today},headers) as response:
            report=json.load(response)
        with self.request('/api/report-file?id='+report['id'],headers=headers) as response:
            self.assertTrue(response.read().startswith(b'%PDF'))
        with self.assertRaises(HTTPError) as error:
            self.request('/api/report-file?id=unknown',headers=headers)
        self.assertEqual(error.exception.code,404)

    def test_solution_endpoint_requires_auth_and_returns_evidence(self):
        headers={"X-App-Token":self.token}
        with self.request('/api/demo',{},headers) as response:json.load(response)
        with self.request('/api/state',headers=headers) as response:state=json.load(response)
        group=next(g for g in state['topics'] if g['message_count']==2)
        with self.assertRaises(HTTPError) as error:
            self.request('/api/solution',{'topic_id':group['id']})
        self.assertEqual(error.exception.code,403)
        with self.request('/api/solution',{'topic_id':group['id']},headers) as response:result=json.load(response)
        self.assertEqual(len(result['evidence']),1)
        self.assertIn('최신 메일 요약 1건',result['scope'])
        self.assertGreater(len(result['steps']),0)
        with self.assertRaises(HTTPError) as error:
            self.request('/api/solution',{'topic_id':'missing'},headers)
        self.assertEqual(error.exception.code,400)

    def test_task_status_delete_restore_api(self):
        from urllib.parse import urlencode
        from mailbrain.demo import messages
        headers={'X-App-Token':self.token}
        with self.request('/api/demo',{},headers) as response:json.load(response)
        mail={**messages('me@example.com')[0],'id':'api-task-case','thread_id':'api-task-thread','subject':'[API] 상태 관리 테스트'}
        with self.request('/api/import',{'messages':[mail]},headers) as response:json.load(response)
        with self.request('/api/state',headers=headers) as response:state=json.load(response)
        proposal=next(p for p in state['proposals'] if p['source_id']=='api-task-case')
        with self.request('/api/resolve',{'id':proposal['id'],'accept':True},headers) as response:json.load(response)
        with self.request('/api/tasks',headers=headers) as response:tasks=json.load(response)['tasks']
        task=tasks[0]
        with self.request('/api/task',{'id':task['id'],'changes':{'status':'완료'}},headers) as response:json.load(response)
        with self.request('/api/tasks?'+urlencode({'status':'완료'}),headers=headers) as response:completed=json.load(response)['tasks']
        self.assertTrue(next(t for t in completed if t['id']==task['id'])['completed_at'])
        with self.request('/api/task/delete',{'id':task['id']},headers) as response:json.load(response)
        with self.request('/api/tasks?scope=deleted',headers=headers) as response:deleted=json.load(response)['tasks']
        self.assertIn(task['id'],[t['id'] for t in deleted])
        with self.request('/api/task/restore',{'id':task['id']},headers) as response:json.load(response)
        with self.request('/api/tasks',headers=headers) as response:restored=json.load(response)['tasks']
        self.assertIn(task['id'],[t['id'] for t in restored])


if __name__ == "__main__":
    unittest.main()
