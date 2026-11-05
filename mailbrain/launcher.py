"""중복 실행은 기존 화면으로 연결하고, 타 프로그램 포트는 건드리지 않습니다."""
import errno
import hashlib
import json
import webbrowser
from urllib.request import ProxyHandler, build_opener

from .core import ROOT, Pipeline, Store, dump


def identity(cfg):
    settings = {k: v for k, v in cfg.items() if k != "port"}
    return hashlib.sha256(dump({"root": str(ROOT), "settings": settings}).encode()).hexdigest()


def revision():
    files = [ROOT / "app.py", ROOT / "main_prompt"]
    for folder, pattern in (("mailbrain", "*.py"), ("static", "*"), ("prompts", "*.md")):
        files += sorted(p for p in (ROOT / folder).glob(pattern) if p.is_file())
    digest = hashlib.sha256()
    for file in files:
        digest.update(file.name.encode())
        digest.update(file.read_bytes())
    return digest.hexdigest()


def running_instance(port, expected):
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=0.5) as response:
            data = json.loads(response.read(2048))
        return data == expected
    except Exception:
        return False


def address_in_use(error):
    return getattr(error, "winerror", None) == 10048 or error.errno in {errno.EADDRINUSE, 10048}


def launch(cfg, open_browser=False):
    from .web import serve
    first = int(cfg.get("port", 18765))
    if not 1024 <= first <= 65535:
        raise ValueError("port는 1024~65535 사이여야 합니다.")
    expected = {"app": "mailbrain", "instance": identity(cfg), "revision": revision()}
    store = pipeline = None
    for port in range(first, min(first + 20, 65536)):
        url = f"http://127.0.0.1:{port}"
        if running_instance(port, expected):
            print(f"이미 같은 메일 브레인이 실행 중입니다. 기존 화면: {url}")
            if open_browser:
                webbrowser.open(url)
            return {"url": url, "reused": True}
        if store is None:
            store = Store(cfg["data_dir"])
            pipeline = Pipeline(cfg, store)
        runtime = {**cfg, "port": port}
        def ready():
            if port != first:
                print(f"기본 포트 {first} 사용 중: 빈 포트 {port}에서 실행합니다. 설정 파일은 유지합니다.")
            if open_browser:
                webbrowser.open(url)
        try:
            serve(runtime, store, pipeline, on_ready=ready, health=expected)
            return {"url": url, "reused": False}
        except OSError as exc:
            if not address_in_use(exc):
                raise
    raise ValueError(f"포트 {first}~{min(first+19,65535)}가 모두 사용 중입니다. config.json의 port를 변경하세요. 다른 프로그램은 종료하지 않았습니다.")
