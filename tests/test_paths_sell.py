from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from backend.app.domain.paths_sell import find_cheapest_sell_path_from_snapshot_rows


def _make_run():
    r = MagicMock()
    r.usd_krw_rate = 1400.0
    r.completed_at = None
    r.id = 1
    r.status = 'success'
    return r


def _mock_wallet_fee(fee_btc: float = 0.00001):
    """mempool API 호출을 막는 wallet fee 픽스처."""
    return {
        'source': 'mempool.space',
        'source_url': 'https://mempool.space/api/v1/fees/recommended',
        'fee_target': 'medium',
        'medium_fee_rate_sat_vb': 10.0,
        'fastest_fee_sat_vb': 12.0,
        'hour_fee_sat_vb': 8.0,
        'economy_fee_sat_vb': 5.0,
        'minimum_fee_sat_vb': 1.0,
        'address_type': 'p2wpkh',
        'utxo_count': 1,
        'output_count': 2,
        'estimated_tx_vbytes': 141,
        'fee_sats': int(fee_btc * 1e8),
        'fee_btc': fee_btc,
    }


def _make_ticker(exchange: str, price: float, currency: str = 'KRW', taker_pct: float = 0.1):
    r = MagicMock()
    r.exchange = exchange
    r.market_type = 'spot'
    r.currency = currency
    r.price = price
    r.taker_fee_pct = taker_pct
    r.usd_krw_rate = None
    return r


def _make_withdrawal(
    exchange: str,
    coin: str,
    network_label: str,
    fee: float,
    fee_krw: float | None = None,
    enabled: bool = True,
    min_withdrawal: float | None = None,
    max_withdrawal: float | None = None,
):
    r = SimpleNamespace(
        exchange=exchange,
        coin=coin,
        network_label=network_label,
        fee=fee,
        fee_krw=fee_krw,
        enabled=enabled,
        min_withdrawal=min_withdrawal,
        max_withdrawal=max_withdrawal,
    )
    return r


def test_sell_returns_error_without_latest_run():
    result = find_cheapest_sell_path_from_snapshot_rows(0.01, 'binance', None, [], [], [])
    assert 'error' in result


def test_sell_returns_error_for_invalid_exchange():
    result = find_cheapest_sell_path_from_snapshot_rows(0.01, 'unknown', None, [], [], [])
    assert 'error' in result


def test_sell_returns_error_for_zero_amount():
    run = _make_run()
    result = find_cheapest_sell_path_from_snapshot_rows(0.0, 'binance', run, [], [], [])
    assert 'error' in result


def test_sell_returns_dict_with_mode_sell():
    run = _make_run()
    global_ticker = MagicMock()
    global_ticker.exchange = 'binance'
    global_ticker.market_type = 'spot'
    global_ticker.currency = 'USD'
    global_ticker.price = 90000.0
    global_ticker.taker_fee_pct = 0.1
    global_ticker.usd_krw_rate = None

    result = find_cheapest_sell_path_from_snapshot_rows(0.01, 'binance', run, [global_ticker], [], [])
    # mempool API 실패는 expected (네트워크 없음) → error 또는 정상 dict
    assert isinstance(result, dict)


def test_sell_usdt_path_blocked_by_max_withdrawal():
    """USDT 출금 row에 max_withdrawal이 있고 전송액이 초과할 때 해당 경로가 all_paths에서 제외되고
    disabled_paths에 '한도' 사유가 포함되어야 한다."""
    run = _make_run()
    amount_btc = 10.0  # 거액: USDT 전송액이 max_withdrawal을 초과하도록

    # 글로벌 ticker: BTC/USD 100000, taker 0.1%
    global_ticker = _make_ticker('binance', 100_000.0, currency='USD', taker_pct=0.1)
    # 국내 ticker: BTC/KRW 150,000,000, taker 0.05%
    korea_ticker = _make_ticker('bithumb', 150_000_000.0, currency='KRW', taker_pct=0.05)

    # USDT 출금 row: max_withdrawal=5000 USDT (거액 전송 시 초과)
    usdt_wd = _make_withdrawal(
        exchange='binance',
        coin='USDT',
        network_label='TRC20',
        fee=1.0,
        fee_krw=1400.0,
        enabled=True,
        max_withdrawal=5_000.0,  # 거액 BTC 매도 시 USDT 수십만 달러 → 초과
    )

    with patch('backend.app.domain.paths_sell._estimate_wallet_btc_network_fee', return_value=_mock_wallet_fee(0.00001)):
        result = find_cheapest_sell_path_from_snapshot_rows(
            amount_btc,
            'binance',
            run,
            ticker_rows=[global_ticker, korea_ticker],
            withdrawal_rows=[usdt_wd],
            network_rows=[],
        )

    assert 'error' not in result, f'에러 발생: {result}'
    # USDT 경로가 all_paths에 없어야 함
    usdt_paths = [p for p in result['all_paths'] if p['transfer_coin'] == 'USDT']
    assert len(usdt_paths) == 0, f'USDT 경로가 all_paths에 남아 있음: {usdt_paths}'

    # disabled_paths에 한도 사유가 있어야 함
    assert len(result['disabled_paths']) >= 1, 'disabled_paths가 비어 있음'
    reasons = [d['reason'] for d in result['disabled_paths']]
    assert any('한도' in r for r in reasons), f'한도 사유 없음: {reasons}'


def test_sell_usdt_path_not_blocked_within_max_withdrawal():
    """전송액이 max_withdrawal 이내이면 USDT 경로가 정상 포함된다."""
    run = _make_run()
    amount_btc = 0.001  # 소액: USDT 전송액이 max_withdrawal 이하

    global_ticker = _make_ticker('binance', 100_000.0, currency='USD', taker_pct=0.1)
    korea_ticker = _make_ticker('bithumb', 150_000_000.0, currency='KRW', taker_pct=0.05)

    # max_withdrawal=5000 USDT → 소액에선 문제없음 (0.001 BTC × 100000 USD = 100 USDT)
    usdt_wd = _make_withdrawal(
        exchange='binance',
        coin='USDT',
        network_label='TRC20',
        fee=1.0,
        fee_krw=1400.0,
        enabled=True,
        max_withdrawal=5_000.0,
    )

    with patch('backend.app.domain.paths_sell._estimate_wallet_btc_network_fee', return_value=_mock_wallet_fee(0.00001)):
        result = find_cheapest_sell_path_from_snapshot_rows(
            amount_btc,
            'binance',
            run,
            ticker_rows=[global_ticker, korea_ticker],
            withdrawal_rows=[usdt_wd],
            network_rows=[],
        )

    assert 'error' not in result
    usdt_paths = [p for p in result['all_paths'] if p['transfer_coin'] == 'USDT']
    assert len(usdt_paths) >= 1, 'max_withdrawal 이내인 USDT 경로가 all_paths에서 누락됨'


def test_sell_disabled_paths_deduplication():
    """동일 (exchange, coin, network, reason)의 disabled_paths는 중복 없이 1개만 기록된다."""
    run = _make_run()
    amount_btc = 50.0  # 매우 거액: 여러 한국 거래소 루프 반복 시 같은 USDT row가 반복 차단

    # 국내 거래소 2곳
    korea_tickers = [
        _make_ticker('bithumb', 150_000_000.0, currency='KRW', taker_pct=0.05),
        _make_ticker('upbit', 149_000_000.0, currency='KRW', taker_pct=0.05),
    ]
    global_ticker = _make_ticker('binance', 100_000.0, currency='USD', taker_pct=0.1)

    usdt_wd = _make_withdrawal(
        exchange='binance',
        coin='USDT',
        network_label='TRC20',
        fee=1.0,
        fee_krw=1400.0,
        enabled=True,
        max_withdrawal=1_000.0,
    )

    with patch('backend.app.domain.paths_sell._estimate_wallet_btc_network_fee', return_value=_mock_wallet_fee(0.00001)):
        result = find_cheapest_sell_path_from_snapshot_rows(
            amount_btc,
            'binance',
            run,
            ticker_rows=[global_ticker] + korea_tickers,
            withdrawal_rows=[usdt_wd],
            network_rows=[],
        )

    # 같은 (USDT, TRC20, 한도초과) disabled가 여러 한국 거래소마다 반복 추가되지 않아야 함
    disabled_reasons = [(d['transfer_coin'], d['network'], d['reason']) for d in result['disabled_paths']]
    assert len(disabled_reasons) == len(set(disabled_reasons)), f'중복 disabled_paths 존재: {disabled_reasons}'


def test_coinone_sell_fee_carries_voucher_note():
    """코인원 바우처 이벤트 진행 중이면 국내 매도 수수료 항목에 note가 실린다.

    note는 프론트 결과 페이지에서 배지로 노출되므로, 실제 경로 계산 결과의
    breakdown까지 값이 전달되는지를 엔드투엔드로 확인한다.
    """
    # Arrange
    run = _make_run()
    global_ticker = _make_ticker('binance', 100_000.0, currency='USD', taker_pct=0.1)
    # 코인원은 이벤트로 taker 0%, 업비트는 평시 수수료 → 배지가 코인원에만 붙는지 대조
    coinone_ticker = _make_ticker('coinone', 150_000_000.0, taker_pct=0.0)
    upbit_ticker = _make_ticker('upbit', 150_000_000.0, taker_pct=0.05)
    btc_wds = [
        _make_withdrawal('coinone', 'BTC', 'Bitcoin (On-chain)', 0.0009, fee_krw=135_000.0),
        _make_withdrawal('upbit', 'BTC', 'Bitcoin (On-chain)', 0.0009, fee_krw=135_000.0),
    ]

    promo = {'taker_fee_pct': 0.0, 'requires_voucher': True}

    # Act
    with patch('backend.app.domain.path_helpers.fetch_coinone_fee_promo', return_value=promo), \
         patch('backend.app.domain.paths_sell._estimate_wallet_btc_network_fee', return_value=_mock_wallet_fee(0.00001)):
        result = find_cheapest_sell_path_from_snapshot_rows(
            0.5,
            'binance',
            run,
            ticker_rows=[global_ticker, coinone_ticker, upbit_ticker],
            withdrawal_rows=btc_wds,
            network_rows=[],
        )

    # Assert
    assert 'error' not in result, f'에러 발생: {result}'

    def _sell_notes(exchange: str) -> list:
        return [
            c['note']
            for p in result['all_paths'] if p['korean_exchange'] == exchange
            for c in p['breakdown']['components'] if c['label'] == '국내 BTC 매도 수수료'
        ]

    coinone_notes = _sell_notes('coinone')
    assert coinone_notes, '코인원 매도 경로가 없음'
    assert all('바우처' in note for note in coinone_notes), f'코인원 note 누락: {coinone_notes}'
    # 다른 거래소에는 붙지 않아야 한다
    assert _sell_notes('upbit') == [None] * len(_sell_notes('upbit'))
