"""LLM 호출 공용 헬퍼 + 논문 요약.

OpenAI Chat Completions API or a local OpenAI-compatible endpoint is called with the openai package.

num_ctx is checked against the server when using a local endpoint. Hosted API requests use the
configured max_input_chars input budget instead.

judge(filter.py)와 요약이 이 모듈의 make_client/chat을 공유한다.
"""
from __future__ import annotations

import logging
import os
import re
import time

from openai import OpenAI

from . import llmserver
from .config import Config
from .extractor import ExtractResult
from .models import Paper
from .prompts import build_summary_system_prompt, build_summary_user_prompt

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
# 출력이 max_tokens에 잘려 </think> 없이 끝난 경우 — 미완 thinking 전체를 제거.
_THINK_OPEN_RE = re.compile(r"<think>.*\Z", re.DOTALL | re.IGNORECASE)


class LLMError(Exception):
    """재시도를 모두 소진한 뒤에도 LLM 호출에 실패한 경우."""


def make_client(cfg: Config) -> OpenAI:
    if llmserver.is_openai_api(cfg.llm.base_url):
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise LLMError("GPT-6 Luna API를 쓰려면 ~/.paperbot_env에 OPENAI_API_KEY를 설정하세요.")
        return OpenAI(base_url=cfg.llm.base_url, api_key=api_key, timeout=600.0)
    # llama-swap은 API 키 검증을 끄는 구성이므로 SDK에 더미 값을 넘긴다.
    return OpenAI(base_url=cfg.llm.base_url, api_key="none", timeout=600.0)


def _clean_output(text: str) -> str:
    text = _THINK_RE.sub("", text or "")
    text = _THINK_OPEN_RE.sub("", text)  # 잘린(미완) thinking → 빈 응답 처리 → 재시도 유도
    return text.strip()


_ctx_verified = False
_llm_used = False   # 이 프로세스가 LLM을 한 번이라도 불렀나 — 실행 끝 언로드 판단용 (llm_used())


def _verify_num_ctx_once(cfg: Config) -> None:
    """서버에 실제 로드된 컨텍스트 길이를 확인, config보다 작으면 경고 (1회만)."""
    global _ctx_verified
    if _ctx_verified:
        return
    _ctx_verified = True
    if llmserver.is_openai_api(cfg.llm.base_url):
        return  # Hosted API context is not discoverable through llama-server /props.
    actual = llmserver.server_ctx(llmserver.origin(cfg.llm.base_url), cfg.llm.model)
    if actual is None:
        logger.debug("서버 컨텍스트 확인 불가(무시)")
    elif actual < cfg.llm.num_ctx:
        logger.warning(
            "서버의 실제 컨텍스트(%d)가 config num_ctx(%d)보다 작습니다 — 긴 논문이 조용히 잘릴 수 있습니다. "
            "~/llama-swap/config.yaml의 llama-server -c를 %d 이상으로 올리세요.",
            actual, cfg.llm.num_ctx, cfg.llm.num_ctx,
        )
    else:
        logger.info("서버 컨텍스트 확인: %d (config num_ctx=%d)", actual, cfg.llm.num_ctx)


def chat(
    client: OpenAI,
    cfg: Config,
    messages: list[dict],
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    response_format: dict | None = None,
    label: str = "llm",
) -> str:
    """지수 백오프 재시도(3회)를 포함한 chat completion. 실패 시 LLMError.

    max_tokens 미지정 시 config의 max_output_tokens 사용 — 폭주 생성 방지.
    GPT-6 Luna uses reasoning_effort=none; local llama.cpp uses chat_template_kwargs to disable thinking.
    response_format uses the OpenAI-compatible json_schema format.
    """
    global _llm_used
    _llm_used = True
    last_err: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            kwargs = {}
            if response_format is not None:
                kwargs["response_format"] = response_format
            hosted_api = llmserver.is_openai_api(cfg.llm.base_url)
            kwargs["max_completion_tokens" if hosted_api else "max_tokens"] = (
                cfg.llm.max_output_tokens if max_tokens is None else max_tokens
            )
            if hosted_api:
                # GPT-6 Luna's Chat Completions function-call compatibility requires effort=none.
                kwargs["reasoning_effort"] = "none"
            else:
                kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
            resp = client.chat.completions.create(
                model=cfg.llm.model,
                messages=messages,
                temperature=cfg.llm.temperature if temperature is None else temperature,
                **kwargs,
            )
            content = _clean_output(resp.choices[0].message.content)
            if not content:
                raise ValueError("LLM이 빈 응답을 반환")
            _verify_num_ctx_once(cfg)
            return content
        except Exception as e:  # noqa: BLE001 — 연결/응답 오류 모두 재시도
            last_err = e
            wait = 2 ** attempt  # 2s, 4s, 8s
            logger.warning("%s 호출 실패 (%d/%d): %s", label, attempt, _MAX_RETRIES, e)
            if attempt < _MAX_RETRIES:
                time.sleep(wait)
    raise LLMError(f"{label} 최종 실패") from last_err


def llm_used() -> bool:
    """이 프로세스에서 chat()(judge·요약)을 호출했는지 반환한다 — 호출하지 않았으면 정리할 모델도 없다."""
    return _llm_used


def unload_model(cfg: Config) -> bool:
    """로컬 endpoint를 쓰는 경우 실행 종료 시 모델을 내려 VRAM을 반납한다.

    호스팅 API에서는 아무 작업도 하지 않는다. 로컬 endpoint의 다른 요청이 진행 중이면 기다린 뒤 내리며,
    실패해도 치명적이지 않다 — 서버 ttl이 지나면 모델이 내려간다.
    """
    return llmserver.unload(llmserver.origin(cfg.llm.base_url), cfg.llm.model)


def summarize(cfg: Config, paper: Paper, extracted: ExtractResult, client: OpenAI | None = None) -> str:
    """논문 하나를 요약해 마크다운 문자열을 반환. 실패 시 LLMError."""
    client = client or make_client(cfg)
    math = "latex" if cfg.platform_for(paper.topic_name) == "zulip" else "plain"   # Zulip만 $$LaTeX$$ 렌더링
    messages = [
        {"role": "system", "content": build_summary_system_prompt(cfg.llm.language, math)},
        {"role": "user", "content": build_summary_user_prompt(paper, extracted.text, extracted.abstract_only)},
    ]
    content = chat(client, cfg, messages, label=f"요약({paper.uid})")
    logger.info("요약 완료 %s: %d자", paper.uid, len(content))
    return content
