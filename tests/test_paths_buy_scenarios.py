"""buy 경로 전 시나리오 결정론 테스트.

라이브 API/DB에 의존하지 않고 합성 스냅샷 행을 직접 주입해
5종 경로(BTC 직접 / BTC 경유 온체인 / BTC 경유 LN / USDT 온체인 / USDT LN)와
트래블룰 분할 경계, 불변식을 고정한다.

advisor 가이드: bare MagicMock 금지 (truthy Mock 이 산술/`== 'ln_to_onchain'`을 깨뜨림).
SimpleNamespace + 실수치 + 명시적 direction 사용.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.app.domain.paths_buy import find_cheapest_path_from_snapshot_rows

USD_KRW = 1400.0
BTC_USD = 100_000.0
KRW_PER_BTC = BTC_USD * USD_KRW  # 140,000,000


def _run():
    return SimpleNamespace(id=1, status="success", usd_krw_rate=USD_KRW, completed_at=None)


def _ticker(exchange, price, *, market_type="spot", currency="KRW", taker_fee_pct=0.25):
    return SimpleNamespace(
        exchange=exchange,
        market_type=market_type,
        currency=currency,
        price=price,
        taker_fee_pct=taker_fee_pct,
        usd_krw_rate=None,
    )


def _wd(exchange, coin, network_label, fee, *, enabled=True, fee_krw=None,
        min_withdrawal=None, max_withdrawal=None):
    return SimpleNamespace(
        exchange=exchange,
        coin=coin,
        network_label=network_label,
        fee=fee,
        fee_krw=fee_krw,
        enabled=enabled,
        source="api",
        min_withdrawal=min_withdrawal,
        max_withdrawal=max_withdrawal,
    )


def _swap(service_name, *, fee_pct=0.45, direction="ln_to_onchain", enabled=True,
          fee_fixed_sat=0, min_amount_sat=0, max_amount_sat=100_000_000):
    return SimpleNamespace(
        service_name=service_name,
        direction=direction,
        enabled=enabled,
        fee_pct=fee_pct,
        fee_fixed_sat=fee_fixed_sat,
        min_amount_sat=min_amount_sat,
        max_amount_sat=max_amount_sat,
    )


def _tickers():
    # 국내: bithumb (트래블룰 1M 한도), upbit (1M 한도) — 둘 다 KRW spot
    # 글로벌: binance spot (USD)
    return [
        _ticker("bithumb", KRW_PER_BTC),
        _ticker("upbit", KRW_PER_BTC),
        _ticker("binance", BTC_USD, currency="USD", taker_fee_pct=0.1),
    ]


def _withdrawals(*, global_exchange="binance", okx_btc_onchain=False):
    rows = [
        # 국내 BTC 출금
        _wd("bithumb", "BTC", "Bitcoin", 0.0002, fee_krw=28_000),
        _wd("upbit", "BTC", "Bitcoin", 0.0009, fee_krw=126_000),
        # 국내 USDT 출금 (TRC20)
        _wd("bithumb", "USDT", "Tron (TRC20)", 1.0, fee_krw=1_400),
        _wd("upbit", "USDT", "Tron (TRC20)", 1.0, fee_krw=1_400),
        # 글로벌 BTC 출금 (온체인 + 라이트닝)
        _wd(global_exchange, "BTC", "Bitcoin", 0.00002, fee_krw=2_800),
        _wd(global_exchange, "BTC", "Lightning Network", 0.000001, fee_krw=140),
        # 글로벌 USDT 입금 가능 네트워크 (TRC20)
        _wd(global_exchange, "USDT", "Tron (TRC20)", 1.0, fee_krw=1_400),
    ]
    return rows


def _calc(amount_krw, *, global_exchange="binance", swaps=None):
    return find_cheapest_path_from_snapshot_rows(
        amount_krw=amount_krw,
        global_exchange=global_exchange,
        latest_run=_run(),
        ticker_rows=_tickers(),
        withdrawal_rows=_withdrawals(global_exchange=global_exchange),
        network_rows=[],
        lightning_swap_rows=swaps if swaps is not None else [_swap("BitFreezer")],
    )


def _classify(p):
    if p.get("path_type") == "lightning_exit":
        return f"LN/{p['transfer_coin']}/{p.get('route_variant', '-')}"
    return f"ON/{p['transfer_coin']}/{p.get('route_variant', '-')}"


# ── 경로 종류 커버리지 ────────────────────────────────────────────────────────

def test_all_five_path_types_present():
    result = _calc(10_000_000)
    kinds = {_classify(p) for p in result["all_paths"]}
    assert "ON/BTC/btc_direct" in kinds
    assert "ON/BTC/btc_via_global" in kinds
    assert "LN/BTC/btc_via_global" in kinds
    assert "ON/USDT/-" in kinds
    assert "LN/USDT/-" in kinds


# ── 불변식: 모든 경로 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("amount", [1_000_000, 1_000_001, 5_000_000, 10_000_000, 50_000_000])
def test_invariants_hold_for_all_paths(amount):
    result = _calc(amount)
    assert result["all_paths"], "최소 1개 경로는 생성되어야 한다"
    for p in result["all_paths"]:
        tag = f"{p['korean_exchange']}/{_classify(p)}"
        comp_sum = sum(c["amount_krw"] for c in p["breakdown"]["components"])
        assert comp_sum == p["total_fee_krw"], f"{tag}: 구성요소 합 != total_fee_krw"
        assert p["btc_received"] > 0, f"{tag}: btc_received<=0"
        assert abs(round(p["total_fee_krw"] / amount * 100, 4) - p["fee_pct"]) <= 0.0002, f"{tag}: fee_pct 불일치"
        if p.get("path_type") == "lightning_exit":
            assert p.get("lightning_exit_provider"), f"{tag}: LN provider 누락"


def test_paths_sorted_by_total_fee():
    result = _calc(10_000_000)
    fees = [p["total_fee_krw"] for p in result["all_paths"]]
    assert fees == sorted(fees), "경로는 total_fee_krw 오름차순 정렬되어야 한다"


# ── USDT 매수 환율 주입 (테더/원달러 환율 차이 아티팩트 제거) ──────────────────

def _usdt_path(result):
    return next(p for p in result["all_paths"] if p["transfer_coin"] == "USDT")


def test_components_expose_move_amount_for_all_paths():
    """각 경로 component에 이동 수량(move_amount)+코인+원화 환산이 채워진다 (결과 페이지 표시용)."""
    result = _calc(10_000_000)
    for p in result["all_paths"]:
        tag = f"{p['korean_exchange']}/{_classify(p)}"
        moved = [c for c in p["breakdown"]["components"] if c.get("move_amount")]
        assert moved, f"{tag}: 이동 수량 노출 component가 없음"
        for c in moved:
            assert c["move_coin"] in ("USDT", "BTC"), f"{tag}: move_coin={c['move_coin']}"
            assert c["move_amount"] > 0, f"{tag}: move_amount<=0"
            assert c["move_amount_krw"] and c["move_amount_krw"] > 0, f"{tag}: move_amount_krw 누락"


def test_paths_expose_discarded_krw():
    """각 경로에 최소주문 잔돈(discarded_krw)이 노출되고, 업비트는 5,000원 단위 나머지와 일치한다."""
    result = _calc(10_000_001)  # 5,000으로 나눠떨어지지 않음
    for p in result["all_paths"]:
        assert "discarded_krw" in p, "discarded_krw 필드 누락"
        assert p["discarded_krw"] >= 0
    upbit_paths = [p for p in result["all_paths"] if p["korean_exchange"] == "upbit"]
    for p in upbit_paths:
        assert p["discarded_krw"] == 10_000_001 % 5000  # == 1


def test_usdt_purchase_uses_injected_rate_not_forex():
    """usdt_krw_rate < 포렉스이면 USDT를 더 싸게 사 btc_received가 증가한다.

    backend가 포렉스(1400) 대신 주입된 한국 USDT/KRW(1300)로 매수 계산하므로
    같은 KRW로 더 많은 USDT→BTC를 받는다.
    """
    base = find_cheapest_path_from_snapshot_rows(
        amount_krw=10_000_000, global_exchange="binance", latest_run=_run(),
        ticker_rows=_tickers(), withdrawal_rows=_withdrawals(),
        network_rows=[], lightning_swap_rows=[_swap("BitFreezer")],
    )
    cheaper_usdt = find_cheapest_path_from_snapshot_rows(
        amount_krw=10_000_000, global_exchange="binance", latest_run=_run(),
        ticker_rows=_tickers(), withdrawal_rows=_withdrawals(),
        network_rows=[], lightning_swap_rows=[_swap("BitFreezer")],
        usdt_krw_rate=1300.0,
    )
    assert _usdt_path(cheaper_usdt)["btc_received"] > _usdt_path(base)["btc_received"]
    # 응답에 매수 환율을 실어 프론트가 동일 환율로 평가(잔차 0)하도록 한다
    assert cheaper_usdt["usdt_buy_krw_rate"] == 1300.0
    assert base["usdt_buy_krw_rate"] == USD_KRW  # 미주입 시 포렉스 폴백


def test_btc_direct_path_unaffected_by_usdt_rate():
    """BTC 직접 경로(USDT 미사용)는 usdt_krw_rate 주입에 영향받지 않는다."""
    base = find_cheapest_path_from_snapshot_rows(
        amount_krw=10_000_000, global_exchange="binance", latest_run=_run(),
        ticker_rows=_tickers(), withdrawal_rows=_withdrawals(),
        network_rows=[], lightning_swap_rows=[_swap("BitFreezer")],
    )
    injected = find_cheapest_path_from_snapshot_rows(
        amount_krw=10_000_000, global_exchange="binance", latest_run=_run(),
        ticker_rows=_tickers(), withdrawal_rows=_withdrawals(),
        network_rows=[], lightning_swap_rows=[_swap("BitFreezer")],
        usdt_krw_rate=1300.0,
    )

    def _btc_direct(result):
        return next(
            p for p in result["all_paths"]
            if p["transfer_coin"] == "BTC" and p.get("route_variant") == "btc_direct"
            and p["korean_exchange"] == "bithumb"
        )

    assert _btc_direct(injected)["btc_received"] == _btc_direct(base)["btc_received"]


# ── 트래블룰 분할 경계 (개인지갑 직접출금만 분할) ───────────────────────────────

@pytest.mark.parametrize("amount,expected_txs", [
    (1_000_000, 1),       # 한도와 동일 → 1회
    (1_000_001, 2),       # 1원 초과 → 2회
    (2_000_000, 2),       # 정확히 2배 → 2회
    (10_000_000, 10),     # 10배 → 10회
])
def test_btc_direct_travel_rule_split(amount, expected_txs):
    result = _calc(amount)
    direct = next(
        p for p in result["all_paths"]
        if p["korean_exchange"] == "bithumb"
        and p["transfer_coin"] == "BTC" and p.get("route_variant") == "btc_direct"
    )
    assert direct["num_withdrawal_txs"] == expected_txs


@pytest.mark.parametrize("amount", [1_000_001, 10_000_000])
def test_btc_via_global_not_split_by_travel_rule(amount):
    """VASP(글로벌 거래소)行 출금은 트래블룰 분할 대상이 아님 → 항상 1회."""
    result = _calc(amount)
    via = next(
        p for p in result["all_paths"]
        if p["korean_exchange"] == "bithumb"
        and p.get("route_variant") == "btc_via_global" and p.get("path_type") != "lightning_exit"
    )
    assert via["num_withdrawal_txs"] == 1


def test_travel_rule_split_makes_direct_costlier_at_scale():
    """동일 금액에서 분할(10회) 직접출금의 국내 출금비가 경유(1회)보다 크다."""
    result = _calc(10_000_000)
    def dom_wd(p):
        return next(c["amount_krw"] for c in p["breakdown"]["components"] if "출금" in c["label"] and "해외" not in c["label"] and "라이트닝" not in c["label"])
    direct = next(p for p in result["all_paths"] if p["korean_exchange"] == "bithumb" and p.get("route_variant") == "btc_direct")
    via = next(p for p in result["all_paths"] if p["korean_exchange"] == "bithumb" and p.get("route_variant") == "btc_via_global" and p.get("path_type") != "lightning_exit")
    assert dom_wd(direct) == dom_wd(via) * 10


# ── 라이트닝 / 데이터 갭 ─────────────────────────────────────────────────────

def test_lightning_disabled_when_global_has_no_lightning_fee():
    """글로벌 LN 출금 수수료가 None(okx식 데이터 갭)이면 LN 경로 미생성."""
    rows = _withdrawals()
    # 라이트닝 행 fee 를 None 으로 (enabled 이지만 미수집)
    for r in rows:
        if r.network_label == "Lightning Network":
            r.fee = None
    result = find_cheapest_path_from_snapshot_rows(
        amount_krw=10_000_000, global_exchange="binance", latest_run=_run(),
        ticker_rows=_tickers(), withdrawal_rows=rows, network_rows=[],
        lightning_swap_rows=[_swap("BitFreezer")],
    )
    ln = [p for p in result["all_paths"] if p.get("path_type") == "lightning_exit"]
    assert ln == [], "LN 출금 수수료 미수집 시 라이트닝 경로는 생성되지 않아야 한다"
    # 온체인 경로는 여전히 존재
    assert any(p.get("path_type") != "lightning_exit" for p in result["all_paths"])


def test_okx_btc_via_global_onchain_includes_withdrawal_fee():
    """okx 온체인 출금 수수료가 경로 비용에 포함된다."""
    result = find_cheapest_path_from_snapshot_rows(
        amount_krw=10_000_000, global_exchange="okx", latest_run=_run(),
        ticker_rows=[_ticker("bithumb", KRW_PER_BTC), _ticker("okx", BTC_USD, currency="USD", taker_fee_pct=0.1)],
        withdrawal_rows=_withdrawals(global_exchange="okx"),
        network_rows=[], lightning_swap_rows=[_swap("BitFreezer")],
    )
    via_onchain = [
        p for p in result["all_paths"]
        if p.get("route_variant") == "btc_via_global" and p.get("path_type") != "lightning_exit"
    ]
    assert len(via_onchain) > 0, "OKX 온체인 경로가 생성되어야 한다"
    for p in via_onchain:
        labels = [c["label"] for c in p["breakdown"]["components"]]
        assert any("출금" in label for label in labels), "OKX BTC 출금 수수료가 breakdown에 포함되어야 한다"


def test_lightning_swap_services_listed():
    result = _calc(10_000_000, swaps=[_swap("BitFreezer"), _swap("Coinos", fee_pct=0.4)])
    assert set(result["lightning_swap_services"]) == {"BitFreezer", "Coinos"}


def test_available_filters_include_lightning_exit():
    result = _calc(10_000_000)
    opts = result["available_filters"]["global_exit_options"]
    modes = {o["mode"] for o in opts}
    assert "lightning" in modes
    assert "onchain" in modes


# ── 직접 LN 출금 경로 (__direct__ = 라이트닝 지갑 종착) ──────────────────────

def test_direct_ln_paths_present_when_global_ln_withdrawal_available():
    """글로벌 거래소 LN 출금 수수료가 있으면 __direct__ 경로가 생성된다."""
    result = _calc(10_000_000)
    direct_ln = [
        p for p in result["all_paths"]
        if p.get("path_type") == "lightning_exit"
        and p.get("lightning_exit_provider") == "__direct__"
    ]
    assert direct_ln, "__direct__ 라이트닝 경로가 1개 이상 생성되어야 한다"


def test_direct_ln_destination_is_lightning_wallet():
    """__direct__(LN 직접출금) 경로의 종착지는 lightning_wallet이다."""
    result = _calc(10_000_000)
    for p in result["all_paths"]:
        if p.get("lightning_exit_provider") == "__direct__":
            assert p.get("destination") == "lightning_wallet", \
                f"__direct__ 경로 destination이 lightning_wallet이어야 함: {p.get('destination')}"


def test_non_direct_paths_are_personal_destination():
    """__direct__가 아닌 모든 경로(온체인/스왑 경유)의 종착지는 personal이다."""
    result = _calc(10_000_000)
    for p in result["all_paths"]:
        if p.get("lightning_exit_provider") != "__direct__":
            assert p.get("destination") == "personal", \
                f"{p.get('path_id')}: 비-__direct__ 경로는 personal이어야 함: {p.get('destination')}"


def test_direct_ln_has_zero_swap_fee():
    """__direct__ 경로는 스왑 수수료가 0 이어야 한다."""
    result = _calc(10_000_000)
    for p in result["all_paths"]:
        if p.get("lightning_exit_provider") == "__direct__":
            swap_components = [
                c for c in p["breakdown"]["components"]
                if "스왑" in c["label"]
            ]
            assert swap_components == [], f"__direct__ 경로에 스왑 수수료 항목이 있으면 안 됨: {swap_components}"


def test_direct_ln_breakdown_sum_matches_total():
    """__direct__ 경로 breakdown 합계가 total_fee_krw와 일치한다."""
    result = _calc(10_000_000)
    for p in result["all_paths"]:
        if p.get("lightning_exit_provider") == "__direct__":
            comp_sum = sum(c["amount_krw"] for c in p["breakdown"]["components"])
            assert comp_sum == p["total_fee_krw"], \
                f"__direct__ breakdown 합({comp_sum}) != total_fee_krw({p['total_fee_krw']})"


def test_direct_ln_cheaper_than_swap_paths():
    """동일 국내거래소/코인 조건에서 __direct__는 스왑 경로보다 수수료가 낮아야 한다."""
    result = _calc(10_000_000, swaps=[_swap("BitFreezer", fee_pct=0.5)])
    for exchange in ("bithumb",):
        for coin in ("BTC", "USDT"):
            direct = [
                p for p in result["all_paths"]
                if p["korean_exchange"] == exchange
                and p.get("transfer_coin") == coin
                and p.get("lightning_exit_provider") == "__direct__"
            ]
            swap = [
                p for p in result["all_paths"]
                if p["korean_exchange"] == exchange
                and p.get("transfer_coin") == coin
                and p.get("path_type") == "lightning_exit"
                and p.get("lightning_exit_provider") != "__direct__"
            ]
            if direct and swap:
                min_direct = min(p["total_fee_krw"] for p in direct)
                min_swap = min(p["total_fee_krw"] for p in swap)
                assert min_direct < min_swap, \
                    f"{exchange}/{coin}: __direct__({min_direct}) >= swap({min_swap})"


def test_direct_ln_absent_when_no_global_ln_withdrawal():
    """글로벌 LN 출금 수수료가 없으면 __direct__ 경로도 생성되지 않는다."""
    rows = _withdrawals()
    for r in rows:
        if r.network_label == "Lightning Network":
            r.fee = None
    result = find_cheapest_path_from_snapshot_rows(
        amount_krw=10_000_000, global_exchange="binance", latest_run=_run(),
        ticker_rows=_tickers(), withdrawal_rows=rows, network_rows=[],
        lightning_swap_rows=[_swap("BitFreezer")],
    )
    direct_ln = [
        p for p in result["all_paths"]
        if p.get("lightning_exit_provider") == "__direct__"
    ]
    assert direct_ln == [], "LN 출금 수수료 미수집 시 __direct__ 경로 미생성"


# ── max_withdrawal 제약 회귀 시나리오 ────────────────────────────────────────

def _withdrawals_with_ln_max(max_withdrawal_btc: float | None):
    """글로벌 LN 출금 행에 max_withdrawal을 주입한 출금 행 목록."""
    rows = [
        _wd("bithumb", "BTC", "Bitcoin", 0.0002, fee_krw=28_000),
        _wd("upbit", "BTC", "Bitcoin", 0.0009, fee_krw=126_000),
        _wd("bithumb", "USDT", "Tron (TRC20)", 1.0, fee_krw=1_400),
        _wd("upbit", "USDT", "Tron (TRC20)", 1.0, fee_krw=1_400),
        _wd("binance", "BTC", "Bitcoin", 0.00002, fee_krw=2_800),
        # 글로벌 LN 행 — max_withdrawal 주입
        _wd("binance", "BTC", "Lightning Network", 0.000001, fee_krw=140,
            max_withdrawal=max_withdrawal_btc),
        _wd("binance", "USDT", "Tron (TRC20)", 1.0, fee_krw=1_400),
    ]
    return rows


def test_ln_path_present_for_small_amount_within_max():
    """소액(100만원 ≈ 0.0071 BTC)은 LN max=0.01 BTC 내 → LN 경로 생성.

    Arrange: 글로벌 LN 출금 max_withdrawal=0.01 BTC
    Act: 100만원 계산
    Assert: lightning_exit 경로 ≥ 1개
    """
    # Arrange
    rows = _withdrawals_with_ln_max(max_withdrawal_btc=0.01)

    # Act
    result = find_cheapest_path_from_snapshot_rows(
        amount_krw=1_000_000,
        global_exchange="binance",
        latest_run=_run(),
        ticker_rows=_tickers(),
        withdrawal_rows=rows,
        network_rows=[],
        lightning_swap_rows=[_swap("BitFreezer")],
    )

    # Assert
    ln_paths = [p for p in result["all_paths"] if p.get("path_type") == "lightning_exit"]
    assert ln_paths, "소액(100만원)은 LN max 한도 내 → LN 경로가 1개 이상 생성되어야 한다"


def test_ln_path_split_for_large_amount_exceeding_max():
    """거액(2억원 ≈ 1.43 BTC)은 LN max=0.01 BTC 초과 → 차단 대신 분할 출금 경로 생성.

    Arrange: 글로벌 LN 출금 max_withdrawal=0.01 BTC, 단일 LN 수수료 fee_krw=140
    Act: 2억원 계산
    Assert:
        - lightning_exit 경로 ≥ 1개 (차단되지 않음)
        - 분할 경로의 num_withdrawal_txs > 1
        - 분할 경로의 LN 출금 수수료 = 단일 수수료(140) × num_withdrawal_txs
        - disabled_paths에 '최대 한도 초과' 사유 없음 (분할로 해소)
    """
    # Arrange
    rows = _withdrawals_with_ln_max(max_withdrawal_btc=0.01)

    # Act
    result = find_cheapest_path_from_snapshot_rows(
        amount_krw=200_000_000,
        global_exchange="binance",
        latest_run=_run(),
        ticker_rows=_tickers(),
        withdrawal_rows=rows,
        network_rows=[],
        lightning_swap_rows=[_swap("BitFreezer")],
    )

    # Assert — LN 경로 생성됨
    ln_paths = [p for p in result["all_paths"] if p.get("path_type") == "lightning_exit"]
    assert ln_paths, "2억원도 분할 출금으로 LN 경로가 생성되어야 한다."

    # Assert — 분할(num_withdrawal_txs > 1) 경로 존재 + 수수료 × 횟수
    split_paths = [p for p in ln_paths if (p.get("num_withdrawal_txs") or 1) > 1]
    assert split_paths, (
        "LN max 초과 시 num_withdrawal_txs > 1 분할 경로가 있어야 한다. "
        f"num_txs들: {[p.get('num_withdrawal_txs') for p in ln_paths]}"
    )
    for p in split_paths:
        n = p["num_withdrawal_txs"]
        assert p["global_withdrawal_fee_krw"] == 140 * n, (
            f"LN 출금 수수료는 단일(140) × {n}회 = {140 * n} 이어야 한다. "
            f"실제: {p['global_withdrawal_fee_krw']}"
        )

    # Assert — '최대 한도 초과' 사유는 더 이상 없음 (분할로 해소)
    over_max = [d for d in result["disabled_paths"] if "최대 한도" in d.get("reason", "")]
    assert not over_max, f"분할 출금으로 최대 한도 초과 차단이 없어야 한다. 실제: {over_max}"


def test_ln_path_blocked_for_below_min_withdrawal():
    """극소액이 LN min_withdrawal 미달 시 LN 경로 미생성 + disabled_paths 사유 기록.

    Arrange: 글로벌 LN 출금 min_withdrawal=0.5 BTC (매우 높게 설정)
    Act: 1000만원 계산 (≈ 0.071 BTC — min 미달)
    Assert:
        - lightning_exit 경로 = 0개
        - disabled_paths에 '최소 한도' 관련 사유 ≥ 1개
    """
    # Arrange — min_withdrawal을 0.5 BTC로 설정해 모든 금액이 미달
    rows = [
        _wd("bithumb", "BTC", "Bitcoin", 0.0002, fee_krw=28_000),
        _wd("upbit", "BTC", "Bitcoin", 0.0009, fee_krw=126_000),
        _wd("bithumb", "USDT", "Tron (TRC20)", 1.0, fee_krw=1_400),
        _wd("upbit", "USDT", "Tron (TRC20)", 1.0, fee_krw=1_400),
        _wd("binance", "BTC", "Bitcoin", 0.00002, fee_krw=2_800),
        # 글로벌 LN 행 — min_withdrawal=0.5 BTC (실질적으로 모든 금액이 미달)
        _wd("binance", "BTC", "Lightning Network", 0.000001, fee_krw=140,
            min_withdrawal=0.5),
        _wd("binance", "USDT", "Tron (TRC20)", 1.0, fee_krw=1_400),
    ]

    # Act
    result = find_cheapest_path_from_snapshot_rows(
        amount_krw=10_000_000,
        global_exchange="binance",
        latest_run=_run(),
        ticker_rows=_tickers(),
        withdrawal_rows=rows,
        network_rows=[],
        lightning_swap_rows=[_swap("BitFreezer")],
    )

    # Assert — LN 경로 없음
    ln_paths = [p for p in result["all_paths"] if p.get("path_type") == "lightning_exit"]
    assert ln_paths == [], f"min_withdrawal 미달 → LN 경로 0개여야 한다. 실제: {len(ln_paths)}개"

    # Assert — disabled_paths에 최소 한도 사유 ≥ 1개
    ln_min_disabled = [
        d for d in result["disabled_paths"]
        if "최소 한도" in d.get("reason", "")
    ]
    assert ln_min_disabled, (
        "LN min_withdrawal 미달로 Blocked된 사유가 disabled_paths에 기록되어야 한다. "
        f"disabled_paths: {result['disabled_paths']}"
    )


# ── 글로벌 온체인 출금 검증 통일 (withdraw_leg invariant) ─────────────────────

def _network_row(exchange, coin, network, status="suspended", reason="점검 중"):
    return SimpleNamespace(exchange=exchange, coin=coin, network=network,
                           status=status, reason=reason)


def test_usdt_path_dropped_when_global_onchain_missing():
    """글로벌 BTC 온체인 행이 없으면 USDT 온체인 경로를 만들지 않는다 (수수료 누락 경로 금지)."""
    rows = [r for r in _withdrawals() if r.network_label != "Bitcoin" or r.exchange != "binance"]
    result = find_cheapest_path_from_snapshot_rows(
        amount_krw=10_000_000, global_exchange="binance", latest_run=_run(),
        ticker_rows=_tickers(), withdrawal_rows=rows, network_rows=[],
        lightning_swap_rows=[_swap("BitFreezer")],
    )
    usdt_onchain = [
        p for p in result["all_paths"]
        if p["transfer_coin"] == "USDT" and p.get("path_type") != "lightning_exit"
    ]
    assert usdt_onchain == [], "글로벌 온체인 출금 수수료 미상 시 USDT 온체인 경로 생성 금지"


def test_global_onchain_suspension_blocks_via_paths():
    """글로벌 BTC 온체인 정지 시 경유(via_global)/USDT 온체인 경로가 차단된다."""
    result = find_cheapest_path_from_snapshot_rows(
        amount_krw=10_000_000, global_exchange="binance", latest_run=_run(),
        ticker_rows=_tickers(), withdrawal_rows=_withdrawals(),
        network_rows=[_network_row("binance", "BTC", "Bitcoin")],
        lightning_swap_rows=[_swap("BitFreezer")],
    )
    onchain_via = [
        p for p in result["all_paths"]
        if p.get("path_type") != "lightning_exit"
        and (p.get("route_variant") == "btc_via_global" or p["transfer_coin"] == "USDT")
    ]
    assert onchain_via == [], "글로벌 온체인 정지 중엔 경유 온체인 경로 생성 금지"
    # 직접출금 경로는 영향 없음
    assert any(p.get("route_variant") == "btc_direct" for p in result["all_paths"])
    # 차단 사유 기록
    assert any(d.get("reason") == "점검 중" for d in result["disabled_paths"])


def test_global_onchain_min_withdrawal_blocks_small_amounts():
    """글로벌 BTC 온체인 min_withdrawal 미달 시 경유/USDT 경로 차단 + 사유 기록."""
    rows = _withdrawals()
    for r in rows:
        if r.exchange == "binance" and r.network_label == "Bitcoin":
            r.min_withdrawal = 0.5  # 10M KRW ≈ 0.07 BTC → 미달
    result = find_cheapest_path_from_snapshot_rows(
        amount_krw=10_000_000, global_exchange="binance", latest_run=_run(),
        ticker_rows=_tickers(), withdrawal_rows=rows, network_rows=[],
        lightning_swap_rows=[_swap("BitFreezer")],
    )
    onchain_via = [
        p for p in result["all_paths"]
        if p.get("path_type") != "lightning_exit"
        and (p.get("route_variant") == "btc_via_global" or p["transfer_coin"] == "USDT")
    ]
    assert onchain_via == []
    assert any("최소 한도" in d.get("reason", "") for d in result["disabled_paths"])


def test_best_path_and_top5_exclude_disabled():
    """강제계산된 disabled 경로는 all_paths엔 남지만 best_path/top5에선 제외."""
    rows = _withdrawals()
    for r in rows:
        if r.exchange in ("bithumb", "upbit") and r.coin == "BTC":
            r.enabled = False  # 국내 BTC 출금 전부 정지 → disabled 강제계산 경로 발생
    result = find_cheapest_path_from_snapshot_rows(
        amount_krw=10_000_000, global_exchange="binance", latest_run=_run(),
        ticker_rows=_tickers(), withdrawal_rows=rows, network_rows=[],
        lightning_swap_rows=[_swap("BitFreezer")],
    )
    assert any(p.get("disabled") for p in result["all_paths"]), "disabled 경로가 all_paths에 존재해야 함"
    assert result["best_path"] is not None
    assert not result["best_path"].get("disabled")
    for p in result["top5"]:
        assert not p.get("disabled")


# ── 코인원 바우처 이벤트 배지 (note) ──────────────────────────────────────────

def test_coinone_buy_fee_carries_voucher_note():
    """코인원 바우처 이벤트 진행 중이면 국내 매수 수수료 항목에 note가 실린다.

    note는 프론트 결과 페이지에서 배지로 노출되므로, 실제 경로 계산 결과의
    breakdown까지 값이 전달되는지를 엔드투엔드로 확인한다.
    """
    # Arrange — 코인원(이벤트로 taker 0%)과 업비트(평시)를 함께 넣어 대조
    tickers = [
        _ticker("coinone", KRW_PER_BTC, taker_fee_pct=0.0),
        _ticker("upbit", KRW_PER_BTC),
        _ticker("binance", BTC_USD, currency="USD", taker_fee_pct=0.1),
    ]
    withdrawals = [
        _wd("coinone", "BTC", "Bitcoin", 0.0009, fee_krw=126_000),
        _wd("upbit", "BTC", "Bitcoin", 0.0009, fee_krw=126_000),
        _wd("coinone", "USDT", "Tron (TRC20)", 1.0, fee_krw=1_400),
        _wd("upbit", "USDT", "Tron (TRC20)", 1.0, fee_krw=1_400),
        _wd("binance", "BTC", "Bitcoin", 0.00002, fee_krw=2_800),
        _wd("binance", "USDT", "Tron (TRC20)", 1.0, fee_krw=1_400),
    ]
    promo = {"taker_fee_pct": 0.0, "requires_voucher": True}

    # Act — 업비트는 이 테스트의 관심사가 아니므로 실제 USDT 이벤트 상태와 무관하게
    # None으로 고정해 격리한다 (코인원 note 전파만 검증하는 테스트).
    with patch("backend.app.domain.path_helpers.fetch_coinone_fee_promo", return_value=promo), \
         patch("backend.app.domain.path_helpers.fetch_upbit_usdt_fee_promo", return_value=None):
        result = find_cheapest_path_from_snapshot_rows(
            amount_krw=10_000_000,
            global_exchange="binance",
            latest_run=_run(),
            ticker_rows=tickers,
            withdrawal_rows=withdrawals,
            network_rows=[],
            lightning_swap_rows=[],
        )

    # Assert
    def _buy_notes(exchange):
        return [
            c["note"]
            for p in result["all_paths"] if p["korean_exchange"] == exchange
            for c in p["breakdown"]["components"] if c["label"] == "국내 매수 수수료"
        ]

    coinone_notes = _buy_notes("coinone")
    assert coinone_notes, "코인원 매수 경로가 없음"
    assert all("바우처" in note for note in coinone_notes), f"코인원 note 누락: {coinone_notes}"
    assert all(note is None for note in _buy_notes("upbit"))
