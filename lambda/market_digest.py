"""장마감 TOP 10 등락률 + 뉴스 분석 → Discord 전송 핸들러.

Lambda 핸들러: handler(event, context)
로컬 실행:    python market_digest.py
환경변수:     .env 또는 Lambda 환경변수
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

# 로컬 실행 시 .env 로드 (Lambda에서는 환경변수가 이미 주입됨)
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:
    pass

import discord_sender
import kis_client as kis
import llm_summarizer
import news_crawler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


def _run() -> None:
    logger.info("market_digest 시작")

    # 1. KIS 인증 + TOP 10 조회
    try:
        client = kis.build_client()
        movers = client.get_top_movers(n=10)
    except Exception as exc:  # noqa: BLE001
        logger.exception("KIS 조회 실패, 중단")
        raise

    gainers = movers["gainers"]
    losers = movers["losers"]
    logger.info("상승 %d종목 / 하락 %d종목", len(gainers), len(losers))

    # 2. 시세 + 뉴스 크롤링 (병렬)
    def _fetch_stock_data(stock):
        sise = news_crawler.fetch_sise(stock.code)
        stock.prev_price = sise["prev"]
        stock.change_price = stock.price - sise["prev"] if sise["prev"] else 0
        stock.high = sise["high"]
        stock.low = sise["low"]
        stock.open_price = sise["open"]
        stock.volume = sise["volume"]
        stock.trade_amount = sise["amount"]
        stock.articles = news_crawler.fetch_articles(stock.code, limit=3)

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch_stock_data, s): s for s in gainers + losers}
        for f in as_completed(futures):
            f.result()

    # 3. LLM 요약 (병렬)
    summarizer = llm_summarizer.build_summarizer()

    def _summarize(stock):
        stock.summary = summarizer.summarize(
            name=stock.name,
            code=stock.code,
            change_rate=stock.change_rate,
            articles=stock.articles,
        )

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_summarize, s): s for s in gainers + losers}
        for f in as_completed(futures):
            f.result()

    # 4. Discord 전송
    webhook_url = os.environ["DISCORD_WEBHOOK_URL"]
    discord_sender.send_digest(webhook_url, gainers, losers)

    logger.info("market_digest 완료")


def handler(event: dict, context: object) -> dict:
    """AWS Lambda 엔트리포인트."""
    try:
        _run()
        return {"statusCode": 200, "body": "ok"}
    except Exception as exc:  # noqa: BLE001
        logger.exception("handler 오류")
        return {"statusCode": 500, "body": str(exc)}


if __name__ == "__main__":
    _run()
