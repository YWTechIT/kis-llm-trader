"""Claude 호출 → tool use로 {action, quantity, reason} 강제 반환.

- 출력은 **tool use(function calling)** 로만 받는다. 자유 텍스트 매매 금지.
- 스키마 외 응답·파싱 실패·API 오류는 전부 **안전하게 HOLD로 폴백**(무인 매매 안전).
- 시스템/전략 명세는 안정적이므로 prompt caching 적용(반복 호출 비용 절감).
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import anthropic

from risk.guardrails import Decision
from strategy.context import STRATEGY_SPEC, build_decision_input

logger = logging.getLogger(__name__)

_BASE_SYSTEM = (
    "너는 한국 주식 단기 매매 보조 에이전트다. 주어진 전략 명세와 시세 스냅샷을 보고 "
    "하나의 종목에 대해 BUY/SELL/HOLD 중 하나와 수량만 결정한다. "
    "반드시 submit_decision 도구로만 답하고, 자유 텍스트로 매매하지 마라. "
    "손절·한도·거래횟수 등 안전장치는 별도 코드가 강제하므로 신경 쓰지 말고, "
    "확신이 없으면 HOLD를 선택하라.\n"
    "[근거 작성 규칙 — 엄수]\n"
    "- signal: 결정의 직접적 트리거. 반드시 스냅샷에 실제로 존재하는 수치를 인용한다 "
    "(현재가/전일대비율/고저가/평균매입가/평가손익률 등). "
    "'관망', '불확실' 같은 추상어만 쓰지 말고 어떤 값이 무엇을 가리키는지 적어라. "
    "예: '현재가 71,200원, 전일대비 -2.3% → 눌림목 진입 구간'.\n"
    "- rule: 위 signal이 전략 명세의 어느 조항에 해당하는지 그 조항을 지칭한다. "
    "예: '진입 조건: 전일 대비 -2% 이상 눌림목 분할매수'.\n"
    "- summary: signal+rule을 한국어 한 문장으로 압축(알림 표시용).\n"
    "- 스냅샷에 없는 지표(이동평균선·거래량 등)는 지어내지 마라. 주어진 값만으로 판단한다."
)

_DECISION_TOOL = {
    "name": "submit_decision",
    "description": "이번 종목에 대한 매매 결정을 제출한다. 반드시 이 도구로만 응답.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["BUY", "SELL", "HOLD"],
                "description": "매수/매도/관망 중 하나",
            },
            "quantity": {
                "type": "integer",
                "minimum": 0,
                "description": "주문 수량(주). HOLD면 0. 코드가 한도 내로 축소할 수 있음.",
            },
            "signal": {
                "type": "string",
                "description": (
                    "결정의 직접 트리거. 스냅샷의 구체적 수치를 반드시 인용(현재가/전일대비율 등). "
                    "추상어 금지. 예: '현재가 71,200원, 전일대비 -2.3% 눌림목'."
                ),
            },
            "rule": {
                "type": "string",
                "description": (
                    "이 결정이 근거한 전략 명세 조항. "
                    "예: '진입 조건: 전일 대비 -2% 이상 눌림목 분할매수'."
                ),
            },
            "summary": {
                "type": "string",
                "description": "signal+rule을 한국어 한 문장으로 압축(알림 표시용).",
            },
        },
        "required": ["action", "quantity", "signal", "rule", "summary"],
        "additionalProperties": False,
    },
}


def _hold(code: str, reason: str) -> Decision:
    return Decision(action="HOLD", quantity=0, reason=reason, code=code,
                    signal=reason, rule="-")


class LLMDecider:
    def __init__(self, api_key: str, model: str = "claude-haiku-4-5",
                 max_tokens: int = 512) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens
        # 시스템 프롬프트는 안정적 → 캐시 대상(전략 명세 블록에 breakpoint)
        self._system = [
            {"type": "text", "text": _BASE_SYSTEM},
            {
                "type": "text",
                "text": f"[전략 명세]\n{STRATEGY_SPEC}",
                "cache_control": {"type": "ephemeral"},
            },
        ]

    def decide(self, snapshot: dict, code: str) -> tuple[Decision, dict]:
        """반환: (Decision, meta). meta는 journal 기록용(model/raw/usage)."""
        payload = build_decision_input(snapshot, code)
        user_content = (
            "다음 컨텍스트로 submit_decision을 호출하라.\n"
            + json.dumps(payload, ensure_ascii=False, default=str)
        )
        meta = {"model": self._model, "payload": payload}

        try:
            resp = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=self._system,
                tools=[_DECISION_TOOL],
                tool_choice={"type": "tool", "name": "submit_decision"},
                messages=[{"role": "user", "content": user_content}],
            )
        except anthropic.APIError as exc:
            logger.error("LLM 호출 실패 → HOLD 폴백: %s", exc)
            return _hold(code, f"LLM 호출 실패: {type(exc).__name__}"), meta
        except Exception as exc:  # noqa: BLE001 — 어떤 예외도 매매를 막지 않게
            logger.exception("LLM 예기치 못한 오류 → HOLD 폴백")
            return _hold(code, f"LLM 오류: {type(exc).__name__}"), meta

        meta["raw"] = resp.to_dict() if hasattr(resp, "to_dict") else None
        decision = self._parse(resp, code)
        return decision, meta

    def _parse(self, resp, code: str) -> Decision:
        """tool_use 블록에서 결정 추출. 스키마 외/누락 시 HOLD 폴백."""
        tool_block = next(
            (b for b in resp.content if getattr(b, "type", None) == "tool_use"
             and getattr(b, "name", None) == "submit_decision"),
            None,
        )
        if tool_block is None:
            return _hold(code, "LLM이 도구 형식으로 응답하지 않음 → HOLD")

        data = tool_block.input or {}
        action = data.get("action")
        if action not in ("BUY", "SELL", "HOLD"):
            return _hold(code, f"알 수 없는 action '{action}' → HOLD")

        try:
            quantity = int(data.get("quantity", 0))
        except (TypeError, ValueError):
            quantity = 0
        if quantity < 0:
            quantity = 0

        signal = str(data.get("signal", "")).strip()
        rule = str(data.get("rule", "")).strip()
        summary = str(data.get("summary", "")).strip()
        # reason은 사람이 읽을 한 줄(= summary). summary 누락 시 signal로 폴백.
        reason = summary or signal or "(사유 없음)"
        if action == "HOLD":
            quantity = 0
        return Decision(action=action, quantity=quantity, reason=reason, code=code,
                        signal=signal, rule=rule)
