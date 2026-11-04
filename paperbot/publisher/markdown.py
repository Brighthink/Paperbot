"""Save a Markdown summary and extracted figure/table files in a per-paper folder.
논문별 폴더에 요약 Markdown과 추출한 그림·표 파일을 저장한다.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile

from ..figures import Figure, label_slug
from ..models import Paper
from .base import Publisher

logger = logging.getLogger(__name__)


def _stem(paper: Paper) -> str:
    """Use a readable, filesystem-safe title plus a stable uid suffix."""
    title = " ".join(paper.title.split())
    slug = re.sub(r"[^\w.-]+", "-", title, flags=re.UNICODE).strip(".-_")[:64].rstrip(".-_")
    suffix = hashlib.sha256(paper.uid.encode("utf-8")).hexdigest()[:12]
    return f"{slug or 'paper'}-{suffix}"


def markdown_path(output_dir: str, paper: Paper) -> str:
    """논문에 대응하는 Markdown 파일 경로를 반환한다."""
    stem = _stem(paper)
    return os.path.join(output_dir, stem, f"{stem}.md")


def _document(paper: Paper, summary: str, abstract_only: bool,
              image_files: list[tuple[str, str]] | None = None) -> str:
    authors = ", ".join(paper.authors) if paper.authors else "저자 미상"
    venue = paper.venue or "arXiv preprint"
    year = str(paper.year) if paper.year else "연도 미상"
    citation = f"{paper.citation_count}" if paper.citation_count is not None else "정보 없음"
    source = "Semantic Scholar" if paper.source == "semantic_scholar" else "arXiv"
    if abstract_only:
        summary_basis = "초록 기반" if (paper.abstract or "").strip() else "원문·초록 미확보 (메타데이터 안내)"
    else:
        summary_basis = "PDF 본문 기반"
    document = (
        f"# {paper.title.strip()}\n\n"
        f"- **저자:** {authors}\n"
        f"- **학술지/학회:** {venue}\n"
        f"- **연도:** {year}\n"
        f"- **인용 수:** {citation}\n"
        f"- **검색 주제:** {paper.topic_name}\n"
        f"- **검색 출처:** {source}\n"
        f"- **요약 기준:** {summary_basis}\n"
        f"- **논문 링크:** {paper.url}\n"
        f"- **논문 ID:** `{paper.uid}`\n\n"
        f"---\n\n"
        f"{summary.strip()}\n"
    )
    if image_files:
        items = []
        for label, filename in image_files:
            safe_label = label.replace("\\", "\\\\").replace("]", "\\]")
            items.append(f"### {safe_label}\n\n![{safe_label}]({filename})")
        document += "\n\n---\n\n## 논문 그림·표\n\n" + "\n\n".join(items) + "\n"
    return document


def _write_bytes_atomic(path: str, data: bytes) -> None:
    fd, temporary_path = tempfile.mkstemp(prefix=".paperbot-", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary_path, path)
    except Exception:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise


class MarkdownPublisher(Publisher):
    """Write summaries into an output directory, replacing the same paper on reruns."""

    def __init__(self, output_dir: str, dry_run: bool = False):
        self.output_dir = output_dir
        self.dry_run = dry_run

    def publish(self, paper: Paper, summary: str, abstract_only: bool = False,
                figures: list[Figure] | None = None) -> None:
        stem = _stem(paper)
        paper_dir = os.path.join(self.output_dir, stem)
        target_path = markdown_path(self.output_dir, paper)
        image_assets: list[tuple[str, str, bytes]] = []
        used_names: set[str] = set()

        for i, figure in enumerate(figures or [], start=1):
            # extract_figures() can return a full-page fallback for message cards;
            # Markdown output stores only actual figure and table crops.
            if figure.kind not in {"figure", "table"} or not figure.png:
                continue
            base = label_slug(figure.label) or f"{figure.kind}_{i}"
            image_name = f"{base}.png"
            suffix = 2
            while image_name in used_names:
                image_name = f"{base}_{suffix}.png"
                suffix += 1
            used_names.add(image_name)
            image_assets.append((figure.label, image_name, figure.png))

        image_files = [(label, image_name) for label, image_name, _ in image_assets]
        if self.dry_run:
            print(f"[DRY-RUN/markdown → {target_path}]")
            print(_document(paper, summary, abstract_only, image_files))
            return

        os.makedirs(paper_dir, exist_ok=True)
        previous_images: set[str] = set()
        if os.path.isfile(target_path):
            try:
                with open(target_path, encoding="utf-8") as stream:
                    previous = stream.read()
                previous_images = set(re.findall(r"!\[[^\]]*\]\(([^)]+\.png)\)", previous))
            except OSError as e:
                logger.warning("이전 Markdown 자산 목록을 읽지 못했습니다 (%s): %s", target_path, e)

        saved_images: list[tuple[str, str]] = []
        for label, image_name, image in image_assets:
            try:
                _write_bytes_atomic(os.path.join(paper_dir, image_name), image)
                saved_images.append((label, image_name))
            except OSError as e:
                logger.warning("그림·표 PNG 저장 실패 (%s): %s", image_name, e)

        content = _document(paper, summary, abstract_only, saved_images)
        fd, temporary_path = tempfile.mkstemp(prefix=".paperbot-", suffix=".tmp", dir=paper_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
            os.replace(temporary_path, target_path)
        except Exception:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
            raise
        current_images = {name for _, name in saved_images}
        for old_name in previous_images - current_images:
            if os.path.basename(old_name) != old_name:
                continue
            try:
                os.remove(os.path.join(paper_dir, old_name))
            except FileNotFoundError:
                pass
            except OSError as e:
                logger.warning("이전 그림·표 PNG 정리 실패 (%s): %s", old_name, e)
        logger.info("Markdown 저장: %s", target_path)
