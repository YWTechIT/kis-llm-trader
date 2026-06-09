"""Claude Haiku로 종목 뉴스를 2~3줄 요약.

llm_decider.py 패턴 재사용:
- anthropic.Anthropic(api_key=...)
- 실패 시 "요약 불가" 반환 — 절대 예외 전파하지 않는다.
"""

from __future__ import annotations

import logging
import os

import anthropic

logger = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5"
_MAX_TOKENS = 300

_SYSTEM = (
    "너는 한국 주식 뉴스 요약 전문가다. "
    "주어진 종목 정보와 뉴스 기사 제목들을 보고 "
    "왜 오늘 해당 종목이 크게 오르거나 내렸는지 핵심 이유를 "
    "한국어 2~3문장으로 간결하게 요약한다. "
    "수식어 없이 팩트 중심으로 작성하고, '요약:' 같은 레이블 없이 바로 본문만 출력한다."
)


class LLMSummarizer:
    def __init__(self, api_key: str, model: str = _MODEL) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def summarize(self, name: str, code: str, change_rate: float,
                  articles: list[dict]) -> str:
        """뉴스 요약 반환. 실패 시 빈 문자열."""
        if not articles:
            return ""

        titles = "\n".join(
            f"- {a['title']}" for a in articles
        )
        direction = "상승" if change_rate >= 0 else "하락"
        user_msg = (
            f"종목: {name} ({code})\n"
            f"오늘 등락률: {change_rate:+.2f}% ({direction})\n\n"
            f"관련 뉴스 제목:\n{titles}\n\n"
            "위 뉴스를 바탕으로 오늘 이 종목이 크게 움직인 이유를 요약해줘."
        )

        try:
            resp = self._client.messages.create(
                model=self._model,
                max_tokens=_MAX_TOKENS,
                system=_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = resp.content[0].text.strip() if resp.content else ""
            logger.info("LLM 요약 완료 %s(%s)", name, code)
            return text
        except anthropic.APIError as exc:
            logger.error("LLM 요약 실패 %s: %s", code, exc)
            return ""
        except Exception as exc:  # noqa: BLE001
            logger.exception("LLM 요약 예기치 못한 오류 %s", code)
            return ""


def build_summarizer() -> LLMSummarizer:
    api_key = os.environ["ANTHROPIC_API_KEY"]
    return LLMSummarizer(api_key=api_key)
