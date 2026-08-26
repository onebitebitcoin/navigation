"""
거래소 공지사항 스크래퍼

각 함수 반환 형식:
  list[dict] where each dict has:
    - exchange: str
    - title: str
    - url: str | None
    - published_at: datetime | None

다국어 지원 설계:
  글로벌 거래소(Binance 등)는 _BINANCE_NOTICE_LOCALE 상수로 기본 언어를 제어한다.
  향후 한국어 공지 추가 시 상수를 'ko'로 변경하거나, get_all_notices()에서
  locale='ko' 호출을 병행 추가하면 된다.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

from backend.app.domain.notice_match import (
    FEE_KEYWORDS,
    has_btc,
    has_usdt,
    is_relevant_title,
    keyword_in_title,
)

logger = logging.getLogger(__name__)

_TIMEOUT = 12
_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json',
    'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7',
}
_MAX_NOTICES = 5   # 반환할 최대 건수
_MAX_FETCH = 30    # 필터링 전 최대 수집 건수 (국내 거래소용)
_BINANCE_MAX_FETCH = 20  # Binance API pageSize 최대 허용값 (25 이상 400 에러)

# --- Binance 다국어 설정 ---
# 'en' → 영어 공지, 'ko' → 한국어 공지
# 한국어 지원 시: 이 값을 'ko'로 변경하거나 get_all_notices()에서 병행 호출
_BINANCE_NOTICE_LOCALE: str = 'en'

_BINANCE_LOCALE_HEADERS: dict[str, dict] = {
    'en': {'Accept-Language': 'en-US,en;q=0.9'},
    'ko': {'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7'},
}
_BINANCE_LOCALE_URL_PREFIX: dict[str, str] = {
    'en': 'https://www.binance.com/en/support/announcement',
    'ko': 'https://www.binance.com/ko/support/announcement',
}

# 키워드 매칭은 backend.app.domain.notice_match 로 단일화(SSoT).
# 아래는 모듈 내부/테스트 호환을 위한 얇은 위임 래퍼.
_keyword_in_title = keyword_in_title


def _is_relevant(title: str) -> bool:
    """국내 거래소 공지 관련성 판단: 공통 키워드 + 수수료 특화 키워드

    수수료 이벤트 공지(코인원 '전 종목 거래 수수료 0원' 등)는 실제 수수료 계산에
    반영되므로, 어드민 공지 피드에서 사람이 교차검증할 수 있도록 함께 노출한다.
    """
    return is_relevant_title(title, include_fee=True)


def _is_relevant_for_binance(title: str) -> bool:
    """Binance 공지 관련성 판단: 공통 키워드 + 수수료 특화 키워드"""
    return is_relevant_title(title, include_fee=True)


# 카탈로그별 필터 전략
#   skip        → 무관 카탈로그, 전량 제외
#   btc_only    → 제목에 BTC/Bitcoin 직접 언급 필수
#   keyword     → 공통 BTC/USDT + 수수료 특화 키워드 (_is_relevant_for_binance)
#   fee_or_btc  → 수수료 이벤트 키워드 또는 BTC 직접 언급 (활동/프로모션용)
#   maintenance → BTC 무조건 통과, USDT는 출금/점검 맥락일 때만
_BINANCE_CATALOG_STRATEGY: dict[int, str] = {
    48: 'btc_only',      # New Cryptocurrency Listing — XYZUSDT 선물 상장 제외
    49: 'keyword',       # Latest Binance News — 수수료/BTC/USDT 뉴스
    50: 'skip',          # New Fiat Listings — 무관
    51: 'skip',          # API Updates — 무관
    93: 'fee_or_btc',    # Latest Activities — BTC 리워드·수수료 이벤트만
    128: 'skip',         # Crypto Airdrop — 무관
    157: 'maintenance',  # Maintenance Updates — BTC/USDT 네트워크 점검만
    161: 'btc_only',     # Delisting — BTC/USDT 직접 관련만
}
_BINANCE_CATALOG_DEFAULT_STRATEGY = 'keyword'  # 미등록 카탈로그 기본값

_MAINTENANCE_CONTEXT_KEYWORDS = ('withdrawal', 'deposit', 'maintenance', 'suspend', 'cease', 'halt')


def _binance_catalog_filter(catalog_id: int, title: str) -> bool:
    """카탈로그 ID별 관련성 판단 — USDT→BTC 경로에 영향을 주는 공지인지 확인"""
    strategy = _BINANCE_CATALOG_STRATEGY.get(catalog_id, _BINANCE_CATALOG_DEFAULT_STRATEGY)
    lower = title.lower()

    if strategy == 'skip':
        return False
    if strategy == 'btc_only':
        return has_btc(lower)
    if strategy == 'keyword':
        return _is_relevant_for_binance(title)
    if strategy == 'fee_or_btc':
        return has_btc(lower) or any(kw.lower() in lower for kw in FEE_KEYWORDS)
    if strategy == 'maintenance':
        if has_btc(lower):
            return True
        if has_usdt(lower):
            return any(kw in lower for kw in _MAINTENANCE_CONTEXT_KEYWORDS)
        return False
    return _is_relevant_for_binance(title)


def _parse_iso(s: str | None) -> datetime | None:
    """ISO 8601 문자열을 UTC datetime으로 파싱"""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


def _parse_epoch(ts: int | None) -> datetime | None:
    """Unix 타임스탬프(초)를 UTC datetime으로 파싱"""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


def _parse_date_str(s: str | None) -> datetime | None:
    """YYYY.MM.DD 형식 날짜 문자열 파싱"""
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), '%Y.%m.%d')
    except Exception:
        return None


def fetch_upbit_notices() -> list[dict]:
    """Upbit 공지사항 API 스크래핑 (api-manager.upbit.com)"""
    exchange = 'upbit'
    url = f'https://api-manager.upbit.com/api/v1/announcements?os=web&page=1&per_page={_MAX_FETCH}&category=all'
    headers = {**_HEADERS, 'Referer': 'https://upbit.com/'}
    try:
        resp = requests.get(url, headers=headers, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if not data.get('success'):
            return []
        notices = data.get('data', {}).get('notices', [])
        results = []
        for item in notices:
            title = item.get('title', '').strip()
            notice_id = item.get('id')
            if not title or not notice_id:
                continue
            if not _is_relevant(title):
                continue
            results.append({
                'exchange': exchange,
                'title': title,
                'url': f'https://upbit.com/service_center/notice/{notice_id}',
                'published_at': _parse_iso(item.get('listed_at')),
            })
            if len(results) >= _MAX_NOTICES:
                break
        return results
    except Exception as e:
        logger.warning('Upbit notice scrape failed: %s', e)
        return []


def fetch_bithumb_notices() -> list[dict]:
    """Bithumb 공지사항 스크래핑 (feed.bithumb.com - SSR 페이지, Scrapling Fetcher 사용)"""
    exchange = 'bithumb'
    try:
        from scrapling.fetchers import Fetcher
        f = Fetcher()
        page = f.get('https://feed.bithumb.com/notice')
        results = []
        for a in page.css('a[href*="/notice/"]')[:_MAX_FETCH]:
            href = a.attrib.get('href', '')
            # 제목: link-title 클래스 스팬
            title_spans = a.css('span[class*="link-title"]')
            title = title_spans[0].text.strip() if title_spans else ''
            # 날짜: link-date 클래스 스팬
            date_spans = a.css('span[class*="link-date"]')
            date_str = date_spans[0].text.strip() if date_spans else None

            if not title or len(title) < 3:
                continue
            if not _is_relevant(title):
                continue
            full_url = f'https://feed.bithumb.com{href}' if href.startswith('/') else href
            results.append({
                'exchange': exchange,
                'title': title,
                'url': full_url,
                'published_at': _parse_date_str(date_str),
            })
            if len(results) >= _MAX_NOTICES:
                break
        return results
    except ImportError:
        logger.debug('Scrapling not installed, skipping Bithumb notices')
        return []
    except Exception as e:
        logger.warning('Bithumb notice scrape failed: %s', e)
        return []


def fetch_coinone_notices() -> list[dict]:
    """Coinone 공지사항 API 스크래핑 (api-gateway.coinone.co.kr)"""
    exchange = 'coinone'
    url = f'https://api-gateway.coinone.co.kr/notice/v1/announcements/posts?includePin=false&page=0&pageSize={_MAX_FETCH}'
    headers = {**_HEADERS, 'Referer': 'https://coinone.co.kr/'}
    try:
        resp = requests.get(url, headers=headers, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        body = data.get('body', {})
        notices = body.get('notices', [])
        results = []
        for item in notices:
            title = item.get('title', '').strip()
            notice_id = item.get('id')
            if not title or not notice_id:
                continue
            if not _is_relevant(title):
                continue
            results.append({
                'exchange': exchange,
                'title': title,
                'url': f'https://coinone.co.kr/info/notice/{notice_id}',
                'published_at': _parse_epoch(item.get('createdAt')),
            })
            if len(results) >= _MAX_NOTICES:
                break
        return results
    except Exception as e:
        logger.warning('Coinone notice scrape failed: %s', e)
        return []


def fetch_binance_notices(locale: str = _BINANCE_NOTICE_LOCALE) -> list[dict]:
    """Binance 공지사항 API 스크래핑 (비공개 CMS API 사용)

    Args:
        locale: 'en' (기본값) 또는 'ko'. 한국어 공지 추가 시 'ko' 전달.

    Binance API 응답 구조:
      data.catalogs[].articles[] 또는 data.articles[]
      releaseDate는 밀리초 단위 Unix 타임스탬프
    """
    exchange = 'binance'
    api_url = 'https://www.binance.com/bapi/composite/v1/public/cms/article/list/query'
    url_prefix = _BINANCE_LOCALE_URL_PREFIX.get(locale, _BINANCE_LOCALE_URL_PREFIX['en'])

    headers = {
        **_HEADERS,
        **_BINANCE_LOCALE_HEADERS.get(locale, _BINANCE_LOCALE_HEADERS['en']),
        'Referer': 'https://www.binance.com/',
        'Origin': 'https://www.binance.com',
    }
    params: dict[str, str] = {
        'type': '1',
        'pageNo': '1',
        'pageSize': str(_BINANCE_MAX_FETCH),
    }

    try:
        resp = requests.get(api_url, headers=headers, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        if data.get('code') != '000000':
            logger.warning('Binance notice API non-success code: %s', data.get('code'))
            return []

        data_body: dict = data.get('data') or {}
        catalogs: list[dict] = data_body.get('catalogs') or []

        results: list[dict] = []

        def _parse_article(item: dict, catalog_id: int) -> dict | None:
            title = (item.get('title') or '').strip()
            article_code = item.get('code') or str(item.get('id') or '')
            if not title or not article_code:
                return None
            if not _binance_catalog_filter(catalog_id, title):
                return None
            release_ms: int | None = item.get('releaseDate')
            published_at: datetime | None = None
            if release_ms:
                try:
                    published_at = datetime.fromtimestamp(release_ms / 1000, tz=timezone.utc).replace(tzinfo=None)
                except (OSError, OverflowError, ValueError):
                    pass
            return {
                'exchange': exchange,
                'title': title,
                'url': f'{url_prefix}/{article_code}',
                'published_at': published_at,
            }

        if catalogs:
            # 카탈로그별 전략 적용
            for catalog in catalogs:
                catalog_id = int(catalog.get('catalogId') or -1)
                for item in catalog.get('articles') or []:
                    entry = _parse_article(item, catalog_id)
                    if entry:
                        results.append(entry)
        else:
            # flat articles 구조: 카탈로그 정보 없으므로 keyword 전략 사용
            for item in data_body.get('articles') or []:
                entry = _parse_article(item, -1)
                if entry:
                    results.append(entry)

        # 전체 매칭 중 최신순 상위 _MAX_NOTICES개 반환
        results.sort(key=lambda x: x['published_at'] or datetime.min, reverse=True)
        return results[:_MAX_NOTICES]
    except Exception as e:
        logger.warning('Binance notice scrape failed: %s', e)
        return []


def fetch_korbit_notices() -> list[dict]:
    """Korbit 공지사항 스크래핑 (현재 모든 접근 방법이 홈으로 리다이렉트됨)"""
    # Korbit의 /announce 페이지는 headless browser 접근 시 홈으로 리다이렉트되어
    # 현재 스크래핑이 불가능합니다.
    logger.debug('Korbit notice scraping skipped (page redirects to homepage)')
    return []


def fetch_notices_for_exchange(exchange: str, extra_keywords: list[str]) -> list[dict]:
    """특정 거래소의 공지를 extra_keywords 기준으로 탐색.

    extra_keywords: 변경된 코인명/네트워크명 (예: ['USDT', 'Aptos'])
    기존 키워드 + extra_keywords 모두 포함된 공지를 반환.
    """
    targets = [kw for kw in extra_keywords if kw]

    def _is_targeted(title: str) -> bool:
        lower = title.lower()
        # 티커(BTC/USDT)는 라틴 경계 매칭 → HUSDT 등 부분 일치 오탐 방지
        return any(_keyword_in_title(lower, kw) for kw in targets)

    fetcher_map: dict[str, object] = {
        'upbit': fetch_upbit_notices,
        'bithumb': fetch_bithumb_notices,
        'coinone': fetch_coinone_notices,
        'binance': fetch_binance_notices,
        'korbit': fetch_korbit_notices,
    }
    fn = fetcher_map.get(exchange)
    if fn is None:
        logger.debug('No notice fetcher for exchange: %s', exchange)
        return []

    try:
        all_notices = fn()  # type: ignore[operator]
        return [n for n in all_notices if _is_targeted(n.get('title', ''))]
    except Exception as e:
        logger.warning('Targeted notice fetch failed for %s: %s', exchange, e)
        return []


def get_all_notices() -> list[dict]:
    """모든 거래소 공지사항을 병렬로 스크래핑

    한국어 Binance 공지 추가 시:
      scrapers에 lambda: fetch_binance_notices(locale='ko') 를 병행 추가
    """
    scrapers = [
        fetch_upbit_notices,
        fetch_bithumb_notices,
        fetch_coinone_notices,
        fetch_korbit_notices,
        fetch_binance_notices,  # 글로벌: 기본 locale(_BINANCE_NOTICE_LOCALE) 사용
    ]

    all_notices: list[dict] = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fn): getattr(fn, '__name__', repr(fn)) for fn in scrapers}
        for future in as_completed(futures):
            try:
                results = future.result()
                all_notices.extend(results)
            except Exception as e:
                logger.warning('Notice scraper %s failed: %s', futures[future], e)

    return all_notices
