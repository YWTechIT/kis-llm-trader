"""환경설정 로딩·검증 + 공식 kis_auth용 kis_devlp.yaml 런타임 생성.

설계 원칙
- 크리덴셜은 오직 `.env`(gitignore)에만. 코드/로그/알림 어디에도 값을 출력하지 않는다.
- 공식 `vendor/kis/kis_auth.py`는 **import 시점에** `~/KIS/config/kis_devlp.yaml`을 읽는다.
  따라서 kis_auth를 import하기 전에 이 모듈이 먼저 yaml을 생성해 두어야 한다.
  → `adapter/broker.py`/`market_stream.py`는 반드시 `import config`를 kis import보다 먼저 한다.
- 필수 키 누락 시 **명확한 메시지와 함께 기동을 거부**한다(무인 매매에서 침묵 실패 방지).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

# 공식 kis_auth.py가 기대하는 고정 경로(상수). 사본을 수정하지 않으려고 이 경로에 맞춘다.
KIS_CONFIG_ROOT = Path.home() / "KIS" / "config"
KIS_DEVLP_YAML = KIS_CONFIG_ROOT / "kis_devlp.yaml"

# 공식 도메인(샘플 kis_devlp.yaml과 동일). 크리덴셜이 아니므로 코드에 둬도 무방.
_DOMAINS = {
    "prod": "https://openapi.koreainvestment.com:9443",
    "vps": "https://openapivts.koreainvestment.com:29443",
    "ops": "ws://ops.koreainvestment.com:21000",
    "vops": "ws://ops.koreainvestment.com:31000",
}
_DEFAULT_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
)


class ConfigError(RuntimeError):
    """필수 환경변수 누락/형식 오류. 기동을 중단시키는 용도."""


def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise ConfigError(
            f"필수 환경변수 '{name}' 가 비어 있습니다. .env(.env.example 참고)를 확인하세요."
        )
    return val


def _get_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"환경변수 '{name}' 는 숫자여야 합니다 (현재값 파싱 실패): {exc}")


def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"환경변수 '{name}' 는 정수여야 합니다: {exc}")


@dataclass(frozen=True)
class Settings:
    # 실행 환경
    kis_env: str               # "vps"(모의) | "prod"(실전)
    env_dv: str                # kis 함수 인자용: "demo" | "real"
    # LLM
    anthropic_api_key: str
    anthropic_model: str
    # 알림
    discord_webhook_url: str
    discord_tradelog_url: str   # 매 사이클 LLM 결정 전부를 보낼 별도 채널(비면 일반 채널로 폴백)
    # 양방향 조회 봇(잔고/보유/체결/매수가능 질의 응답). 비활성 시 토큰 불필요.
    discord_bot_enabled: bool
    discord_bot_token: str      # 크리덴셜 — 절대 로깅/노출 금지
    discord_bot_channel_id: int  # 조회 허용 채널 ID(0이면 전체 허용)
    discord_rank_channel_id: int  # 순위/등락 결과 전송 채널 ID(#daily-market, 0이면 명령 채널)
    # 로깅
    log_level: str              # 파일 로그 레벨(DEBUG/INFO/...). 콘솔은 INFO 고정.
    log_file: str               # 상세 로그 파일 경로(로테이션). 비면 파일 로그 비활성.
    # 운용 대상/주기
    watch_codes: list[str]
    strategy_name: str          # 활성 전략 프리셋 키(strategy/presets.py). 기본 golden_cross.
    decision_interval_min: int
    # 순위 조회(거래량/등락률) 디스코드 표시 기본값
    rank_top_n: int             # 순위 리스트 표시 개수(기본 10)
    rank_exclude_etf: bool      # ETF/ETN 기본 제외 여부(봇 명령으로 토글 가능)
    journal_db: str
    # 가드레일 파라미터
    max_order_krw: int
    max_position_pct: float
    daily_max_loss_krw: int
    max_trades_per_day: int
    hard_stop_loss_pct: float
    # kis_devlp.yaml 생성용 원자료(크리덴셜 — 절대 로깅 금지)
    _kis_yaml: dict = field(default_factory=dict, repr=False)

    @property
    def is_paper(self) -> bool:
        return self.kis_env == "vps"


def load_settings() -> Settings:
    """`.env`를 읽어 검증된 Settings를 반환. 누락 시 ConfigError."""
    load_dotenv()

    kis_env = os.environ.get("KIS_ENV", "vps").strip().lower()
    if kis_env not in ("vps", "prod"):
        raise ConfigError(f"KIS_ENV 는 'vps' 또는 'prod' 여야 합니다 (현재: '{kis_env}').")

    # 환경에 해당하는 크리덴셜만 필수로 요구한다.
    if kis_env == "vps":
        app_key = _require("KIS_PAPER_APP_KEY")
        app_sec = _require("KIS_PAPER_APP_SECRET")
    else:
        app_key = _require("KIS_PROD_APP_KEY")
        app_sec = _require("KIS_PROD_APP_SECRET")

    hts_id = _require("KIS_HTS_ID")
    acct8 = _require("KIS_ACCOUNT_8")
    acct_pd = os.environ.get("KIS_ACCOUNT_PD", "01").strip() or "01"

    # 양방향 조회 봇: 활성화 시에만 토큰을 필수로 요구한다(기존 배포가 봇 끄면 안 깨지도록).
    bot_enabled = os.environ.get("DISCORD_BOT_ENABLED", "false").strip().lower() == "true"
    bot_token = _require("DISCORD_BOT_TOKEN") if bot_enabled else os.environ.get(
        "DISCORD_BOT_TOKEN", ""
    ).strip()

    watch_raw = os.environ.get("WATCH_CODES", "005930")
    watch_codes = [c.strip() for c in watch_raw.split(",") if c.strip()]
    if not watch_codes:
        raise ConfigError("WATCH_CODES 에 관심종목 코드가 최소 1개 필요합니다.")

    # 활성 전략 프리셋. 미등록 키면 침묵 실패 대신 기동을 거부한다.
    # (presets는 config를 import하지 않으므로 순환 의존 없음)
    from strategy.presets import DEFAULT_STRATEGY, PRESETS

    strategy_name = os.environ.get("STRATEGY_NAME", DEFAULT_STRATEGY).strip().lower()
    if strategy_name not in PRESETS:
        raise ConfigError(
            f"STRATEGY_NAME='{strategy_name}' 는 등록되지 않은 전략입니다. "
            f"사용 가능: {', '.join(PRESETS)}"
        )

    # 공식 kis_auth는 svr에 따라 paper_app/my_app 키를 각각 읽으므로,
    # 현재 환경 쪽 키에 실제값을 넣고 반대편은 빈 자리표시자로 둔다.
    kis_yaml = {
        "my_app": app_key if kis_env == "prod" else "",
        "my_sec": app_sec if kis_env == "prod" else "",
        "paper_app": app_key if kis_env == "vps" else "",
        "paper_sec": app_sec if kis_env == "vps" else "",
        "my_htsid": hts_id,
        "my_acct_stock": acct8 if kis_env == "prod" else "",
        "my_acct_future": "",
        "my_paper_stock": acct8 if kis_env == "vps" else "",
        "my_paper_future": "",
        "my_prod": acct_pd,
        "prod": _DOMAINS["prod"],
        "ops": _DOMAINS["ops"],
        "vps": _DOMAINS["vps"],
        "vops": _DOMAINS["vops"],
        "my_token": "",
        "my_agent": os.environ.get("KIS_USER_AGENT", _DEFAULT_AGENT),
    }

    return Settings(
        kis_env=kis_env,
        env_dv="demo" if kis_env == "vps" else "real",
        anthropic_api_key=_require("ANTHROPIC_API_KEY"),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5").strip(),
        discord_webhook_url=_require("DISCORD_WEBHOOK_URL"),
        discord_tradelog_url=os.environ.get("DISCORD_TRADELOG_URL", "").strip(),
        discord_bot_enabled=bot_enabled,
        discord_bot_token=bot_token,
        discord_bot_channel_id=_get_int("DISCORD_BOT_CHANNEL_ID", 0),
        discord_rank_channel_id=_get_int("DISCORD_RANK_CHANNEL_ID", 0),
        log_level=os.environ.get("LOG_LEVEL", "DEBUG").strip().upper() or "DEBUG",
        log_file=os.environ.get("LOG_FILE", "logs/trader.log").strip(),
        watch_codes=watch_codes,
        strategy_name=strategy_name,
        decision_interval_min=_get_int("DECISION_INTERVAL_MIN", 15),
        rank_top_n=_get_int("RANK_TOP_N", 10),
        rank_exclude_etf=os.environ.get("RANK_EXCLUDE_ETF", "true").strip().lower() == "true",
        journal_db=os.environ.get("JOURNAL_DB", "trader.db").strip() or "trader.db",
        max_order_krw=_get_int("MAX_ORDER_KRW", 100_000),
        max_position_pct=_get_float("MAX_POSITION_PCT", 0.4),
        daily_max_loss_krw=_get_int("DAILY_MAX_LOSS_KRW", 30_000),
        max_trades_per_day=_get_int("MAX_TRADES_PER_DAY", 5),
        hard_stop_loss_pct=_get_float("HARD_STOP_LOSS_PCT", -10.0),
        _kis_yaml=kis_yaml,
    )


def bootstrap_kis_yaml(settings: Settings) -> Path:
    """`~/KIS/config/kis_devlp.yaml`을 생성한다(kis_auth import 이전 필수).

    파일 권한은 소유자 전용(0600)으로 제한한다. 절대 커밋 경로에 두지 않는다.
    """
    try:
        KIS_CONFIG_ROOT.mkdir(parents=True, exist_ok=True)
        with open(KIS_DEVLP_YAML, "w", encoding="UTF-8") as f:
            yaml.safe_dump(settings._kis_yaml, f, allow_unicode=True, sort_keys=False)
        os.chmod(KIS_DEVLP_YAML, 0o600)
    except OSError as exc:
        raise ConfigError(f"kis_devlp.yaml 생성 실패: {exc}") from exc
    return KIS_DEVLP_YAML


# ── import 시 1회 실행: 검증 + yaml 생성을 보장 ──
# 이로써 어떤 모듈이든 `import config`를 vendor kis import보다 먼저 하면 안전하다.
settings: Settings = load_settings()
bootstrap_kis_yaml(settings)
