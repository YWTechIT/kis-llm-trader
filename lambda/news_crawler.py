"""네이버 금융 종목 뉴스 + 시세 크롤러.

종목 코드 기반으로 최신 기사 제목 + URL, 시세 데이터를 반환한다.
실패 시 빈 값 반환 — 절대 예외 전파하지 않는다.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_TIMEOUT = 5


def _to_int(text: str) -> int:
    try:
        return int(text.replace(",", "").strip())
    except (ValueError, TypeError):
        return 0


# sptxt class → result 키 매핑
_SISE_LABEL_MAP = {
    "sp_txt2": "prev",      # 전일
    "sp_txt4": "high",      # 고가
    "sp_txt5": "low",       # 저가
    "sp_txt3": "open",      # 시가
    "sp_txt9": "volume",    # 거래량
    "sp_txt10": "amount",   # 거래대금
}


def fetch_sise(code: str) -> dict:
    """네이버 금융 시세 페이지에서 전일/고가/저가/시가/거래량/거래대금 반환.

    반환 형식: {"prev": int, "high": int, "low": int, "open": int, "volume": int, "amount": int}
    값은 span.blind 텍스트에서 추출한다.
    """
    url = f"https://finance.naver.com/item/sise.naver?code={code}"
    headers = {"User-Agent": _USER_AGENT}
    result = {"prev": 0, "high": 0, "low": 0, "open": 0, "volume": 0, "amount": 0}
    try:
        resp = requests.get(url, headers=headers, timeout=_TIMEOUT)
        resp.raise_for_status()
        resp.encoding = "euc-kr"
        soup = BeautifulSoup(resp.text, "html.parser")

        for tbl in soup.select("table"):
            if "거래량" not in tbl.get_text():
                continue
            for td in tbl.select("td"):
                label_span = td.select_one("span.sptxt")
                blind_span = td.select_one("span.blind")
                if not label_span or not blind_span:
                    continue
                classes = label_span.get("class", [])
                for cls in classes:
                    if cls in _SISE_LABEL_MAP:
                        result[_SISE_LABEL_MAP[cls]] = _to_int(blind_span.get_text())
                        break
            break
    except Exception as exc:  # noqa: BLE001
        logger.warning("시세 크롤링 실패 code=%s: %s", code, exc)
    return result


def _to_news_url(href: str) -> str:
    """네이버 금융 뉴스 읽기 URL → n.news.naver.com 직링크로 변환.

    PC/모바일 모두 정상 작동하는 URL을 반환한다.
    파싱 실패 시 원본 URL 그대로 반환.
    """
    try:
        qs = parse_qs(urlparse(href).query)
        article_id = qs["article_id"][0]
        office_id = qs["office_id"][0]
        return f"https://n.news.naver.com/article/{office_id}/{article_id}"
    except (KeyError, IndexError):
        return href


def fetch_articles(code: str, limit: int = 3) -> list[dict]:
    """네이버 금융 종목 뉴스에서 최신 기사 `limit`건을 반환.

    반환 형식: [{"title": str, "url": str}, ...]

    뉴스 목록은 iframe으로 로드되므로 iframe의 src URL을 직접 요청해야 한다.
    Referer 헤더 없이 page/clusterId 파라미터를 생략하면 빈 결과를 반환한다.
    """
    url = f"https://finance.naver.com/item/news_news.naver?code={code}&page=1&clusterId="
    headers = {
        "User-Agent": _USER_AGENT,
        "Referer": f"https://finance.naver.com/item/news.naver?code={code}",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=_TIMEOUT)
        resp.raise_for_status()
        resp.encoding = "euc-kr"
    except requests.RequestException as exc:
        logger.warning("뉴스 크롤링 실패 code=%s: %s", code, exc)
        return []

    try:
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("table.type5 tbody tr")
    except Exception as exc:  # noqa: BLE001
        logger.warning("뉴스 파싱 실패 code=%s: %s", code, exc)
        return []

    articles: list[dict] = []
    for row in rows:
        a_tag = row.select_one("td.title a")
        if not a_tag:
            continue
        title = a_tag.get_text(strip=True)
        href = a_tag.get("href", "")
        if href.startswith("/"):
            href = f"https://finance.naver.com{href}"
        url = _to_news_url(href)
        if title:
            articles.append({"title": title, "url": url})
        if len(articles) >= limit:
            break

    logger.info("뉴스 크롤링 code=%s → %d건", code, len(articles))
    return articles
