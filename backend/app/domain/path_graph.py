"""엣지 파이프라인 엔진 — 경로 계산의 공통 엣지 함수.

각 엣지 함수는 성공 시 Leg, 제약 위반 시 Blocked를 반환한다.
모든 출금이 withdraw_leg를 통과하므로 enabled/min/max/suspension 검증이 통일된다.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from types import SimpleNamespace

from backend.app.domain.path_helpers import (
    fee_component,
    is_suspended,
)

logger = logging.getLogger(__name__)


def row_from_dict(d: dict) -> SimpleNamespace:
    """live-fetch 출금 dict를 withdraw_leg가 소비하는 row 객체로 변환.

    live 데이터 키: {'label', 'fee', 'enabled', 'min', 'max', 'fee_krw'?}
    → row 속성: network_label / fee / enabled / fee_krw / min_withdrawal / max_withdrawal

    fee_krw는 live dict에 보통 없으므로 None (withdraw_leg가 price_krw로 환산).
    """
    return SimpleNamespace(
        network_label=d.get('label', ''),
        fee=d.get('fee'),
        enabled=d.get('enabled', True),
        fee_krw=d.get('fee_krw'),
        min_withdrawal=d.get('min'),
        max_withdrawal=d.get('max'),
    )


@dataclass(frozen=True)
class Leg:
    """엣지 성공 결과. 다음 구간으로 전달할 수량과 이 구간 수수료."""
    amount_out: float        # 다음 구간 입력 코인 수량
    fee_krw: int             # 이 구간 수수료(원화)
    components: list[dict]   # fee_component() dict 목록

    def __post_init__(self):
        # frozen dataclass이므로 list는 tuple이 아닌 list로 유지(하지만 내부 요소 불변)
        object.__setattr__(self, 'components', list(self.components))


@dataclass(frozen=True)
class Blocked:
    """엣지 제약 위반 결과."""
    reason: str


def korea_buy_leg(
    amount_krw: int,
    korean_taker: float,
    korean_price: float,
    target_asset: str,
    usd_krw_rate: float,
    note: str | None = None,
) -> Leg:
    """KRW → BTC 또는 USDT 매수 엣지.

    target_asset: 'BTC' 이면 한국 KRW 시세 기준, 'USDT'면 usd_krw_rate 기준.
    note: 매수 수수료에 붙는 조건 안내(예: 코인원 바우처 이벤트). 호출부가 계산해 전달.
    Returns Leg(amount_out=구매한 코인 수량, fee_krw=거래 수수료)
    """
    trading_fee_krw = round(amount_krw * korean_taker)
    after_fee_krw = amount_krw - trading_fee_krw

    if target_asset == 'BTC':
        amount_out = after_fee_krw / korean_price
    else:  # USDT
        amount_out = after_fee_krw / usd_krw_rate

    move_krw = round(amount_out * (korean_price if target_asset == 'BTC' else usd_krw_rate))
    comp = fee_component(
        '국내 매수 수수료',
        trading_fee_krw,
        rate_pct=korean_taker * 100,
        is_fixed=False,
        move_amount=amount_out,
        move_coin=target_asset,
        move_amount_krw=move_krw,
        note=note,
    )
    return Leg(amount_out=amount_out, fee_krw=trading_fee_krw, components=[comp])


def withdraw_leg(
    row,
    amount_coin: float,
    *,
    coin: str,
    price_krw: float,
    usd_krw: float,
    num_txs: int = 1,
    split_on_max: bool = False,
    source_url: str | None = None,
    label_override: str | None = None,
    maintenance_status: dict | None = None,
    exchange: str | None = None,
) -> Leg | Blocked:
    """모든 출금이 통과하는 핵심 엣지.

    검증 순서:
    1. enabled 확인
    2. suspension (is_suspended) 확인
    3. min_withdrawal 확인 (amount_coin < min → Blocked)
    4. max_withdrawal 확인 (amount_coin > max → Blocked)
    5. 통과 시 수수료 차감 후 Leg 반환

    getattr로 min/max를 안전 접근 → 기존 테스트 픽스처 호환.
    """
    if not row.enabled:
        reason = getattr(row, 'suspension_reason', None) or 'disabled'
        return Blocked(reason=reason)

    # suspension 검증 (maintenance 테이블 기반)
    if maintenance_status and exchange:
        susp = is_suspended(maintenance_status, exchange, coin, row.network_label)
        if susp:
            return Blocked(reason=susp)

    if row.fee is None:
        return Blocked(reason='출금 수수료 미수집')

    # min/max 제약 — getattr로 안전 접근 (기존 픽스처에 필드 없을 수 있음)
    min_wd = getattr(row, 'min_withdrawal', None)
    max_wd = getattr(row, 'max_withdrawal', None)

    if min_wd is not None and amount_coin < min_wd:
        return Blocked(reason='출금 최소 한도 미달')
    if max_wd is not None and amount_coin > max_wd:
        if split_on_max:
            # 1회 한도 초과 시 차단 대신 ceil(수량/한도)회로 분할 출금 (수수료 × 횟수)
            num_txs = max(num_txs, math.ceil(amount_coin / max_wd))
        else:
            return Blocked(reason='출금 1회 최대 한도 초과')

    # 수수료 계산
    single_fee = row.fee
    total_fee_coin = single_fee * num_txs

    # KRW 환산
    if row.fee_krw is not None:
        single_fee_krw = int(round(row.fee_krw))
    else:
        single_fee_krw = round(single_fee * price_krw)
    total_fee_krw = single_fee_krw * num_txs

    amount_out = amount_coin - total_fee_coin

    # label 결정
    if label_override:
        wd_label = label_override
        wd_amount_text = None
    elif num_txs > 1:
        wd_label = f'{coin} 출금 수수료 ({num_txs}회 × {single_fee} {coin})'
        wd_amount_text = f'{round(total_fee_coin, 8)} {coin} ({num_txs}회)'
    else:
        wd_label = f'{coin} 출금 수수료'
        wd_amount_text = f'{single_fee} {coin}'

    comp = fee_component(
        wd_label,
        total_fee_krw,
        amount_text=wd_amount_text,
        source_url=source_url,
        is_fixed=True,
        move_amount=amount_out,
        move_coin=coin,
        move_amount_krw=round(amount_out * price_krw),
        network=getattr(row, 'network_label', None),
    )
    return Leg(amount_out=amount_out, fee_krw=total_fee_krw, components=[comp])


def global_buy_leg(
    usdt_in: float,
    global_taker: float,
    btc_usd: float,
    usd_krw: float,
) -> Leg:
    """글로벌 거래소 USDT → BTC 매수 엣지."""
    global_trading_fee_usdt = usdt_in * global_taker
    usdt_for_btc = usdt_in - global_trading_fee_usdt
    btc_out = usdt_for_btc / btc_usd
    fee_krw = round(global_trading_fee_usdt * usd_krw)

    comp = fee_component(
        '해외 BTC 매수 수수료',
        fee_krw,
        rate_pct=global_taker * 100,
        amount_text=f'{round(global_trading_fee_usdt, 8)} USDT',
        is_fixed=False,
        move_amount=btc_out,
        move_coin='BTC',
        move_amount_krw=round(btc_out * btc_usd * usd_krw),
    )
    return Leg(amount_out=btc_out, fee_krw=fee_krw, components=[comp])


def global_buy_maker_leg(
    usdt_in: float,
    maker_fee: float,
    convert_spread: float,
    btc_usd: float,
    usd_krw: float,
) -> Leg:
    """USDT → FDUSD 전환 → BTC/FDUSD 지정가(maker) 매수 엣지 (dynamic 전용).

    USDT를 FDUSD로 전환(스프레드 비용) 후 maker 수수료로 BTC 매수.
    components: [전환 스프레드, maker 매수 수수료] 2개.
    """
    convert_fee_usdt = usdt_in * convert_spread
    fdusd_amount = usdt_in - convert_fee_usdt
    convert_fee_krw = round(convert_fee_usdt * usd_krw)

    maker_fee_fdusd = fdusd_amount * maker_fee
    fdusd_for_btc = fdusd_amount - maker_fee_fdusd
    btc_out = fdusd_for_btc / btc_usd
    maker_fee_krw = round(maker_fee_fdusd * usd_krw)

    fee_krw = convert_fee_krw + maker_fee_krw
    comps = [
        fee_component(
            'USDT→FDUSD 전환 스프레드',
            convert_fee_krw,
            rate_pct=convert_spread * 100,
            amount_text=f'{round(convert_fee_usdt, 6)} USDT',
            is_fixed=False,
        ),
        fee_component(
            f'BTC/FDUSD 매수 수수료 (maker {maker_fee * 100:g}%)',
            maker_fee_krw,
            rate_pct=maker_fee * 100,
            amount_text=f'{round(maker_fee_fdusd, 8)} FDUSD',
            is_fixed=False,
        ),
    ]
    return Leg(amount_out=btc_out, fee_krw=fee_krw, components=comps)


def korea_sell_leg(
    amount_asset: float,
    korean_taker: float,
    korean_price: float,
    source_asset: str,
    usd_krw_rate: float,
    note: str | None = None,
) -> Leg:
    """자산 → KRW 매도 엣지 (매도 방향).

    source_asset: 'BTC'이면 korean_price(원화 시세) 기준, 'USDT'면 usd_krw_rate 기준.
    note: 매도 수수료에 붙는 조건 안내(예: 코인원 바우처 이벤트). 호출부가 계산해 전달.
    Returns Leg(amount_out=수령 KRW, fee_krw=매도 수수료).
    """
    if source_asset == 'BTC':
        gross_krw = amount_asset * korean_price
        label = '국내 BTC 매도 수수료'
    else:  # USDT → KRW 전환
        gross_krw = amount_asset * usd_krw_rate
        label = '국내 KRW 전환 수수료'

    sell_fee_krw = round(gross_krw * korean_taker)
    krw_out = round(gross_krw - sell_fee_krw)

    comp = fee_component(
        label,
        sell_fee_krw,
        rate_pct=korean_taker * 100,
        amount_text=f'{round(amount_asset, 8)} {source_asset}',
        is_fixed=False,
        note=note,
    )
    return Leg(amount_out=krw_out, fee_krw=sell_fee_krw, components=[comp])


def global_sell_leg(
    btc_in: float,
    global_taker: float,
    btc_usd: float,
    usd_krw: float,
) -> Leg:
    """글로벌 거래소 BTC → USDT 매도 엣지 (매도 방향)."""
    gross_usdt = btc_in * btc_usd
    global_sell_fee_usdt = gross_usdt * global_taker
    usdt_out = gross_usdt - global_sell_fee_usdt
    fee_krw = round(global_sell_fee_usdt * usd_krw)

    comp = fee_component(
        '해외 BTC 매도 수수료',
        fee_krw,
        rate_pct=global_taker * 100,
        amount_text=f'{round(gross_usdt, 8)} USDT',
        is_fixed=False,
    )
    return Leg(amount_out=usdt_out, fee_krw=fee_krw, components=[comp])


def swap_leg(
    swap,
    btc_in: float,
    btc_usd: float,
    usd_krw: float,
) -> Leg | Blocked:
    """라이트닝 스왑 엣지 (ln_to_onchain).

    min_amount_sat / max_amount_sat 검증 후 수수료 계산.
    """
    fee_pct = swap.fee_pct / 100
    fee_fixed_btc = (getattr(swap, 'fee_fixed_sat', 0) or 0) / 1e8

    min_btc = (getattr(swap, 'min_amount_sat', 0) or 0) / 1e8
    max_btc_sat = getattr(swap, 'max_amount_sat', None)
    max_btc = max_btc_sat / 1e8 if max_btc_sat is not None else float('inf')

    if btc_in < min_btc:
        return Blocked(reason=f'스왑 최소 금액 미달 ({swap.service_name})')
    if btc_in > max_btc:
        return Blocked(reason=f'스왑 최대 금액 초과 ({swap.service_name})')

    pct_fee_btc = btc_in * fee_pct
    ln_swap_fee_btc = pct_fee_btc + fee_fixed_btc
    btc_out = btc_in - ln_swap_fee_btc
    fee_krw = round(ln_swap_fee_btc * btc_usd * usd_krw)

    pct_fee_krw = round(pct_fee_btc * btc_usd * usd_krw)
    fixed_fee_krw = round(fee_fixed_btc * btc_usd * usd_krw)
    fee_fixed_sat = getattr(swap, 'fee_fixed_sat', 0) or 0

    components = []
    if fee_pct > 0:
        components.append(fee_component(
            f'라이트닝 스왑 수수료 ({swap.service_name})',
            pct_fee_krw,
            rate_pct=swap.fee_pct,
            amount_text=f'{round(pct_fee_btc, 8)} BTC',
            is_fixed=False,
            move_amount=btc_out,
            move_coin='BTC',
            move_amount_krw=round(btc_out * btc_usd * usd_krw),
        ))
    if fee_fixed_sat > 0:
        components.append(fee_component(
            f'라이트닝 스왑 네트워크 수수료 ({swap.service_name})',
            fixed_fee_krw,
            rate_pct=None,
            amount_text=f'{fee_fixed_sat:,} sats',
            is_fixed=True,
            move_amount=btc_out,
            move_coin='BTC',
            move_amount_krw=round(btc_out * btc_usd * usd_krw),
        ))
    if not components:
        components.append(fee_component(
            f'라이트닝 스왑 수수료 ({swap.service_name})',
            0,
            rate_pct=0,
            amount_text='0 BTC',
            is_fixed=False,
            move_amount=btc_out,
            move_coin='BTC',
            move_amount_krw=round(btc_out * btc_usd * usd_krw),
        ))
    return Leg(amount_out=btc_out, fee_krw=fee_krw, components=components)
