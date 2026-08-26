"""
코빗 거래 수수료 프로모션 감지 테스트
- 코인원과 달리 공지 JSON API가 없고 Playwright 렌더링에 의존하므로,
  실제 브라우저 호출은 전부 mock 하고 텍스트 파싱/캐시 로직만 검증한다.
- 캐시는 tmp_path + CACHE_FILE monkeypatch 로 격리
"""
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import fee_checker
from backend.app.domain import market_core, path_helpers
from backend.app.domain.path_helpers import (
    KORBIT_FEE_PROMO_NOTE,
    exchange_fee_promo_note,
)
from fee_checker import fetch_korbit_fee_promo

# 실제 코빗 수수료 안내 페이지 렌더링 결과 발췌 (2026-08-26 확인)
ACTIVE_PROMO_TEXT = (
    '수수료 안내\n거래 수수료\n입출금 수수료\n'
    '2026.08.24 09:00 부터 코빗 모든 회원의 거래 수수료가 전면 무료로 적용됩니다.\n'
    '자세한 내용은 공지사항을 참고해주시기 바랍니다.\n'
)
FUTURE_PROMO_TEXT = (
    '2099.01.01 09:00 부터 코빗 모든 회원의 거래 수수료가 전면 무료로 적용됩니다.\n'
)
NO_PROMO_TEXT = '수수료 안내\n거래 수수료\n입출금 수수료\nMaker 0.04% / Taker 0.2%\n'


def _use_temp_cache(monkeypatch, tmp_path, cache_data=None):
    """캐시 파일을 tmp_path로 격리. cache_data 를 주면 미리 기록한다."""
    cache_file = tmp_path / 'cache.json'
    if cache_data is not None:
        cache_file.write_text(json.dumps(cache_data), encoding='utf-8')
    monkeypatch.setattr(fee_checker, 'CACHE_FILE', str(cache_file))
    return cache_file


# ─────────────────────────────────────────────────────────────
# 텍스트 파싱 (_parse_korbit_fee_promo) — 순수 함수, 브라우저 불필요
# ─────────────────────────────────────────────────────────────

class TestParseKorbitFeePromo:
    def test_active_promo_parsed_as_free(self):
        """시작 시각이 지난 '전면 무료' 문구는 maker/taker 모두 0으로 파싱"""
        # Act
        result = fee_checker._parse_korbit_fee_promo(ACTIVE_PROMO_TEXT)

        # Assert
        assert result is not None
        assert result['maker_fee_pct'] == 0.0
        assert result['taker_fee_pct'] == 0.0
        assert result['requires_voucher'] is False
        assert result['starts_at'] == '2026-08-24T09:00:00'
        assert result['source_url'] == fee_checker.KORBIT_FEE_PAGE_URL

    def test_dash_separated_date_also_parsed(self):
        """'2026-08-24 09:00' 처럼 하이픈 구분 날짜도 파싱한다"""
        # Arrange
        text = '2026-08-24 09:00 부터 코빗 모든 회원의 거래 수수료가 전면 무료로 적용됩니다.'

        # Act
        result = fee_checker._parse_korbit_fee_promo(text)

        # Assert
        assert result is not None
        assert result['starts_at'] == '2026-08-24T09:00:00'

    def test_future_start_date_returns_none(self):
        """공지는 있으나 시작 시각이 아직 도래하지 않았으면 반영하지 않는다"""
        # Act
        result = fee_checker._parse_korbit_fee_promo(FUTURE_PROMO_TEXT)

        # Assert
        assert result is None

    def test_no_promo_text_returns_none(self):
        """전면 무료 문구가 없으면 None — 하드코딩 fallback 금지"""
        # Act
        result = fee_checker._parse_korbit_fee_promo(NO_PROMO_TEXT)

        # Assert
        assert result is None

    def test_empty_text_returns_none(self):
        assert fee_checker._parse_korbit_fee_promo('') is None
        assert fee_checker._parse_korbit_fee_promo(None) is None

    def test_voucher_mention_sets_flag(self):
        """본문에 '바우처'가 언급되면 requires_voucher=True로 파싱 (현재 코빗 공지엔 없음)"""
        # Arrange
        text = ACTIVE_PROMO_TEXT + '\n바우처 발급 후 적용됩니다.'

        # Act
        result = fee_checker._parse_korbit_fee_promo(text)

        # Assert
        assert result['requires_voucher'] is True


# ─────────────────────────────────────────────────────────────
# 프로모션 조회 + 캐시 TTL (fetch_korbit_fee_promo)
# 실제 Playwright 호출은 _scrape_korbit_fee_promo monkeypatch로 대체
# ─────────────────────────────────────────────────────────────

class TestFetchKorbitFeePromo:
    def test_active_promo_returned_and_cached(self, tmp_path, monkeypatch):
        """스크래핑 결과가 있으면 그대로 반환하고 캐시에 기록한다"""
        # Arrange
        cache_file = _use_temp_cache(monkeypatch, tmp_path)
        calls = []

        def _fake_scrape():
            calls.append(1)
            return {
                'maker_fee_pct': 0.0, 'taker_fee_pct': 0.0, 'requires_voucher': False,
                'starts_at': '2026-08-24T09:00:00', 'source_url': fee_checker.KORBIT_FEE_PAGE_URL,
            }

        monkeypatch.setattr(fee_checker, '_scrape_korbit_fee_promo', _fake_scrape)

        # Act
        result = fetch_korbit_fee_promo()

        # Assert
        assert result['taker_fee_pct'] == 0.0
        assert len(calls) == 1
        saved = json.loads(cache_file.read_text(encoding='utf-8'))
        assert saved['fee_promo']['korbit']['taker_fee_pct'] == 0.0

    def test_no_promo_returns_none_and_caches_checked_at(self, tmp_path, monkeypatch):
        """프로모션이 없어도 checked_at을 기록해 TTL 내 재요청을 막는다"""
        # Arrange
        cache_file = _use_temp_cache(monkeypatch, tmp_path)
        calls = []

        def _fake_scrape():
            calls.append(1)

        monkeypatch.setattr(fee_checker, '_scrape_korbit_fee_promo', _fake_scrape)

        # Act
        assert fetch_korbit_fee_promo() is None
        assert fetch_korbit_fee_promo() is None

        # Assert
        assert len(calls) == 1  # 두 번째 호출은 캐시로 스킵
        saved = json.loads(cache_file.read_text(encoding='utf-8'))
        assert 'checked_at' in saved['fee_promo']['korbit']

    def test_valid_cache_skips_scrape(self, tmp_path, monkeypatch):
        """TTL 이내면 Playwright 재실행 없이 캐시값을 반환한다"""
        # Arrange
        _use_temp_cache(monkeypatch, tmp_path, {
            'last_updated': None,
            'fees': {},
            'fee_promo': {
                'korbit': {
                    'maker_fee_pct': 0.0, 'taker_fee_pct': 0.0, 'requires_voucher': False,
                    'starts_at': '2026-08-24T09:00:00', 'source_url': fee_checker.KORBIT_FEE_PAGE_URL,
                    'checked_at': datetime.now().isoformat(),
                },
            },
        })

        def _should_not_be_called():
            raise AssertionError('TTL 이내에는 Playwright를 재실행하면 안 된다')

        monkeypatch.setattr(fee_checker, '_scrape_korbit_fee_promo', _should_not_be_called)

        # Act
        result = fetch_korbit_fee_promo()

        # Assert
        assert result['taker_fee_pct'] == 0.0

    def test_expired_cache_rescrapes(self, tmp_path, monkeypatch):
        """TTL(1시간)이 지나면 다시 스크래핑한다"""
        # Arrange
        _use_temp_cache(monkeypatch, tmp_path, {
            'last_updated': None,
            'fees': {},
            'fee_promo': {
                'korbit': {
                    'taker_fee_pct': 0.2,
                    'checked_at': (datetime.now() - timedelta(hours=2)).isoformat(),
                },
            },
        })
        monkeypatch.setattr(
            fee_checker, '_scrape_korbit_fee_promo',
            lambda: {'maker_fee_pct': 0.0, 'taker_fee_pct': 0.0, 'requires_voucher': False},
        )

        # Act
        result = fetch_korbit_fee_promo()

        # Assert
        assert result['taker_fee_pct'] == 0.0

    def test_scrape_exception_returns_none(self, tmp_path, monkeypatch):
        """스크래핑 중 예외가 나도 삼키고 None 반환 — 크롤 전체를 죽이지 않는다"""
        # Arrange
        _use_temp_cache(monkeypatch, tmp_path)

        def _boom():
            raise RuntimeError('playwright launch failed')

        monkeypatch.setattr(fee_checker, '_scrape_korbit_fee_promo', _boom)

        # Act
        result = fetch_korbit_fee_promo()

        # Assert
        assert result is None

    def test_cache_preserves_existing_keys(self, tmp_path, monkeypatch):
        """fee_promo 저장이 기존 출금 수수료 캐시나 coinone 항목을 덮어쓰지 않는다"""
        # Arrange
        cache_file = _use_temp_cache(monkeypatch, tmp_path, {
            'last_updated': datetime.now().isoformat(),
            'fees': {'upbit_btc': 0.0002},
            'fee_promo': {'coinone': {'notice_id': 5695}},
        })
        monkeypatch.setattr(
            fee_checker, '_scrape_korbit_fee_promo',
            lambda: {'maker_fee_pct': 0.0, 'taker_fee_pct': 0.0, 'requires_voucher': False},
        )

        # Act
        fetch_korbit_fee_promo()

        # Assert
        saved = json.loads(cache_file.read_text(encoding='utf-8'))
        assert saved['fees'] == {'upbit_btc': 0.0002}
        assert saved['fee_promo']['coinone']['notice_id'] == 5695
        assert saved['fee_promo']['korbit']['taker_fee_pct'] == 0.0


# ─────────────────────────────────────────────────────────────
# 이벤트 안내 배지 문구 (exchange_fee_promo_note — 코빗)
# ─────────────────────────────────────────────────────────────

class TestKorbitFeePromoNote:
    @staticmethod
    def _stub_promo(monkeypatch, promo):
        monkeypatch.setattr(path_helpers, 'fetch_korbit_fee_promo', lambda: promo)

    def test_active_promo_returns_note(self, monkeypatch):
        # Arrange
        self._stub_promo(monkeypatch, {'taker_fee_pct': 0.0, 'requires_voucher': False})

        # Act / Assert
        assert exchange_fee_promo_note('korbit') == KORBIT_FEE_PROMO_NOTE

    def test_no_promo_returns_none(self, monkeypatch):
        # Arrange
        self._stub_promo(monkeypatch, None)

        # Act / Assert
        assert exchange_fee_promo_note('korbit') is None

    def test_case_insensitive_exchange(self, monkeypatch):
        # Arrange
        self._stub_promo(monkeypatch, {'taker_fee_pct': 0.0})

        # Act / Assert
        assert exchange_fee_promo_note('KORBIT') == KORBIT_FEE_PROMO_NOTE

    def test_other_exchange_skips_korbit_lookup(self, monkeypatch):
        """코빗이 아니면 코빗 프로모션 캐시를 건드리지 않는다"""
        # Arrange
        def _should_not_be_called():
            raise AssertionError('코빗 외 거래소에서 코빗 프로모션을 조회하면 안 된다')

        monkeypatch.setattr(path_helpers, 'fetch_korbit_fee_promo', _should_not_be_called)

        # Act / Assert
        assert exchange_fee_promo_note('upbit') is None
        assert exchange_fee_promo_note(None) is None


# ─────────────────────────────────────────────────────────────
# get_ticker_data 연동 — 코빗 프로모션 수수료가 티커에 반영되는지
# ─────────────────────────────────────────────────────────────

class TestGetTickerDataKorbitFeePromo:
    @staticmethod
    def _stub_ticker(monkeypatch, exchange):
        monkeypatch.setitem(
            market_core.KOREA_FETCHERS,
            exchange,
            lambda: {'price': 100_000_000.0, 'high': 1.0, 'low': 1.0, 'volume': 1.0, 'currency': 'KRW'},
        )

    def test_promo_overrides_static_fees(self, monkeypatch):
        """프로모션이 있으면 TRADING_FEES 대신 파싱값을 쓴다"""
        # Arrange
        self._stub_ticker(monkeypatch, 'korbit')
        monkeypatch.setattr(
            market_core, 'fetch_korbit_fee_promo',
            lambda: {'maker_fee_pct': 0.0, 'taker_fee_pct': 0.0},
        )

        # Act
        data = market_core.get_ticker_data('korbit')

        # Assert
        assert data['maker_fee_pct'] == 0.0
        assert data['taker_fee_pct'] == 0.0

    def test_static_fees_used_without_promo(self, monkeypatch):
        """프로모션이 없으면(None) 기존 정적 수수료를 유지한다"""
        # Arrange
        self._stub_ticker(monkeypatch, 'korbit')
        monkeypatch.setattr(market_core, 'fetch_korbit_fee_promo', lambda: None)

        # Act
        data = market_core.get_ticker_data('korbit')

        # Assert
        expected = market_core.TRADING_FEES['korbit']
        assert data['maker_fee_pct'] == expected['maker'] * 100
        assert data['taker_fee_pct'] == expected['taker'] * 100

    def test_coinone_untouched_by_korbit_promo(self, monkeypatch):
        """코인원 조회 시 코빗 프로모션을 조회하지 않는다 (그 반대도 기존 테스트로 보장됨)"""
        # Arrange
        self._stub_ticker(monkeypatch, 'coinone')
        monkeypatch.setattr(market_core, 'fetch_coinone_fee_promo', lambda: None)

        def _should_not_be_called():
            raise AssertionError('coinone 조회 중 코빗 프로모션을 조회하면 안 된다')

        monkeypatch.setattr(market_core, 'fetch_korbit_fee_promo', _should_not_be_called)

        # Act
        market_core.get_ticker_data('coinone')
