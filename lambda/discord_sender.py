"""Discord Webhook으로 장마감 등락률 TOP 10 embed 전송.

notify/discord.py 패턴 재사용:
- requests.post() + embed JSON
- 실패해도 예외 전파하지 않는다.
"""

from __future__ import annotations

import logging
import os
from datetime import date

import requests

from kis_client import StockMover

logger = logging.getLogger(__name__)

_TIMEOUT = 10
_COLOR_GAIN = 0xE74C3C   # 빨강 (상승 — 한국 주식 관례)
_COLOR_LOSS = 0x3498DB   # 파랑 (하락 — 한국 주식 관례)
_COLOR_INFO = 0x9B59B6   # 보라 (헤더)
_USERNAME = "market-digest"


def _post(webhook_url: str, payload: dict) -> None:
    try:
        resp = requests.post(webhook_url, json=payload, timeout=_TIMEOUT)
        if resp.status_code >= 400:
            logger.error("Discord 전송 실패 status=%s body=%s",
                         resp.status_code, resp.text[:200])
    except requests.RequestException as exc:
        logger.error("Discord 전송 예외: %s", exc)


def _mover_embed(stock: StockMover, is_gain: bool, rank: int = 0) -> dict:
    if is_gain:
        sign, rate_emoji, price_emoji, color = "▲", "🔴", "📈", _COLOR_GAIN
    else:
        sign, rate_emoji, price_emoji, color = "▼", "🔵", "📉", _COLOR_LOSS

    change_sign = "+" if stock.change_price >= 0 else ""

    fields = [
        {"name": "💰 현재가",  "value": f"{stock.price:,}원", "inline": True},
        {"name": f"{rate_emoji} 등락률", "value": f"{sign} {abs(stock.change_rate):.2f}%", "inline": True},
        {"name": f"{rate_emoji} 전일대비", "value": f"{change_sign}{stock.change_price:,}원", "inline": True},
        {"name": "📅 전일",   "value": f"{stock.prev_price:,}원" if stock.prev_price else "-", "inline": True},
        {"name": "⬆️ 고가",   "value": f"{stock.high:,}원" if stock.high else "-", "inline": True},
        {"name": "⬇️ 저가",   "value": f"{stock.low:,}원" if stock.low else "-", "inline": True},
        {"name": "🔔 시가",   "value": f"{stock.open_price:,}원" if stock.open_price else "-", "inline": True},
        {"name": "📊 거래량", "value": f"{stock.volume:,}주" if stock.volume else "-", "inline": True},
        {"name": "💵 거래대금", "value": f"{stock.trade_amount:,}백만원" if stock.trade_amount else "-", "inline": True},
    ]
    if stock.summary:
        fields.append({"name": "📰 AI 뉴스 요약", "value": stock.summary[:1000], "inline": False})

    news_links = ""
    for a in stock.articles[:3]:
        news_links += f"• {a['title'][:40]}\n{a['url']}\n"
    if news_links:
        fields.append({"name": "🔗 관련 기사", "value": news_links.strip(), "inline": False})

    rank_str = f"#{rank} " if rank else ""
    return {
        "title": f"{price_emoji} {rank_str}{sign} {stock.name} ({stock.code})",
        "color": color,
        "fields": fields,
    }


def send_digest(
    webhook_url: str,
    gainers: list[StockMover],
    losers: list[StockMover],
) -> None:
    today = date.today().strftime("%Y-%m-%d")

    # 헤더 메시지 — 상승/하락 요약 리스트
    gain_list = "\n".join(
        f"`#{i}` **{s.name}** ({s.code}) ▲ {s.change_rate:.2f}%"
        for i, s in enumerate(gainers, 1)
    )
    loss_list = "\n".join(
        f"`#{i}` **{s.name}** ({s.code}) ▼ {abs(s.change_rate):.2f}%"
        for i, s in enumerate(losers, 1)
    )
    header_embed = {
        "title": f"📊 장마감 등락률 TOP 10 — {today}",
        "color": _COLOR_INFO,
        "fields": [
            {"name": "🔴 상승 TOP 10", "value": gain_list or "-", "inline": True},
            {"name": "🔵 하락 TOP 10", "value": loss_list or "-", "inline": True},
        ],
    }
    _post(webhook_url, {"username": _USERNAME, "embeds": [header_embed]})

    # 상승 종목 (종목당 1개씩 전송)
    for rank, stock in enumerate(gainers, 1):
        _post(webhook_url, {"username": _USERNAME, "embeds": [_mover_embed(stock, is_gain=True, rank=rank)]})

    # 하락 종목 (종목당 1개씩 전송)
    for rank, stock in enumerate(losers, 1):
        _post(webhook_url, {"username": _USERNAME, "embeds": [_mover_embed(stock, is_gain=False, rank=rank)]})

    logger.info("Discord 전송 완료 — 상승 %d / 하락 %d", len(gainers), len(losers))
