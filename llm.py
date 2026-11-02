"""LLM 호출 창구 — OpenAI 호환 API 로 '구조화 출력'(정해 둔 스키마대로만 답하게 하는 방식)을 받는다.

호출마다 모델·토큰·소요 시간을 metrics 에 남겨, 비용과 호출 수를 나중에 셀 수 있게 한다.
"""
from __future__ import annotations

import os
from json import JSONDecodeError
import time

import openai
from openai import OpenAI
from openai.types.chat import ChatCompletion
from pydantic import BaseModel, ValidationError
from pathlib import Path
from contextvars import ContextVar

from 호출예산 import RequestBudget, BudgetExceeded


_active_budget = ContextVar("newsletter_request_budget", default=None)


class _BudgetAbort(BaseException):
    """SDK의 Exception 재시도/래핑을 우회하고 parse 경계에서 원래 오류로 복원한다."""
    def __init__(self, error):
        self.error = error


def _reserve_http_attempt(request):
    budget = _active_budget.get()
    if budget is not None:
        try:
            budget.reserve()
        except BudgetExceeded as exc:
            raise _BudgetAbort(exc) from None


class LLMError(Exception):
    """모델 호출 실패·거부·스키마 불일치. 호출한 노드가 대안(스킵·재생성)을 고르게 한다."""


class LLM:
    def __init__(self, llm_cfg: dict, metrics=None, client: OpenAI | None = None, *, budget=None) -> None:
        self._owns_client = client is None
        self._closed = False
        self.budget = budget if budget is not None else RequestBudget(
            Path(__file__).resolve().parent / "store" / "호출예산.sqlite3",
            limit=llm_cfg.get("request_limit", 65))
        self.cfg = llm_cfg
        self.metrics = metrics
        self.calls = 0
        key_env = llm_cfg.get("api_key_env", "OPENAI_API_KEY")
        if client is None:
            api_key = os.environ.get(key_env)
            if not api_key:
                raise LLMError(f"환경변수 {key_env} 가 비어 있습니다. .env 에 키를 넣어 주세요.")
            client = OpenAI(api_key=api_key, base_url=llm_cfg["base_url"],
                            timeout=llm_cfg.get("timeout_s", 60), max_retries=llm_cfg.get("max_retries", 2))
        # 설치된 OpenAI 3.41/httpx 0.28의 요청 훅: SDK 재시도마다 다시 실행된다.
        # ContextVar로 같은 HTTP 클라이언트를 쓰는 동시 호출의 장부를 분리한다.
        # SDK 후처리의 직렬화 경고가 응답 원문을 출력하기 전에 봉투를 검증한다.
        self.client = client.with_options(max_retries=llm_cfg.get("max_retries", 2),
                                          _extra_kwargs={"_strict_response_validation": True})
        hooks = self.client._client.event_hooks["request"]
        if _reserve_http_attempt not in hooks:
            hooks.append(_reserve_http_attempt)
        self._send_temperature = llm_cfg.get("temperature") is not None

    def close(self) -> None:
        """직접 생성한 연결만 정리한다. 주입받은 클라이언트의 수명은 호출자가 소유한다."""
        if not self._closed:
            if self._owns_client:
                self.client.close()
            self._closed = True

    def parse(self, stage: str, system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        """stage(select·report·verify)에 맞는 모델로 호출하고 schema 객체를 돌려준다."""
        model = self.cfg["models"][stage.split("_")[0]]
        kwargs = {"model": model, "response_format": schema,
                  "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if self._send_temperature:
            kwargs["temperature"] = self.cfg["temperature"]
        started = time.perf_counter()
        token = _active_budget.set(self.budget)
        try:
            try:
                resp = self.client.chat.completions.parse(**kwargs)
            except openai.BadRequestError as exc:
                if "temperature" in str(exc).lower():     # 온도 설정을 받지 않는 모델이면 빼고 한 번 더
                    self._send_temperature = False
                    kwargs.pop("temperature", None)
                    resp = self.client.chat.completions.parse(**kwargs)
                else:
                    raise
            # SDK의 관대한 응답 생성은 빈 choices/잘못된 message를 검증하지 않는다.
            # 출력 스키마와 별도로 응답 봉투를 검증하며 원문은 오류에 넣지 않는다.
            if not isinstance(resp, ChatCompletion):
                raise LLMError("invalid_response")
            try:
                ChatCompletion.model_validate(resp.model_dump(warnings=False,
                    exclude={"choices": {"__all__": {"message": {"parsed"}}}}), strict=True)
            except ValidationError:
                raise LLMError("invalid_response") from None
            if not resp.choices:
                raise LLMError("invalid_response")
            message = resp.choices[0].message
            if message.refusal or not isinstance(getattr(message, "parsed", None), schema):
                raise LLMError("invalid_response")
        except _BudgetAbort as exc:
            raise exc.error from None
        except (openai.OpenAIError, ValidationError, JSONDecodeError, UnicodeDecodeError,
                AttributeError, TypeError, IndexError, LLMError) as exc:
            # SDK의 응답 후처리도 AttributeError/TypeError로 실패할 수 있다.
            # BudgetExceeded/BaseException은 이 복구 경계에 포함하지 않는다.
            error = (str(exc) if isinstance(exc, LLMError) else
                     "invalid_response" if isinstance(exc, (AttributeError, TypeError, IndexError,
                                                           openai.APIResponseValidationError))
                     else type(exc).__name__)
            self._log(stage, model, None, started, error=error)
            # 제공자 예외 본문/URL에는 인증 정보가 섞일 수 있으므로 내보내지 않는다.
            raise LLMError(error) from None
        finally:
            _active_budget.reset(token)
        self._log(stage, getattr(resp, "model", model), resp.usage, started)
        return message.parsed

    def _log(self, stage, model, usage, started, error=None) -> None:
        self.calls += 1
        if self.metrics:
            self.metrics.emit("llm_call", call=stage, model=model,
                              prompt_tokens=getattr(usage, "prompt_tokens", None),
                              completion_tokens=getattr(usage, "completion_tokens", None),
                              ms=round((time.perf_counter() - started) * 1000), error=error)
