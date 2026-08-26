from __future__ import annotations

from fee_checker import (
    ALL_EXCHANGES,
    GROUPS,
    TRADING_FEES,
    check_maintenance_status,
    fetch_binance_perp,
    fetch_binance_spot,
    fetch_binance_withdrawal,
    fetch_bitget,
    fetch_bitget_withdrawal,
    fetch_bithumb,
    fetch_bithumb_withdrawal,
    fetch_bybit,
    fetch_bybit_withdrawal,
    fetch_gate,
    fetch_gate_withdrawal,
    fetch_coinbase_withdrawal,
    fetch_coinbase,
    fetch_coinone,
    fetch_coinone_fee_promo,
    fetch_gopax,
    fetch_gopax_withdrawal,
    fetch_kraken_withdrawal,
    fetch_kraken,
    fetch_korbit,
    fetch_korbit_fee_promo,
    fetch_okx_perp,
    fetch_okx_spot,
    fetch_okx_withdrawal,
    fetch_upbit,
    fetch_usd_krw_rate,
    get_scraped_withdrawal,
    get_withdrawal_source_url,
    refresh_withdrawal_cache,
)

KOREA_FETCHERS = {
    'upbit': fetch_upbit,
    'bithumb': fetch_bithumb,
    'korbit': fetch_korbit,
    'coinone': fetch_coinone,
    'gopax': fetch_gopax,
}

GLOBAL_FETCHERS = {
    'binance': {'spot': fetch_binance_spot, 'perpetual': fetch_binance_perp},
    'okx': {'spot': fetch_okx_spot, 'perpetual': fetch_okx_perp},
    'coinbase': {'spot': fetch_coinbase},
    'kraken': {'spot': fetch_kraken},
    'bitget': {'spot': fetch_bitget},
    'bybit': {'spot': fetch_bybit},
    'gate':  {'spot': fetch_gate},
}

WITHDRAWAL_FETCHERS = {
    'bithumb': fetch_bithumb_withdrawal,
    'binance': fetch_binance_withdrawal,
    'okx': fetch_okx_withdrawal,
    'coinbase': fetch_coinbase_withdrawal,
    'kraken': fetch_kraken_withdrawal,
    'gopax': fetch_gopax_withdrawal,
    'bitget': fetch_bitget_withdrawal,
    'bybit': fetch_bybit_withdrawal,
    'gate':  fetch_gate_withdrawal,
}

SCRAPED_WITHDRAWAL_FETCHER_EXCHANGES = {'coinbase', 'kraken'}

# (거래소, 코인) — 공개 API 미제공으로 정적 등록값을 쓰는 출금 수수료. 어드민에 'static'으로 노출.
STATIC_WITHDRAWAL_FEE_KEYS = {('coinbase', 'BTC')}


def withdrawal_source(exchange: str, coin: str) -> str:
    """출금 수수료 데이터 출처 라벨: static / realtime_api / scraped_page."""
    if (exchange, coin.upper()) in STATIC_WITHDRAWAL_FEE_KEYS:
        return 'static'
    if exchange in SCRAPED_WITHDRAWAL_FETCHER_EXCHANGES:
        return 'scraped_page'
    if exchange in WITHDRAWAL_FETCHERS:
        return 'realtime_api'
    return 'scraped_page'

__all__ = [
    'ALL_EXCHANGES',
    'GLOBAL_FETCHERS',
    'GROUPS',
    'KOREA_FETCHERS',
    'TRADING_FEES',
    'WITHDRAWAL_FETCHERS',
    'STATIC_WITHDRAWAL_FEE_KEYS',
    'withdrawal_source',
    'check_maintenance_status',
    'enrich_ticker_fees',
    'fetch_binance_perp',
    'fetch_binance_spot',
    'fetch_binance_withdrawal',
    'fetch_bitget',
    'fetch_bitget_withdrawal',
    'fetch_bithumb',
    'fetch_bithumb_withdrawal',
    'fetch_coinbase',
    'fetch_coinbase_withdrawal',
    'fetch_coinone',
    'fetch_coinone_fee_promo',
    'fetch_gopax',
    'fetch_gopax_withdrawal',
    'fetch_kraken',
    'fetch_kraken_withdrawal',
    'fetch_korbit',
    'fetch_korbit_fee_promo',
    'fetch_okx_perp',
    'fetch_okx_spot',
    'fetch_okx_withdrawal',
    'fetch_gate',
    'fetch_gate_withdrawal',
    'fetch_upbit',
    'fetch_usd_krw_rate',
    'get_scraped_withdrawal',
    'get_ticker',
    'get_ticker_data',
    'get_withdrawal_data',
    'get_withdrawal_fees',
    'get_withdrawal_source_url',
    'list_exchanges',
    'refresh_withdrawal_cache',
]


def list_exchanges() -> dict:
    return {
        'korea': GROUPS['korea'],
        'global': GROUPS['global'],
        'all': ALL_EXCHANGES,
        'total': len(ALL_EXCHANGES),
    }


def get_ticker_data(exchange: str) -> dict | list[dict]:
    if exchange in KOREA_FETCHERS:
        ticker = KOREA_FETCHERS[exchange]()
        fees = TRADING_FEES[exchange]
        maker_fee_pct = fees['maker'] * 100
        taker_fee_pct = fees['taker'] * 100
        # 진행 중인 수수료 프로모션 공지가 확인되면 정적 등급표 대신 공지 파싱값을 쓴다.
        # 프로모션이 없거나 조회 실패면 None → 정적값 유지(하드코딩 fallback 없음).
        if exchange == 'coinone':
            promo = fetch_coinone_fee_promo()
            if promo:
                maker_fee_pct = promo['maker_fee_pct']
                taker_fee_pct = promo['taker_fee_pct']
        elif exchange == 'korbit':
            promo = fetch_korbit_fee_promo()
            if promo:
                maker_fee_pct = promo['maker_fee_pct']
                taker_fee_pct = promo['taker_fee_pct']
        return {
            'exchange': exchange,
            'pair': f"BTC/{ticker.get('currency', 'KRW')}",
            'market_type': 'spot',
            'price': ticker['price'],
            'high_24h': ticker.get('high'),
            'low_24h': ticker.get('low'),
            'volume_24h_btc': ticker.get('volume'),
            'currency': ticker.get('currency', 'KRW'),
            'maker_fee_pct': maker_fee_pct,
            'taker_fee_pct': taker_fee_pct,
        }
    if exchange in GLOBAL_FETCHERS:
        results = []
        for market_type, fetcher in GLOBAL_FETCHERS[exchange].items():
            ticker = fetcher()
            fees_entry = TRADING_FEES[exchange]
            fees = fees_entry[market_type] if isinstance(fees_entry.get('spot'), dict) else fees_entry
            results.append({
                'exchange': exchange,
                'pair': f"BTC/{ticker.get('currency', 'USD')}",
                'market_type': market_type,
                'price': ticker['price'],
                'high_24h': ticker.get('high'),
                'low_24h': ticker.get('low'),
                'volume_24h_btc': ticker.get('volume'),
                'currency': ticker.get('currency', 'USD'),
                'maker_fee_pct': fees['maker'] * 100,
                'taker_fee_pct': fees['taker'] * 100,
            })
        return results[0] if len(results) == 1 else results
    raise ValueError(f'알 수 없는 거래소: {exchange}')


def enrich_ticker_fees(data: dict, usd_krw_rate: float) -> None:
    price = data.get('price', 0)
    currency = data.get('currency', 'USD')
    price_usd = price / usd_krw_rate if currency == 'KRW' else price
    maker_usd = round(price_usd * data.get('maker_fee_pct', 0) / 100, 2)
    taker_usd = round(price_usd * data.get('taker_fee_pct', 0) / 100, 2)
    data['maker_role'] = '지정가 매도 (Limit Sell)'
    data['taker_role'] = '시장가 매수 (Market Buy)'
    data['maker_fee_usd'] = maker_usd
    data['maker_fee_krw'] = round(maker_usd * usd_krw_rate)
    data['taker_fee_usd'] = taker_usd
    data['taker_fee_krw'] = round(taker_usd * usd_krw_rate)
    data['usd_krw_rate'] = round(usd_krw_rate)


def get_ticker(exchange: str) -> dict:
    exchange = exchange.lower()
    if exchange not in ALL_EXCHANGES:
        return {'error': f'지원하지 않는 거래소: {exchange}. list_exchanges()로 목록 확인'}
    try:
        data = get_ticker_data(exchange)
        usd_krw_rate = fetch_usd_krw_rate()
        if isinstance(data, list):
            for item in data:
                enrich_ticker_fees(item, usd_krw_rate)
            return {'markets': data}
        enrich_ticker_fees(data, usd_krw_rate)
        return data
    except Exception as exc:
        return {'error': str(exc), 'exchange': exchange}


def get_withdrawal_data(exchange: str, coin: str) -> list:
    normalized_coin = coin.upper()
    if exchange in WITHDRAWAL_FETCHERS:
        return WITHDRAWAL_FETCHERS[exchange](normalized_coin)
    return get_scraped_withdrawal(exchange, normalized_coin)


def get_withdrawal_fees(exchange: str, coin: str = 'BTC') -> dict:
    exchange = exchange.lower()
    coin = coin.upper()
    if exchange not in ALL_EXCHANGES:
        return {'error': f'지원하지 않는 거래소: {exchange}'}
    if coin not in ('BTC', 'USDT'):
        return {'error': "coin은 'BTC' 또는 'USDT'만 지원합니다"}

    try:
        networks = get_withdrawal_data(exchange, coin)
        result = {
            'exchange': exchange,
            'coin': coin,
            'source': withdrawal_source(exchange, coin),
            'networks': networks,
        }
        usd_krw_rate = fetch_usd_krw_rate()
        if coin == 'BTC':
            try:
                btc_price_usd = fetch_kraken()['price']
                for network in networks:
                    fee = network.get('fee')
                    if fee is None:
                        network['fee_usd'] = None
                        network['fee_krw'] = None
                        continue
                    fee_usd = round(fee * btc_price_usd, 2)
                    network['fee_usd'] = fee_usd
                    network['fee_krw'] = round(fee_usd * usd_krw_rate)
                result['btc_price_usd'] = btc_price_usd
            except Exception:
                pass
        else:
            for network in networks:
                fee = network.get('fee')
                if fee is None:
                    network['fee_usd'] = None
                    network['fee_krw'] = None
                    continue
                network['fee_usd'] = round(fee, 4)
                network['fee_krw'] = round(fee * usd_krw_rate)
        result['usd_krw_rate'] = round(usd_krw_rate)
        return result
    except Exception as exc:
        return {'error': str(exc), 'exchange': exchange, 'coin': coin}
