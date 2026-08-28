"""
업비트 USDT/KRW 등 스테이블코인 페어 거래 수수료 무료 이벤트 감지 테스트
- 코인원/코빗과 달리 공지 API가 없고 Playwright 렌더링에 의존하므로,
  실제 브라우저 호출은 전부 mock하고 텍스트 파싱/캐시 로직만 검증한다.
- 코인원/코빗과 결정적으로 다른 점: 이 이벤트는 USDT/KRW 등 원화마켓 스테이블코인
  페어에만 적용되고 BTC/KRW에는 적용되지 않는다. 그래서 coin 파라미터 분기
  (exchange_fee_promo_note)와 taker rate 오버라이드(korean_usdt_taker_rate)가
  BTC 레그를 절대 건드리지 않는지를 중점적으로 검증한다.
- 캐시는 tmp_path + CACHE_FILE monkeypatch로 격리
"""
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import fee_checker
from backend.app.domain import path_helpers
from backend.app.domain.path_helpers import (
    UPBIT_USDT_PROMO_NOTE,
    exchange_fee_promo_note,
    korean_usdt_taker_rate,
)
from fee_checker import fetch_upbit_usdt_fee_promo

# 실제 업비트 공지(notice id=644912522, "새롭게 돌아온 스테이블 코인 그룹 페어
# 거래 수수료 무료 이벤트 안내") 렌더링 결과 발췌 (2026-08-28 확인)
ACTIVE_PROMO_TEXT = (
    '이벤트 기간 (변경) : ~ 2026-08-30(일) 23:59:59\n'
    '적용 대상 : 이벤트 기간 동안 업비트 USDT/KRW, USDC/KRW 페어 내 모든 주문(Maker/Taker)의 거래 수수료\n'
    '대상 페어\t거래 수수료\n'
    'USDT/KRW\t0.05% → 0.00%\n'
    'USDC/KRW\t0.05% → 0.00%\n'
)
EXPIRED_PROMO_TEXT = (
    '이벤트 기간 : 2020-01-01(수) 09:00:00 ~ 2020-01-08(수) 23:59:59\n'
    'USDT/KRW\t0.05% → 0.00%\n'
)
NO_USDT_ZERO_TEXT = (
    '이벤트 기간 : ~ 2099-01-01(목) 23:59:59\n'
    'BTC/KRW\t0.05% → 0.00%\n'  # USDT/KRW가 대상에 없음 — 제목만 보고 오탐하면 안 됨
)
NO_PROMO_TEXT = '거래 수수료 안내\nMaker 0.05% / Taker 0.05%\n'


def _use_temp_cache(monkeypatch, tmp_path, cache_data=None):
    """캐시 파일을 tmp_path로 격리. cache_data 를 주면 미리 기록한다."""
    cache_file = tmp_path / 'cache.json'
    if cache_data is not None:
        cache_file.write_text(json.dumps(cache_data), encoding='utf-8')
    monkeypatch.setattr(fee_checker, 'CACHE_FILE', str(cache_file))
    return cache_file


# ─────────────────────────────────────────────────────────────
# 텍스트 파싱 (_parse_upbit_usdt_fee_promo) — 순수 함수, 브라우저 불필요
# ─────────────────────────────────────────────────────────────

class TestParseUpbitUsdtFeePromo:
    def test_active_promo_parsed_as_free(self):
        """USDT/KRW가 0%로 인하 대상에 있고 종료 시각이 아직 안 지났으면 무료로 파싱"""
        # Act
        result = fee_checker._parse_upbit_usdt_fee_promo(ACTIVE_PROMO_TEXT)

        # Assert
        assert result is not None
        assert result['maker_fee_pct'] == 0.0
        assert result['taker_fee_pct'] == 0.0
        assert result['pairs'] == ['USDT/KRW']
        assert result['ends_at'] == '2026-08-30T23:59:59'

    def test_expired_promo_returns_none(self):
        """공지는 있으나 이벤트 기간(종료 시각)이 이미 지났으면 반영하지 않는다"""
        # Act
        result = fee_checker._parse_upbit_usdt_fee_promo(EXPIRED_PROMO_TEXT)

        # Assert
        assert result is None

    def test_missing_usdt_zero_rate_returns_none(self):
        """USDT/KRW가 0% 인하 대상 표에 없으면(다른 코인만 무료 등) None — 제목 오탐 방지"""
        # Act
        result = fee_checker._parse_upbit_usdt_fee_promo(NO_USDT_ZERO_TEXT)

        # Assert
        assert result is None

    def test_no_promo_text_returns_none(self):
        """이벤트 문구가 전혀 없으면 None — 하드코딩 fallback 금지"""
        # Act
        result = fee_checker._parse_upbit_usdt_fee_promo(NO_PROMO_TEXT)

        # Assert
        assert result is None

    def test_empty_text_returns_none(self):
        assert fee_checker._parse_upbit_usdt_fee_promo('') is None
        assert fee_checker._parse_upbit_usdt_fee_promo(None) is None


# ─────────────────────────────────────────────────────────────
# 프로모션 조회 + 캐시 TTL (fetch_upbit_usdt_fee_promo)
# 실제 Playwright 호출은 _scrape_upbit_fee_promo monkeypatch로 대체
# ─────────────────────────────────────────────────────────────

class TestFetchUpbitUsdtFeePromo:
    def test_active_promo_returned_and_cached(self, tmp_path, monkeypatch):
        """스크래핑 결과가 있으면 그대로 반환하고 캐시에 기록한다"""
        # Arrange
        cache_file = _use_temp_cache(monkeypatch, tmp_path)
        calls = []

        def _fake_scrape():
            calls.append(1)
            return {
                'maker_fee_pct': 0.0, 'taker_fee_pct': 0.0, 'pairs': ['USDT/KRW'],
                'ends_at': '2026-08-30T23:59:59', 'source_url': fee_checker.UPBIT_NOTICE_LIST_URL,
            }

        monkeypatch.setattr(fee_checker, '_scrape_upbit_fee_promo', _fake_scrape)

        # Act
        result = fetch_upbit_usdt_fee_promo()

        # Assert
        assert result['taker_fee_pct'] == 0.0
        assert len(calls) == 1
        saved = json.loads(cache_file.read_text(encoding='utf-8'))
        assert saved['fee_promo']['upbit_usdt']['taker_fee_pct'] == 0.0

    def test_no_promo_returns_none_and_caches_checked_at(self, tmp_path, monkeypatch):
        """프로모션이 없어도 checked_at을 기록해 TTL 내 재요청을 막는다"""
        # Arrange
        cache_file = _use_temp_cache(monkeypatch, tmp_path)
        calls = []

        def _fake_scrape():
            calls.append(1)

        monkeypatch.setattr(fee_checker, '_scrape_upbit_fee_promo', _fake_scrape)

        # Act
        assert fetch_upbit_usdt_fee_promo() is None
        assert fetch_upbit_usdt_fee_promo() is None

        # Assert
        assert len(calls) == 1  # 두 번째 호출은 캐시로 스킵
        saved = json.loads(cache_file.read_text(encoding='utf-8'))
        assert 'checked_at' in saved['fee_promo']['upbit_usdt']

    def test_valid_cache_skips_scrape(self, tmp_path, monkeypatch):
        """TTL 이내면 Playwright 재실행 없이 캐시값을 반환한다"""
        # Arrange
        _use_temp_cache(monkeypatch, tmp_path, {
            'last_updated': None,
            'fees': {},
            'fee_promo': {
                'upbit_usdt': {
                    'maker_fee_pct': 0.0, 'taker_fee_pct': 0.0, 'pairs': ['USDT/KRW'],
                    'ends_at': '2026-08-30T23:59:59',
                    'checked_at': datetime.now().isoformat(),
                },
            },
        })

        def _should_not_be_called():
            raise AssertionError('TTL 이내에는 Playwright를 재실행하면 안 된다')

        monkeypatch.setattr(fee_checker, '_scrape_upbit_fee_promo', _should_not_be_called)

        # Act
        result = fetch_upbit_usdt_fee_promo()

        # Assert
        assert result['taker_fee_pct'] == 0.0

    def test_expired_cache_rescrapes(self, tmp_path, monkeypatch):
        """TTL(1시간)이 지나면 다시 스크래핑한다"""
        # Arrange
        _use_temp_cache(monkeypatch, tmp_path, {
            'last_updated': None,
            'fees': {},
            'fee_promo': {
                'upbit_usdt': {
                    'taker_fee_pct': None,
                    'checked_at': (datetime.now() - timedelta(hours=2)).isoformat(),
                },
            },
        })
        monkeypatch.setattr(
            fee_checker, '_scrape_upbit_fee_promo',
            lambda: {'maker_fee_pct': 0.0, 'taker_fee_pct': 0.0, 'pairs': ['USDT/KRW']},
        )

        # Act
        result = fetch_upbit_usdt_fee_promo()

        # Assert
        assert result['taker_fee_pct'] == 0.0

    def test_scrape_exception_returns_none(self, tmp_path, monkeypatch):
        """스크래핑 중 예외가 나도 삼키고 None 반환 — 크롤 전체를 죽이지 않는다"""
        # Arrange
        _use_temp_cache(monkeypatch, tmp_path)

        def _boom():
            raise RuntimeError('playwright launch failed')

        monkeypatch.setattr(fee_checker, '_scrape_upbit_fee_promo', _boom)

        # Act
        result = fetch_upbit_usdt_fee_promo()

        # Assert
        assert result is None

    def test_cache_preserves_existing_keys(self, tmp_path, monkeypatch):
        """fee_promo 저장이 기존 출금 수수료 캐시나 korbit 항목을 덮어쓰지 않는다"""
        # Arrange
        cache_file = _use_temp_cache(monkeypatch, tmp_path, {
            'last_updated': datetime.now().isoformat(),
            'fees': {'upbit_btc': 0.0002},
            'fee_promo': {'korbit': {'taker_fee_pct': 0.0}},
        })
        monkeypatch.setattr(
            fee_checker, '_scrape_upbit_fee_promo',
            lambda: {'maker_fee_pct': 0.0, 'taker_fee_pct': 0.0, 'pairs': ['USDT/KRW']},
        )

        # Act
        fetch_upbit_usdt_fee_promo()

        # Assert
        saved = json.loads(cache_file.read_text(encoding='utf-8'))
        assert saved['fees'] == {'upbit_btc': 0.0002}
        assert saved['fee_promo']['korbit']['taker_fee_pct'] == 0.0
        assert saved['fee_promo']['upbit_usdt']['taker_fee_pct'] == 0.0


# ─────────────────────────────────────────────────────────────
# 이벤트 안내 배지 문구 (exchange_fee_promo_note — 업비트 USDT 레그 한정)
# ─────────────────────────────────────────────────────────────

class TestExchangeFeePromoNoteUpbitUsdt:
    @staticmethod
    def _stub_promo(monkeypatch, promo):
        monkeypatch.setattr(path_helpers, 'fetch_upbit_usdt_fee_promo', lambda: promo)

    def test_usdt_coin_returns_note_when_active(self, monkeypatch):
        # Arrange
        self._stub_promo(monkeypatch, {'taker_fee_pct': 0.0})

        # Act / Assert
        assert exchange_fee_promo_note('upbit', coin='USDT') == UPBIT_USDT_PROMO_NOTE

    def test_btc_coin_does_not_trigger_upbit_lookup(self, monkeypatch):
        """BTC 레그에는 이 이벤트를 절대 붙이면 안 된다 — 조회 자체를 안 해야 함"""
        # Arrange
        def _should_not_be_called():
            raise AssertionError('BTC 레그에서 업비트 USDT 프로모션을 조회하면 안 된다')

        monkeypatch.setattr(path_helpers, 'fetch_upbit_usdt_fee_promo', _should_not_be_called)

        # Act / Assert (coin 기본값은 'BTC')
        assert exchange_fee_promo_note('upbit') is None
        assert exchange_fee_promo_note('upbit', coin='BTC') is None

    def test_no_promo_returns_none(self, monkeypatch):
        # Arrange
        self._stub_promo(monkeypatch, None)

        # Act / Assert
        assert exchange_fee_promo_note('upbit', coin='USDT') is None

    def test_case_insensitive_exchange_and_coin(self, monkeypatch):
        # Arrange
        self._stub_promo(monkeypatch, {'taker_fee_pct': 0.0})

        # Act / Assert
        assert exchange_fee_promo_note('UPBIT', coin='usdt') == UPBIT_USDT_PROMO_NOTE

    def test_other_exchange_skips_upbit_lookup(self, monkeypatch):
        """업비트가 아니면 업비트 USDT 프로모션 캐시를 건드리지 않는다

        bithumb은 promo 분기가 아예 없는 거래소라 다른 프로모션 캐시와 섞일 걱정 없이
        '업비트 조회를 안 한다'만 순수하게 검증할 수 있다 (coinone/korbit는 coin과
        무관하게 자체 promo 로직이 있어 이 목적에 안 맞음).
        """
        # Arrange
        def _should_not_be_called():
            raise AssertionError('업비트 외 거래소에서 업비트 USDT 프로모션을 조회하면 안 된다')

        monkeypatch.setattr(path_helpers, 'fetch_upbit_usdt_fee_promo', _should_not_be_called)

        # Act / Assert
        assert exchange_fee_promo_note('bithumb', coin='USDT') is None
        assert exchange_fee_promo_note(None, coin='USDT') is None


# ─────────────────────────────────────────────────────────────
# USDT 레그 taker rate 오버라이드 (korean_usdt_taker_rate)
# ─────────────────────────────────────────────────────────────

class TestKoreanUsdtTakerRate:
    def test_upbit_active_promo_overrides_to_zero(self, monkeypatch):
        # Arrange
        monkeypatch.setattr(path_helpers, 'fetch_upbit_usdt_fee_promo', lambda: {'taker_fee_pct': 0.0})

        # Act / Assert
        assert korean_usdt_taker_rate('upbit', 0.0005) == 0.0

    def test_upbit_no_promo_keeps_base_taker(self, monkeypatch):
        # Arrange
        monkeypatch.setattr(path_helpers, 'fetch_upbit_usdt_fee_promo', lambda: None)

        # Act / Assert — 이벤트가 없으면 기존 정적/DB 수수료를 그대로 유지
        assert korean_usdt_taker_rate('upbit', 0.0005) == 0.0005

    def test_non_upbit_exchange_untouched(self, monkeypatch):
        """업비트가 아닌 거래소는 조회 자체를 안 하고 base_taker를 그대로 반환한다"""
        # Arrange
        def _should_not_be_called():
            raise AssertionError('업비트 외 거래소에서 업비트 USDT 프로모션을 조회하면 안 된다')

        monkeypatch.setattr(path_helpers, 'fetch_upbit_usdt_fee_promo', _should_not_be_called)

        # Act / Assert
        assert korean_usdt_taker_rate('coinone', 0.001) == 0.001
        assert korean_usdt_taker_rate(None, 0.001) == 0.001

    def test_case_insensitive_exchange(self, monkeypatch):
        # Arrange
        monkeypatch.setattr(path_helpers, 'fetch_upbit_usdt_fee_promo', lambda: {'taker_fee_pct': 0.0})

        # Act / Assert
        assert korean_usdt_taker_rate('UPBIT', 0.0005) == 0.0
