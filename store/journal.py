"""SQLite 기록 — LLM 판단/주문/체결/일일손익.

나중에 "AI가 buy&hold를 이겼나" 오프라인 분석/재현이 가능하도록 판단 입력
스냅샷·프롬프트·모델·응답을 함께 남긴다. 모든 쓰기는 try/catch로 감싸 기록 실패가
매매를 막지 않게 한다(단, 에러는 로깅).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    code TEXT PRIMARY KEY,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    code TEXT,
    action TEXT,            -- LLM 원 결정
    quantity INTEGER,
    reason TEXT,
    model TEXT,
    snapshot_json TEXT,     -- LLM에 넘긴 시세/포지션/현금 스냅샷
    final_action TEXT,      -- 가드레일 통과 후 실제 액션
    final_quantity INTEGER,
    approved INTEGER,       -- 0/1
    guard_reason TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    code TEXT,
    side TEXT,
    quantity INTEGER,
    odno TEXT,
    raw_json TEXT
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    code TEXT,
    side TEXT,
    filled_qty INTEGER,
    avg_price INTEGER,
    filled_amt INTEGER,
    odno TEXT
);
CREATE TABLE IF NOT EXISTS daily_pnl (
    trade_date TEXT PRIMARY KEY,
    realized_pnl INTEGER,
    trades INTEGER,
    total_eval INTEGER,
    buy_hold_pnl INTEGER,
    note TEXT
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Journal:
    def __init__(self, db_path: str) -> None:
        self._path = db_path
        try:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        except sqlite3.Error as exc:
            logger.error("저널 DB 초기화 실패(%s): %s", db_path, exc)
            raise

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def _exec(self, sql: str, params: tuple) -> Optional[int]:
        try:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.lastrowid
        except sqlite3.Error as exc:
            logger.error("저널 쓰기 실패: %s | sql=%s", exc, sql.split("(")[0])
            return None

    # ── 기록 메서드 ──
    def log_decision(self, *, code: str, action: str, quantity: int, reason: str,
                     model: str, snapshot: dict, final_action: str,
                     final_quantity: int, approved: bool, guard_reason: str) -> Optional[int]:
        return self._exec(
            """INSERT INTO decisions
               (ts, code, action, quantity, reason, model, snapshot_json,
                final_action, final_quantity, approved, guard_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (_now(), code, action, quantity, reason, model,
             json.dumps(snapshot, ensure_ascii=False, default=str),
             final_action, final_quantity, int(approved), guard_reason),
        )

    def log_order(self, *, code: str, side: str, quantity: int, odno: str,
                  raw: dict[str, Any]) -> Optional[int]:
        return self._exec(
            "INSERT INTO orders (ts, code, side, quantity, odno, raw_json) VALUES (?,?,?,?,?,?)",
            (_now(), code, side, quantity, odno,
             json.dumps(raw, ensure_ascii=False, default=str)),
        )

    def log_fill(self, *, code: str, side: str, filled_qty: int, avg_price: int,
                 filled_amt: int, odno: str) -> Optional[int]:
        return self._exec(
            """INSERT INTO fills (ts, code, side, filled_qty, avg_price, filled_amt, odno)
               VALUES (?,?,?,?,?,?,?)""",
            (_now(), code, side, filled_qty, avg_price, filled_amt, odno),
        )

    def upsert_daily_pnl(self, *, trade_date: str, realized_pnl: int, trades: int,
                         total_eval: int, buy_hold_pnl: int = 0, note: str = "") -> None:
        self._exec(
            """INSERT INTO daily_pnl (trade_date, realized_pnl, trades, total_eval, buy_hold_pnl, note)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(trade_date) DO UPDATE SET
                 realized_pnl=excluded.realized_pnl, trades=excluded.trades,
                 total_eval=excluded.total_eval, buy_hold_pnl=excluded.buy_hold_pnl,
                 note=excluded.note""",
            (trade_date, realized_pnl, trades, total_eval, buy_hold_pnl, note),
        )

    # ── 관심종목 관리 ──
    def add_watch(self, code: str) -> None:
        self._exec(
            "INSERT OR IGNORE INTO watchlist (code, added_at) VALUES (?, ?)",
            (code, _now()),
        )

    def remove_watch(self, code: str) -> None:
        self._exec("DELETE FROM watchlist WHERE code = ?", (code,))

    def get_watch_codes(self) -> list[str]:
        try:
            cur = self._conn.execute("SELECT code FROM watchlist ORDER BY added_at")
            return [row[0] for row in cur.fetchall()]
        except sqlite3.Error as exc:
            logger.error("관심종목 조회 실패: %s", exc)
            return []

    def count_today_orders(self, trade_date: str) -> int:
        """당일 주문 건수(거래 횟수 가드레일 보조용)."""
        try:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM orders WHERE ts LIKE ?", (f"{trade_date}%",)
            )
            return int(cur.fetchone()[0])
        except sqlite3.Error as exc:
            logger.error("주문 카운트 조회 실패: %s", exc)
            return 0
