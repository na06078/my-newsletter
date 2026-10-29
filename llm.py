"""LLM 호출 창구 — OpenAI 호환 API 로 '구조화 출력'(정해 둔 스키마대로만 답하게 하는 방식)을 받는다.

호출마다 모델·토큰·소요 시간을 metrics 에 남겨, 비용과 호출 수를 나중에 셀 수 있게 한다.
"""
from __future__ import annotations

import os
import time

import openai
from openai import OpenAI
from pydantic import BaseModel


class LLMError(Exception):
    """모델 호출 실패·거부·스키마 불일치. 호출한 노드가 대안(스킵·재생성)을 고르게 한다."""


class LLM:
    def __init__(self, llm_cfg: dict, metrics=None, client: OpenAI | None = None) -> None:
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
        self.client = client
        self._send_temperature = llm_cfg.get("temperature") is not None

    def parse(self, stage: str, system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        """stage(select·report·verify)에 맞는 모델로 호출하고 schema 객체를 돌려준다."""
        model = self.cfg["models"][stage.split("_")[0]]
        kwargs = {"model": model, "response_format": schema,
                  "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if self._send_temperature:
            kwargs["temperature"] = self.cfg["temperature"]
        started = time.perf_counter()
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
        except openai.OpenAIError as exc:
            self._log(stage, model, None, started, error=type(exc).__name__)
            raise LLMError(f"{type(exc).__name__}: {str(exc)[:160]}") from exc
        message = resp.choices[0].message
        self._log(stage, getattr(resp, "model", model), resp.usage, started)
        if getattr(message, "refusal", None) or message.parsed is None:
            raise LLMError(f"모델이 스키마에 맞는 답을 주지 않았습니다 (refusal={bool(message.refusal)})")
        return message.parsed

    def _log(self, stage, model, usage, started, error=None) -> None:
        self.calls += 1
        if self.metrics:
            self.metrics.emit("llm_call", call=stage, model=model,
                              prompt_tokens=getattr(usage, "prompt_tokens", None),
                              completion_tokens=getattr(usage, "completion_tokens", None),
                              ms=round((time.perf_counter() - started) * 1000), error=error)
