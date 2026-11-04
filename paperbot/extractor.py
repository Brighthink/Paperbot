"""PDF download + text extraction (figure rendering lives in figures.py).
PDF 다운로드 + 텍스트 추출 (figure 렌더링은 figures.py).

- 다운로드: 후보 URL 폴백 체인(iter_candidate_urls), 재시도 2회, 타임아웃 60s.
- 추출: pymupdf. References 섹션 이후 제거(휴리스틱).
- max_input_chars 초과 시 앞 70% + 뒤 30%를 남기고 중간 절단.
- PDF 본문을 확보하지 못하면 abstract를 반환하고, abstract도 없으면 제목을 표시용 입력으로 반환
  (abstract_only=True) → 상위에서 내용 미확보 안내를 저장.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import re
import tempfile
import time
import urllib.request
from dataclasses import dataclass

import pymupdf

from .config import Config
from .figures import Figure, extract_figures
from .models import Paper

logger = logging.getLogger(__name__)

_TRUNC_MARK = "\n\n[... truncated ...]\n\n"
_REFERENCE_HEADINGS = {"references", "reference", "bibliography"}
_USER_AGENT = "paperbot/0.2 (paper summary bot)"
_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MIN_PDF_TEXT_CHARS = 300
_MIN_PDF_TEXT_WORDS = 40
_NON_PAPER_MARKERS = (
    "please enable javascript",
    "enable javascript to continue",
    "checking your browser",
    "access denied",
    "verify you are human",
)


@dataclass
class ExtractResult:
    text: str                                        # 요약기에 넣는 본문 (max_input_chars로 절단)
    abstract_only: bool                              # True면 PDF 본문 대신 abstract 또는 메타데이터 안내를 사용
    figures: list[Figure] = dataclasses.field(default_factory=list)  # 대표 그림·표 크롭 — Markdown 저장 및 게시용
    full_text: str = ""                              # 절단 전 전체 본문 (참고문헌 제거) — 챗봇 캐시(chatdb)에 저장


def _pdf_path(cfg: Config, paper: Paper) -> str:
    pdf_dir = cfg.resolve(cfg.storage.pdf_dir)
    os.makedirs(pdf_dir, exist_ok=True)
    safe_id = _SAFE_RE.sub("_", paper.uid)
    return os.path.join(pdf_dir, f"{safe_id}.pdf")


def _cleanup_pdf(cfg: Config, paper: Paper) -> None:
    """보관 설정이 꺼져 있으면 논문의 임시 PDF를 정리한다."""
    if cfg.storage.keep_pdfs:
        return
    path = _pdf_path(cfg, paper)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError as e:
            logger.warning("임시 PDF 삭제 실패 (%s): %s", path, e)


def download_pdf(url: str, dest: str, *, retries: int = 2, timeout: int = 60) -> str:
    """유효한 PDF만 임시 파일에 받은 뒤 원자적으로 dest에 저장한다."""
    last_err: Exception | None = None
    for attempt in range(1, retries + 2):  # 최초 1회 + retries회
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            if not data:
                raise ValueError("빈 응답")
            if not data.startswith(b"%PDF-"):
                raise ValueError("응답에 PDF 헤더가 없습니다 (HTML 오류/로그인 페이지일 수 있음)")
            try:
                with pymupdf.open(stream=data, filetype="pdf") as doc:
                    if doc.page_count < 1:
                        raise ValueError("페이지가 없는 PDF 응답")
            except Exception as e:  # noqa: BLE001 — HTML이나 잘린 응답은 저장하지 않는다.
                raise ValueError(f"응답을 유효한 PDF로 열 수 없습니다: {e}") from e

            dest_dir = os.path.dirname(os.path.abspath(dest))
            fd, temporary_path = tempfile.mkstemp(prefix=".paperbot-pdf-", suffix=".part", dir=dest_dir)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                os.replace(temporary_path, dest)
            except Exception:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass
                raise
            return dest
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("PDF 다운로드 실패 (%d/%d): %s", attempt, retries + 1, e)
            if attempt <= retries:
                time.sleep(2 * attempt)
    raise RuntimeError(f"PDF 다운로드 최종 실패: {url}") from last_err


def extract_text(pdf_path: str) -> str:
    """PDF 형식으로 열어 전체 텍스트를 추출한다 (HTML 자동 판독은 허용하지 않음)."""
    with open(pdf_path, "rb") as stream:
        header = stream.read(8)
    if not header.startswith(b"%PDF-"):
        raise ValueError("파일 시작에 PDF 헤더가 없습니다 (HTML 오류/로그인 페이지일 수 있음)")
    parts: list[str] = []
    with pymupdf.open(pdf_path, filetype="pdf") as doc:
        for page in doc:
            parts.append(page.get_text())
    return "\n".join(parts)


def strip_references(text: str) -> str:
    """References/Bibliography 헤딩 이후를 잘라낸다(문서 후반부에서만).

    본문 중간의 'references' 단어 언급을 오탐하지 않도록,
    라인 전체가 헤딩인 경우만 대상으로 하고 문서의 30% 지점 이후만 자른다.
    """
    lines = text.splitlines()
    if not lines:
        return text

    cut_idx: int | None = None
    for i, ln in enumerate(lines):
        # 앞의 번호/점/공백 장식을 제거해 순수 헤딩만 비교.
        s = ln.strip().lower().strip("0123456789.)( \t").strip()
        if s in _REFERENCE_HEADINGS:
            cut_idx = i  # 마지막 등장 위치를 사용

    if cut_idx is not None and cut_idx > len(lines) * 0.3:
        return "\n".join(lines[:cut_idx])
    return text


def truncate(text: str, max_chars: int) -> str:
    """max_chars 초과 시 앞 70% + 뒤 30%만 남기고 중간을 절단."""
    if len(text) <= max_chars:
        return text
    budget = max_chars - len(_TRUNC_MARK)
    head_len = int(budget * 0.7)
    tail_len = budget - head_len
    return text[:head_len] + _TRUNC_MARK + text[-tail_len:]


def _normalize(text: str) -> str:
    # 과도한 빈 줄 축소 (토큰 낭비 방지).
    lines = [ln.rstrip() for ln in text.splitlines()]
    out: list[str] = []
    blank = 0
    for ln in lines:
        if ln.strip():
            blank = 0
            out.append(ln)
        else:
            blank += 1
            if blank <= 1:
                out.append("")
    return "\n".join(out).strip()


def _abstract_fallback(paper: Paper) -> ExtractResult:
    return ExtractResult(text=(paper.abstract or "").strip() or paper.title, abstract_only=True)


def _usable_paper_text(text: str) -> bool:
    """Reject short publisher notices/empty first pages instead of calling them paper full text."""
    cleaned = " ".join(text.split())
    if len(cleaned) < _MIN_PDF_TEXT_CHARS:
        return False
    if len(re.findall(r"\b[\w'-]+\b", cleaned)) < _MIN_PDF_TEXT_WORDS:
        return False
    lowered = cleaned.casefold()
    if any(marker in lowered for marker in _NON_PAPER_MARKERS):
        return False
    return True


def _norm_title(t: str) -> str:
    """제목 매칭용 정규화 — 소문자 + 영숫자만."""
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())


def _unpaywall_pdf(doi: str, email: str) -> str | None:
    """Unpaywall에서 합법 OA PDF URL 조회 (없으면 None)."""
    try:
        import requests

        resp = requests.get(
            f"https://api.unpaywall.org/v2/{doi}",
            params={"email": email}, timeout=20,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        locations = [data.get("best_oa_location") or {}] + (data.get("oa_locations") or [])
        for loc in locations:
            if loc.get("url_for_pdf"):
                logger.info("Unpaywall에서 OA 사본 발견 (%s): %s", loc.get("host_type"), doi)
                return loc["url_for_pdf"]
    except Exception as e:  # noqa: BLE001
        logger.debug("Unpaywall 조회 실패(무시): %s", e)
    return None


def _arxiv_pdf_by_title(title: str, authors: list[str]) -> str | None:
    """S2가 arXiv 링크를 누락한 경우 — 제목 검색으로 프리프린트를 찾는다.

    오매칭 방지: 정규화 제목 완전 일치 + (저자 정보가 있으면) 1저자 성 포함 확인.
    """
    try:
        import arxiv

        client = arxiv.Client(page_size=5, delay_seconds=3.0, num_retries=2)
        search = arxiv.Search(query=f'ti:"{title}"', max_results=5)
        want = _norm_title(title)
        first_author_surname = ""
        if authors:
            first_author_surname = (authors[0].split()[-1] if authors[0].split() else "").lower()

        for r in client.results(search):
            if _norm_title(r.title) != want:
                continue
            if first_author_surname:
                result_authors = " ".join(a.name for a in r.authors).lower()
                if first_author_surname not in result_authors:
                    continue
            logger.info("arXiv 제목 검색으로 프리프린트 발견: %s", r.get_short_id())
            return r.pdf_url
    except Exception as e:  # noqa: BLE001
        logger.debug("arXiv 제목 검색 실패(무시): %s", e)
    return None


def iter_candidate_urls(paper: Paper, unpaywall_email: str = ""):
    """PDF 후보 URL 생성기 — 순서대로 시도, 뒤의 조회형 후보는 필요할 때만 실행.
    (zoterobot도 이 체인을 쓰므로 Config 없이 호출 가능하게 둔다)

    1) S2/arXiv가 준 직접 링크 (출판사 링크는 봇 차단으로 403이 잦음)
    2) arXiv 미러 (id가 있을 때)
    3) Unpaywall 합법 OA 사본 (DOI + 이메일 설정 시 — 실패 시에만 조회)
    4) arXiv 제목 검색 (DOI는 있는데 arXiv 링크가 누락된 출판사 논문 구조용)
    """
    if paper.pdf_url:
        yield paper.pdf_url
    if paper.arxiv_id:
        ax = f"https://arxiv.org/pdf/{paper.arxiv_id}"
        if ax != paper.pdf_url:
            yield ax
    if paper.doi:  # 조회형 후보는 출판사 논문(DOI 보유)에만 의미가 있다
        if unpaywall_email:
            url = _unpaywall_pdf(paper.doi, unpaywall_email)
            if url and url != paper.pdf_url:
                yield url
        if not paper.arxiv_id:
            url = _arxiv_pdf_by_title(paper.title, paper.authors)
            if url:
                yield url


def _download_any(urls, dest: str) -> str:
    """PDF 형식과 본문 텍스트 품질을 확인하고 첫 논문 PDF의 텍스트를 반환."""
    last_err: Exception | None = None
    for url in urls:
        try:
            download_pdf(url, dest, retries=2, timeout=60)
            text = extract_text(dest)
            if not _usable_paper_text(text):
                raise ValueError(f"PDF에서 논문 본문 텍스트를 충분히 추출하지 못했습니다 ({len(text.strip())}자)")
            return text
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("PDF 후보 다운로드 또는 판독 실패, 다음 후보 시도: %s (%s)", url, e)
            try:
                os.remove(dest)
            except FileNotFoundError:
                pass
            except OSError as cleanup_error:
                logger.warning("잘못 받은 PDF 임시 파일 삭제 실패 (%s): %s", dest, cleanup_error)
    raise RuntimeError("모든 PDF 후보 다운로드 또는 판독 실패") from last_err


def extract(cfg: Config, paper: Paper, figures: bool = True) -> ExtractResult:
    """논문 하나에 대한 추출 파이프라인. 실패해도 예외 대신 abstract fallback.

    figures=False면 figure 렌더링을 생략한다(백필용).
    """
    dest = _pdf_path(cfg, paper)
    try:
        raw = ""
        valid_existing_pdf = False
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            try:
                raw = extract_text(dest)
                valid_existing_pdf = True
            except Exception as e:  # noqa: BLE001 — 남은 임시 PDF가 손상됐을 수 있음
                logger.warning("기존 PDF를 읽지 못해 다시 받습니다 %s: %s", paper.uid, e)
                try:
                    os.remove(dest)
                except OSError:
                    pass
        if valid_existing_pdf and raw.strip() and not _usable_paper_text(raw):
            logger.warning("기존 PDF 텍스트가 너무 짧거나 논문 본문이 아니어서 다시 찾습니다: %s", paper.uid)
            try:
                os.remove(dest)
            except OSError:
                pass
            raw = ""
            valid_existing_pdf = False
        if not raw.strip():
            if valid_existing_pdf:
                # 스캔 PDF처럼 문서는 열리지만 텍스트 레이어가 없을 수 있다.
                logger.warning("PDF에서 텍스트를 추출하지 못해 초록으로 요약합니다: %s", paper.uid)
                return _abstract_fallback(paper)
            raw = _download_any(iter_candidate_urls(paper, cfg.fetch.unpaywall_email), dest)
        if not _usable_paper_text(raw):
            logger.warning(
                "PDF에서 논문 본문을 충분히 추출하지 못해 초록으로 요약합니다: %s (chars=%d)",
                paper.uid,
                len(raw.strip()),
            )
            return _abstract_fallback(paper)

        full = _normalize(strip_references(raw))
        text = truncate(full, cfg.llm.max_input_chars)

        # Markdown 저장 또는 선택된 게시처에서 그림·표를 쓸 때만 추출해 렌더링 비용을 줄인다.
        attach, max_figs, max_tabs = cfg.figures_for(paper.topic_name)
        figs: list[Figure] = []
        if attach and figures:
            figs = extract_figures(dest, max_figures=max_figs, max_tables=max_tabs)
        logger.info("추출 완료 %s: %d자(전체 %d자), figure %d·표 %d개", paper.uid, len(text), len(full),
                    sum(1 for f in figs if f.kind == "figure"), sum(1 for f in figs if f.kind == "table"))
        return ExtractResult(text=text, abstract_only=False, figures=figs, full_text=full)

    except Exception as e:  # noqa: BLE001 — abstract로 폴백
        logger.error("PDF 처리 실패 %s (abstract로 폴백): %s", paper.uid, e)
        return _abstract_fallback(paper)
    finally:
        _cleanup_pdf(cfg, paper)
