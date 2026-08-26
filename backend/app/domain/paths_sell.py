"""sell 경로 계산 — find_cheapest_sell_path_from_snapshot_rows."""
from __future__ import annotations

import logging
import math
import time

import requests

from backend.app.domain.market_core import GROUPS, TRADING_FEES, get_withdrawal_source_url
from backend.app.domain.path_graph import (
    Blocked,
    global_sell_leg,
    korea_sell_leg,
    swap_leg,
    withdraw_leg,
)
from backend.app.domain.path_helpers import (
    _build_path_id,
    coinone_voucher_note,
    fee_component,
    is_suspended,
)
from backend.app.domain.paths_context import SnapshotContext, build_snapshot_context
from backend.app.domain.paths_buy import _build_available_filters

logger = logging.getLogger(__name__)

MEMPOOL_RECOMMENDED_FEES_URL = 'https://mempool.space/api/v1/fees/recommended'
P2WPKH_INPUT_VBYTES = 68
P2WPKH_OUTPUT_VBYTES = 31
P2WPKH_BASE_TX_VBYTES = 10.5
DEFAULT_SELL_TX_OUTPUT_COUNT = 2

_mempool_cache: dict[str, dict] = {}
_MEMPOOL_CACHE_TTL = 30  # seconds


def _estimate_native_segwit_tx_vbytes(utxo_count: int, output_count: int = DEFAULT_SELL_TX_OUTPUT_COUNT) -> int:
    if utxo_count <= 0:
        raise ValueError('wallet_utxo_count는 1 이상이어야 합니다.')
    if output_count <= 0:
        raise ValueError('output_count는 1 이상이어야 합니다.')
    return math.ceil(P2WPKH_BASE_TX_VBYTES + (P2WPKH_INPUT_VBYTES * utxo_count) + (P2WPKH_OUTPUT_VBYTES * output_count))


def _fetch_mempool_recommended_fees() -> dict:
    cached = _mempool_cache.get('fees')
    if cached and time.time() - cached['ts'] < _MEMPOOL_CACHE_TTL:
        data = cached['data']
    else:
        try:
            response = requests.get(MEMPOOL_RECOMMENDED_FEES_URL, timeout=10, headers={'Accept': 'application/json'})
            response.raise_for_status()
            data = response.json()
            _mempool_cache['fees'] = {'data': data, 'ts': time.time()}
        except Exception as exc:  # pragma: no cover - network dependency
            raise ValueError(f'mempool.space 수수료 조회 실패: {exc}') from exc

    medium_fee_rate = data.get('halfHourFee') or data.get('hourFee') or data.get('fastestFee')
    if medium_fee_rate is None:
        raise ValueError('mempool.space 응답에 halfHourFee/hourFee/fastestFee가 없습니다.')

    return {
        'source': 'mempool.space',
        'source_url': MEMPOOL_RECOMMENDED_FEES_URL,
        'fee_target': 'medium',
        'medium_fee_rate_sat_vb': float(medium_fee_rate),
        'fastest_fee_sat_vb': float(data.get('fastestFee')) if data.get('fastestFee') is not None else None,
        'hour_fee_sat_vb': float(data.get('hourFee')) if data.get('hourFee') is not None else None,
        'economy_fee_sat_vb': float(data.get('economyFee')) if data.get('economyFee') is not None else None,
        'minimum_fee_sat_vb': float(data.get('minimumFee')) if data.get('minimumFee') is not None else None,
    }


def _estimate_wallet_btc_network_fee(*, wallet_utxo_count: int = 1) -> dict:
    fee_data = _fetch_mempool_recommended_fees()
    tx_vbytes = _estimate_native_segwit_tx_vbytes(wallet_utxo_count)
    fee_sats = max(math.ceil(fee_data['medium_fee_rate_sat_vb'] * tx_vbytes), 1)
    fee_btc = round(fee_sats / 100_000_000, 8)
    return {
        **fee_data,
        'address_type': 'p2wpkh',
        'utxo_count': wallet_utxo_count,
        'output_count': DEFAULT_SELL_TX_OUTPUT_COUNT,
        'estimated_tx_vbytes': tx_vbytes,
        'fee_sats': fee_sats,
        'fee_btc': fee_btc,
    }


def _estimate_wallet_btc_network_fee_btc(wallet_utxo_count: int = 1) -> float:
    return _estimate_wallet_btc_network_fee(wallet_utxo_count=wallet_utxo_count)['fee_btc']


def find_cheapest_sell_path_from_snapshot_rows(
    amount_btc: float,
    global_exchange: str,
    latest_run,
    ticker_rows: list,
    withdrawal_rows: list,
    network_rows: list,
    lightning_swap_rows: list | None = None,
    exchange_capability_rows: list | None = None,
    wallet_utxo_count: int = 1,
) -> dict:
    global_exchange = global_exchange.lower()
    if global_exchange not in GROUPS['global']:
        return {'error': f"지원하지 않는 글로벌 거래소: {global_exchange}. {GROUPS['global']} 중 선택"}
    if latest_run is None:
        return {'error': '최신 수집 결과가 없습니다. 먼저 수동 크롤링을 실행하세요.'}
    if amount_btc <= 0:
        return {'error': 'amount_btc는 0보다 커야 합니다.'}
    if wallet_utxo_count <= 0:
        return {'error': 'wallet_utxo_count는 1 이상이어야 합니다.'}

    def build_entry(
        *,
        route_variant: str,
        korean_exchange: str,
        transfer_coin: str,
        domestic_withdrawal_network: str,
        global_exit_mode: str,
        global_exit_network: str,
        lightning_exit_provider: str | None,
        krw_received: int,
        total_fee_krw: int,
        breakdown_components: list[dict],
    ) -> dict:
        gross_krw = krw_received + total_fee_krw
        fee_pct = round(total_fee_krw / gross_krw * 100, 4) if gross_krw > 0 else 0
        return {
            'route_variant': route_variant,
            'korean_exchange': korean_exchange,
            'transfer_coin': transfer_coin,
            'network': domestic_withdrawal_network,
            'domestic_withdrawal_network': domestic_withdrawal_network,
            'global_exit_mode': global_exit_mode,
            'global_exit_network': global_exit_network,
            'lightning_exit_provider': lightning_exit_provider,
            'path_id': _build_path_id(
                global_exchange=global_exchange,
                korean_exchange=korean_exchange,
                transfer_coin=transfer_coin,
                domestic_withdrawal_network=domestic_withdrawal_network,
                global_exit_mode=global_exit_mode,
                global_exit_network=global_exit_network,
                lightning_exit_provider=lightning_exit_provider,
            ),
            'krw_received': krw_received,
            'total_fee_krw': total_fee_krw,
            'fee_pct': fee_pct,
            'breakdown': {
                'components': breakdown_components,
                'total_fee_krw': total_fee_krw,
            },
        }

    try:
        wallet_fee_estimate = _estimate_wallet_btc_network_fee(wallet_utxo_count=wallet_utxo_count)
    except ValueError as exc:
        return {'error': str(exc)}

    ctx_or_err = build_snapshot_context(global_exchange, latest_run, ticker_rows, withdrawal_rows, network_rows)
    if isinstance(ctx_or_err, dict):
        return ctx_or_err
    ctx: SnapshotContext = ctx_or_err

    wallet_network_fee_btc = wallet_fee_estimate['fee_btc']
    wallet_network_fee_krw = round(wallet_network_fee_btc * ctx.global_btc_price_usd * ctx.usd_krw_rate)
    wallet_fee_estimate = {
        **wallet_fee_estimate,
        'fee_krw': wallet_network_fee_krw,
    }
    wallet_fee_amount_text = (
        f"{wallet_fee_estimate['fee_sats']} sats · {wallet_fee_estimate['estimated_tx_vbytes']} vB @ "
        f"{wallet_fee_estimate['medium_fee_rate_sat_vb']:g} sat/vB"
    )

    capability_by_exchange: dict[str, object] = {
        row.exchange: row for row in (exchange_capability_rows or [])
    }

    paths: list[dict] = []
    disabled_paths: list[dict] = []
    # disabled_paths 중복 제거용 키 세트
    _disabled_keys: set[tuple] = set()

    def _add_disabled(*, korean_exchange: str, transfer_coin: str, network: str, reason: str) -> None:
        # USDT 출금 등 글로벌 거래소 기반 disabled는 korean_exchange와 무관하게 동일 row이므로
        # (coin, network, reason) 기준으로 중복 제거한다.
        key = (transfer_coin, network, reason)
        if key not in _disabled_keys:
            _disabled_keys.add(key)
            disabled_paths.append({
                'korean_exchange': korean_exchange,
                'transfer_coin': transfer_coin,
                'network': network,
                'reason': reason,
            })

    for exchange in GROUPS['korea']:
        ticker_row = ctx.ticker_by_exchange.get(exchange)
        if ticker_row is None:
            continue

        korean_btc_price_krw = float(ticker_row.price)
        korean_taker = (ticker_row.taker_fee_pct / 100) if ticker_row.taker_fee_pct is not None else TRADING_FEES[exchange]['taker']
        # 거래소당 1회만 조회 (파일 캐시라 경로마다 부르면 디스크를 반복해서 읽는다)
        voucher_note = coinone_voucher_note(exchange)

        # ----- 경로 1: BTC 직접 (개인지갑 BTC → 온체인 → 국내 BTC 매도) -----
        for row in ctx.withdrawals_by_key.get((exchange, 'BTC'), []):
            if not row.enabled or row.fee is None:
                continue
            suspension_reason = is_suspended(ctx.maintenance_status, exchange, 'BTC', row.network_label)
            if suspension_reason:
                _add_disabled(korean_exchange=exchange, transfer_coin='BTC', network=row.network_label, reason=suspension_reason)
                continue

            label_lower = (row.network_label or '').lower()
            if 'lightning' in label_lower:
                continue

            btc_after_network = amount_btc - wallet_network_fee_btc
            if btc_after_network <= 0:
                continue

            sell = korea_sell_leg(btc_after_network, korean_taker, korean_btc_price_krw, 'BTC', ctx.usd_krw_rate, note=voucher_note)
            krw_received = sell.amount_out
            korean_sell_fee_krw = sell.fee_krw
            total_fee_krw = wallet_network_fee_krw + korean_sell_fee_krw
            paths.append(build_entry(
                route_variant='btc_direct',
                korean_exchange=exchange,
                transfer_coin='BTC',
                domestic_withdrawal_network=row.network_label,
                global_exit_mode='onchain',
                global_exit_network=row.network_label,
                lightning_exit_provider=None,
                krw_received=krw_received,
                total_fee_krw=total_fee_krw,
                breakdown_components=[
                    fee_component('개인지갑 BTC 네트워크 수수료', wallet_network_fee_krw, amount_text=wallet_fee_amount_text, source_url=wallet_fee_estimate['source_url']),
                    fee_component('국내 BTC 매도 수수료', korean_sell_fee_krw, rate_pct=korean_taker * 100, amount_text=f'{round(btc_after_network, 8)} BTC', note=voucher_note),
                ],
            ))

        # ----- 경로 2: USDT via global (개인지갑 BTC → 글로벌 BTC 매도 → USDT 출금 → 국내 USDT→KRW) -----
        for row in ctx.withdrawals_by_key.get((global_exchange, 'USDT'), []):
            suspension_reason = is_suspended(ctx.maintenance_status, global_exchange, 'USDT', row.network_label)
            if suspension_reason:
                _add_disabled(korean_exchange=exchange, transfer_coin='USDT', network=row.network_label, reason=suspension_reason)
                continue

            btc_at_global = amount_btc - wallet_network_fee_btc
            if btc_at_global <= 0:
                continue

            # 글로벌 BTC 매도 → USDT
            gsell = global_sell_leg(btc_at_global, ctx.global_taker, ctx.global_btc_price_usd, ctx.usd_krw_rate)
            usdt_after_global_sell = gsell.amount_out
            global_sell_fee_krw = gsell.fee_krw

            # USDT 출금 (withdraw_leg로 min/max 검증 포함)
            wd = withdraw_leg(
                row,
                usdt_after_global_sell,
                coin='USDT',
                price_krw=ctx.usd_krw_rate,
                usd_krw=ctx.usd_krw_rate,
                source_url=get_withdrawal_source_url(global_exchange, 'USDT', row.network_label),
                maintenance_status=ctx.maintenance_status,
                exchange=global_exchange,
            )
            if isinstance(wd, Blocked):
                _add_disabled(korean_exchange=exchange, transfer_coin='USDT', network=row.network_label, reason=wd.reason)
                continue

            usdt_at_korean = wd.amount_out
            usdt_transfer_fee_krw = wd.fee_krw
            if usdt_at_korean <= 0:
                continue

            # 국내 USDT → KRW 전환
            ksell = korea_sell_leg(usdt_at_korean, korean_taker, korean_btc_price_krw, 'USDT', ctx.usd_krw_rate, note=voucher_note)
            krw_received = ksell.amount_out
            korean_sell_fee_krw = ksell.fee_krw

            total_fee_krw = wallet_network_fee_krw + global_sell_fee_krw + usdt_transfer_fee_krw + korean_sell_fee_krw

            # breakdown: 기존과 동일한 텍스트 유지
            gross_usdt = btc_at_global * ctx.global_btc_price_usd
            paths.append(build_entry(
                route_variant='usdt_via_global',
                korean_exchange=exchange,
                transfer_coin='USDT',
                domestic_withdrawal_network=row.network_label,
                global_exit_mode='onchain',
                global_exit_network='Bitcoin',
                lightning_exit_provider=None,
                krw_received=krw_received,
                total_fee_krw=total_fee_krw,
                breakdown_components=[
                    fee_component('개인지갑 BTC 네트워크 수수료', wallet_network_fee_krw, amount_text=wallet_fee_amount_text, source_url=wallet_fee_estimate['source_url']),
                    fee_component('해외 BTC 매도 수수료', global_sell_fee_krw, rate_pct=ctx.global_taker * 100, amount_text=f'{round(gross_usdt, 8)} USDT'),
                    fee_component('USDT 전송 수수료', usdt_transfer_fee_krw, amount_text=f'{row.fee} USDT', source_url=get_withdrawal_source_url(global_exchange, 'USDT', row.network_label)),
                    fee_component('국내 KRW 전환 수수료', korean_sell_fee_krw, rate_pct=korean_taker * 100, amount_text=f'{round(usdt_at_korean, 8)} USDT', note=voucher_note),
                ],
            ))

    if lightning_swap_rows:
        # sell 모드: 개인 온체인 지갑 → onchain_to_ln 스왑 → Lightning → 거래소 입금
        active_swaps = [
            s for s in lightning_swap_rows
            if s.enabled and s.fee_pct is not None and getattr(s, 'direction', None) == 'onchain_to_ln'
        ]
        for swap in active_swaps:
            for exchange in GROUPS['korea']:
                ticker_row = ctx.ticker_by_exchange.get(exchange)
                if ticker_row is None:
                    continue
                korean_btc_price_krw = float(ticker_row.price)
                korean_taker = (ticker_row.taker_fee_pct / 100) if ticker_row.taker_fee_pct is not None else TRADING_FEES[exchange]['taker']
                voucher_note = coinone_voucher_note(exchange)

                cap = capability_by_exchange.get(exchange)
                korean_has_lightning = cap.supports_lightning_deposit if cap is not None else False
                if not korean_has_lightning:
                    continue

                btc_after_network = amount_btc - wallet_network_fee_btc
                if btc_after_network <= 0:
                    continue

                # onchain_to_ln 스왑 엣지
                sl = swap_leg(swap, btc_after_network, ctx.global_btc_price_usd, ctx.usd_krw_rate)
                if isinstance(sl, Blocked):
                    continue

                btc_at_korean = sl.amount_out
                swap_fee_krw = sl.fee_krw
                if btc_at_korean <= 0:
                    continue

                # ----- 경로 3: lightning_direct (LN → 국내 BTC 매도) -----
                sell = korea_sell_leg(btc_at_korean, korean_taker, korean_btc_price_krw, 'BTC', ctx.usd_krw_rate, note=voucher_note)
                krw_received = sell.amount_out
                korean_sell_fee_krw = sell.fee_krw
                total_fee_krw = wallet_network_fee_krw + swap_fee_krw + korean_sell_fee_krw

                # breakdown amount_text: 기존과 동일
                swap_fee_btc = btc_after_network - btc_at_korean
                paths.append(build_entry(
                    route_variant='lightning_direct',
                    korean_exchange=exchange,
                    transfer_coin='BTC',
                    domestic_withdrawal_network='Lightning Network',
                    global_exit_mode='lightning',
                    global_exit_network='Lightning Network',
                    lightning_exit_provider=swap.service_name,
                    krw_received=krw_received,
                    total_fee_krw=total_fee_krw,
                    breakdown_components=[
                        fee_component('개인지갑 BTC 네트워크 수수료', wallet_network_fee_krw, amount_text=wallet_fee_amount_text, source_url=wallet_fee_estimate['source_url']),
                        fee_component(f'라이트닝 스왑 수수료 ({swap.service_name})', swap_fee_krw, rate_pct=swap.fee_pct, amount_text=f'{round(swap_fee_btc, 8)} BTC'),
                        fee_component('국내 BTC 매도 수수료', korean_sell_fee_krw, rate_pct=korean_taker * 100, amount_text=f'{round(btc_at_korean, 8)} BTC', note=voucher_note),
                    ],
                ))

                # ----- 경로 4: lightning_via_global (LN → 글로벌 BTC 매도 → USDT 출금 → 국내 KRW) -----
                for row in ctx.withdrawals_by_key.get((global_exchange, 'USDT'), []):
                    suspension_reason = is_suspended(ctx.maintenance_status, global_exchange, 'USDT', row.network_label)
                    if suspension_reason:
                        continue

                    btc_at_global = btc_at_korean  # 스왑 후 BTC → 글로벌로 전송
                    if btc_at_global <= 0:
                        continue

                    # 글로벌 BTC 매도 → USDT
                    gsell = global_sell_leg(btc_at_global, ctx.global_taker, ctx.global_btc_price_usd, ctx.usd_krw_rate)
                    usdt_after_global_sell = gsell.amount_out
                    global_sell_fee_krw = gsell.fee_krw

                    # USDT 출금 (withdraw_leg로 min/max 검증 포함)
                    wd = withdraw_leg(
                        row,
                        usdt_after_global_sell,
                        coin='USDT',
                        price_krw=ctx.usd_krw_rate,
                        usd_krw=ctx.usd_krw_rate,
                        source_url=get_withdrawal_source_url(global_exchange, 'USDT', row.network_label),
                        maintenance_status=ctx.maintenance_status,
                        exchange=global_exchange,
                    )
                    if isinstance(wd, Blocked):
                        _add_disabled(korean_exchange=exchange, transfer_coin='USDT', network=row.network_label, reason=wd.reason)
                        continue

                    usdt_at_korean = wd.amount_out
                    usdt_transfer_fee_krw = wd.fee_krw
                    if usdt_at_korean <= 0:
                        continue

                    # 국내 USDT → KRW 전환
                    ksell = korea_sell_leg(usdt_at_korean, korean_taker, korean_btc_price_krw, 'USDT', ctx.usd_krw_rate, note=voucher_note)
                    krw_received_ln = ksell.amount_out
                    korean_sell_fee_krw_ln = ksell.fee_krw

                    total_fee_krw_ln = wallet_network_fee_krw + swap_fee_krw + global_sell_fee_krw + usdt_transfer_fee_krw + korean_sell_fee_krw_ln

                    gross_usdt = btc_at_global * ctx.global_btc_price_usd
                    paths.append(build_entry(
                        route_variant='lightning_via_global',
                        korean_exchange=exchange,
                        transfer_coin='USDT',
                        domestic_withdrawal_network=row.network_label,
                        global_exit_mode='lightning',
                        global_exit_network='Lightning Network',
                        lightning_exit_provider=swap.service_name,
                        krw_received=krw_received_ln,
                        total_fee_krw=total_fee_krw_ln,
                        breakdown_components=[
                            fee_component('개인지갑 BTC 네트워크 수수료', wallet_network_fee_krw, amount_text=wallet_fee_amount_text, source_url=wallet_fee_estimate['source_url']),
                            fee_component(f'라이트닝 스왑 수수료 ({swap.service_name})', swap_fee_krw, rate_pct=swap.fee_pct, amount_text=f'{round(swap_fee_btc, 8)} BTC'),
                            fee_component('해외 BTC 매도 수수료', global_sell_fee_krw, rate_pct=ctx.global_taker * 100, amount_text=f'{round(gross_usdt, 8)} USDT'),
                            fee_component('USDT 전송 수수료', usdt_transfer_fee_krw, amount_text=f'{row.fee} USDT', source_url=get_withdrawal_source_url(global_exchange, 'USDT', row.network_label)),
                            fee_component('국내 KRW 전환 수수료', korean_sell_fee_krw_ln, rate_pct=korean_taker * 100, amount_text=f'{round(usdt_at_korean, 8)} USDT', note=voucher_note),
                        ],
                    ))

    paths.sort(key=lambda item: (-item['krw_received'], item['total_fee_krw']))
    lightning_services = sorted({
        s.service_name for s in (lightning_swap_rows or [])
        if s.enabled and s.fee_pct is not None
        and getattr(s, 'direction', None) == 'onchain_to_ln'
    })
    return {
        'mode': 'sell',
        'amount_btc': amount_btc,
        'wallet_fee_estimate': wallet_fee_estimate,
        'global_exchange': global_exchange,
        'global_btc_price_usd': ctx.global_btc_price_usd,
        'usd_krw_rate': round(ctx.usd_krw_rate),
        'total_paths_evaluated': len(paths),
        'best_path': paths[0] if paths else None,
        'top5': paths[:5],
        'all_paths': paths,
        'disabled_paths': disabled_paths,
        'available_filters': _build_available_filters(paths),
        'maintenance_checked_at': ctx.maintenance_checked_at,
        'data_source': 'latest_snapshot',
        'latest_scraping_time': ctx.last_run['completed_at'],
        'lightning_swap_services': lightning_services,
        'last_run': ctx.last_run,
    }
