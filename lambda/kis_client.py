"""KIS REST API 경량 래퍼 — 토큰 발급 + 등락률 순위 조회.

Lambda 환경(stateless)에서 실행마다 토큰을 새로 발급한다.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import requests

logger = logging.getLogger(__name__)

_PAPER_BASE = "https://openapivts.koreainvestment.com:29443"
_PROD_BASE = "https://openapi.koreainvestment.com:9443"
_TIMEOUT = 10


@dataclass
class StockMover:
    code: str
    name: str
    price: int
    change_rate: float  # 등락률(%), 양수=상승/음수=하락
    prev_price: int = 0     # 전일
    change_price: int = 0   # 전일대비
    high: int = 0           # 고가
    low: int = 0            # 저가
    open_price: int = 0     # 시가
    volume: int = 0         # 거래량
    trade_amount: int = 0   # 거래대금(백만)
    summary: str = ""       # LLM 요약 (나중에 채워짐)
    articles: list[dict] = field(default_factory=list)  # 뉴스 기사 목록


class KISClient:
    def __init__(
        self,
        app_key: str,
        app_secret: str,
        *,
        is_paper: bool = False,
    ) -> None:
        self._app_key = app_key
        self._app_secret = app_secret
        self._base = _PAPER_BASE if is_paper else _PROD_BASE
        self._token: str = ""

    def authenticate(self) -> None:
        url = f"{self._base}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": self._app_key,
            "appsecret": self._app_secret,
        }
        resp = requests.post(url, json=body, timeout=_TIMEOUT)
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        logger.info("KIS 토큰 발급 완료")

    def _headers(self, tr_id: str) -> dict:
        return {
            "Content-Type": "application/json",
            "authorization": f"Bearer {self._token}",
            "appkey": self._app_key,
            "appsecret": self._app_secret,
            "tr_id": tr_id,
        }

    def get_top_movers(self, n: int = 10) -> dict[str, list[StockMover]]:
        """상승/하락 각 n개 반환. {"gainers": [...], "losers": [...]}"""
        gainers = self._fetch_rank(sort="0", n=n)
        losers = self._fetch_rank(sort="1", n=n)
        return {"gainers": gainers, "losers": losers}

    def _fetch_rank(self, sort: str, n: int) -> list[StockMover]:
        """
        FHPST01700000 — 국내주식 등락률 순위
        sort: "0"=상승율순, "1"=하락율순
        """
        url = f"{self._base}/uapi/domestic-stock/v1/ranking/fluctuation"
        params = {
            "fid_cond_mrkt_div_code": "J",      # 주식시장
            "fid_cond_scr_div_code": "20170",
            "fid_input_iscd": "0000",            # 전체
            "fid_rank_sort_cls_code": sort,
            "fid_input_cnt_1": "0",
            "fid_prc_cls_code": "1",
            "fid_input_price_1": "",
            "fid_input_price_2": "",
            "fid_vol_cnt": "",
            "fid_trgt_cls_code": "0",
            "fid_trgt_exls_cls_code": "0000001100",  # ETF + ETN 제외
            "fid_div_cls_code": "0",
            "fid_rsfl_rate1": "",
            "fid_rsfl_rate2": "",
        }
        try:
            resp = requests.get(
                url,
                headers=self._headers("FHPST01700000"),
                params=params,
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            logger.error("KIS 등락률 순위 조회 실패 sort=%s: %s", sort, exc)
            return []

        output = data.get("output", [])
        result = []
        for row in output:
            if len(result) >= n:
                break
            code = row.get("stck_shrn_iscd", "")
            name = row.get("hts_kor_isnm", "")
            if code.endswith("5") or name.endswith("우") or name.endswith("우B"):
                continue
            try:
                result.append(StockMover(
                    code=code,
                    name=name,
                    price=int(row.get("stck_prpr", 0)),
                    change_rate=float(row.get("prdy_ctrt", 0)),
                ))
            except (ValueError, KeyError) as exc:
                logger.warning("종목 파싱 오류: %s — %s", row, exc)
        return result


def build_client() -> KISClient:
    """환경변수에서 KIS 클라이언트를 생성하고 인증."""
    kis_env = os.environ.get("KIS_ENV", "vps")
    if kis_env == "prod":
        app_key = os.environ["KIS_PROD_APP_KEY"]
        app_secret = os.environ["KIS_PROD_APP_SECRET"]
        is_paper = False
    else:
        app_key = os.environ["KIS_PAPER_APP_KEY"]
        app_secret = os.environ["KIS_PAPER_APP_SECRET"]
        is_paper = True

    client = KISClient(app_key, app_secret, is_paper=is_paper)
    client.authenticate()
    return client
