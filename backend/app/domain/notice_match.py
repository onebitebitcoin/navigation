"""공지 제목 관련성 매칭 (순수 텍스트 로직 — I/O 없음).

스크래퍼(`services/notice_scraper`)와 저장소(`db/repositories`) 양쪽에서 공유하는
단일 진실 공급원(SSoT). 키워드 매칭이 두 계층에 중복되면 한쪽만 고쳐도 버그가
남기 때문에(예: 'USDT' substring이 'HUSDT' 선물 공지에 오탐) 이 모듈로 통일한다.

티커(BTC/USDT)는 부분 문자열 오탐을 막기 위해 라틴 문자 전후방탐색으로 매칭한다.
"""
from __future__ import annotations

import re

# 부분 문자열 오탐을 막아야 하는 티커 심볼.
#   예) "HUSDT"·"BTCUSDT" 같은 (무기한) 선물 페어가 'USDT'/'BTC' 로 오인되는 것을 방지.
# \b(word boundary) 대신 라틴 문자 전후방탐색을 쓴다 — 한글 조사 결합("BTC를")은
# 허용해야 하므로(\b는 라틴-한글 경계를 단어 경계로 보지 않아 false negative 발생).
TICKER_KEYWORDS: frozenset[str] = frozenset({'btc', 'usdt'})
TICKER_PATTERNS: dict[str, re.Pattern[str]] = {
    kw: re.compile(r'(?<![a-z])' + kw + r'(?![a-z])') for kw in TICKER_KEYWORDS
}

# BTC/USDT/Lightning 관련 공지 필터 키워드
BTC_KEYWORDS: tuple[str, ...] = (
    'BTC', 'Bitcoin', '비트코인',
    'USDT', 'Tether', '테더',
    'Lightning', '라이트닝',
    'SegWit', '세그윗',
    'halving', '반감기',
)
# 거래소 전체에 영향을 미치는 주요 공지 (알트코인 특정 공지 제외)
MAJOR_KEYWORDS: tuple[str, ...] = (
    '전체 점검', '전체점검',
    '서비스 점검', '서비스점검',
    '시스템 점검', '시스템점검',
    '거래소 점검', '거래소점검',
    '긴급 점검', '긴급점검',
)
# 수수료 특화 키워드 (Binance·국내 거래소 수수료 이벤트 공지용)
FEE_KEYWORDS: tuple[str, ...] = (
    'zero fee', 'zero-fee', '0% fee', '0% maker', '0% taker',
    'fee promotion', 'fee update', 'trading fee',
    'fee structure', 'fee change', 'fee rate', 'fee waiver',
    'FDUSD',
    # 국내 거래소 수수료 이벤트 (예: 코인원 '전 종목 거래 수수료 0원!').
    # '거래 수수료'는 '거래 수수료 0원'·'거래 수수료 무료'를 모두 포괄하며,
    # 알트코인 출금 수수료 공지는 걸러낸다.
    '거래 수수료', '수수료율', '수수료 무료', '수수료 이벤트', '수수료 0원', '바우처',
)


def keyword_in_title(title_lower: str, keyword: str) -> bool:
    """키워드가 제목(소문자)에 포함되는지 판단.

    BTC/USDT 등 티커 심볼은 라틴 문자에 인접하면 매칭 제외(HUSDT·BTCUSDT 등
    부분 일치 오탐 방지). 그 외 서술형/한글 키워드는 단순 substring 매칭.

    Args:
        title_lower: 이미 소문자로 변환된 공지 제목.
        keyword: 매칭할 키워드(대소문자 무관).
    """
    kw = keyword.lower()
    pat = TICKER_PATTERNS.get(kw)
    if pat is not None:
        return pat.search(title_lower) is not None
    return kw in title_lower


def has_btc(title_lower: str) -> bool:
    """BTC 티커(라틴 경계) 또는 'bitcoin' 언급 여부"""
    return keyword_in_title(title_lower, 'btc') or 'bitcoin' in title_lower


def has_usdt(title_lower: str) -> bool:
    """USDT 티커(라틴 경계) 언급 여부"""
    return keyword_in_title(title_lower, 'usdt')


def is_relevant_title(title: str, *, include_fee: bool = False) -> bool:
    """BTC/USDT/Lightning 관련이거나 거래소 전체 주요 공지인지 판단.

    Args:
        title: 공지 제목(원문).
        include_fee: True 면 수수료 특화 키워드도 관련으로 인정(Binance 등).
    """
    lower = title.lower()
    for kw in BTC_KEYWORDS:
        if keyword_in_title(lower, kw):
            return True
    for kw in MAJOR_KEYWORDS:
        if kw in title:
            return True
    if include_fee:
        return any(kw.lower() in lower for kw in FEE_KEYWORDS)
    return False
