"""Send one Markdown summary email per paper through the user's Gmail account."""
from __future__ import annotations

import base64
import hashlib
import html
import logging
import os
import re
from email.message import EmailMessage
from email.utils import getaddresses
from pathlib import Path
from typing import Callable
from urllib.parse import unquote

import requests

from .config import GmailConfig
from .models import Paper

logger = logging.getLogger(__name__)

_GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
_USERINFO_EMAIL_SCOPE = "https://www.googleapis.com/auth/userinfo.email"
_SCOPES = [_GMAIL_SEND_SCOPE, "openid", _USERINFO_EMAIL_SCOPE]
_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


class GmailError(RuntimeError):
    """Gmail OAuth or delivery failure."""

    def __init__(self, message: str, *, delivery_unknown: bool = False):
        super().__init__(message)
        self.delivery_unknown = delivery_unknown


def _google_flow(config: GmailConfig):
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as e:
        raise GmailError(
            "Gmail API 패키지가 없습니다. requirements-paperbot.txt를 다시 설치하세요."
        ) from e

    if not os.path.isfile(config.credentials_file):
        raise GmailError(
            "Gmail OAuth 클라이언트 파일을 찾지 못했습니다: "
            f"{config.credentials_file} (README의 Gmail 설정 절차를 따르세요.)"
        )
    try:
        return InstalledAppFlow.from_client_secrets_file(config.credentials_file, _SCOPES)
    except Exception as e:  # noqa: BLE001 — OAuth JSON 오류를 설정 오류로 표시
        raise GmailError(f"Gmail OAuth 클라이언트 파일을 읽지 못했습니다: {e}") from e


def _save_token(path: str, token_json: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(token_json)
            stream.write("\n")
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def authorize_gmail(config: GmailConfig) -> None:
    """Run one interactive OAuth consent flow and store its refresh token locally."""
    flow = _google_flow(config)
    try:
        credentials = flow.run_local_server(port=0)
        _save_token(config.token_file, credentials.to_json())
    except GmailError:
        raise
    except Exception as e:  # noqa: BLE001 — browser/OAuth transport errors
        raise GmailError(f"Gmail OAuth 승인이 완료되지 않았습니다: {e}") from e


def _load_credentials(config: GmailConfig):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as e:
        raise GmailError(
            "Gmail API 패키지가 없습니다. requirements-paperbot.txt를 다시 설치하세요."
        ) from e

    if not os.path.isfile(config.token_file):
        raise GmailError(
            "Gmail OAuth 승인이 아직 없습니다. 먼저 `python -m paperbot --authorize-gmail`을 실행하세요."
        )
    try:
        credentials = Credentials.from_authorized_user_file(config.token_file, _SCOPES)
        if not credentials.valid:
            if not credentials.expired or not credentials.refresh_token:
                raise GmailError(
                    "Gmail OAuth 토큰을 새로 고칠 수 없습니다. `--authorize-gmail`로 다시 승인하세요."
                )
            credentials.refresh(Request())
            _save_token(config.token_file, credentials.to_json())
        return credentials
    except GmailError:
        raise
    except Exception as e:  # noqa: BLE001 — 토큰 형식·만료·권한 오류
        raise GmailError(f"Gmail OAuth 토큰을 읽거나 갱신하지 못했습니다: {e}") from e


def _sender_email(access_token: str) -> str:
    try:
        response = requests.get(
            _USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as e:  # noqa: BLE001 — account identity lookup errors
        raise GmailError(f"OAuth로 승인한 Gmail 주소를 확인하지 못했습니다: {e}") from e
    address = str(data.get("email") or "").strip()
    if not address or not data.get("email_verified", False):
        raise GmailError("OAuth로 승인한 Google 계정의 확인된 이메일 주소를 받지 못했습니다.")
    return address


def _markdown_html(markdown_text: str, asset_dir: str, paper_uid: str) -> tuple[str, list[tuple[str, bytes]]]:
    """Render Markdown safely and replace its local PNG links with CID images."""
    try:
        import bleach
        import markdown
    except ImportError as e:
        raise GmailError(
            "Markdown 이메일 패키지가 없습니다. requirements-paperbot.txt를 다시 설치하세요."
        ) from e

    rendered = markdown.markdown(
        markdown_text,
        extensions=["tables", "fenced_code", "sane_lists"],
        output_format="html",
    )
    base_dir = Path(asset_dir).resolve()
    inline_images: list[tuple[str, bytes]] = []
    img_tag = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
    src_attr = re.compile(r"\bsrc=(['\"])(.*?)\1", re.IGNORECASE)

    def replace_image(match: re.Match[str]) -> str:
        tag = match.group(0)
        src_match = src_attr.search(tag)
        if not src_match:
            return ""
        source = unquote(html.unescape(src_match.group(2)))
        if not source or Path(source).name != source:
            return ""
        image_path = (base_dir / source).resolve()
        if image_path.parent != base_dir or image_path.suffix.lower() != ".png" or not image_path.is_file():
            return ""
        cid = "paperbot-" + hashlib.sha256(
            f"{paper_uid}:{source}".encode("utf-8")
        ).hexdigest()[:24]
        inline_images.append((cid, image_path.read_bytes()))
        return src_attr.sub(f'src="cid:{cid}"', tag, count=1)

    rendered = img_tag.sub(replace_image, rendered)
    allowed_tags = {
        "a", "blockquote", "br", "code", "del", "div", "em", "h1", "h2", "h3", "h4",
        "hr", "img", "li", "ol", "p", "pre", "strong", "sub", "sup", "table", "tbody",
        "td", "th", "thead", "tr", "ul",
    }
    allowed_attributes = {
        "a": ["href", "title"],
        "img": ["src", "alt", "title"],
        "code": ["class"],
        "td": ["colspan", "rowspan"],
        "th": ["colspan", "rowspan"],
    }
    safe_html = bleach.clean(
        rendered,
        tags=allowed_tags,
        attributes=allowed_attributes,
        protocols={"cid", "http", "https", "mailto"},
        strip=True,
    )
    return safe_html, inline_images


class GmailSender:
    """Gmail API client. Requests only send permission plus OpenID email identity."""

    def __init__(self, config: GmailConfig):
        recipients = [address for _, address in getaddresses([config.to]) if address]
        if not recipients or any("@" not in address for address in recipients):
            raise GmailError("gmail.to 수신 주소 형식이 올바르지 않습니다.")
        self.config = config
        self.recipients = recipients
        credentials = _load_credentials(config)
        self.sender = _sender_email(credentials.token)
        try:
            from googleapiclient.discovery import build
            self.service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        except Exception as e:  # noqa: BLE001 — API client setup errors
            raise GmailError(f"Gmail API 클라이언트를 만들지 못했습니다: {e}") from e

    def send_paper(
        self,
        paper: Paper,
        markdown_file: str,
        *,
        before_send: Callable[[], None] | None = None,
    ) -> str:
        """Send one paper's rendered Markdown and inline PNGs without file attachments."""
        md_path = Path(markdown_file)
        if not md_path.is_file():
            raise GmailError(f"Markdown 파일을 찾지 못했습니다: {md_path}")
        try:
            markdown_text = md_path.read_text(encoding="utf-8")
            html_body, inline_images = _markdown_html(markdown_text, str(md_path.parent), paper.uid)
        except GmailError:
            raise
        except OSError as e:
            raise GmailError(f"메일 Markdown을 읽지 못했습니다: {e}") from e

        message = EmailMessage()
        message["To"] = ", ".join(self.recipients)
        message["From"] = self.sender
        subject = f"{self.config.subject_prefix} {paper.title}".strip()
        message["Subject"] = subject
        plain_text = markdown_text.rstrip()
        if inline_images:
            plain_text += "\n\n[그림·표는 HTML 이메일 본문에 포함되어 있습니다.]"
        message.set_content(plain_text + "\n")
        message.add_alternative(html_body, subtype="html")
        html_part = message.get_body(preferencelist=("html",))
        if html_part is None:
            raise GmailError("HTML 이메일 본문을 구성하지 못했습니다.")
        for cid, image_bytes in inline_images:
            html_part.add_related(
                image_bytes,
                maintype="image",
                subtype="png",
                cid=f"<{cid}>",
                disposition="inline",
            )
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        if before_send is not None:
            before_send()
        try:
            result = self.service.users().messages().send(
                userId="me", body={"raw": raw}
            ).execute()
        except Exception as e:  # noqa: BLE001 — Gmail API errors
            response_status = getattr(getattr(e, "resp", None), "status", None)
            try:
                status_code = int(response_status) if response_status is not None else None
            except (TypeError, ValueError):
                status_code = None
            # 명시적인 4xx 거부는 재시도 가능. 타임아웃·연결 오류·5xx는 Gmail이
            # 메일을 수락한 뒤 응답만 유실했을 수 있어 자동 재전송하면 중복될 수 있다.
            delivery_unknown = status_code is None or status_code == 408 or status_code >= 500
            raise GmailError(
                f"Gmail 발송 실패: {e}", delivery_unknown=delivery_unknown
            ) from e
        message_id = str(result.get("id") or "")
        logger.info("Gmail 발송 완료 %s (message_id=%s)", paper.uid, message_id or "unknown")
        return message_id
