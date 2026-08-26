"""
코인원 거래 수수료 프로모션 감지 테스트
- 공지 목록/상세 API는 전부 mock (네트워크 호출 없음)
- 캐시는 tmp_path + CACHE_FILE monkeypatch 로 격리
"""
import json
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import fee_checker
from backend.app.domain import market_core, path_helpers
from backend.app.domain.path_graph import korea_buy_leg, korea_sell_leg
from backend.app.domain.path_helpers import COINONE_VOUCHER_NOTE, exchange_fee_promo_note
from fee_checker import fetch_coinone_fee_promo


# ─────────────────────────────────────────────────────────────
# 픽스처 헬퍼 — 실제 코인원 API 응답 구조를 그대로 축약
# ─────────────────────────────────────────────────────────────

# 실제 공지 5695 본문 발췌 — 태그 제거 후 "Maker 0% / Taker 0%" 가 남는다.
VOUCHER_PROMO_CONTENT = (
    '<p>전 종목 거래 수수료 전면 무료를 시행합니다.</p>'
    '<p>- 적용 대상 : <span style="color: rgb(230, 0, 0);">바우처 발급</span> 시 즉시 적용</p>'
    '<p>- 수수료율 : Maker 0% / Taker 0%</p>'
)
# 바우처 조건 없는 수수료 인하 공지
PLAIN_PROMO_CONTENT = '<p>- 수수료율 : Maker 0.02% / Taker 0.04%</p>'
# 수수료율 표기가 없는 공지 — 파싱 실패 케이스
NO_RATE_CONTENT = '<p>수수료 이벤트를 진행합니다. 자세한 내용은 앱에서 확인하세요.</p>'


def _notice(notice_id, title, *, status='INPROGRESS', updated_at=1787647860):
    """공지 목록 항목 1건 생성. status=None 이면 eventInformation 자체가 없다."""
    return {
        'id': notice_id,
        'title': title,
        'isPinned': False,
        'updatedAt': updated_at,
        'eventInformation': None if status is None else {
            'isRegularEvent': True,
            'eventStartDate': None,
            'eventEndDate': None,
            'eventStatus': status,
        },
    }


def _install_api_mock(monkeypatch, notices, contents, calls=None):
    """공지 목록/상세 API mock 설치. calls 리스트에 요청 URL을 기록한다."""
    calls = [] if calls is None else calls

    def _fake_get(url, **kwargs):
        calls.append(url)
        resp = MagicMock()
        resp.status_code = 200
        if '/posts/' in url:
            notice_id = int(url.rsplit('/', 1)[-1])
            resp.json.return_value = {'body': {'id': notice_id, 'content': contents[notice_id]}}
        else:
            resp.json.return_value = {'body': {'pinnedNotices': [], 'notices': notices}}
        return resp

    monkeypatch.setattr(fee_checker.requests, 'get', _fake_get)
    return calls


def _use_temp_cache(monkeypatch, tmp_path, cache_data=None):
    """캐시 파일을 tmp_path로 격리. cache_data 를 주면 미리 기록한다."""
    cache_file = tmp_path / 'cache.json'
    if cache_data is not None:
        cache_file.write_text(json.dumps(cache_data), encoding='utf-8')
    monkeypatch.setattr(fee_checker, 'CACHE_FILE', str(cache_file))
    return cache_file


# ─────────────────────────────────────────────────────────────
# 프로모션 감지 / 파싱
# ─────────────────────────────────────────────────────────────

class TestFetchCoinoneFeePromo:
    def test_inprogress_promo_returns_parsed_rates(self, tmp_path, monkeypatch):
        """진행 중 수수료 이벤트 → 공지 본문의 Maker/Taker 값을 그대로 반환"""
        # Arrange
        _use_temp_cache(monkeypatch, tmp_path)
        _install_api_mock(
            monkeypatch,
            [_notice(5695, '코인원 전 종목 거래 수수료 0원!')],
            {5695: VOUCHER_PROMO_CONTENT},
        )

        # Act
        result = fetch_coinone_fee_promo()

        # Assert
        assert result is not None
        assert result['maker_fee_pct'] == 0.0
        assert result['taker_fee_pct'] == 0.0
        assert result['requires_voucher'] is True
        assert result['notice_id'] == 5695
        assert result['notice_title'] == '코인원 전 종목 거래 수수료 0원!'
        assert result['source_url'] == 'https://coinone.co.kr/info/notice/5695'

    def test_decimal_rates_parsed(self, tmp_path, monkeypatch):
        """소수점 수수료율(0.02% / 0.04%)도 파싱한다"""
        # Arrange
        _use_temp_cache(monkeypatch, tmp_path)
        _install_api_mock(
            monkeypatch,
            [_notice(5614, '거래 수수료율 인하 안내')],
            {5614: PLAIN_PROMO_CONTENT},
        )

        # Act
        result = fetch_coinone_fee_promo()

        # Assert
        assert result['maker_fee_pct'] == 0.02
        assert result['taker_fee_pct'] == 0.04
        assert result['requires_voucher'] is False

    def test_no_matching_notice_returns_none(self, tmp_path, monkeypatch):
        """수수료 이벤트 공지가 없으면 None (추정치 만들지 않음)"""
        # Arrange
        _use_temp_cache(monkeypatch, tmp_path)
        _install_api_mock(
            monkeypatch,
            [
                _notice(5700, 'BTC 입출금 일시 중단 안내'),
                _notice(5701, '신규 상장 안내', status=None),
            ],
            {},
        )

        # Act
        result = fetch_coinone_fee_promo()

        # Assert
        assert result is None

    def test_finished_event_ignored(self, tmp_path, monkeypatch):
        """종료된(FINISHED) 수수료 이벤트는 반영하지 않는다"""
        # Arrange
        _use_temp_cache(monkeypatch, tmp_path)
        _install_api_mock(
            monkeypatch,
            [_notice(5694, '거래 수수료 무료 데일리 랭킹전', status='FINISHED')],
            {5694: VOUCHER_PROMO_CONTENT},
        )

        # Act
        result = fetch_coinone_fee_promo()

        # Assert
        assert result is None

    def test_notice_without_event_information_ignored(self, tmp_path, monkeypatch):
        """eventInformation 이 없는 수수료 공지는 이벤트로 보지 않는다"""
        # Arrange
        _use_temp_cache(monkeypatch, tmp_path)
        _install_api_mock(
            monkeypatch,
            [_notice(5614, '업계 최저 수준 거래 수수료 0.04% 안내', status=None)],
            {5614: PLAIN_PROMO_CONTENT},
        )

        # Act
        result = fetch_coinone_fee_promo()

        # Assert
        assert result is None

    def test_unparsable_content_returns_none(self, tmp_path, monkeypatch):
        """본문에 Maker/Taker 표기가 없으면 None — 하드코딩 fallback 금지"""
        # Arrange
        _use_temp_cache(monkeypatch, tmp_path)
        _install_api_mock(
            monkeypatch,
            [_notice(5695, '코인원 전 종목 거래 수수료 0원!')],
            {5695: NO_RATE_CONTENT},
        )

        # Act
        result = fetch_coinone_fee_promo()

        # Assert
        assert result is None

    def test_network_failure_returns_none(self, tmp_path, monkeypatch):
        """네트워크 오류는 삼키고 None 반환 — 크롤 전체를 죽이지 않는다"""
        # Arrange
        _use_temp_cache(monkeypatch, tmp_path)

        def _boom(url, **kwargs):
            raise fee_checker.requests.RequestException('connection reset')

        monkeypatch.setattr(fee_checker.requests, 'get', _boom)

        # Act
        result = fetch_coinone_fee_promo()

        # Assert
        assert result is None

    def test_latest_notice_tried_first(self, tmp_path, monkeypatch):
        """진행 중 이벤트가 여러 건이면 updatedAt 최신 공지를 채택한다"""
        # Arrange
        _use_temp_cache(monkeypatch, tmp_path)
        _install_api_mock(
            monkeypatch,
            [
                _notice(5677, 'API 거래 수수료 무료', updated_at=1787238960),
                _notice(5695, '코인원 전 종목 거래 수수료 0원!', updated_at=1787647860),
            ],
            {5677: PLAIN_PROMO_CONTENT, 5695: VOUCHER_PROMO_CONTENT},
        )

        # Act
        result = fetch_coinone_fee_promo()

        # Assert
        assert result['notice_id'] == 5695

    def test_falls_through_when_latest_detail_unparsable(self, tmp_path, monkeypatch):
        """최신 공지가 파싱 실패면 다음 후보 공지를 시도한다"""
        # Arrange
        _use_temp_cache(monkeypatch, tmp_path)
        _install_api_mock(
            monkeypatch,
            [
                _notice(5677, 'API 거래 수수료 무료', updated_at=1787238960),
                _notice(5695, '코인원 전 종목 거래 수수료 0원!', updated_at=1787647860),
            ],
            {5677: PLAIN_PROMO_CONTENT, 5695: NO_RATE_CONTENT},
        )

        # Act
        result = fetch_coinone_fee_promo()

        # Assert
        assert result['notice_id'] == 5677


# ─────────────────────────────────────────────────────────────
# 캐시 TTL (1시간)
# ─────────────────────────────────────────────────────────────

class TestFeePromoCache:
    def test_valid_cache_skips_http(self, tmp_path, monkeypatch):
        """TTL 이내면 HTTP 재요청 없이 캐시값을 반환한다"""
        # Arrange
        _use_temp_cache(monkeypatch, tmp_path, {
            'last_updated': None,
            'fees': {},
            'fee_promo': {
                'coinone': {
                    'maker_fee_pct': 0.0,
                    'taker_fee_pct': 0.0,
                    'requires_voucher': True,
                    'notice_id': 5695,
                    'notice_title': '코인원 전 종목 거래 수수료 0원!',
                    'source_url': 'https://coinone.co.kr/info/notice/5695',
                    'checked_at': datetime.now().isoformat(),
                },
            },
        })
        calls = _install_api_mock(monkeypatch, [], {})

        # Act
        result = fetch_coinone_fee_promo()

        # Assert
        assert result['notice_id'] == 5695
        assert calls == []

    def test_expired_cache_refetches(self, tmp_path, monkeypatch):
        """TTL(1시간)이 지나면 다시 조회한다"""
        # Arrange
        _use_temp_cache(monkeypatch, tmp_path, {
            'last_updated': None,
            'fees': {},
            'fee_promo': {
                'coinone': {
                    'maker_fee_pct': 0.1,
                    'taker_fee_pct': 0.1,
                    'notice_id': 1,
                    'checked_at': (datetime.now() - timedelta(hours=2)).isoformat(),
                },
            },
        })
        calls = _install_api_mock(
            monkeypatch,
            [_notice(5695, '코인원 전 종목 거래 수수료 0원!')],
            {5695: VOUCHER_PROMO_CONTENT},
        )

        # Act
        result = fetch_coinone_fee_promo()

        # Assert
        assert result['notice_id'] == 5695
        assert len(calls) == 2  # 목록 + 상세

    def test_promo_result_is_cached(self, tmp_path, monkeypatch):
        """첫 조회 결과가 캐시에 기록되어 두 번째 호출은 HTTP를 타지 않는다"""
        # Arrange
        _use_temp_cache(monkeypatch, tmp_path)
        calls = _install_api_mock(
            monkeypatch,
            [_notice(5695, '코인원 전 종목 거래 수수료 0원!')],
            {5695: VOUCHER_PROMO_CONTENT},
        )

        # Act
        first = fetch_coinone_fee_promo()
        call_count_after_first = len(calls)
        second = fetch_coinone_fee_promo()

        # Assert
        assert first == second
        assert len(calls) == call_count_after_first

    def test_no_promo_result_is_cached(self, tmp_path, monkeypatch):
        """프로모션이 없어도 checked_at 을 기록해 TTL 내 재요청을 막는다"""
        # Arrange
        _use_temp_cache(monkeypatch, tmp_path)
        calls = _install_api_mock(monkeypatch, [_notice(5700, 'BTC 점검 안내')], {})

        # Act
        assert fetch_coinone_fee_promo() is None
        call_count_after_first = len(calls)
        assert fetch_coinone_fee_promo() is None

        # Assert
        assert len(calls) == call_count_after_first

    def test_cache_preserves_existing_keys(self, tmp_path, monkeypatch):
        """fee_promo 저장이 기존 출금 수수료 캐시를 덮어쓰지 않는다"""
        # Arrange
        cache_file = _use_temp_cache(monkeypatch, tmp_path, {
            'last_updated': datetime.now().isoformat(),
            'fees': {'upbit_btc': 0.0002},
        })
        _install_api_mock(
            monkeypatch,
            [_notice(5695, '코인원 전 종목 거래 수수료 0원!')],
            {5695: VOUCHER_PROMO_CONTENT},
        )

        # Act
        fetch_coinone_fee_promo()

        # Assert
        saved = json.loads(cache_file.read_text(encoding='utf-8'))
        assert saved['fees'] == {'upbit_btc': 0.0002}
        assert saved['fee_promo']['coinone']['notice_id'] == 5695


# ─────────────────────────────────────────────────────────────
# get_ticker_data 연동 — 프로모션 수수료가 티커에 반영되는지
# ─────────────────────────────────────────────────────────────

class TestGetTickerDataFeePromo:
    @staticmethod
    def _stub_ticker(monkeypatch, exchange):
        monkeypatch.setitem(
            market_core.KOREA_FETCHERS,
            exchange,
            lambda: {'price': 100_000_000.0, 'high': 1.0, 'low': 1.0, 'volume': 1.0, 'currency': 'KRW'},
        )

    def test_promo_overrides_static_fees(self, monkeypatch):
        """프로모션이 있으면 TRADING_FEES 대신 공지 파싱값을 쓴다"""
        # Arrange
        self._stub_ticker(monkeypatch, 'coinone')
        monkeypatch.setattr(
            market_core, 'fetch_coinone_fee_promo',
            lambda: {'maker_fee_pct': 0.0, 'taker_fee_pct': 0.0, 'notice_id': 5695},
        )

        # Act
        data = market_core.get_ticker_data('coinone')

        # Assert
        assert data['maker_fee_pct'] == 0.0
        assert data['taker_fee_pct'] == 0.0

    def test_static_fees_used_without_promo(self, monkeypatch):
        """프로모션이 없으면(None) 기존 정적 수수료를 유지한다"""
        # Arrange
        self._stub_ticker(monkeypatch, 'coinone')
        monkeypatch.setattr(market_core, 'fetch_coinone_fee_promo', lambda: None)

        # Act
        data = market_core.get_ticker_data('coinone')

        # Assert
        expected = market_core.TRADING_FEES['coinone']
        assert data['maker_fee_pct'] == expected['maker'] * 100
        assert data['taker_fee_pct'] == expected['taker'] * 100

    def test_other_korea_exchange_untouched(self, monkeypatch):
        """다른 국내 거래소는 프로모션 조회를 하지 않는다"""
        # Arrange
        self._stub_ticker(monkeypatch, 'upbit')

        def _should_not_be_called():
            raise AssertionError('coinone 외 거래소에서 프로모션을 조회하면 안 된다')

        monkeypatch.setattr(market_core, 'fetch_coinone_fee_promo', _should_not_be_called)

        # Act
        data = market_core.get_ticker_data('upbit')

        # Assert
        expected = market_core.TRADING_FEES['upbit']
        assert data['taker_fee_pct'] == expected['taker'] * 100


# ─────────────────────────────────────────────────────────────
# 바우처/이벤트 배지 문구 (exchange_fee_promo_note)
# ─────────────────────────────────────────────────────────────

class TestCoinoneVoucherNote:
    @staticmethod
    def _stub_promo(monkeypatch, promo):
        monkeypatch.setattr(path_helpers, 'fetch_coinone_fee_promo', lambda: promo)

    def test_voucher_promo_returns_note(self, monkeypatch):
        """바우처 전제 이벤트가 진행 중이면 안내 문구를 반환"""
        # Arrange
        self._stub_promo(monkeypatch, {'taker_fee_pct': 0.0, 'requires_voucher': True})

        # Act / Assert
        assert exchange_fee_promo_note('coinone') == COINONE_VOUCHER_NOTE

    def test_promo_without_voucher_returns_none(self, monkeypatch):
        """바우처 조건이 없는 수수료 인하는 배지를 띄우지 않는다"""
        # Arrange
        self._stub_promo(monkeypatch, {'taker_fee_pct': 0.04, 'requires_voucher': False})

        # Act / Assert
        assert exchange_fee_promo_note('coinone') is None

    def test_no_promo_returns_none(self, monkeypatch):
        # Arrange
        self._stub_promo(monkeypatch, None)

        # Act / Assert
        assert exchange_fee_promo_note('coinone') is None

    def test_case_insensitive_exchange(self, monkeypatch):
        # Arrange
        self._stub_promo(monkeypatch, {'requires_voucher': True})

        # Act / Assert
        assert exchange_fee_promo_note('COINONE') == COINONE_VOUCHER_NOTE

    def test_other_exchange_skips_lookup(self, monkeypatch):
        """코인원이 아니면 파일 캐시를 아예 건드리지 않는다"""
        # Arrange
        def _should_not_be_called():
            raise AssertionError('코인원 외 거래소에서 프로모션을 조회하면 안 된다')

        monkeypatch.setattr(path_helpers, 'fetch_coinone_fee_promo', _should_not_be_called)

        # Act / Assert
        assert exchange_fee_promo_note('upbit') is None
        assert exchange_fee_promo_note(None) is None


# ─────────────────────────────────────────────────────────────
# 엣지 함수 note 전달 (path_graph)
# ─────────────────────────────────────────────────────────────

_KRW_PER_BTC = 100_000_000.0
_USD_KRW = 1_400.0


class TestLegNotePassthrough:
    def test_buy_leg_note_defaults_to_none(self):
        """note 미지정 기존 호출부는 그대로 동작한다(하위호환)"""
        # Act
        leg = korea_buy_leg(1_000_000, 0.001, _KRW_PER_BTC, 'BTC', _USD_KRW)

        # Assert
        assert leg.components[0]['note'] is None

    def test_buy_leg_forwards_note(self):
        # Act
        leg = korea_buy_leg(1_000_000, 0.0, _KRW_PER_BTC, 'BTC', _USD_KRW, note=COINONE_VOUCHER_NOTE)

        # Assert
        assert leg.components[0]['label'] == '국내 매수 수수료'
        assert leg.components[0]['note'] == COINONE_VOUCHER_NOTE

    def test_sell_leg_note_defaults_to_none(self):
        # Act
        leg = korea_sell_leg(1.0, 0.001, _KRW_PER_BTC, 'BTC', _USD_KRW)

        # Assert
        assert leg.components[0]['note'] is None

    def test_sell_leg_forwards_note(self):
        # Act
        leg = korea_sell_leg(1.0, 0.0, _KRW_PER_BTC, 'BTC', _USD_KRW, note=COINONE_VOUCHER_NOTE)

        # Assert
        assert leg.components[0]['label'] == '국내 BTC 매도 수수료'
        assert leg.components[0]['note'] == COINONE_VOUCHER_NOTE
