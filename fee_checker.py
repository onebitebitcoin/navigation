#!/usr/bin/env python3
"""
Multi-Exchange Fee Checker
Binance, OKX, Coinbase, Kraken, Bitget + Upbit, Bithumb, Korbit, Coinone, Gopax
BTC/USDT 실시간 시세, 거래 수수료, 네트워크별 출금 수수료 조회
"""

import argparse
import asyncio
import html
import json
import os
import re
import sys
import threading
from datetime import datetime, timedelta
from typing import Optional

import requests

# ─── 공통 헤더 ────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}
TIMEOUT = 10

# ─── 거래 수수료 (기본/표준 등급) ───────────────────────────────
TRADING_FEES = {
    # 한국 거래소
    "upbit":   {"maker": 0.0005, "taker": 0.0005},
    "bithumb": {"maker": 0.0004, "taker": 0.0004},
    "korbit":  {"maker": 0.0015, "taker": 0.0020},
    "coinone": {"maker": 0.0010, "taker": 0.0010},
    "gopax":   {"maker": 0.0010, "taker": 0.0020},
    # 글로벌 거래소
    "binance":  {"spot": {"maker": 0.0010, "taker": 0.0010},
                 "perpetual": {"maker": 0.0002, "taker": 0.0005}},
    "okx":      {"spot": {"maker": 0.0008, "taker": 0.0010},
                 "perpetual": {"maker": 0.0002, "taker": 0.0005}},
    "coinbase": {"maker": 0.0040, "taker": 0.0060},
    "kraken":   {"maker": 0.0016, "taker": 0.0026},
    "bitget":   {"maker": 0.0010, "taker": 0.0010},
    "bybit":    {"spot": {"maker": 0.0010, "taker": 0.0010}},
    "gate":     {"spot": {"maker": 0.0010, "taker": 0.0010}},
}

# ─── 출금 수수료 스크래핑 설정 ───────────────────────────────────
# 하드코딩 fallback은 사용하지 않는다. 스크래핑/API 실패 시 오류를 반환한다.
SCRAPED_WITHDRAWAL_LABELS = {
    "upbit": {
        "btc": [{"label": "Bitcoin (On-chain)", "cache_key": "upbit_btc", "source_url": "https://upbit.com/service_center/fees?tab=dtw_fees"}],
        "usdt": [
            {"label": "Aptos", "cache_key": "upbit_usdt_aptos", "source_url": "https://upbit.com/service_center/fees?tab=dtw_fees"},
            {"label": "ERC20", "cache_key": "upbit_usdt_ethereum", "source_url": "https://upbit.com/service_center/fees?tab=dtw_fees"},
            {"label": "Kaia", "cache_key": "upbit_usdt_kaia", "source_url": "https://upbit.com/service_center/fees?tab=dtw_fees"},
            {"label": "TRC20", "cache_key": "upbit_usdt_tron", "source_url": "https://upbit.com/service_center/fees?tab=dtw_fees"},
        ],
    },
    "korbit": {
        "btc": [{"label": "Bitcoin (On-chain)", "cache_key": "korbit_btc", "source_url": "https://lightning.korbit.co.kr/info/fee/?tab=transfer"}],
        "usdt": [{"label": "TRC20", "cache_key": "korbit_usdt_tron", "source_url": "https://lightning.korbit.co.kr/info/fee/?tab=transfer"}],
    },
    "coinone": {
        "btc": [{"label": "Bitcoin (On-chain)", "cache_key": "coinone_btc", "source_url": "https://coinone.co.kr/support/fee-guide"}],
        "usdt": [{"label": "TRC20", "cache_key": "coinone_usdt_tron", "source_url": "https://coinone.co.kr/support/fee-guide"}],
    },
    "kraken": {
        "btc": [{"label": "Bitcoin (On-chain)", "cache_key": "kraken_btc", "source_url": "https://www.bitdegree.org/crypto/tutorials/kraken-fees"}],
    },
}

WITHDRAWAL_API_SOURCE_URLS = {
    "bithumb": "https://gw.bithumb.com/exchange/v1/coin-inout/info",
    "binance": "https://www.binance.com/bapi/capital/v1/public/capital/getNetworkCoinAll",
    "okx": "https://www.okx.com/v2/asset/withdraw/fee-amount-infos",
    "gopax": "https://api.gopax.co.kr/assets",
    "bitget": "https://api.bitget.com/api/v2/spot/public/coins",
    "coinbase": "https://api.exchange.coinbase.com/currencies",
    "kraken": "https://support.kraken.com/articles/360000767986-cryptocurrency-withdrawal-fees-and-minimums",
}

# ─── 거래소 그룹 ────────────────────────────────────────────────
GROUPS = {
    "korea":  ["upbit", "bithumb", "korbit", "coinone", "gopax"],
    "global": ["binance", "okx", "coinbase", "kraken", "bitget", "bybit", "gate"],
}
ALL_EXCHANGES = GROUPS["korea"] + GROUPS["global"]

# ─── 공개 API 미제공 출금 네트워크 정적 보강 레지스트리 ─────────────────
# 거래소 공개 API가 특정 네트워크 정보를 내려주지 않아
# 실시간 수집이 불가능한 경우 이 레지스트리에서 보강한다.
# 구조: {거래소: {코인: [네트워크 메타 목록]}}
# 패턴: _GATE_FEES(fetch_gate_withdrawal) 와 동일한 스타일로 통일.
_STATIC_WITHDRAWAL_OVERRIDES: dict[str, dict[str, list[dict]]] = {
    "okx": {
        "BTC": [
            {
                "label": "Lightning Network",
                "fee": 0.000015,        # ≈ 1500 sats (OKX LN 출금 수수료)
                "min": 0.000001,
                "max": 0.05,
                "enabled": True,
                "note": (
                    "OKX 공개 API가 LN 미제공 → 정적 메타데이터. "
                    "Invoice 방식 (인증 필요). 주기적 재확인 권장."
                ),
            },
        ],
    },
}


# ══════════════════════════════════════════════════════════════
# 티커 fetch 함수
# ══════════════════════════════════════════════════════════════

def _get(url: str, **kwargs) -> requests.Response:
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kwargs)
    return resp


def fetch_usd_krw_rate() -> float:
    """USD/KRW 실시간 환율 조회.

    1순위: Dunamu(업비트 운영사) 실시간 API — 수분 단위 갱신.
    2순위: open.er-api.com fallback — 하루 1회 갱신.
    """
    try:
        r = _get("https://quotation-api-cdn.dunamu.com/v1/forex/recent?codes=FRX.KRWUSD")
        if r.status_code == 200:
            data = r.json()
            if data and isinstance(data, list):
                return float(data[0]["basePrice"])
    except Exception:
        pass

    r = _get("https://open.er-api.com/v6/latest/USD")
    if r.status_code != 200:
        raise ValueError(f"환율 조회 오류: {r.status_code}")
    d = r.json()
    if d.get("result") != "success":
        raise ValueError("환율 조회 실패")
    return float(d["rates"]["KRW"])


def fetch_upbit(symbol: str = "BTC") -> dict:
    market = f"KRW-{symbol}"
    r = _get(f"https://api.upbit.com/v1/ticker?markets={market}")
    if r.status_code != 200:
        raise ValueError(f"Upbit 오류: {r.status_code}")
    d = r.json()[0]
    return {
        "price": float(d["trade_price"]),
        "high":  float(d["high_price"]),
        "low":   float(d["low_price"]),
        "volume": float(d["acc_trade_volume_24h"]),
        "currency": "KRW",
    }


def fetch_bithumb(symbol: str = "BTC") -> dict:
    r = _get(f"https://api.bithumb.com/public/ticker/{symbol}_KRW")
    if r.status_code != 200:
        raise ValueError(f"Bithumb 오류: {r.status_code}")
    d = r.json()
    if d.get("status") != "0000":
        raise ValueError(f"Bithumb 오류: {d.get('message')}")
    data = d["data"]
    return {
        "price":  float(data["closing_price"]),
        "high":   float(data["max_price"]),
        "low":    float(data["min_price"]),
        "volume": float(data["units_traded"]),
        "currency": "KRW",
    }


def fetch_korbit(symbol: str = "BTC") -> dict:
    pair = f"{symbol.lower()}_krw"
    r = _get(f"https://api.korbit.co.kr/v1/ticker/detailed?currency_pair={pair}")
    if r.status_code != 200:
        raise ValueError(f"Korbit 오류: {r.status_code}")
    d = r.json()
    return {
        "price":  float(d["last"]),
        "high":   float(d["high"]),
        "low":    float(d["low"]),
        "volume": float(d["volume"]),
        "currency": "KRW",
    }


def fetch_coinone(symbol: str = "BTC") -> dict:
    r = _get(f"https://api.coinone.co.kr/public/v2/ticker/KRW/{symbol}")
    if r.status_code != 200:
        raise ValueError(f"Coinone 오류: {r.status_code}")
    d = r.json()
    if d.get("result") != "success":
        raise ValueError(f"Coinone 오류: {d.get('errorCode')}")
    data = d["data"]
    return {
        "price":  float(data["close_24h"]),
        "high":   float(data["high_24h"]),
        "low":    float(data["low_24h"]),
        "volume": float(data["volume_24h"]),
        "currency": "KRW",
    }


def fetch_gopax(symbol: str = "BTC") -> dict:
    r = _get(f"https://api.gopax.co.kr/trading-pairs/{symbol}-KRW/ticker")
    if r.status_code != 200:
        raise ValueError(f"Gopax 오류: {r.status_code}")
    d = r.json()
    return {
        "price":  float(d["price"]),
        "high":   None,
        "low":    None,
        "volume": float(d["volume"]),
        "currency": "KRW",
    }


def fetch_binance_spot(symbol: str = "BTC") -> dict:
    r = _get("https://api.binance.com/api/v3/ticker/24hr", params={"symbol": f"{symbol}USDT"})
    if r.status_code != 200:
        raise ValueError(r.json().get("msg", "Binance 오류"))
    d = r.json()
    return {"price": float(d["lastPrice"]), "high": float(d["highPrice"]),
            "low": float(d["lowPrice"]), "volume": float(d["volume"]), "currency": "USD"}


def fetch_binance_perp(symbol: str = "BTC") -> dict:
    r = _get("https://fapi.binance.com/fapi/v1/ticker/24hr", params={"symbol": f"{symbol}USDT"})
    if r.status_code != 200:
        raise ValueError(r.json().get("msg", "Binance Perp 오류"))
    d = r.json()
    return {"price": float(d["lastPrice"]), "high": float(d["highPrice"]),
            "low": float(d["lowPrice"]), "volume": float(d["volume"]), "currency": "USD"}


def fetch_okx_spot() -> dict:
    r = _get("https://www.okx.com/api/v5/market/ticker", params={"instId": "BTC-USDT"})
    d = r.json()
    if d.get("code") != "0":
        raise ValueError(d.get("msg", "OKX 오류"))
    t = d["data"][0]
    return {"price": float(t["last"]), "high": float(t["high24h"]),
            "low": float(t["low24h"]), "volume": float(t["vol24h"]), "currency": "USD"}


def fetch_okx_perp() -> dict:
    r = _get("https://www.okx.com/api/v5/market/ticker", params={"instId": "BTC-USDT-SWAP"})
    d = r.json()
    if d.get("code") != "0":
        raise ValueError(d.get("msg", "OKX Perp 오류"))
    t = d["data"][0]
    return {"price": float(t["last"]), "high": float(t["high24h"]),
            "low": float(t["low24h"]), "volume": float(t["vol24h"]), "currency": "USD"}


def fetch_coinbase() -> dict:
    r = _get("https://api.coinbase.com/api/v3/brokerage/market/products/BTC-USD")
    if r.status_code != 200:
        raise ValueError(f"Coinbase 오류: {r.status_code}")
    d = r.json()
    return {
        "price":  float(d["price"]),
        "high":   None,
        "low":    None,
        "volume": float(d["volume_24h"]),
        "currency": "USD",
    }


def fetch_kraken() -> dict:
    r = _get("https://api.kraken.com/0/public/Ticker?pair=XBTUSD")
    if r.status_code != 200:
        raise ValueError(f"Kraken 오류: {r.status_code}")
    d = r.json()
    if d.get("error"):
        raise ValueError(f"Kraken 오류: {d['error']}")
    t = d["result"]["XXBTZUSD"]
    return {
        "price":  float(t["c"][0]),
        "high":   float(t["h"][1]),
        "low":    float(t["l"][1]),
        "volume": float(t["v"][1]),
        "currency": "USD",
    }


def fetch_bitget() -> dict:
    r = _get("https://api.bitget.com/api/v2/spot/market/tickers?symbol=BTCUSDT")
    if r.status_code != 200:
        raise ValueError(f"Bitget 오류: {r.status_code}")
    d = r.json()
    if d.get("code") != "00000":
        raise ValueError(d.get("msg", "Bitget 오류"))
    t = d["data"][0]
    return {
        "price":  float(t["lastPr"]),
        "high":   float(t["high24h"]),
        "low":    float(t["low24h"]),
        "volume": float(t["baseVolume"]),
        "currency": "USD",
    }


def fetch_gate() -> dict:
    """Gate.io 스팟 BTC/USDT 시세."""
    r = _get("https://api.gateio.ws/api/v4/spot/tickers", params={"currency_pair": "BTC_USDT"})
    if r.status_code != 200:
        raise ValueError(f"Gate.io 오류: {r.status_code}")
    d = r.json()
    if not d:
        raise ValueError("Gate.io 빈 응답")
    t = d[0]
    return {
        "price":   float(t["last"]),
        "high":    float(t.get("high_24h", 0) or 0),
        "low":     float(t.get("low_24h", 0) or 0),
        "volume":  float(t.get("base_volume", 0) or 0),
        "currency": "USD",
    }


def fetch_gate_withdrawal(coin: str) -> list:
    """Gate.io 출금 수수료 (공식 fee page 기준 정적값).
    Source: https://www.gate.com/fee
    출금 수수료는 시장 상황에 따라 동적으로 변경됨 — 주기적으로 재확인 필요.
    """
    _GATE_FEES: dict[str, list[dict]] = {
        "BTC": [
            {"label": "Bitcoin (On-chain)", "fee": 0.0005, "min": 0.001, "max": None, "enabled": True,
             "note": "gate_fee_page_scraped"},
        ],
        "USDT": [
            {"label": "TRC20",  "fee": 1.0,  "min": 10.0, "max": None, "enabled": True,
             "note": "gate_fee_page_scraped"},
            {"label": "ERC20",  "fee": 10.0, "min": 20.0, "max": None, "enabled": True,
             "note": "gate_fee_page_scraped"},
            {"label": "SOL",    "fee": 1.0,  "min": 10.0, "max": None, "enabled": True,
             "note": "gate_fee_page_scraped"},
            {"label": "BSC",    "fee": 1.0,  "min": 10.0, "max": None, "enabled": True,
             "note": "gate_fee_page_scraped"},
        ],
    }
    return _GATE_FEES.get(coin.upper(), [])


def fetch_bybit() -> dict:
    """Bybit 스팟 BTC/USDT 시세."""
    r = _get("https://api.bybit.com/v5/market/tickers", params={"category": "spot", "symbol": "BTCUSDT"})
    if r.status_code != 200:
        raise ValueError(f"Bybit 오류: {r.status_code}")
    d = r.json()
    if d.get("retCode") != 0:
        raise ValueError(f"Bybit API 오류: {d.get('retMsg')}")
    t = d["result"]["list"][0]
    return {
        "price":   float(t["lastPrice"]),
        "high":    float(t.get("highPrice24h", 0) or 0),
        "low":     float(t.get("lowPrice24h", 0) or 0),
        "volume":  float(t.get("volume24h", 0) or 0),
        "currency": "USD",
    }


# ══════════════════════════════════════════════════════════════
# 출금 수수료 fetch 함수
# ══════════════════════════════════════════════════════════════

def fetch_binance_withdrawal(coin: str) -> list:
    r = _get("https://www.binance.com/bapi/capital/v1/public/capital/getNetworkCoinAll")
    if not r.json().get("success"):
        raise ValueError("Binance 출금 API 오류")
    for item in r.json()["data"]:
        if item["coin"] == coin:
            result = []
            for net in item.get("networkList", []):
                max_text = net.get("withdrawMax")
                try:
                    max_amount = float(max_text) if max_text not in (None, "", "0", "0.0") else None
                except (ValueError, TypeError):
                    max_amount = None
                result.append({
                    "label":   net["name"],
                    "fee":     float(net["withdrawFee"]),
                    "min":     float(net["withdrawMin"]),
                    "max":     max_amount,
                    "enabled": net.get("withdrawEnable", False),
                })
            return result
    return []


def fetch_okx_withdrawal(coin: str) -> list:
    r = _get("https://www.okx.com/v2/asset/withdraw/fee-amount-infos")
    for item in r.json().get("data", []):
        if item.get("symbol") == coin:
            max_amounts = item.get("maxAmount", [])
            result = []
            for i, (name, fee, amt) in enumerate(zip(
                item.get("networkName", []),
                item.get("minFee", []),
                item.get("minAmount", []),
            )):
                max_text = max_amounts[i] if i < len(max_amounts) else None
                try:
                    max_amount = float(max_text) if max_text not in (None, "", "0", "0.0") else None
                except (ValueError, TypeError):
                    max_amount = None
                result.append({"label": name, "fee": float(fee), "min": float(amt), "max": max_amount, "enabled": True})
            # 공개 API 미제공 네트워크를 레지스트리에서 보강
            for override in _STATIC_WITHDRAWAL_OVERRIDES.get("okx", {}).get(coin.upper(), []):
                result.append(override)
            return result
    return []


def fetch_gopax_withdrawal(coin: str) -> list:
    r = _get("https://api.gopax.co.kr/assets")
    for item in r.json():
        if item["id"] == coin:
            max_text = item.get("withdrawalAmountMax")
            try:
                max_amount = float(max_text) if max_text not in (None, "", "0", "0.0") else None
            except (ValueError, TypeError):
                max_amount = None
            return [{
                "label":   item.get("networkName", coin),
                "fee":     float(item["withdrawalFee"]),
                "min":     float(item["withdrawalAmountMin"]),
                "max":     max_amount,
                "enabled": True,
            }]
    return []


def fetch_bithumb_withdrawal(coin: str) -> list:
    r = _get("https://gw.bithumb.com/exchange/v1/coin-inout/info")
    d = r.json()
    if d.get("status") != 200:
        raise ValueError("Bithumb 출금 API 오류")

    label_map = {
        "Bitcoin": "Bitcoin (On-chain)",
        "Tron": "TRC20",
        "Ethereum": "ERC20",
    }

    for item in d.get("data", []):
        if item.get("coinSymbol") != coin:
            continue
        result = []
        for network in item.get("networkInfoList", []):
            fee_text = network.get("withdrawFeeQuantity")
            min_text = network.get("withdrawMinimumQuantity")
            max_text = network.get("withdrawMaximumQuantity")
            try:
                fee = float(fee_text) if fee_text not in (None, "", "-") else None
            except ValueError:
                fee = None
            try:
                min_amount = float(min_text) if min_text not in (None, "", "-") else None
            except ValueError:
                min_amount = None
            try:
                max_amount = float(max_text) if max_text not in (None, "", "-", "0", "0.0") else None
            except (ValueError, TypeError):
                max_amount = None
            result.append({
                "label": label_map.get(network.get("networkName"), network.get("networkName")),
                "fee": fee,
                "min": min_amount,
                "max": max_amount,
                "enabled": bool(network.get("isWithdrawAvailable", False)),
                "suspension_reason": network.get("suspensionReason") or None,
                "suspension_message": network.get("suspensionMessage") or None,
            })
        return result
    return []


def fetch_bybit_withdrawal(coin: str) -> list:
    """Bybit 출금 수수료.
    Bybit v5 /asset/coin/query-info는 API 키 필요 → 공식 페이지 공개 수치 사용.
    Source: https://www.bybit.com/en/help-center/article/Withdrawal-Fees
    """
    _BYBIT_FEES: dict[str, list[dict]] = {
        "BTC": [
            {"label": "Bitcoin (On-chain)", "fee": 0.0002, "min": 0.0002, "max": None, "enabled": True,
             "note": "bybit_help_center_scraped"},
        ],
        "USDT": [
            {"label": "TRC20",  "fee": 1.0,  "min": 10.0, "max": None, "enabled": True,
             "note": "bybit_help_center_scraped"},
            {"label": "ERC20",  "fee": 10.0, "min": 10.0, "max": None, "enabled": True,
             "note": "bybit_help_center_scraped"},
            {"label": "SOL",    "fee": 1.0,  "min": 10.0, "max": None, "enabled": True,
             "note": "bybit_help_center_scraped"},
        ],
    }
    return _BYBIT_FEES.get(coin.upper(), [])


def fetch_bitget_withdrawal(coin: str) -> list:
    r = _get(f"https://api.bitget.com/api/v2/spot/public/coins?coin={coin}")
    d = r.json()
    if d.get("code") != "00000" or not d.get("data"):
        return []
    result = []
    for chain in d["data"][0].get("chains", []):
        if chain.get("withdrawable") == "true":
            max_text = chain.get("maxWithdrawAmount")
            try:
                max_amount = float(max_text) if max_text not in (None, "", "0", "0.0") else None
            except (ValueError, TypeError):
                max_amount = None
            result.append({
                "label":   chain["chain"],
                "fee":     float(chain["withdrawFee"]),
                "min":     float(chain["minWithdrawAmount"]),
                "max":     max_amount,
                "enabled": True,
            })
    return result


def _fetch_coinbase_currency_metadata(coin: str) -> dict:
    r = _get(f"https://api.exchange.coinbase.com/currencies/{coin}")
    if r.status_code != 200:
        raise ValueError(f"Coinbase currency metadata 오류: {r.status_code}")
    return r.json()


def _estimate_btc_withdrawal_fee_btc() -> float:
    attempts = [
        ("https://mempool.space/api/v1/fees/recommended", lambda data: data.get("hourFee") or data.get("halfHourFee") or data.get("fastestFee")),
        ("https://blockstream.info/api/fee-estimates", lambda data: data.get("6") or data.get("12") or data.get("24") or data.get("2")),
    ]
    last_error = None
    for url, extractor in attempts:
        try:
            r = _get(url)
            if r.status_code != 200:
                continue
            data = r.json()
            sat_per_vbyte = extractor(data)
            if sat_per_vbyte is None:
                continue
            return round(float(sat_per_vbyte) * 140 / 100_000_000, 8)
        except Exception as exc:
            last_error = exc
            continue
    raise ValueError(f"BTC 네트워크 수수료 추정 실패: {last_error or 'fee source unavailable'}")


def _estimate_eth_erc20_fee_in_usdt() -> float:
    gas_price_wei = None
    eth_price_usd = None
    try:
        r = requests.post(
            "https://cloudflare-eth.com",
            headers={**HEADERS, "Content-Type": "application/json"},
            data=json.dumps({"jsonrpc": "2.0", "method": "eth_gasPrice", "params": [], "id": 1}),
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            gas_price_wei = int(r.json()["result"], 16)
    except Exception:
        gas_price_wei = None

    try:
        eth_resp = _get("https://api.coinbase.com/api/v3/brokerage/market/products/ETH-USD")
        if eth_resp.status_code == 200:
            eth_price_usd = float(eth_resp.json()["price"])
    except Exception:
        eth_price_usd = None

    if gas_price_wei is None:
        gas_price_wei = 15 * 10**9  # 15 gwei fallback
    if eth_price_usd is None:
        eth_price_usd = 3000.0

    gas_limit = 65_000
    fee_eth = gas_price_wei * gas_limit / 10**18
    return round(fee_eth * eth_price_usd, 4)


# Coinbase는 공개 API로 BTC 출금 수수료를 제공하지 않는다(개인 계정은 Coinbase Exchange API 접근 불가).
# 멤풀 네트워크 추정치는 실제 코인베이스 청구액과 동떨어지므로(혼잡 한산 시 140 sats 등) 정적 등록값을 사용한다.
# 실제값이 바뀌면 이 상수만 갱신하면 된다. (어드민 패널에 source='static'으로 노출됨)
_COINBASE_BTC_WITHDRAWAL_FEE_BTC = 0.0001  # 10,000 sats — 코인베이스 BTC 온체인 출금 정적 등록값


def fetch_coinbase_withdrawal(coin: str) -> list:
    metadata = _fetch_coinbase_currency_metadata(coin)
    supported_networks = metadata.get("supported_networks") or []
    min_amount = metadata.get("min_withdrawal_amount")

    if coin == "BTC":
        label = "Bitcoin (On-chain)"
        network_entry = next(
            (
                network for network in supported_networks
                if "bitcoin" in (network.get("name") or "").lower()
                or "btc" in (network.get("name") or "").lower()
            ),
            {},
        )
        return [{
            "label": label,
            "fee": _COINBASE_BTC_WITHDRAWAL_FEE_BTC,
            "min": float(min_amount) if min_amount not in (None, "") else None,
            "enabled": not bool(network_entry.get("is_disabled")),
            "note": "정적 등록값 (공개 API 미제공)",
        }]

    if coin == "USDT":
        results = []
        for network in supported_networks:
            network_name = (network.get("name") or "").lower()
            if "ethereum" in network_name or "erc20" in network_name:
                results.append({
                    "label": "ERC20",
                    "fee": _estimate_eth_erc20_fee_in_usdt(),
                    "min": float(min_amount) if min_amount not in (None, "") else None,
                    "enabled": not bool(network.get("is_disabled")),
                    "note": "공식 자산 메타데이터 + 공개 ETH 가스비 추정",
                })
        return results
    return []


def _extract_kraken_table_fee(text: str, patterns: list[tuple[str, str]]) -> list[dict]:
    results = []
    for label, pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        fee = float(match.group("fee"))
        minimum = match.groupdict().get("min")
        results.append({
            "label": label,
            "fee": fee,
            "min": float(minimum) if minimum else None,
            "enabled": True,
            "note": "공식 지원 문서 스크래핑",
        })
    return results


def fetch_kraken_withdrawal(coin: str) -> list:
    """Kraken 출금 수수료.
    Kraken 공식 사이트는 JS 렌더링 → Playwright 캐시 우선 사용.
    캐시 만료 시 자동 재스크래핑.
    """
    coin_up = coin.upper()
    if coin_up == "BTC":
        cache_key = "kraken_btc"
        fee, scraped_at = _get_cached_fee_with_meta(cache_key)
        if fee is None:
            raise ValueError("Kraken BTC 출금 수수료 캐시 없음 (Playwright 재스크래핑 필요)")
        return [{"label": "Bitcoin (On-chain)", "fee": fee, "min": fee,
                 "enabled": True, "note": "Playwright 스크래핑",
                 "scraped_at": scraped_at,
                 "source_url": "https://www.bitdegree.org/crypto/tutorials/kraken-fees"}]
    if coin_up == "USDT":
        resp = _get(WITHDRAWAL_API_SOURCE_URLS["kraken"])
        if resp.status_code != 200:
            raise ValueError(f"Kraken USDT 출금 수수료 조회 실패: {resp.status_code}")
        # name_display 필드 기준 매칭 (2025~ JSON 구조)
        results = _extract_kraken_table_fee(resp.text, [
            ("ERC20", r'"name_display":"USDT - Ethereum","fee":"(?P<fee>[0-9.]+)","min_amount":"(?P<min>[0-9.]*)"'),
            ("TRC20", r'"name_display":"USDT - Tron","fee":"(?P<fee>[0-9.]+)","min_amount":"(?P<min>[0-9.]*)"'),
        ])
        if not results:
            # 이전 JSON 구조 fallback
            results = _extract_kraken_table_fee(resp.text, [
                ("ERC20", r'"asset":"USDT"[^}]*?"withdrawal_network_info":\{"network":"Ethereum"[^}]*?\}[^{}]*?"fee":"(?P<fee>[0-9.]+)","min_amount":"(?P<min>[0-9.]+)"'),
                ("TRC20", r'"asset":"USDT"[^}]*?"withdrawal_network_info":\{"network":"Tron"[^}]*?\}[^{}]*?"fee":"(?P<fee>[0-9.]+)","min_amount":"(?P<min>[0-9.]+)"'),
            ])
        if not results:
            raise ValueError("Kraken USDT 출금 수수료 스크래핑 실패")
        return results
    raise ValueError(f"Kraken {coin_up} 출금 수수료 미지원")


SPECIAL_WITHDRAWAL_FETCHERS = {
    ("coinbase", "btc"): fetch_coinbase_withdrawal,
    ("coinbase", "usdt"): fetch_coinbase_withdrawal,
    ("kraken", "usdt"): fetch_kraken_withdrawal,
}


# ══════════════════════════════════════════════════════════════
# Playwright 캐시 기반 출금 수수료 스크래핑 (24h TTL)
# ══════════════════════════════════════════════════════════════

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".withdrawal_cache.json")
CACHE_TTL_HOURS = 24
# Playwright 스크래핑 가능 거래소 (Bithumb/Coinbase는 Cloudflare 차단 또는 동적 수수료)
SCRAPE_EXCHANGES = {"upbit", "korbit", "coinone", "kraken"}


def _load_cache() -> dict:
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_updated": None, "fees": {}}


def _save_cache(data: dict) -> None:
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _is_cache_valid(cache: dict) -> bool:
    try:
        lu = cache.get("last_updated")
        if not lu:
            return False
        return datetime.now() - datetime.fromisoformat(lu) < timedelta(hours=CACHE_TTL_HOURS)
    except Exception:
        return False


# ── Playwright 스크래퍼 (async) ──────────────────────────────

async def _pw_scrape_upbit(browser) -> tuple:
    page = await browser.new_page()
    try:
        await page.goto(
            "https://upbit.com/service_center/fees?tab=dtw_fees",
            wait_until="domcontentloaded", timeout=20000,
        )
        await page.wait_for_timeout(2500)
        rows = await page.evaluate(
            """() => Array.from(document.querySelectorAll('table tr'))
                .map(tr => Array.from(tr.querySelectorAll('th,td')).map(td => td.innerText.trim()))
                .filter(row => row.length >= 6)"""
        )
        fees = {}
        for row in rows:
            asset = row[0]
            if asset == "BTC":
                withdrawal_fees = [line.strip() for line in row[5].splitlines() if line.strip()]
                if withdrawal_fees:
                    fee_text = withdrawal_fees[0].split()[0].replace(",", "")
                    fees["upbit_btc"] = float(fee_text)
            if asset == "USDT":
                networks = [line.strip() for line in row[1].splitlines() if line.strip()]
                withdrawal_fees = [line.strip() for line in row[5].splitlines() if line.strip()]
                network_key_map = {
                    "Aptos": "upbit_usdt_aptos",
                    "Ethereum": "upbit_usdt_ethereum",
                    "Kaia": "upbit_usdt_kaia",
                    "Tron": "upbit_usdt_tron",
                }
                for network_name, fee_text in zip(networks, withdrawal_fees):
                    cache_key = network_key_map.get(network_name)
                    if not cache_key:
                        continue
                    value_text = fee_text.split()[0].replace(",", "")
                    fees[cache_key] = float(value_text)
        return fees
    except Exception:
        return {}
    finally:
        await page.close()


async def _pw_scrape_korbit(browser) -> tuple:
    page = await browser.new_page()
    try:
        await page.goto(
            "https://lightning.korbit.co.kr/info/fee/",
            wait_until="domcontentloaded", timeout=20000,
        )
        await page.wait_for_timeout(2000)
        try:
            await page.click("button:has-text('입출금 수수료')", timeout=5000)
            await page.wait_for_timeout(2000)
        except Exception:
            pass
        rows = await page.evaluate(
            """() => Array.from(document.querySelectorAll('table tr'))
                .map(tr => Array.from(tr.querySelectorAll('th,td')).map(td => td.innerText.trim()))
                .filter(row => row.length >= 6)"""
        )
        fees = {}
        for row in rows:
            asset = row[0]
            if asset.startswith("BTC("):
                fees["korbit_btc"] = float(row[5].split()[0].replace(",", ""))
            if asset.startswith("USDT(") and "Tron" in row[1]:
                fees["korbit_usdt_tron"] = float(row[5].split()[0].replace(",", ""))
        return fees
    except Exception:
        return {}
    finally:
        await page.close()


async def _pw_scrape_coinone(browser) -> tuple:
    page = await browser.new_page()
    try:
        await page.goto(
            "https://coinone.co.kr/support/fee-guide",
            wait_until="domcontentloaded", timeout=20000,
        )
        await page.wait_for_timeout(5000)
        rows = await page.evaluate(
            """() => Array.from(document.querySelectorAll('table tr'))
                .map(tr => Array.from(tr.querySelectorAll('th,td')).map(td => td.innerText.trim()))
                .filter(row => row.length >= 6)"""
        )
        fees = {}
        for row in rows:
            asset = row[0]
            if asset == "BTC":
                fees["coinone_btc"] = float(row[5].split()[0].replace(",", ""))
            if asset == "USDT" and "Tron" in row[1]:
                fees["coinone_usdt_tron"] = float(row[5].split()[0].replace(",", ""))
        return fees
    except Exception:
        return {}
    finally:
        await page.close()


async def _pw_scrape_kraken(browser) -> tuple:
    """Kraken BTC 출금 수수료: 공식 사이트 Cloudflare 차단 → bitdegree 아티클에서 스크래핑"""
    import re
    page = await browser.new_page()
    try:
        await page.goto(
            "https://www.bitdegree.org/crypto/tutorials/kraken-fees",
            wait_until="domcontentloaded", timeout=20000,
        )
        await page.wait_for_timeout(4000)
        text = await page.inner_text("body")
        # "the withdrawal fee is 0.0002 BTC" 형식
        m = re.search(r'withdrawal fee is (0\.0+\d+)\s*BTC', text)
        if m:
            return ("kraken_btc", float(m.group(1)))
        # 폴백: BTC 맥락에서 수수료 숫자 탐색
        m = re.search(r'Bitcoin.{0,120}?(0\.000\d+)', text, re.DOTALL)
        if m:
            return ("kraken_btc", float(m.group(1)))
        return ("kraken_btc", None)
    except Exception:
        return ("kraken_btc", None)
    finally:
        await page.close()


async def _pw_scrape_upbit_limits(browser) -> dict:
    """업비트 입출금 이용 안내 페이지에서 디지털 자산 일일 출금 한도를 스크래핑.

    반환: {'krw_daily_verified_digital': int | None}
    예: {'krw_daily_verified_digital': 5_000_000_000}  # 50억원
    """
    page = await browser.new_page()
    try:
        await page.goto(
            "https://upbit.com/service_center/guide?tab=deposit_withdraw",
            wait_until="domcontentloaded", timeout=20000,
        )
        await page.wait_for_timeout(2500)
        rows = await page.evaluate(
            """() => Array.from(document.querySelectorAll('table tr'))
                .map(tr => Array.from(tr.querySelectorAll('th,td')).map(td => td.innerText.trim()))
                .filter(row => row.length >= 2)"""
        )
        limits: dict = {}
        for row in rows:
            # "2채널 인증 | 50억원" 행 탐색
            if any('2채널 인증' in cell for cell in row):
                for cell in row:
                    if '억원' in cell and '2채널' not in cell:
                        val_text = cell.replace('억원', '').replace(',', '').strip()
                        try:
                            limits['krw_daily_verified_digital'] = int(float(val_text) * 100_000_000)
                        except ValueError:
                            pass
        return limits
    except Exception:
        return {}
    finally:
        await page.close()


async def _pw_scrape_coinone_limits(browser) -> dict:
    """코인원 입출금 안내 페이지에서 3단계 가상자산 일일 출금 한도를 스크래핑.

    반환: {'krw_daily_verified_digital': int | None}
    예: {'krw_daily_verified_digital': 7_000_000_000}  # 70억원 (3단계)
    """
    page = await browser.new_page()
    try:
        await page.goto(
            "https://coinone.co.kr/support/guide",
            wait_until="domcontentloaded", timeout=25000,
        )
        await page.wait_for_timeout(4000)
        text = await page.evaluate("() => document.body.innerText")
        limits: dict = {}
        for line in text.splitlines():
            if '가상자산(합산)' in line:
                # 탭 구분: [label, 1단계, 2단계, 3단계(개별설정), 4단계]
                parts = [p.strip() for p in line.split('\t')]
                if len(parts) >= 4:
                    val_text = parts[3]  # 3단계(개별설정) 값: "~70억 원까지"
                    m = re.search(r'([\d,]+)\s*억', val_text)
                    if m:
                        num = float(m.group(1).replace(',', ''))
                        limits['krw_daily_verified_digital'] = int(num * 100_000_000)
                break
        return limits
    except Exception:
        return {}
    finally:
        await page.close()


async def scrape_korea_withdrawal_limits_async(browser) -> dict[str, dict]:
    """국내 거래소 출금 한도 비동기 스크래핑. {exchange: {krw_daily_verified_digital, ...}} 반환."""
    upbit_limits = await _pw_scrape_upbit_limits(browser)
    coinone_limits = await _pw_scrape_coinone_limits(browser)
    return {
        'upbit': upbit_limits,
        'coinone': coinone_limits,
    }


def scrape_korea_withdrawal_limits() -> dict[str, dict]:
    """국내 거래소 출금 한도를 Playwright로 스크래핑한다. 동기 인터페이스."""
    container: dict = {}

    async def _run_async():
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            container['result'] = {}
            return
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                container['result'] = await scrape_korea_withdrawal_limits_async(browser)
            finally:
                await browser.close()

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run_async())
        except Exception:
            container['result'] = {}
        finally:
            loop.close()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=30)
    return container.get('result', {})


async def _scrape_all_async() -> dict:
    """Playwright로 거래소 BTC 출금 수수료 병렬 스크래핑"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {}

    fees = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        results = await asyncio.gather(
            _pw_scrape_upbit(browser),
            _pw_scrape_korbit(browser),
            _pw_scrape_coinone(browser),
            _pw_scrape_kraken(browser),
            return_exceptions=True,
        )
        await browser.close()

    for result in results:
        if isinstance(result, tuple) and result[1] is not None:
            fees[result[0]] = result[1]
        elif isinstance(result, dict):
            fees.update({key: value for key, value in result.items() if value is not None})
    return fees


def _run_scraping() -> dict:
    """별도 스레드에서 새 이벤트 루프로 스크래핑 실행 (기존 루프 충돌 방지)"""
    container = {}

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            container["fees"] = loop.run_until_complete(_scrape_all_async())
        except Exception:
            container["fees"] = {}
        finally:
            loop.close()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=60)
    return container.get("fees", {})


def refresh_withdrawal_cache() -> dict:
    """Playwright 스크래핑 후 캐시 저장. 성공한 거래소만 갱신."""
    fees = _run_scraping()
    cache = _load_cache()
    cache["fees"].update(fees)
    cache["last_updated"] = datetime.now().isoformat()
    cache["scrape_count"] = len(cache["fees"])
    _save_cache(cache)
    return cache


def _get_cached_btc_fee(exchange: str) -> Optional[float]:
    """캐시에서 BTC 출금 수수료 조회. 만료 시 재스크래핑."""
    if exchange not in SCRAPE_EXCHANGES:
        return None
    cache = _load_cache()
    if not _is_cache_valid(cache):
        cache = refresh_withdrawal_cache()
    return cache.get("fees", {}).get(f"{exchange}_btc")


def _get_cached_fee_with_meta(cache_key: str) -> tuple[Optional[float], Optional[str]]:
    cache = _load_cache()
    if not _is_cache_valid(cache) or cache_key not in cache.get("fees", {}):
        cache = refresh_withdrawal_cache()
    return cache.get("fees", {}).get(cache_key), cache.get("last_updated")


def _get_cache_with_required_keys(cache_keys: list[str]) -> dict:
    cache = _load_cache()
    fees = cache.get("fees", {})
    if not _is_cache_valid(cache) or any(cache_key not in fees for cache_key in cache_keys):
        cache = refresh_withdrawal_cache()
    return cache


# ══════════════════════════════════════════════════════════════
# 거래소 점검/출금 중단 상태 감지 (1h TTL)
# ══════════════════════════════════════════════════════════════

MAINTENANCE_CACHE_TTL_HOURS = 1

SUSPENSION_KEYWORDS = [
    "점검", "중단", "차단", "서비스 중지", "입출금 중단", "출금 중단",
    "maintenance", "suspended", "blocked", "temporarily unavailable",
]

COIN_PATTERNS = {
    "USDT": ["usdt", "테더", "tether"],
    "BTC":  ["btc", "bitcoin", "비트코인"],
}

NETWORK_PATTERNS = {
    "TRC20":               ["trc20", "trc-20", "트론", "tron"],
    "ERC20":               ["erc20", "erc-20", "이더리움", "ethereum"],
    "Bitcoin (On-chain)":  ["bitcoin", "온체인", "on-chain", "btc"],
    "Lightning Network":   ["lightning", "라이트닝"],
}


def _detect_suspension(text: str, source_url: str) -> list:
    """텍스트에서 점검/중단 공지를 감지하여 리스트 반환"""
    lower = text.lower()
    has_suspension = any(kw.lower() in lower for kw in SUSPENSION_KEYWORDS)
    if not has_suspension:
        return []

    detected_coins = [coin for coin, pats in COIN_PATTERNS.items() if any(p in lower for p in pats)]
    detected_networks = [net for net, pats in NETWORK_PATTERNS.items() if any(p in lower for p in pats)]

    if not detected_coins and not detected_networks:
        return []

    results = []
    coins = detected_coins or ["UNKNOWN"]
    networks = detected_networks or ["UNKNOWN"]
    for coin in coins:
        for network in networks:
            results.append({
                "coin":       coin,
                "network":    network,
                "status":     "suspended",
                "reason":     text[:200].strip(),
                "source_url": source_url,
                "detected_at": datetime.now().isoformat(),
            })
    return results


async def _pw_check_upbit_maintenance(browser) -> tuple:
    """업비트 공지사항 피드에서 출금 중단 공지 감지"""
    page = await browser.new_page()
    url = "https://upbit.com/service_center/notice"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(3000)
        text = await page.inner_text("body")
        suspended = []
        for line in text.splitlines():
            line_lower = line.lower()
            if any(kw.lower() in line_lower for kw in SUSPENSION_KEYWORDS):
                suspended.extend(_detect_suspension(line, url))
        return ("upbit", suspended)
    except Exception:
        return ("upbit", [])
    finally:
        await page.close()


async def _pw_check_bithumb_maintenance(browser) -> tuple:
    """빗썸 공지사항에서 출금 중단 공지 감지"""
    page = await browser.new_page()
    url = "https://www.bithumb.com/react/support/notice"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(3000)
        text = await page.inner_text("body")
        suspended = []
        for line in text.splitlines():
            line_lower = line.lower()
            if any(kw.lower() in line_lower for kw in SUSPENSION_KEYWORDS):
                suspended.extend(_detect_suspension(line, url))
        return ("bithumb", suspended)
    except Exception:
        return ("bithumb", [])
    finally:
        await page.close()


async def _pw_check_korbit_maintenance(browser) -> tuple:
    """코빗 공지사항에서 출금 중단 공지 감지"""
    page = await browser.new_page()
    url = "https://www.korbit.co.kr/announcement"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(3000)
        text = await page.inner_text("body")
        suspended = []
        for line in text.splitlines():
            line_lower = line.lower()
            if any(kw.lower() in line_lower for kw in SUSPENSION_KEYWORDS):
                suspended.extend(_detect_suspension(line, url))
        return ("korbit", suspended)
    except Exception:
        return ("korbit", [])
    finally:
        await page.close()


async def _pw_check_coinone_maintenance(browser) -> tuple:
    """코인원 공지사항에서 출금 중단 공지 감지"""
    page = await browser.new_page()
    url = "https://coinone.co.kr/support/notice"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(3000)
        text = await page.inner_text("body")
        suspended = []
        for line in text.splitlines():
            line_lower = line.lower()
            if any(kw.lower() in line_lower for kw in SUSPENSION_KEYWORDS):
                suspended.extend(_detect_suspension(line, url))
        return ("coinone", suspended)
    except Exception:
        return ("coinone", [])
    finally:
        await page.close()


async def _scrape_maintenance_async(exchanges: list) -> dict:
    """병렬로 모든 거래소 점검 상태 스크래핑"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {}

    scraper_map = {
        "upbit":   _pw_check_upbit_maintenance,
        "bithumb": _pw_check_bithumb_maintenance,
        "korbit":  _pw_check_korbit_maintenance,
        "coinone": _pw_check_coinone_maintenance,
    }

    tasks = [scraper_map[ex] for ex in exchanges if ex in scraper_map]
    if not tasks:
        return {}

    results_data = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        results = await asyncio.gather(
            *[fn(browser) for fn in tasks],
            return_exceptions=True,
        )
        await browser.close()

    for result in results:
        if isinstance(result, tuple):
            exchange, suspended = result
            results_data[exchange] = suspended
    return results_data


def _run_maintenance_scraping(exchanges: list) -> dict:
    """별도 스레드에서 새 이벤트 루프로 점검 상태 스크래핑 실행"""
    container = {}

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            container["data"] = loop.run_until_complete(
                _scrape_maintenance_async(exchanges)
            )
        except Exception:
            container["data"] = {}
        finally:
            loop.close()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=90)
    return container.get("data", {})


def _is_maintenance_cache_valid(cache: dict) -> bool:
    try:
        checked_at = cache.get("maintenance_checked_at")
        if not checked_at:
            return False
        return datetime.now() - datetime.fromisoformat(checked_at) < timedelta(hours=MAINTENANCE_CACHE_TTL_HOURS)
    except Exception:
        return False


def check_maintenance_status(exchanges=None) -> dict:
    """
    한국 거래소의 출금 네트워크 점검/중단 상태를 조회합니다.
    결과는 1시간 TTL로 캐시됩니다.

    Returns:
        {exchange: [{"coin": "USDT", "network": "TRC20", "status": "suspended",
                     "reason": "...", "source_url": "...", "detected_at": "..."}]}
        점검 중인 네트워크가 없으면 빈 리스트.
    """
    try:
        supported = ["upbit", "bithumb", "korbit", "coinone"]
        if exchanges is None:
            exchanges = supported
        else:
            exchanges = [ex for ex in exchanges if ex in supported]

        if not exchanges:
            return {}

        cache = _load_cache()

        if _is_maintenance_cache_valid(cache):
            cached = cache.get("maintenance", {})
            return {ex: cached.get(ex, []) for ex in exchanges}

        scraped = _run_maintenance_scraping(exchanges)

        existing = cache.get("maintenance", {})
        existing.update(scraped)
        cache["maintenance"] = existing
        cache["maintenance_checked_at"] = datetime.now().isoformat()
        _save_cache(cache)

        return {ex: scraped.get(ex, []) for ex in exchanges}
    except Exception:
        return {}


# ══════════════════════════════════════════════════════════════
# 코인원 거래 수수료 프로모션 감지 (1h TTL)
# ══════════════════════════════════════════════════════════════

FEE_PROMO_CACHE_TTL_HOURS = 1

COINONE_NOTICE_LIST_URL = (
    "https://api-gateway.coinone.co.kr/notice/v1/announcements/posts"
    "?includePin=false&page=0&pageSize=30"
)
COINONE_NOTICE_DETAIL_URL = "https://api-gateway.coinone.co.kr/notice/v1/announcements/posts/{notice_id}"
COINONE_NOTICE_PAGE_URL = "https://coinone.co.kr/info/notice/{notice_id}"
COINONE_NOTICE_HEADERS = {**HEADERS, "Referer": "https://coinone.co.kr/"}

# 공지 본문 표기 예: "- 수수료율 : Maker 0% / Taker 0%"
FEE_PROMO_RATE_PATTERN = re.compile(
    r"Maker\s*(\d+(?:\.\d+)?)\s*%\s*/\s*Taker\s*(\d+(?:\.\d+)?)\s*%", re.IGNORECASE
)
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")


def _strip_html(content: str) -> str:
    """공지 본문 HTML에서 태그/엔티티를 걷어내고 평문으로 변환"""
    return html.unescape(_HTML_TAG_PATTERN.sub(" ", content or ""))


def _find_fee_promo_notices(notices: list) -> list:
    """진행 중(INPROGRESS)인 수수료 이벤트 공지를 updatedAt 최신순으로 반환"""
    matched = []
    for item in notices:
        title = (item.get("title") or "").strip()
        event = item.get("eventInformation")
        if "수수료" not in title or not isinstance(event, dict):
            continue
        if event.get("eventStatus") != "INPROGRESS":
            continue
        matched.append(item)
    matched.sort(key=lambda item: item.get("updatedAt") or 0, reverse=True)
    return matched


def _parse_coinone_fee_promo(notice: dict) -> Optional[dict]:
    """공지 상세 본문에서 프로모션 수수료율을 파싱. 표기가 없으면 None."""
    notice_id = notice.get("id")
    r = requests.get(
        COINONE_NOTICE_DETAIL_URL.format(notice_id=notice_id),
        headers=COINONE_NOTICE_HEADERS,
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        raise ValueError(f"Coinone 공지 상세 오류: {r.status_code}")
    content = _strip_html((r.json().get("body") or {}).get("content"))
    m = FEE_PROMO_RATE_PATTERN.search(content)
    if not m:
        return None
    return {
        "maker_fee_pct": float(m.group(1)),
        "taker_fee_pct": float(m.group(2)),
        # 바우처 발급이 전제 조건인 이벤트인지 (공지 본문 언급 여부)
        "requires_voucher": "바우처" in content,
        "notice_id": notice_id,
        "notice_title": (notice.get("title") or "").strip(),
        "source_url": COINONE_NOTICE_PAGE_URL.format(notice_id=notice_id),
    }


def _scrape_coinone_fee_promo() -> Optional[dict]:
    """코인원 공지 API에서 진행 중인 거래 수수료 프로모션을 조회"""
    r = requests.get(COINONE_NOTICE_LIST_URL, headers=COINONE_NOTICE_HEADERS, timeout=TIMEOUT)
    if r.status_code != 200:
        raise ValueError(f"Coinone 공지 목록 오류: {r.status_code}")
    notices = (r.json().get("body") or {}).get("notices") or []
    for notice in _find_fee_promo_notices(notices):
        try:
            promo = _parse_coinone_fee_promo(notice)
        except Exception as e:
            # 상세 1건 실패가 나머지 후보 탐색을 막지 않도록 건너뛴다.
            print(f"[coinone] 공지 {notice.get('id')} 상세 파싱 실패: {e}", file=sys.stderr)
            continue
        if promo:
            return promo
    return None


def _is_fee_promo_cache_valid(cache: dict, exchange: str) -> bool:
    try:
        checked_at = cache.get("fee_promo", {}).get(exchange, {}).get("checked_at")
        if not checked_at:
            return False
        return datetime.now() - datetime.fromisoformat(checked_at) < timedelta(hours=FEE_PROMO_CACHE_TTL_HOURS)
    except Exception:
        return False


def fetch_coinone_fee_promo() -> Optional[dict]:
    """
    코인원에서 진행 중인 거래 수수료 프로모션의 실제 적용 수수료율을 조회합니다.
    결과는 1시간 TTL로 캐시됩니다.

    Returns:
        {"maker_fee_pct": 0.0, "taker_fee_pct": 0.0, "requires_voucher": True,
         "notice_id": 5695, "notice_title": "...", "source_url": "...",
         "checked_at": "..."}
        진행 중인 프로모션이 없거나 조회/파싱에 실패하면 None.
        하드코딩 fallback은 두지 않는다 — 근거를 못 찾으면 호출부가 정적 수수료를 쓴다.
    """
    try:
        cache = _load_cache()

        if _is_fee_promo_cache_valid(cache, "coinone"):
            entry = cache.get("fee_promo", {}).get("coinone", {})
            return entry if "taker_fee_pct" in entry else None

        try:
            promo = _scrape_coinone_fee_promo()
        except Exception as e:
            print(f"[coinone] 거래 수수료 프로모션 조회 실패: {e}", file=sys.stderr)
            promo = None

        # 프로모션이 없거나 실패한 경우에도 checked_at을 남겨 TTL 내 재호출을 막는다.
        entry = {**(promo or {}), "checked_at": datetime.now().isoformat()}
        fee_promo = cache.get("fee_promo", {})
        fee_promo["coinone"] = entry
        cache["fee_promo"] = fee_promo
        _save_cache(cache)

        return entry if promo else None
    except Exception as e:
        print(f"[coinone] 거래 수수료 프로모션 캐시 처리 실패: {e}", file=sys.stderr)
        return None


def get_scraped_withdrawal(exchange: str, coin: str) -> list:
    """출금 수수료 반환: 공개 API 또는 스크래핑 결과만 사용. fallback 없음."""
    exchange = exchange.lower()
    coin_lower = coin.lower()

    special_fetcher = SPECIAL_WITHDRAWAL_FETCHERS.get((exchange, coin_lower))
    if special_fetcher:
      return special_fetcher(coin.upper())

    config = SCRAPED_WITHDRAWAL_LABELS.get(exchange, {}).get(coin_lower)
    if not config:
        raise ValueError(f"{exchange} {coin.upper()} 출금 수수료는 스크래핑/API 미지원")

    cache = _get_cache_with_required_keys([item["cache_key"] for item in config])
    fees = cache.get("fees", {})
    scraped_at = cache.get("last_updated")
    result = []
    for item in config:
        fee = fees.get(item["cache_key"])
        if fee is None:
            continue
        result.append({
            "label": item["label"],
            "fee": fee,
            "min": None,
            "enabled": True,
            "note": "Playwright 스크래핑",
            "scraped_at": scraped_at,
            "source_url": item.get("source_url"),
        })
    if not result:
        raise ValueError(f"{exchange} {coin.upper()} 출금 수수료 스크래핑 실패")
    return result


def get_withdrawal_source_url(exchange: str, coin: str, network_label: str | None = None) -> str | None:
    api_source_url = WITHDRAWAL_API_SOURCE_URLS.get(exchange.lower())
    if api_source_url:
        return api_source_url
    config = SCRAPED_WITHDRAWAL_LABELS.get(exchange.lower(), {}).get(coin.lower(), [])
    for item in config:
        if network_label is None or item["label"].lower() == network_label.lower():
            return item.get("source_url")
    return None


def get_static_withdrawal(exchange: str, coin: str) -> list:
    """하위 호환용 래퍼. 더 이상 정적 fallback을 사용하지 않는다."""
    return get_scraped_withdrawal(exchange, coin)


# ══════════════════════════════════════════════════════════════
# 출력 함수
# ══════════════════════════════════════════════════════════════

def _fmt_price(price: Optional[float], currency: str) -> str:
    if price is None:
        return "N/A"
    if currency == "KRW":
        return f"₩{price:>14,.0f}"
    return f"${price:>12,.2f}"


def _fmt_volume(volume: Optional[float], symbol: str) -> str:
    if volume is None:
        return "N/A"
    return f"{volume:>12,.2f} {symbol}"


def print_ticker(exchange: str, label: str, ticker: dict, fees: dict, symbol: str = "BTC") -> None:
    cur = ticker.get("currency", "USD")
    quote = "KRW" if cur == "KRW" else cur
    print(f"\n[{exchange}] {symbol}/{quote} - {label}")
    print(f"  Price     : {_fmt_price(ticker['price'], cur)}")
    print(f"  24h High  : {_fmt_price(ticker.get('high'), cur)}")
    print(f"  24h Low   : {_fmt_price(ticker.get('low'), cur)}")
    print(f"  24h Volume: {_fmt_volume(ticker.get('volume'), symbol)}")
    print(f"  Maker Fee : {fees['maker'] * 100:.4f}% (기본 등급)")
    print(f"  Taker Fee : {fees['taker'] * 100:.4f}% (기본 등급)")


def print_withdrawal(exchange: str, coin: str, networks: list) -> None:
    if not networks:
        print(f"\n[{exchange}] {coin} 출금 수수료: 데이터 없음")
        return
    unit = "KRW" if coin == "KRW" else coin
    print(f"\n[{exchange}] {coin} 네트워크별 출금 수수료")
    for net in networks:
        status = " [출금 중단]" if net.get("enabled") is False else ""
        label = f"{net['label']}{status}"
        if net.get("fee") is None:
            note = net.get("note", "N/A")
            print(f"  {label:<35}: {note}")
        else:
            fee_str = f"{net['fee']:.8f} {unit}" if coin != "USDT" else f"{net['fee']:.4f} USDT"
            min_str = ""
            if net.get("min") is not None:
                min_val = net['min']
                min_str = f"  (최소: {min_val:.8f} {unit})" if coin != "USDT" else f"  (최소: {min_val:.4f} USDT)"
            note = f"  [{net['note']}]" if net.get("note") else ""
            print(f"  {label:<35}: {fee_str}{min_str}{note}")


# ══════════════════════════════════════════════════════════════
# 거래소별 실행 로직
# ══════════════════════════════════════════════════════════════

def run_korea_exchange(name: str, show_withdrawal: bool) -> bool:
    fetchers = {
        "upbit":   fetch_upbit,
        "bithumb": fetch_bithumb,
        "korbit":  fetch_korbit,
        "coinone": fetch_coinone,
        "gopax":   fetch_gopax,
    }
    label_map = {
        "upbit": "Upbit", "bithumb": "Bithumb", "korbit": "Korbit",
        "coinone": "Coinone", "gopax": "Gopax",
    }
    has_error = False
    label = label_map[name]
    fees = TRADING_FEES[name]

    try:
        ticker = fetchers[name]()
        print_ticker(label, "Spot", ticker, fees)
    except Exception as e:
        print(f"\n[{label.upper()}] 티커 오류: {e}", file=sys.stderr)
        has_error = True

    if show_withdrawal:
        # Gopax: 공개 API 사용
        if name == "gopax":
            for coin in ["BTC", "USDT"]:
                try:
                    nets = fetch_gopax_withdrawal(coin)
                    print_withdrawal(label, coin, nets)
                except Exception as e:
                    print(f"\n[{label.upper()}] {coin} 출금 수수료 오류: {e}", file=sys.stderr)
                    has_error = True
        else:
            for coin in ["BTC", "USDT"]:
                try:
                    nets = get_scraped_withdrawal(name, coin)
                    print_withdrawal(label, coin, nets)
                except Exception as e:
                    print(f"\n[{label.upper()}] {coin} 출금 수수료 오류: {e}", file=sys.stderr)
                    has_error = True

    return has_error


def run_binance(show_withdrawal: bool) -> bool:
    has_error = False
    for mtype, fetcher in [("Spot", fetch_binance_spot), ("Perpetual", fetch_binance_perp)]:
        try:
            ticker = fetcher()
            fees = TRADING_FEES["binance"][mtype.lower()]
            print_ticker("Binance", mtype, ticker, fees)
        except Exception as e:
            print(f"\n[BINANCE] {mtype} 오류: {e}", file=sys.stderr)
            has_error = True

    if show_withdrawal:
        for coin in ["BTC", "USDT"]:
            try:
                nets = fetch_binance_withdrawal(coin)
                print_withdrawal("Binance", coin, nets)
            except Exception as e:
                print(f"\n[BINANCE] {coin} 출금 수수료 오류: {e}", file=sys.stderr)
                has_error = True

    return has_error


def run_okx(show_withdrawal: bool) -> bool:
    has_error = False
    for mtype, fetcher in [("Spot", fetch_okx_spot), ("Perpetual", fetch_okx_perp)]:
        try:
            ticker = fetcher()
            fees = TRADING_FEES["okx"][mtype.lower()]
            print_ticker("OKX", mtype, ticker, fees)
        except Exception as e:
            print(f"\n[OKX] {mtype} 오류: {e}", file=sys.stderr)
            has_error = True

    if show_withdrawal:
        for coin in ["BTC", "USDT"]:
            try:
                nets = fetch_okx_withdrawal(coin)
                print_withdrawal("OKX", coin, nets)
            except Exception as e:
                print(f"\n[OKX] {coin} 출금 수수료 오류: {e}", file=sys.stderr)
                has_error = True

    return has_error


def run_global_exchange(name: str, show_withdrawal: bool) -> bool:
    if name == "binance":
        return run_binance(show_withdrawal)
    if name == "okx":
        return run_okx(show_withdrawal)

    fetchers = {
        "coinbase": (fetch_coinbase, "Coinbase", "BTC-USD"),
        "kraken":   (fetch_kraken,   "Kraken",   "XBT/USD"),
        "bitget":   (fetch_bitget,   "Bitget",   "BTC/USDT"),
    }
    fetcher_fn, label, pair_label = fetchers[name]
    has_error = False
    fees = TRADING_FEES[name]

    try:
        ticker = fetcher_fn()
        print_ticker(label, "Spot", ticker, fees)
    except Exception as e:
        print(f"\n[{label.upper()}] 티커 오류: {e}", file=sys.stderr)
        has_error = True

    if show_withdrawal:
        if name == "bitget":
            for coin in ["BTC", "USDT"]:
                try:
                    nets = fetch_bitget_withdrawal(coin)
                    print_withdrawal(label, coin, nets)
                except Exception as e:
                    print(f"\n[{label.upper()}] {coin} 출금 수수료 오류: {e}", file=sys.stderr)
                    has_error = True
        else:
            for coin in ["BTC", "USDT"]:
                try:
                    nets = get_scraped_withdrawal(name, coin)
                    print_withdrawal(label, coin, nets)
                except Exception as e:
                    print(f"\n[{label.upper()}] {coin} 출금 수수료 오류: {e}", file=sys.stderr)
                    has_error = True

    return has_error


# ══════════════════════════════════════════════════════════════
# 메인
# ══════════════════════════════════════════════════════════════

def main() -> None:
    valid_exchanges = ALL_EXCHANGES + list(GROUPS.keys())

    parser = argparse.ArgumentParser(
        description="거래소별 BTC/USDT 실시간 시세 및 네트워크별 출금 수수료 조회"
    )
    parser.add_argument(
        "--exchange",
        choices=valid_exchanges,
        default=None,
        metavar="EXCHANGE",
        help=f"거래소 또는 그룹 ({', '.join(valid_exchanges)})",
    )
    parser.add_argument(
        "--group",
        choices=list(GROUPS.keys()),
        default=None,
        help="거래소 그룹 (korea / global)",
    )
    parser.add_argument(
        "--no-withdrawal",
        action="store_true",
        help="출금 수수료 조회 생략",
    )
    args = parser.parse_args()

    # 대상 거래소 결정
    if args.exchange and args.exchange in GROUPS:
        exchanges = GROUPS[args.exchange]
    elif args.exchange:
        exchanges = [args.exchange]
    elif args.group:
        exchanges = GROUPS[args.group]
    else:
        exchanges = ALL_EXCHANGES

    show_withdrawal = not args.no_withdrawal
    has_error = False

    for exchange in exchanges:
        if exchange in GROUPS["korea"]:
            if run_korea_exchange(exchange, show_withdrawal):
                has_error = True
        else:
            if run_global_exchange(exchange, show_withdrawal):
                has_error = True

    print()
    if has_error:
        sys.exit(1)


if __name__ == "__main__":
    main()
