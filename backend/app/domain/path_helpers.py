"""Path calculation helper utilities shared across market_paths.py functions."""
from __future__ import annotations

import logging

from backend.app.domain.market_core import fetch_coinone_fee_promo, fetch_korbit_fee_promo

logger = logging.getLogger(__name__)

# 바우처 발급이 전제인 수수료 이벤트의 안내 문구 (프론트 배지로 노출).
COINONE_VOUCHER_NOTE = '바우처 발급 시 무료 (코인원 이벤트, 앱/웹에서 바우처 발급 필요)'
# 코빗은 바우처 없이 전 회원 자동 적용되는 이벤트라 안내 문구도 그에 맞게 다르다.
KORBIT_FEE_PROMO_NOTE = '코빗 거래 수수료 전면 무료 이벤트 적용 중 (바우처 불필요, 전 회원 자동 적용)'


def exchange_fee_promo_note(exchange: str | None) -> str | None:
    """진행 중인 거래소 수수료 이벤트의 안내 문구. 해당 없으면 None.

    수수료율 자체는 이미 크롤 시점에 티커로 반영되므로, 여기서는 이벤트가
    '조건부(바우처 필요)'인지 '자동 적용'인지 등 부가 사실만 사용자에게 알린다.

    fetch_*_fee_promo()는 1시간 TTL **파일** 캐시라 호출마다 디스크를 읽는다.
    경로 후보마다 호출하지 말고 거래소 루프당 1회만 호출해 note를 재사용할 것.
    """
    ex = (exchange or '').lower()
    if ex == 'coinone':
        promo = fetch_coinone_fee_promo()
        if promo and promo.get('requires_voucher'):
            return COINONE_VOUCHER_NOTE
        return None
    if ex == 'korbit':
        promo = fetch_korbit_fee_promo()
        if promo:
            return KORBIT_FEE_PROMO_NOTE
        return None
    return None


def normalize_usdt_network(label: str) -> str:
    """네트워크 레이블을 정규화된 키로 변환 (거래소 간 매칭용).

    예: 'Tron (TRC20)' → 'trc20', 'Ethereum (ERC20)' → 'erc20'
    """
    s = label.lower()
    if 'trc20' in s or 'tron' in s:
        return 'trc20'
    if 'erc20' in s or 'ethereum' in s:
        return 'erc20'
    if 'bep20' in s or 'bsc' in s or 'bnb smart' in s:
        return 'bep20'
    if 'kaia' in s or 'klaytn' in s:
        return 'kaia'
    if 'solana' in s or s == 'sol':
        return 'sol'
    if 'arbitrum' in s or 'arbone' in s:
        return 'arbitrum'
    if 'polygon' in s or 'matic' in s:
        return 'polygon'
    if 'optimism' in s or 'opeth' in s:
        return 'optimism'
    if 'ton' == s or 'the open network' in s:
        return 'ton'
    if 'aptos' in s:
        return 'aptos'
    if 'avax' in s or 'avalanche' in s:
        return 'avax'
    # fallback: strip whitespace/parens
    return s.replace(' ', '').replace('(', '').replace(')', '').replace('-', '')


def build_ticker_by_exchange(ticker_rows: list, korea_exchanges: list) -> dict:
    """한국 거래소별 spot KRW 티커 행을 딕셔너리로 반환."""
    return {
        row.exchange: row
        for row in ticker_rows
        if row.exchange in korea_exchanges and row.market_type == 'spot' and row.currency == 'KRW'
    }


def build_withdrawals_by_key(withdrawal_rows: list) -> dict:
    """(exchange, coin) 키별 출금 행 리스트를 딕셔너리로 반환."""
    result: dict[tuple[str, str], list] = {}
    for row in withdrawal_rows:
        result.setdefault((row.exchange, row.coin), []).append(row)
    return result


def build_maintenance_status(network_rows: list) -> dict:
    """네트워크 행에서 점검/정지 상태 딕셔너리를 빌드."""
    status: dict[str, list[dict]] = {}
    for row in network_rows:
        if row.status == 'ok':
            continue
        status.setdefault(row.exchange, []).append({
            'coin': row.coin or '',
            'network': row.network or '',
            'reason': row.reason or row.status,
        })
    return status


def resolve_global_onchain_wd_fee(
    withdrawals_by_key: dict,
    global_exchange: str,
    global_btc_price_usd: float,
    usd_krw_rate: float,
) -> tuple[float | None, int, str | None, object | None]:
    """글로벌 거래소 BTC 온체인 출금 수수료를 (fee_btc, fee_krw, network_label, row) 형태로 반환.

    row는 min/max/suspension 검증을 위해 withdraw_leg에 그대로 전달할 원본 행.
    찾지 못하면 (None, 0, None, None) 반환.
    """
    for wd in withdrawals_by_key.get((global_exchange, 'BTC'), []):
        label_lower = (wd.network_label or '').lower()
        if wd.enabled and wd.fee is not None and is_bitcoin_native_network(label_lower):
            fee_btc = wd.fee
            fee_krw = int(round(wd.fee_krw)) if wd.fee_krw is not None else round(wd.fee * global_btc_price_usd * usd_krw_rate)
            return fee_btc, fee_krw, wd.network_label, wd
    return None, 0, None, None


def _slug_path_part(value: str | None) -> str:
    raw = (value or 'na').strip().lower()
    parts = []
    prev_dash = False
    for char in raw:
        if char.isalnum():
            parts.append(char)
            prev_dash = False
        elif not prev_dash:
            parts.append('-')
            prev_dash = True
    return ''.join(parts).strip('-') or 'na'


def _build_path_id(
    *,
    global_exchange: str,
    korean_exchange: str,
    transfer_coin: str,
    domestic_withdrawal_network: str | None,
    global_exit_mode: str | None,
    global_exit_network: str | None,
    lightning_exit_provider: str | None,
) -> str:
    return '__'.join([
        _slug_path_part(global_exchange),
        _slug_path_part(korean_exchange),
        _slug_path_part(transfer_coin),
        _slug_path_part(domestic_withdrawal_network),
        _slug_path_part(global_exit_mode),
        _slug_path_part(global_exit_network),
        _slug_path_part(lightning_exit_provider or 'none'),
    ])


def is_bitcoin_native_network(label_lower: str) -> bool:
    """BTC 온체인 네트워크인지 판별. Lightning 및 EVM 체인 제외."""
    is_bitcoin_native = ('bitcoin' in label_lower or 'btc' in label_lower) and 'lightning' not in label_lower
    is_non_btc_chain = any(x in label_lower for x in ('bep20', 'erc20', 'trc20', 'solana', 'aptos', 'sui', 'x layer', 'bnb'))
    return is_bitcoin_native and not is_non_btc_chain


def is_suspended(maintenance_status: dict, exchange: str, coin: str, network_label: str) -> str | None:
    """점검/정지 상태 확인. 정지 중이면 이유 문자열 반환, 아니면 None."""
    for item in maintenance_status.get(exchange, []):
        if item.get('coin', '').upper() == coin.upper() and item.get('network', '').lower() in network_label.lower():
            return item.get('reason', '점검 중')
    return None


def fee_component(
    label: str,
    amount_krw: int,
    *,
    rate_pct: float | None = None,
    amount_text: str | None = None,
    input_krw: int | None = None,
    source_url: str | None = None,
    is_fixed: bool = False,
    move_amount: float | None = None,
    move_coin: str | None = None,
    move_amount_krw: int | None = None,
    network: str | None = None,
    note: str | None = None,
) -> dict:
    """수수료 구성요소 딕셔너리 생성.

    rate_pct 미지정 시 input_krw 기준으로 자동 계산 (각 노드의 실제 통과 금액 대비 비율).
    is_fixed=True: 금액 고정 수수료 (출금 고정비, LN 출금 sats 등)
    is_fixed=False: 비율 수수료 (taker fee, 스왑 % 등)

    move_amount/move_coin/move_amount_krw: 이 단계에서 '이동하는 본체 수량'과 원화 환산값.
    결과 페이지에서 "몇 USDT/BTC를 옮겼고 원화로 얼마치인지"를 단계별로 표시하기 위함.

    note: 이 수수료에 붙는 조건 안내(예: 바우처 발급 시 무료). 결과 페이지에서 배지로 노출.
    """
    if rate_pct is None and input_krw and input_krw > 0:
        rate_pct = amount_krw / input_krw * 100
    return {
        'label': label,
        'amount_krw': amount_krw,
        'rate_pct': round(rate_pct, 4) if rate_pct is not None else None,
        'input_krw': input_krw,
        'amount_text': amount_text,
        'source_url': source_url,
        'is_fixed': is_fixed,
        'move_amount': round(move_amount, 8) if move_amount is not None else None,
        'move_coin': move_coin,
        'move_amount_krw': move_amount_krw,
        'network': network,
        'note': note,
    }
