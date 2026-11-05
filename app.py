"""메일 브레인 CLI. Python 3.11 이상."""
import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from mailbrain.core import KST, Store, Pipeline, load_config


def main():
    parser = argparse.ArgumentParser(description="로컬 메일 업무 비서")
    parser.add_argument("--config", help="설정 JSON 경로")
    sub = parser.add_subparsers(dest="command", required=True)
    web = sub.add_parser("serve", help="로컬 웹 화면")
    web.add_argument("--open-browser", action="store_true", help="시작 후 기본 브라우저 열기")
    sub.add_parser("demo", help="가상 메일 5건 수집")
    sub.add_parser("wiki", help="프로젝트 위키 갱신")
    imp = sub.add_parser("import", help="JSON 또는 EML 파일/폴더 수집")
    imp.add_argument("path")
    collect = sub.add_parser("collect", help="본인 메일 수집")
    collect.add_argument("source", choices=["outlook", "groupware"])
    collect.add_argument("--preview", action="store_true", help="저장 없이 수집 건수만 확인")
    collect.add_argument("--start", help="과거 수집 시작일 YYYY-MM-DD, Outlook 전용")
    collect.add_argument("--end", help="과거 수집 종료일 YYYY-MM-DD, Outlook 전용")
    sub.add_parser("outlook-status", help="본인 Outlook 프로필 연결 확인")
    report = sub.add_parser("report", help="보고서 생성, 예약 작업에서 사용 가능")
    report.add_argument("--start")
    report.add_argument("--end")
    search = sub.add_parser("search")
    search.add_argument("query")
    args = parser.parse_args()
    cfg = load_config(args.config)
    if args.command == "serve":
        from mailbrain.launcher import launch
        launch(cfg, open_browser=args.open_browser)
        return
    store = Store(cfg["data_dir"])
    pipeline = Pipeline(cfg, store)
    if args.command == "demo":
        from mailbrain.demo import messages
        result = pipeline.ingest(messages(cfg["user_email"]))
    elif args.command == "import":
        from mailbrain.collectors import read_eml, read_json
        source = Path(args.path)
        files = sorted(source.glob("*.eml")) if source.is_dir() else [source]
        if not files:
            raise ValueError("가져올 EML 파일이 없습니다.")
        mails = []
        for file in files:
            if file.suffix.lower() == ".eml":
                mails.append(read_eml(file))
            elif file.suffix.lower() == ".json":
                mails.extend(read_json(file))
            else:
                raise ValueError("EML 또는 JSON만 지원합니다.")
        result = pipeline.ingest(mails)
    elif args.command == "collect":
        from mailbrain.collectors import outlook, groupware
        if args.source == "groupware" and (args.start or args.end):
            raise ValueError("기간 지정은 Outlook 수집에만 지원합니다.")
        known = {row["id"] for row in store.rows("SELECT id FROM messages")} if not args.preview else set()
        def metadata_update(identifier, received_at):
            with store.connect() as db:
                db.execute("UPDATE messages SET received_at=? WHERE id=? AND received_at IS NULL", (received_at, identifier))
        mails = outlook(cfg, args.start, args.end, known, metadata_update=metadata_update) if args.source == "outlook" else groupware(cfg)
        result = {"preview_count": sum(1 for _ in mails), "stored": False} if args.preview else pipeline.ingest(mails)
    elif args.command == "outlook-status":
        from mailbrain.collectors import outlook_probe
        result = outlook_probe(cfg)
    elif args.command == "search":
        result = pipeline.search(args.query)
    elif args.command == "report":
        today = datetime.now(KST).date()
        result = pipeline.report(args.start or (today - timedelta(days=6)).isoformat(), args.end or today.isoformat())
    else:
        pipeline.wiki()
        result = {"ok": True}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"실행 실패: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
