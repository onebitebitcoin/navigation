from backend.app.domain.path_helpers import (
    _build_path_id,
    _slug_path_part,
    fee_component,
    is_bitcoin_native_network,
    is_suspended,
)


class TestSlugPathPart:
    def test_normal_string(self):
        assert _slug_path_part('Bitcoin') == 'bitcoin'

    def test_none_returns_na(self):
        assert _slug_path_part(None) == 'na'

    def test_spaces_become_dashes(self):
        result = _slug_path_part('Hello World')
        assert '-' in result

    def test_empty_string_returns_na(self):
        assert _slug_path_part('') == 'na'

    def test_special_chars_collapsed(self):
        result = _slug_path_part('BTC/ETH')
        assert result == 'btc-eth'


class TestBuildPathId:
    def test_returns_double_underscore_joined_string(self):
        result = _build_path_id(
            global_exchange='binance',
            korean_exchange='bithumb',
            transfer_coin='BTC',
            domestic_withdrawal_network='Bitcoin',
            global_exit_mode='onchain',
            global_exit_network='Bitcoin',
            lightning_exit_provider=None,
        )
        parts = result.split('__')
        assert len(parts) == 7

    def test_none_fields_become_na_or_none(self):
        result = _build_path_id(
            global_exchange='binance',
            korean_exchange='bithumb',
            transfer_coin='BTC',
            domestic_withdrawal_network=None,
            global_exit_mode=None,
            global_exit_network=None,
            lightning_exit_provider=None,
        )
        parts = result.split('__')
        assert parts[3] == 'na'


class TestIsBitcoinNativeNetwork:
    def test_bitcoin_label(self):
        assert is_bitcoin_native_network('bitcoin') is True

    def test_lightning_excluded(self):
        assert is_bitcoin_native_network('bitcoin lightning') is False

    def test_erc20_excluded(self):
        assert is_bitcoin_native_network('btc erc20') is False

    def test_btc_label(self):
        assert is_bitcoin_native_network('btc onchain') is True

    def test_bep20_excluded(self):
        assert is_bitcoin_native_network('btc bep20') is False

    def test_trc20_excluded(self):
        assert is_bitcoin_native_network('btc trc20') is False

    def test_non_btc_string(self):
        assert is_bitcoin_native_network('ethereum') is False


class TestIsSuspended:
    def test_no_maintenance_returns_none(self):
        assert is_suspended({}, 'bithumb', 'BTC', 'Bitcoin') is None

    def test_suspended_returns_reason(self):
        status = {'bithumb': [{'coin': 'BTC', 'network': 'bitcoin', 'reason': '점검 중'}]}
        result = is_suspended(status, 'bithumb', 'BTC', 'Bitcoin')
        assert result == '점검 중'

    def test_different_exchange_returns_none(self):
        status = {'upbit': [{'coin': 'BTC', 'network': 'bitcoin', 'reason': '점검 중'}]}
        assert is_suspended(status, 'bithumb', 'BTC', 'Bitcoin') is None

    def test_different_coin_returns_none(self):
        status = {'bithumb': [{'coin': 'ETH', 'network': 'ethereum', 'reason': '점검 중'}]}
        assert is_suspended(status, 'bithumb', 'BTC', 'Bitcoin') is None

    def test_missing_reason_returns_default(self):
        status = {'bithumb': [{'coin': 'BTC', 'network': 'bitcoin'}]}
        result = is_suspended(status, 'bithumb', 'BTC', 'Bitcoin')
        assert result == '점검 중'


class TestFeeComponent:
    def test_basic(self):
        result = fee_component('수수료', 1000)
        assert result['label'] == '수수료'
        assert result['amount_krw'] == 1000
        assert result['rate_pct'] is None

    def test_with_rate(self):
        result = fee_component('수수료', 1000, rate_pct=0.1)
        assert result['rate_pct'] == 0.1

    def test_rate_rounded_to_4_decimals(self):
        result = fee_component('수수료', 500, rate_pct=0.123456)
        assert result['rate_pct'] == 0.1235

    def test_amount_text_included(self):
        result = fee_component('수수료', 0, amount_text='0.0001 BTC')
        assert result['amount_text'] == '0.0001 BTC'

    def test_source_url_included(self):
        result = fee_component('수수료', 0, source_url='https://example.com')
        assert result['source_url'] == 'https://example.com'

    def test_note_defaults_to_none(self):
        # note를 넘기지 않는 기존 호출부는 키가 있되 값이 None이어야 한다(하위호환).
        result = fee_component('수수료', 1000)
        assert result['note'] is None

    def test_note_included(self):
        result = fee_component('수수료', 0, note='바우처 발급 시 무료')
        assert result['note'] == '바우처 발급 시 무료'
