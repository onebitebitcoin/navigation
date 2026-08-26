"""공용 엣지 체인 — 경로 앞부분(진입 체인)과 글로벌 온체인 종료 엣지의 단일 구현.

빌더들은 여기서 "재료 목록"(진입 체인 반복자)을 받아 종료 엣지만 조합한다.
- 진입 체인: 국내 매수 → 국내 출금 (→ 글로벌 매수)
- 검증(enabled/min/max/suspension)은 전부 path_graph.withdraw_leg 안에서 일어난다.

같은 체인이 usdt.py / lightning.py, btc_via_global.py / lightning.py에 복제되어
한쪽만 고치는 회귀가 났던 것을 단일화한 것.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from backend.app.domain.market_core import get_withdrawal_source_url
from backend.app.domain.path_graph import (
    Blocked,
    Leg,
    global_buy_leg,
    korea_buy_leg,
    withdraw_leg,
)
from backend.app.domain.path_helpers import (
    exchange_fee_promo_note,
    is_suspended,
    normalize_usdt_network,
)
from backend.app.domain.korea_exchange_registry import get_withdrawal_limits
from backend.app.domain.paths.base import (
    BuilderContext,
    _force_calc_withdraw,
    _get_korean_taker,
)


@dataclass
class Entry:
    """진입 체인 실행 결과 — 국내 출금 행 1개당 1개."""
    row: object                  # 국내 출금 행
    amount_out: float            # 종료 엣지에 넘길 코인 수량
    fee_krw: int                 # 진입 체인 누적 수수료(KRW)
    components: list[dict] = field(default_factory=list)
    is_disabled: bool = False
    disabled_reason: str | None = None
    num_txs: int = 1
    krw_per_tx_limit: int | None = None


def iter_btc_entries(
    bctx: BuilderContext,
    exchange: str,
    *,
    mode: str,                        # 'direct'(개인지갑행) | 'via'(글로벌행)
    disabled_out: list[dict] | None = None,
):
    """KRW → BTC 매수 → 국내 BTC 출금 진입 체인.

    mode='direct': 트래블룰 1회 한도 분할 + 비활성/정지 행 강제계산(disabled 표시).
    mode='via': VASP行 — 분할 없음, 비활성/정지 행은 건너뜀, label/amount_text 보정.
    """
    ctx = bctx.ctx
    ticker_row = ctx.ticker_by_exchange.get(exchange)
    if ticker_row is None:
        return

    korean_btc_price_krw = float(ticker_row.price)
    korean_taker = _get_korean_taker(ticker_row, exchange)

    if mode == 'direct':
        # 1회 KRW 출금 제한(트래블룰) — 초과 시 여러 트랜잭션으로 분할
        limits = get_withdrawal_limits(exchange)
        krw_per_tx = limits.krw_per_tx_limit if limits else None
        num_txs = -(-bctx.amount_krw // krw_per_tx) if (krw_per_tx and krw_per_tx > 0) else 1
        label_override = None
    else:
        krw_per_tx = None
        num_txs = 1
        label_override = '국내 BTC 출금 수수료'

    buy = korea_buy_leg(
        bctx.amount_krw, korean_taker, korean_btc_price_krw, 'BTC', ctx.usd_krw_rate,
        note=exchange_fee_promo_note(exchange),
    )

    for row in ctx.withdrawals_by_key.get((exchange, 'BTC'), []):
        is_disabled = False
        row_disabled_reason = None

        if not row.enabled:
            row_disabled_reason = getattr(row, 'suspension_reason', None) or 'disabled'
            is_disabled = True
        elif row.fee is None:
            continue
        else:
            susp = is_suspended(ctx.maintenance_status, exchange, 'BTC', row.network_label)
            if susp:
                row_disabled_reason = susp
                is_disabled = True

        if is_disabled:
            if mode != 'direct':
                continue  # VASP行은 비활성/정지 행 건너뜀
            if row.fee is None:
                disabled_out.append({
                    'korean_exchange': exchange,
                    'transfer_coin': 'BTC',
                    'network': row.network_label,
                    'reason': row_disabled_reason,
                    'suspension_message': getattr(row, 'suspension_message', None),
                })
                continue

        source_url = get_withdrawal_source_url(exchange, 'BTC', row.network_label)
        if is_disabled:
            wd = _force_calc_withdraw(
                row, buy.amount_out,
                coin='BTC', price_krw=korean_btc_price_krw, usd_krw=ctx.usd_krw_rate,
                num_txs=num_txs, source_url=source_url,
            )
        else:
            wd = withdraw_leg(
                row, buy.amount_out,
                coin='BTC', price_krw=korean_btc_price_krw, usd_krw=ctx.usd_krw_rate,
                num_txs=num_txs, source_url=source_url, label_override=label_override,
            )
        if wd is None or isinstance(wd, Blocked):
            if mode == 'direct':
                disabled_out.append({
                    'korean_exchange': exchange,
                    'transfer_coin': 'BTC',
                    'network': row.network_label,
                    'reason': wd.reason if isinstance(wd, Blocked) else (row_disabled_reason or 'disabled'),
                })
            continue
        if wd.amount_out <= 0:
            continue

        if mode == 'via':
            # label_override 사용 시 amount_text=None이므로 보정
            wd_comp = wd.components[0].copy()
            wd_comp['amount_text'] = f'{row.fee} BTC'
            components = list(buy.components) + [wd_comp]
        else:
            components = list(buy.components) + list(wd.components)

        yield Entry(
            row=row,
            amount_out=wd.amount_out,
            fee_krw=buy.fee_krw + wd.fee_krw,
            components=components,
            is_disabled=is_disabled,
            disabled_reason=row_disabled_reason,
            num_txs=num_txs,
            krw_per_tx_limit=krw_per_tx,
        )


def iter_usdt_entries(
    bctx: BuilderContext,
    exchange: str,
    *,
    include_disabled: bool,
    disabled_out: list[dict] | None = None,
):
    """KRW → USDT 매수 → 국내 USDT 출금 → 글로벌 BTC 매수 진입 체인.

    include_disabled=True: 비활성/정지 행 강제계산 + 불가 사유를 disabled_out에 기록.
    include_disabled=False: 해당 행 건너뜀 (LN 빌더용).
    """
    ctx = bctx.ctx
    global_exchange = bctx.global_exchange
    global_usdt_nets = bctx.global_usdt_nets

    ticker_row = ctx.ticker_by_exchange.get(exchange)
    if ticker_row is None:
        return

    korean_taker = _get_korean_taker(ticker_row, exchange)
    # USDT 매수 수량만 한국 USDT/KRW 실거래가(원달러 프리미엄 발생 지점). 수수료 환산은 포렉스.
    buy = korea_buy_leg(
        bctx.amount_krw, korean_taker, 0.0, 'USDT', ctx.usdt_buy_krw_rate,
        note=exchange_fee_promo_note(exchange),
    )

    for row in ctx.withdrawals_by_key.get((exchange, 'USDT'), []):
        is_disabled = False
        row_disabled_reason = None

        if not row.enabled:
            row_disabled_reason = getattr(row, 'suspension_reason', None) or 'disabled'
            is_disabled = True
        elif row.fee is None:
            continue
        elif global_usdt_nets and normalize_usdt_network(row.network_label) not in global_usdt_nets:
            # 글로벌 거래소 미지원 네트워크는 구조적 불가 — 수수료 계산 없이 기록만
            if include_disabled:
                disabled_out.append({
                    'korean_exchange': exchange,
                    'transfer_coin': 'USDT',
                    'network': row.network_label,
                    'reason': f'{global_exchange} USDT 입금 불가 네트워크',
                })
            continue
        else:
            susp = is_suspended(ctx.maintenance_status, exchange, 'USDT', row.network_label)
            if susp:
                row_disabled_reason = susp
                is_disabled = True

        if is_disabled:
            if not include_disabled:
                continue
            if row.fee is None:
                disabled_out.append({
                    'korean_exchange': exchange,
                    'transfer_coin': 'USDT',
                    'network': row.network_label,
                    'reason': row_disabled_reason,
                    'suspension_message': getattr(row, 'suspension_message', None),
                })
                continue

        source_url = get_withdrawal_source_url(exchange, 'USDT', row.network_label)
        if is_disabled:
            usdt_wd = _force_calc_withdraw(
                row, buy.amount_out,
                coin='USDT', price_krw=ctx.usd_krw_rate, usd_krw=ctx.usd_krw_rate,
                source_url=source_url, label_override='USDT 출금 수수료',
            )
        else:
            usdt_wd = withdraw_leg(
                row, buy.amount_out,
                coin='USDT', price_krw=ctx.usd_krw_rate, usd_krw=ctx.usd_krw_rate,
                source_url=source_url, label_override='USDT 출금 수수료',
            )
        if usdt_wd is None or isinstance(usdt_wd, Blocked):
            if include_disabled:
                disabled_out.append({
                    'korean_exchange': exchange,
                    'transfer_coin': 'USDT',
                    'network': row.network_label,
                    'reason': usdt_wd.reason if isinstance(usdt_wd, Blocked) else (row_disabled_reason or 'disabled'),
                })
            continue
        if usdt_wd.amount_out <= 0:
            continue

        usdt_comp = usdt_wd.components[0].copy()
        usdt_comp['amount_text'] = f'{row.fee:g} USDT'

        # 글로벌 매수 엣지 — 수수료 원화 환산은 포렉스(usd_krw_rate)
        gbuy = global_buy_leg(usdt_wd.amount_out, ctx.global_taker, ctx.global_btc_price_usd, ctx.usd_krw_rate)

        yield Entry(
            row=row,
            amount_out=gbuy.amount_out,
            fee_krw=buy.fee_krw + usdt_wd.fee_krw + gbuy.fee_krw,
            components=list(buy.components) + [usdt_comp] + list(gbuy.components),
            is_disabled=is_disabled,
            disabled_reason=row_disabled_reason,
        )


def global_onchain_exit(bctx: BuilderContext, amount_in: float, *, label: str) -> Leg | Blocked:
    """글로벌 BTC 온체인 출금 종료 엣지 — withdraw_leg 통일 검증 + sats amount_text 보정."""
    ctx = bctx.ctx
    global_wd = withdraw_leg(
        bctx.global_onchain_wd_row, amount_in,
        coin='BTC', price_krw=ctx.global_btc_price_usd * ctx.usd_krw_rate,
        usd_krw=ctx.usd_krw_rate,
        split_on_max=True,
        maintenance_status=ctx.maintenance_status, exchange=bctx.global_exchange,
        label_override=label,
    )
    if isinstance(global_wd, Blocked):
        return global_wd
    # 실제 차감된 총 수수료(BTC)를 sats로 표기 (분할 출금 포함)
    fee_btc = amount_in - global_wd.amount_out
    comp = global_wd.components[0].copy()
    comp['amount_text'] = f'{round(fee_btc * 100_000_000):,} sats'
    return Leg(amount_out=global_wd.amount_out, fee_krw=global_wd.fee_krw, components=[comp])
