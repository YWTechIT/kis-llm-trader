"""로컬 계좌 조회 — 잔고/보유종목/당일 거래내역/매수가능금액을 표로 출력.

봇(main.py)과 독립 실행되는 단순 조회 스크립트. 데이터는 전부 adapter/broker.py의
정규화 메서드를 재사용한다(KIS 응답 포맷을 직접 파싱하지 않음). 출력은 외부 의존성
없이 GitHub 마크다운 표 문법으로 그린다.

사용:
    uv run python view.py
"""

from __future__ import annotations

import logging

from kis_bootstrap import settings
from adapter.broker import Broker, BrokerError


def _won(n: int) -> str:
    """천단위 콤마 + 원 단위. None/실패는 '-'."""
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "-"


def _pct(r: float) -> str:
    try:
        return f"{float(r):+.2f}%"
    except (TypeError, ValueError):
        return "-"


def _md_table(title: str, headers: list[str], rows: list[list[str]],
              right_align: set[int] | None = None) -> str:
    """GitHub 마크다운 표 문자열 생성. right_align: 우측 정렬할 컬럼 인덱스 집합."""
    right_align = right_align or set()
    lines = [f"### {title}", ""]
    if not rows:
        lines.append("_(데이터 없음)_")
        return "\n".join(lines)
    lines.append("| " + " | ".join(headers) + " |")
    sep = []
    for i in range(len(headers)):
        sep.append("---:" if i in right_align else "---")
    lines.append("| " + " | ".join(sep) + " |")
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(lines)


def _section_balance(bal: dict) -> str:
    """① 계좌 잔고 — 총평가/예수금/주문가능/총손익/평가수익률."""
    positions = bal.get("positions", {})
    total_pnl = sum(p.get("pnl_amt", 0) for p in positions.values())
    cost = sum(p.get("avg_price", 0) * p.get("qty", 0) for p in positions.values())
    pnl_rate = (total_pnl / cost * 100) if cost else 0.0

    rows = [
        ["총 자산 평가액", _won(bal.get("total_eval"))],
        ["예수금", _won(bal.get("cash"))],
        ["주문가능 현금", _won(bal.get("available_cash"))],
        ["보유 매입원가 합", _won(cost)],
        ["총 평가손익", _won(total_pnl)],
        ["평가 수익률", _pct(pnl_rate)],
    ]
    return _md_table("① 계좌 잔고", ["항목", "값"], rows, right_align={1})


def _section_holdings(bal: dict) -> str:
    """② 보유종목 — 종목/수량/매수평균가/현재가/평가금액/평가손익/수익률."""
    positions = bal.get("positions", {})
    rows = []
    for p in positions.values():
        rows.append([
            f"{p.get('name', '')} ({p.get('code', '')})",
            _won(p.get("qty")),
            _won(p.get("avg_price")),
            _won(p.get("current_price")),
            _won(p.get("eval_amt")),
            _won(p.get("pnl_amt")),
            _pct(p.get("pnl_rate")),
        ])
    headers = ["종목", "수량", "매수평균가", "현재가", "평가금액", "평가손익", "수익률"]
    return _md_table("② 보유종목", headers, rows, right_align={1, 2, 3, 4, 5, 6})


def _section_fills(fills: list[dict]) -> str:
    """③ 당일 거래내역 — KIS 당일 체결조회."""
    rows = []
    for f in fills:
        rows.append([
            f"{f.get('name', '')} ({f.get('code', '')})",
            "매도" if f.get("side") == "sell" else "매수",
            _won(f.get("filled_qty")),
            _won(f.get("avg_price")),
            _won(f.get("filled_amt")),
            str(f.get("odno", "")),
        ])
    headers = ["종목", "구분", "체결수량", "평균체결가", "체결금액", "주문번호"]
    if not rows:
        return "### ③ 당일 거래내역\n\n_당일 체결 없음_"
    return _md_table("③ 당일 거래내역", headers, rows, right_align={2, 3, 4})


def _section_buyable(buyables: list[dict]) -> str:
    """④ 매수가능 금액 — 종목별(시장가 기준)."""
    rows = []
    for b in buyables:
        rows.append([
            str(b.get("code", "")),
            _won(b.get("nrcvb_buy_amt")),
            _won(b.get("nrcvb_buy_qty")),
            _won(b.get("max_buy_amt")),
            _won(b.get("max_buy_qty")),
        ])
    headers = ["종목코드", "미수없는 매수금액", "미수없는 수량", "최대 매수금액", "최대 수량"]
    return _md_table("④ 매수가능 금액 (시장가 기준)", headers, rows,
                     right_align={1, 2, 3, 4})


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    broker = Broker()
    try:
        broker.authenticate()
    except BrokerError as exc:
        print(f"[인증 실패] {exc}")
        return

    mode = "모의투자" if settings.is_paper else "실제투자"
    print(f"# KIS 계좌 조회 ({mode})\n")

    # 각 조회는 독립적으로 try/except — 한 호출이 실패해도 나머지 표는 출력한다.
    bal: dict = {}
    try:
        bal = broker.get_balance()
    except BrokerError as exc:
        print(f"[잔고 조회 실패] {exc}\n")

    if bal:
        print(_section_balance(bal))
        print()
        print(_section_holdings(bal))
        print()

    try:
        fills = broker.get_filled()
        print(_section_fills(fills))
        print()
    except BrokerError as exc:
        print(f"[거래내역 조회 실패] {exc}\n")

    # 매수가능금액은 종목별 조회 → 관심종목 ∪ 보유종목.
    codes = list(dict.fromkeys(
        list(settings.watch_codes) + list(bal.get("positions", {}).keys())
    ))
    buyables = []
    for code in codes:
        try:
            buyables.append(broker.get_buyable(code))
        except BrokerError as exc:
            print(f"[매수가능 조회 실패: {code}] {exc}")
    if buyables:
        print(_section_buyable(buyables))


if __name__ == "__main__":
    main()
