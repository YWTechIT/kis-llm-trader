"""디스코드 양방향 조회 봇 — 채널에서 잔고/보유/체결/매수가능을 질의하면 응답한다.

설계
- 기존 webhook 알림(notify/discord.py)은 그대로 두고 **수신 전용 봇**을 별도 추가한다.
- 봇은 자체 asyncio 이벤트 루프를 **데몬 스레드**에서 돌린다(Trader가 기동/종료).
- broker 호출은 throttle에서 time.sleep하는 blocking 코드라, async 핸들러에서
  직접 부르면 봇 heartbeat가 멈춘다 → `asyncio.to_thread`로 위임. 그 안에서 Broker의
  RLock이 매매 루프와 직렬화하여 KIS 초당 호출제한을 지킨다.
- **봇/조회 실패가 매매 루프를 절대 죽이지 않는다**(notify/discord.py와 동일 철학):
  모든 핸들러 본문을 try/except로 감싸고, 실패해도 짧은 에러 임베드로만 응답한다.

보안
- 봇 토큰은 크리덴셜 — 로그/임베드에 절대 노출하지 않는다.
- 계좌번호는 임베드에 출력하지 않는다(잔고/보유 수치만).
- allowed_channel_ids로 지정 채널에서만 동작 → 계좌 정보(민감) 유출 방지.

주의: Message Content Intent를 Developer Portal에서 활성화해야 메시지 본문을 읽을 수 있다.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import discord

logger = logging.getLogger(__name__)

# webhook 알림과 시각적으로 맞춘 색상
_COLOR_INFO = 0x3498DB    # blue
_COLOR_OK = 0x2ECC71      # green
_COLOR_ERROR = 0xC0392B   # dark red

_HELP_TEXT = (
    "**사용법**\n"
    "`!잔고` / `잔고` — 계좌잔고(총평가·예수금·주문가능·평가손익 합계)\n"
    "`!보유` / `보유종목` — 보유종목별 매수가·현재가·손익·수익률\n"
    "`!체결` / `거래내역` — 당일 체결 내역\n"
    "`!매수가능 005930 [가격]` — 종목 매수가능 금액/수량\n"
    "`!순위` — 거래량+등락률 순위 TOP 10 (ETF 제외)\n"
    "`!순위 etf` — 거래량+등락률 순위 TOP 10 (ETF 포함)\n"
    "`!거래량` — 거래량 순위만 / `!등락` — 등락률 순위만\n"
    "`!도움` / `!help` — 이 도움말"
)

# `!순위 etf` / `!순위 전체` 등 ETF 포함 토글로 인식할 인자
_INCLUDE_ETF_ARGS = ("etf", "전체", "all")


class TraderBot:
    def __init__(self, token: str, broker, *, allowed_channel_ids: list[int] | None = None,
                 is_paper: bool = False) -> None:
        """token: 봇 토큰(크리덴셜). broker: 인증된 Broker 인스턴스(매매 루프와 공유).
        allowed_channel_ids: 조회 허용 채널 ID 목록(비어 있으면 전체 허용).
        is_paper: 모의/실제 라벨 표기용.
        """
        self._token = token
        self._broker = broker
        self._allowed_channel_ids = allowed_channel_ids or []
        self._is_paper = is_paper
        self._thread: threading.Thread | None = None

        intents = discord.Intents.default()
        intents.message_content = True  # ⚠ Developer Portal에서도 활성화 필수
        self._client = discord.Client(intents=intents)
        self._register_handlers()

    # ── 라벨 ──
    @property
    def _env_tag(self) -> str:
        return "🧪 모의투자" if self._is_paper else "🔴 실제투자"

    # ── 이벤트 등록 ──
    def _register_handlers(self) -> None:
        client = self._client

        @client.event
        async def on_ready() -> None:  # noqa: D401
            # 사용자명은 노출돼도 무방(토큰 아님). 봇 정상 기동 확인용.
            logger.info("Discord 봇 로그인: %s", client.user)

        @client.event
        async def on_message(message: discord.Message) -> None:
            # 봇/자기 메시지 무시 → 무한루프 방지
            if message.author.bot:
                return
            # 지정 채널 외 무시(계좌 정보 유출 방지)
            if self._allowed_channel_ids and message.channel.id not in self._allowed_channel_ids:
                return
            content = (message.content or "").strip()
            if not content:
                return
            try:
                embed = await self._handle(content)
            except Exception as exc:  # noqa: BLE001 — 핸들러 실패가 봇/매매를 죽이지 않게
                logger.exception("조회 처리 실패: %s", content)
                embed = self._error_embed("조회 실패", f"{type(exc).__name__}: {exc}")
            if embed is None:
                return  # 인식 못한 입력은 무응답(잡음 방지)
            try:
                await message.channel.send(embed=embed)
            except Exception:  # noqa: BLE001 — 전송 실패도 흐름을 막지 않는다
                logger.exception("디스코드 응답 전송 실패")

    # ── 명령 라우팅 ──
    async def _handle(self, content: str) -> discord.Embed | None:
        parts = content.split()
        cmd = parts[0].lstrip("!").lower()

        if cmd in ("잔고", "계좌", "balance"):
            balance = await asyncio.to_thread(self._broker.get_balance)
            return self._balance_embed(balance)
        if cmd in ("보유", "보유종목", "holdings"):
            balance = await asyncio.to_thread(self._broker.get_balance)
            return self._holdings_embed(balance)
        if cmd in ("체결", "거래내역", "fills"):
            fills = await asyncio.to_thread(self._broker.get_filled)
            return self._fills_embed(fills)
        if cmd in ("매수가능", "orderable"):
            if len(parts) < 2:
                return self._error_embed("입력 오류", "예: `!매수가능 005930 [가격]`")
            code = parts[1].strip()
            price = 0
            if len(parts) >= 3:
                try:
                    price = int(parts[2].replace(",", ""))
                except ValueError:
                    return self._error_embed("입력 오류", "가격은 숫자여야 합니다.")
            data = await asyncio.to_thread(self._broker.get_orderable_cash, code, price)
            return self._orderable_embed(data)
        if cmd in ("순위", "rank"):
            # 거래량 + 등락률 순위를 한 임베드에 함께 보여준다.
            include_etf = len(parts) > 1 and parts[1].lstrip("!").lower() in _INCLUDE_ETF_ARGS
            vol_rows, flt_rows = await asyncio.gather(
                asyncio.to_thread(self._broker.get_volume_rank, top=10,
                                  exclude_etf=not include_etf),
                asyncio.to_thread(self._broker.get_fluctuation, top=10,
                                  exclude_etf=not include_etf),
            )
            return self._combined_rank_embed(vol_rows, flt_rows, include_etf)
        if cmd in ("거래량", "volume"):
            include_etf = len(parts) > 1 and parts[1].lstrip("!").lower() in _INCLUDE_ETF_ARGS
            rows = await asyncio.to_thread(
                self._broker.get_volume_rank, top=10, exclude_etf=not include_etf
            )
            tag = " (ETF 포함)" if include_etf else " (ETF 제외)"
            return self._ranking_embed(f"📊 거래량 순위 TOP 10{tag}", rows, include_etf)
        if cmd in ("등락", "등락률", "fluctuation"):
            include_etf = len(parts) > 1 and parts[1].lstrip("!").lower() in _INCLUDE_ETF_ARGS
            rows = await asyncio.to_thread(
                self._broker.get_fluctuation, top=10, exclude_etf=not include_etf
            )
            return self._ranking_embed("🚀 등락률 순위 TOP 10", rows, include_etf)
        if cmd in ("도움", "help", "명령어"):
            return self._info_embed("🤖 트레이더 조회봇", _HELP_TEXT)
        return None

    # ── 임베드 빌더 ──
    def _base(self, title: str, color: int) -> discord.Embed:
        embed = discord.Embed(title=title, color=color)
        embed.set_footer(text=self._env_tag)
        return embed

    def _info_embed(self, title: str, desc: str) -> discord.Embed:
        embed = self._base(title, _COLOR_INFO)
        embed.description = desc
        return embed

    def _error_embed(self, title: str, desc: str) -> discord.Embed:
        return self._info_embed(f"⚠️ {title}", desc[:1500])

    def _ranking_embed(self, title: str, rows: list[dict],
                       include_etf: bool = False) -> discord.Embed:
        """거래량/등락률 순위 리스트 임베드. 빈 응답이면 안내 문구."""
        embed = self._base(title, _COLOR_INFO)
        if not rows:
            embed.description = "순위 데이터가 없습니다(장중에만 제공)."
            return embed
        lines = []
        for r in rows:
            vol = r.get("volume", 0) or 0
            vol_str = f"{vol // 10_000:,}만주" if vol >= 10_000 else f"{vol:,}주"
            lines.append(
                f"`{r['rank']:>2}` **{r['name']}** ({r['code']})  "
                f"{r['price']:,}원  {r['change_pct']:+.2f}%  {vol_str}"
            )
        embed.description = "\n".join(lines)
        # ETF 포함/제외를 토글할 수 있음을 항상 안내(현재 상태의 반대 명령을 제시)
        hint = "ETF 제외: `!순위`" if include_etf else "ETF 포함: `!순위 etf`"
        embed.set_footer(text=f"{self._env_tag} · {hint}")
        return embed

    @staticmethod
    def _vol_rank_lines(rows: list[dict]) -> str:
        """거래량 순위 필드 본문. 종목명/코드와 수치를 2줄로 나눠 가독성을 높인다."""
        if not rows:
            return "데이터 없음(장중에만 제공)"
        lines = []
        for r in rows:
            vol = r.get("volume", 0) or 0
            vol_str = f"{vol // 10_000:,}만주" if vol >= 10_000 else f"{vol:,}주"
            lines.append(
                f"`{r['rank']:>2}` **{r['name']}** ({r['code']})  "
                f"{r['price']:,}원  {r['change_pct']:+.2f}%  {vol_str}"
            )
        return "\n".join(lines)[:1024]

    @staticmethod
    def _flt_rank_lines(rows: list[dict]) -> str:
        """등락률 순위 필드 본문. 거래량 제외하고 1줄로 표시."""
        if not rows:
            return "데이터 없음(장중에만 제공)"
        return "\n".join(
            f"`{r['rank']:>2}` **{r['name']}** ({r['code']})  "
            f"{r['price']:,}원  {r['change_pct']:+.2f}%"
            for r in rows
        )[:1024]

    def _combined_rank_embed(self, vol_rows: list[dict], flt_rows: list[dict],
                             include_etf: bool = False) -> discord.Embed:
        """거래량 + 등락률 순위를 한 임베드에 두 필드로 함께 표시."""
        etf_tag = "ETF 포함" if include_etf else "ETF 제외"
        embed = self._base(f"📈 순위 TOP 10 ({etf_tag})", _COLOR_INFO)
        embed.add_field(name="📊 거래량 순위", value=self._vol_rank_lines(vol_rows), inline=False)
        embed.add_field(name="🚀 등락률 순위", value=self._flt_rank_lines(flt_rows), inline=False)
        hint = "ETF 제외: `!순위`" if include_etf else "ETF 포함: `!순위 etf`"
        embed.set_footer(text=f"{self._env_tag} · {hint}")
        return embed

    def _balance_embed(self, balance: dict) -> discord.Embed:
        # 계좌 단위 원가(분모)가 없어 계좌 수익률 대신 평가손익 합계를 표시한다.
        positions = balance.get("positions", {}) or {}
        pnl_sum = sum(int(p.get("pnl_amt", 0)) for p in positions.values())
        embed = self._base("💼 계좌잔고", _COLOR_INFO)
        embed.add_field(name="총평가금액", value=f"{balance.get('total_eval', 0):,}원", inline=True)
        embed.add_field(name="💰 예수금", value=f"{balance.get('cash', 0):,}원", inline=True)
        embed.add_field(name="주문가능", value=f"{balance.get('available_cash', 0):,}원", inline=True)
        embed.add_field(name="평가손익 합계", value=f"{pnl_sum:,}원", inline=True)
        embed.add_field(name="보유종목 수", value=f"{len(positions)}개", inline=True)
        # 집계 아래에 보유종목 상세를 이어 붙인다(!보유와 동일 포맷).
        # 디스코드 임베드엔 <hr>이 없어, 구분선 역할의 필드를 끼워 시각 분리한다.
        if positions:
            embed.add_field(name="​", value="━━━━━ 📦 보유종목 ━━━━━", inline=False)
        truncated = self._add_holding_fields(embed, positions)
        if truncated:
            embed.set_footer(text=f"{self._env_tag} · 외 {truncated}종목 생략")
        return embed

    def _holdings_embed(self, balance: dict) -> discord.Embed:
        positions = balance.get("positions", {}) or {}
        embed = self._base("📊 보유종목", _COLOR_INFO)
        if not positions:
            embed.description = "보유 종목이 없습니다."
            return embed
        truncated = self._add_holding_fields(embed, positions)
        if truncated:
            embed.set_footer(text=f"{self._env_tag} · 외 {truncated}종목 생략")
        return embed

    @staticmethod
    def _add_holding_fields(embed: discord.Embed, positions: dict) -> int:
        """보유종목별 상세 필드를 embed에 추가하고, 잘린 종목 수를 반환한다.

        임베드 필드는 최대 25개 — 잔고 집계(5) + 구분선(1)과 합산해도 넘지
        않도록 종목은 19개로 제한하고 초과분 수를 돌려준다(호출자가 footer로 안내).
        """
        if not positions:
            return 0
        limit = 19
        for pos in list(positions.values())[:limit]:
            name = pos.get("name") or pos.get("code", "")
            code = pos.get("code", "")
            value = (
                f"수량 {pos.get('qty', 0):,}주\n"
                f"매수가 {pos.get('avg_price', 0):,} → 현재 {pos.get('current_price', 0):,}원\n"
                f"손익 {pos.get('pnl_amt', 0):,}원 ({pos.get('pnl_rate', 0.0):+.2f}%)"
            )
            embed.add_field(name=f"{name} ({code})", value=value, inline=False)
        return max(0, len(positions) - limit)

    def _fills_embed(self, fills: list[dict]) -> discord.Embed:
        embed = self._base("🧾 당일 거래내역", _COLOR_INFO)
        if not fills:
            embed.description = "당일 체결 내역이 없습니다."
            return embed
        for f in fills[:24]:
            side = "🟢 매수" if f.get("side") == "buy" else "🟠 매도"
            name = f.get("name") or f.get("code", "")
            value = (
                f"{side} {f.get('filled_qty', 0):,}주 @ {f.get('avg_price', 0):,}원\n"
                f"체결금액 {f.get('filled_amt', 0):,}원"
            )
            embed.add_field(name=f"{name} ({f.get('code', '')})", value=value, inline=False)
        if len(fills) > 24:
            embed.set_footer(text=f"{self._env_tag} · 외 {len(fills) - 24}건 생략")
        return embed

    def _orderable_embed(self, data: dict) -> discord.Embed:
        code = data.get("code", "")
        unpr = data.get("ord_unpr", 0)
        price_label = f"{unpr:,}원" if unpr else "시장가"
        embed = self._base(f"💵 매수가능 — {code}", _COLOR_OK)
        embed.add_field(name="기준가", value=price_label, inline=True)
        embed.add_field(name="미수없는 매수금액", value=f"{data.get('nrcvb_buy_amt', 0):,}원", inline=True)
        embed.add_field(name="미수없는 매수수량", value=f"{data.get('nrcvb_buy_qty', 0):,}주", inline=True)
        embed.add_field(name="최대 매수금액", value=f"{data.get('max_buy_amt', 0):,}원", inline=True)
        embed.add_field(name="최대 매수수량", value=f"{data.get('max_buy_qty', 0):,}주", inline=True)
        return embed

    # ── 수명주기 ──
    def run_in_thread(self) -> threading.Thread:
        """봇을 데몬 스레드에서 기동. client.run()이 자체 이벤트 루프를 만든다."""
        def _runner() -> None:
            try:
                # log_handler=None: discord.py가 루트 로거를 가로채지 않게(기존 로깅 유지)
                self._client.run(self._token, log_handler=None)
            except Exception:  # noqa: BLE001 — 봇 치명 오류가 프로세스를 죽이지 않게
                logger.exception("Discord 봇 실행 중단")

        thread = threading.Thread(target=_runner, name="discord-bot", daemon=True)
        thread.start()
        self._thread = thread
        return thread

    def stop(self) -> None:
        """봇 종료. 다른 스레드에서 이벤트 루프에 close를 안전하게 예약한다."""
        loop = getattr(self._client, "loop", None)
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._client.close(), loop)
        except Exception:  # noqa: BLE001 — 종료 실패가 셧다운을 막지 않게
            logger.exception("Discord 봇 종료 실패")
