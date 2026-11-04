"""Command line entry for paperbot (`python -m paperbot`): args, logging, lockfile.
paperbot 명령줄 진입점 — 인자 파싱, 로깅, 중복 실행 방지 lockfile. 파이프라인 자체는 pipeline.py.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from .config import Config, ConfigError, load_config
from .pipeline import RunOptions, run
from .store import Store

logger = logging.getLogger("paperbot")

_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="paperbot — search papers, summarize them, and save Markdown files")
    p.add_argument("--config", default=os.path.join(_REPO_DIR, "config.yaml"),
                   help="config.yaml 경로 (기본: 리포 루트의 config.yaml)")
    p.add_argument("--dry-run", action="store_true", help="Markdown 파일 대신 stdout으로 미리보기 (DB에는 처리 완료로 기록)")
    p.add_argument("--limit", type=int, default=None, help="처리할 논문 수 제한 (테스트용)")
    p.add_argument("--topic", default=None, help="특정 topic만 처리 (config의 name)")
    p.add_argument("--backfill", action="store_true", help="lookback 필터를 끄고 검색 결과 전부 처리 (첫 실행용)")
    p.add_argument("--skip-filter", action="store_true", help="relevance 필터 없이 전부 요약 (디버그용)")
    p.add_argument("--backfill-chat", action="store_true",
                   help="DB의 done 논문 전문(+Zulip에 있는 요약)을 챗봇 캐시(storage.chat_text_db)에 채움 — PDF 재다운로드, GPU·발행 없음. "
                        "--limit/--topic 적용, 이미 본문이 있는 논문은 건너뜀")
    p.add_argument("--authorize-gmail", action="store_true",
                   help="브라우저에서 Gmail OAuth 승인을 한 번 진행하고 로컬 토큰을 저장")
    return p.parse_args(argv)


def setup_logging(cfg: Config) -> None:
    log_path = cfg.resolve("logs/paperbot.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console)


# ---------------------------------------------------------------- lockfile

def _lock_path(cfg: Config) -> str:
    data_dir = os.path.dirname(cfg.resolve(cfg.storage.db_path))
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, ".lock")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def acquire_lock(path: str) -> bool:
    """중복 실행 방지 lockfile 획득. 이미 살아있는 실행이 있으면 False."""
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                old_pid = int((f.read().strip() or "0"))
        except (ValueError, OSError):
            old_pid = 0
        if old_pid and _pid_alive(old_pid):
            logger.warning("이미 실행 중(pid=%s). 종료합니다.", old_pid)
            return False
        logger.warning("오래된 lockfile 발견(pid=%s, 미실행). 회수합니다.", old_pid)

    with open(path, "w") as f:
        f.write(str(os.getpid()))
    return True


def release_lock(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"[설정 오류] {e}", file=sys.stderr)
        return 2
    setup_logging(cfg)

    if args.authorize_gmail:
        from .gmail import GmailError, authorize_gmail
        try:
            authorize_gmail(cfg.gmail)
        except GmailError as e:
            logger.error("Gmail OAuth 설정 실패: %s", e)
            return 1
        logger.info("Gmail OAuth 승인 완료. 토큰은 config의 gmail.token_file에 저장했습니다.")
        return 0

    if args.backfill_chat:
        # 일반 실행과 lock을 공유하지 않는다 — 백필은 papers.db 이력만 읽으므로 동시에 실행해도 된다.
        from . import chatdb
        if not cfg.storage.chat_text_db:
            print("[설정 오류] storage.chat_text_db가 비어 있습니다.", file=sys.stderr)
            return 2
        with Store(cfg.resolve(cfg.storage.db_path)) as store:
            stats = chatdb.backfill(cfg, store, limit=args.limit, topic=args.topic)
        print(stats)
        return 0

    lock = _lock_path(cfg)
    if not acquire_lock(lock):
        return 0
    try:
        return run(cfg, RunOptions(dry_run=args.dry_run, limit=args.limit, topic=args.topic,
                                   backfill=args.backfill, skip_filter=args.skip_filter))
    finally:
        release_lock(lock)
