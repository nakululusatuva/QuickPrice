"""Shared controlled identifiers used by catalog and provider compilation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .domain import RewardAccrualMode
from .equities import DIVIDEND_FREQUENCIES, DIVIDEND_SYMBOLS, LISTED_TICKERS
from .fx import FX_HUB_SYMBOLS, FX_SYMBOLS

LATEST_REGULAR_CASH_DIVIDEND_STRATEGY = "latest_regular_cash_annualized_x4"
SUPPORTED_DIVIDEND_STRATEGIES = frozenset({LATEST_REGULAR_CASH_DIVIDEND_STRATEGY})

# The public yield method is explicitly a three-month Treasury proxy. Allowing
# a different maturity under that method name would silently change its
# financial meaning.
TREASURY_3M_FRED_SERIES = "DGS3MO"
SUPPORTED_TREASURY_PROXY_FRED_SERIES = frozenset(
    {"DGS1MO", TREASURY_3M_FRED_SERIES, "DGS6MO", "DGS1"}
)

# CoinGecko's adapters normalize the quote into one of these supported output
# currencies. A coin id alone cannot safely imply an arbitrary quote currency.
COINGECKO_SUPPORTED_QUOTE_ASSETS = frozenset({"USD", "USDC"})


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_deep_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class BuiltinSyntheticRecipePolicy:
    symbol: str
    operation: str
    inputs: tuple[str, ...]
    max_skew_seconds: float
    input_max_age_seconds: tuple[float | None, ...] = ()
    provider_name: str = "synthetic"


@dataclass(frozen=True, slots=True)
class BuiltinEthereumExchangeRatePolicy:
    symbol: str
    index_symbol: str
    underlying_asset: str
    contract_address: str
    chain_id: int
    call_data: str
    event_topic: str


@dataclass(frozen=True, slots=True)
class BuiltinStakingRatioPolicy:
    symbol: str
    staking_pair: str
    underlying_pair: str
    underlying_asset: str
    accrual_mode: RewardAccrualMode


@dataclass(frozen=True, slots=True)
class BuiltinStakingBackingQuotePolicy:
    symbol: str
    ratio_symbol: str
    underlying_pair: str
    underlying_asset: str
    ratio_kind: str
    constant_ratio: str = "1"
    contract_address: str | None = None
    chain_id: int | None = None
    call_data: str | None = None
    scale: str = str(10**18)


BUILTIN_BINANCE_SYMBOLS = _deep_freeze(
    {
        "BTC:USDC": "BTCUSDC",
        "ETH:USDC": "ETHUSDC",
        "SOL:USDC": "SOLUSDC",
        "POL:USDC": "POLUSDC",
        "BNB:USDC": "BNBUSDC",
        "TRX:USDC": "TRXUSDC",
        "WBETH:ETH": "WBETHETH",
        "WBETH:USDT": "WBETHUSDT",
        "USDC:USDT": "USDCUSDT",
    }
)
BUILTIN_BINANCE_MIDPOINT_SYMBOLS = frozenset({"WBETH:ETH", "WBETH:USDT", "USDC:USDT"})
BUILTIN_KRAKEN_SYMBOLS = _deep_freeze(
    {
        "BTC:USDC": "XBTUSDC",
        "ETH:USDC": "ETHUSDC",
        "SOL:USDC": "SOLUSDC",
        "XMR:USDC": "XMRUSDC",
        "BNB:USDC": "BNBUSDC",
    }
)
BUILTIN_KRAKEN_MAX_QUOTE_AGE_SECONDS = _deep_freeze({"XMR:USDC": 300.0})
BUILTIN_COINGECKO_COIN_IDS = _deep_freeze(
    {
        "BTC:USDC": "bitcoin",
        "ETH:USDC": "ethereum",
        "SOL:USDC": "solana",
        "XMR:USDC": "monero",
        "POL:USDC": "polygon-ecosystem-token",
        "BNB:USDC": "binancecoin",
        "TRX:USDC": "tron",
        "WBETH:USDC": "wrapped-beacon-eth",
        "BETH:USDC": "okx-beth",
        "STETH:USDC": "staked-ether",
        "WSTETH:USDC": "wrapped-steth",
        "ETH:USD": "ethereum",
        "STETH:USD": "staked-ether",
        "WSTETH:USD": "wrapped-steth",
    }
)
BUILTIN_COINGECKO_HISTORY_SYMBOLS = frozenset(
    {
        "BETH:USDC",
        "STETH:USDC",
        "WSTETH:USDC",
        "ETH:USD",
        "STETH:USD",
        "WSTETH:USD",
    }
)
BUILTIN_COINGECKO_COMPONENT_SKEW_SECONDS = _deep_freeze(
    {
        "WBETH:USDC": 60.0,
        "BETH:USDC": 60.0,
        "STETH:USDC": 60.0,
        "WSTETH:USDC": 60.0,
    }
)
BUILTIN_COINGECKO_NORMALIZATION_COIN_ID = "usd-coin"
BUILTIN_COINGECKO_NORMALIZATION_COMPONENT_SYMBOL = "USDC:USD"

BUILTIN_OKX_MARKETS = _deep_freeze(
    {
        "BETH:ETH": "BETH-ETH",
        "ETH:USDC": "ETH-USDC",
        "BETH:USDT": "BETH-USDT",
        "USDC:USDT": "USDC-USDT",
    }
)
BUILTIN_OKX_INTERNAL_ALIASES = _deep_freeze(
    {
        "OKX_BETH:ETH": "BETH:ETH",
        "OKX_ETH:USDC": "ETH:USDC",
        "OKX_BETH:USDT": "BETH:USDT",
        "OKX_USDC:USDT": "USDC:USDT",
    }
)
BUILTIN_OKX_YIELD_SYMBOLS = _deep_freeze(
    {
        "BETH:USDC": {
            "component_symbol": "BETH",
            "underlying_asset": "ETH",
            "accrual_mode": RewardAccrualMode.DISTRIBUTED_UNITS,
            "method": "okx_beth_provider_reported_apr",
        }
    }
)

BUILTIN_LISTED_PROVIDER_SYMBOLS = _deep_freeze(
    {
        provider: MappingProxyType(dict(LISTED_TICKERS))
        for provider in ("alpaca", "finnhub", "twelve_data", "alpha_vantage")
    }
)
BUILTIN_ALPACA_DIVIDEND_FREQUENCIES = _deep_freeze(dict(DIVIDEND_FREQUENCIES))
BUILTIN_ALPACA_ALLOWED_DIVIDEND_SUBTYPES = _deep_freeze(
    {symbol: (("", "interest") if symbol == "SGOV:USD" else ("",)) for symbol in DIVIDEND_SYMBOLS}
)
BUILTIN_FX_PROVIDER_SYMBOLS = _deep_freeze(
    {symbol: symbol.replace(":", "/") for symbol in FX_HUB_SYMBOLS}
)
BUILTIN_TWELVE_FX_CACHE_FLOOR_SECONDS = _deep_freeze(
    {symbol: 240.0 if symbol == "USD:CNH" else 900.0 for symbol in FX_HUB_SYMBOLS}
)
BUILTIN_TWELVE_FX_POLL_SETTING = _deep_freeze(
    {
        symbol: ("usd_cnh_poll_seconds" if symbol == "USD:CNH" else "usd_hkd_poll_seconds")
        for symbol in FX_HUB_SYMBOLS
    }
)
BUILTIN_FX_HUB_MAX_AGE_SECONDS = _deep_freeze(
    {symbol: 300.0 if symbol == "USD:CNH" else 1_200.0 for symbol in FX_HUB_SYMBOLS}
)
BUILTIN_FX_REQUIREMENTS = _deep_freeze(
    {
        symbol: (
            (f"USD:{symbol.split(':', 1)[0]}",)
            if symbol.split(":", 1)[1] == "USD"
            else (
                f"USD:{symbol.split(':', 1)[1]}",
                f"USD:{symbol.split(':', 1)[0]}",
            )
        )
        for symbol in FX_SYMBOLS
        if not symbol.startswith("USD:")
    }
)

BUILTIN_SYNTHETIC_RECIPES = _deep_freeze(
    {
        "wbeth_primary": BuiltinSyntheticRecipePolicy(
            "WBETH:USDC",
            "multiply",
            ("WBETH:ETH", "ETH:USDC"),
            2.0,
            (15.0, 15.0),
            "synthetic_binance",
        ),
        "wbeth_alternate": BuiltinSyntheticRecipePolicy(
            "WBETH:USDC",
            "divide",
            ("WBETH:USDT", "USDC:USDT"),
            2.0,
            (15.0, 15.0),
            "synthetic_binance",
        ),
        "beth_primary": BuiltinSyntheticRecipePolicy(
            "BETH:USDC",
            "multiply",
            ("OKX_BETH:ETH", "OKX_ETH:USDC"),
            2.0,
            (15.0, 15.0),
            "synthetic_okx",
        ),
        "beth_alternate": BuiltinSyntheticRecipePolicy(
            "BETH:USDC",
            "divide",
            ("OKX_BETH:USDT", "OKX_USDC:USDT"),
            2.0,
            (15.0, 15.0),
            "synthetic_okx",
        ),
    }
)
BUILTIN_ETHEREUM_EXCHANGE_RATE_POLICIES = (
    BuiltinEthereumExchangeRatePolicy(
        symbol="WBETH:USDC",
        index_symbol="WBETH:ETH",
        underlying_asset="ETH",
        contract_address="0xa2e3356610840701bdf5611a53974510ae27e2e1",
        chain_id=1,
        call_data="0x3ba0b9a9",
        event_topic="0x0b4e9390054347e2a16d95fd8376311b0d2deedecba526e9742bcaa40b059f0b",
    ),
)
BUILTIN_STAKING_BACKING_QUOTE_POLICIES = (
    BuiltinStakingBackingQuotePolicy(
        symbol="BETH:USDC",
        ratio_symbol="BETH:ETH",
        underlying_pair="ETH:USDC",
        underlying_asset="ETH",
        ratio_kind="constant",
    ),
    BuiltinStakingBackingQuotePolicy(
        symbol="STETH:USDC",
        ratio_symbol="STETH:ETH",
        underlying_pair="ETH:USDC",
        underlying_asset="ETH",
        ratio_kind="constant",
    ),
    BuiltinStakingBackingQuotePolicy(
        symbol="WSTETH:USDC",
        ratio_symbol="WSTETH:STETH",
        underlying_pair="ETH:USDC",
        underlying_asset="ETH",
        ratio_kind="ethereum_call",
        contract_address="0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0",
        chain_id=1,
        call_data="0x035faf82",
    ),
)
BUILTIN_BINANCE_STAKING_RATE_POLICIES = _deep_freeze(
    {
        "WBETH:USDC": {
            "index_symbol": "WBETH:ETH",
            "underlying_asset": "ETH",
            "accrual_mode": RewardAccrualMode.VALUE_ACCRUING,
            "method": "binance_wbeth_rate_history_apr",
        }
    }
)
BUILTIN_LIDO_YIELD_POLICIES = _deep_freeze(
    {
        "STETH:USDC": {
            "provider_asset": "steth",
            "underlying_asset": "ETH",
            "accrual_mode": RewardAccrualMode.REBASING_BALANCE,
        },
        "WSTETH:USDC": {
            "provider_asset": "steth",
            "underlying_asset": "ETH",
            "accrual_mode": RewardAccrualMode.VALUE_ACCRUING,
        },
    }
)
BUILTIN_LIDO_CONTRACT_ADDRESS = "0xae7ab96520de3a18e5e111b5eaab095312d7fe84"
BUILTIN_LIDO_CHAIN_ID = 1
BUILTIN_STAKING_RATIO_POLICIES = (
    BuiltinStakingRatioPolicy(
        symbol="WBETH:USDC",
        staking_pair="WBETH:USDC",
        underlying_pair="ETH:USDC",
        underlying_asset="ETH",
        accrual_mode=RewardAccrualMode.VALUE_ACCRUING,
    ),
    BuiltinStakingRatioPolicy(
        symbol="WSTETH:USDC",
        staking_pair="WSTETH:USD",
        underlying_pair="ETH:USD",
        underlying_asset="ETH",
        accrual_mode=RewardAccrualMode.VALUE_ACCRUING,
    ),
)
BUILTIN_FRED_POLICIES = _deep_freeze(
    {
        "BOXX:USD": {
            "series": TREASURY_3M_FRED_SERIES,
            "expense_ratio_percentage_points": "0.1949",
            "method": "treasury_3m_proxy_minus_expense",
            "component_role": "treasury_3m_yield_percent",
        }
    }
)

BUILTIN_PROVIDER_ROUTES = _deep_freeze(
    {
        "BTC:USDC": {
            "quote": ("binance", "kraken", "coingecko"),
            "history": ("binance", "kraken"),
        },
        "ETH:USDC": {
            "quote": ("binance", "kraken", "coingecko"),
            "history": ("binance", "kraken"),
        },
        "SOL:USDC": {
            "quote": ("binance", "kraken", "coingecko"),
            "history": ("binance", "kraken"),
        },
        "BNB:USDC": {
            "quote": ("binance", "kraken", "coingecko"),
            "history": ("binance", "kraken"),
        },
        "POL:USDC": {
            "quote": ("binance", "coingecko"),
            "history": ("binance",),
        },
        "TRX:USDC": {
            "quote": ("binance", "coingecko"),
            "history": ("binance",),
        },
        "XMR:USDC": {
            "quote": ("kraken", "coingecko"),
            "history": ("kraken",),
        },
        "WBETH:USDC": {
            "quote": (
                "synthetic_wbeth_primary",
                "synthetic_wbeth_alternate",
                "coingecko",
            ),
            "history": (
                "synthetic_wbeth_history_primary",
                "synthetic_wbeth_history_alternate",
            ),
            "yield": (
                "binance_wbeth_rate",
                "ethereum_exchange_rate",
                "staking_market_ratio_proxy",
            ),
        },
        "BETH:USDC": {
            "quote": (
                "synthetic_beth_primary",
                "synthetic_beth_alternate",
                "coingecko",
                "staking_backing_proxy",
            ),
            "history": (
                "synthetic_beth_history_primary",
                "synthetic_beth_history_alternate",
                "coingecko",
            ),
            "yield": ("okx_beth_yield",),
        },
        "STETH:USDC": {
            "quote": ("coingecko", "staking_backing_proxy"),
            "history": ("coingecko",),
            "yield": ("lido",),
        },
        "WSTETH:USDC": {
            "quote": ("coingecko", "staking_backing_proxy"),
            "history": ("coingecko",),
            "yield": ("lido", "staking_market_ratio_proxy"),
        },
        **{
            symbol: {
                "quote": ("alpaca", "finnhub", "twelve_data", "alpha_vantage"),
                "history": ("alpaca", "twelve_data", "alpha_vantage"),
                **({"dividend": ("alpaca",)} if symbol in DIVIDEND_SYMBOLS else {}),
                **({"yield": ("fred",)} if symbol in BUILTIN_FRED_POLICIES else {}),
            }
            for symbol in LISTED_TICKERS
        },
        **{
            symbol: (
                {
                    "quote": ("twelve_data", "alpha_vantage"),
                    "history": ("twelve_data", "alpha_vantage"),
                }
                if symbol in FX_HUB_SYMBOLS
                else {
                    "quote": ("synthetic_fx",),
                    "history": ("synthetic_fx_history",),
                }
            )
            for symbol in FX_SYMBOLS
        },
    }
)


def builtin_provider_symbols(symbol: str) -> Mapping[str, str]:
    canonical = symbol.strip().upper()
    bindings: dict[str, str] = {}
    for provider, provider_bindings in (
        ("binance", BUILTIN_BINANCE_SYMBOLS),
        ("kraken", BUILTIN_KRAKEN_SYMBOLS),
        ("coingecko", BUILTIN_COINGECKO_COIN_IDS),
        *BUILTIN_LISTED_PROVIDER_SYMBOLS.items(),
        ("twelve_data", BUILTIN_FX_PROVIDER_SYMBOLS),
        ("alpha_vantage", BUILTIN_FX_PROVIDER_SYMBOLS),
    ):
        vendor_symbol = provider_bindings.get(canonical)
        if vendor_symbol is not None:
            bindings[provider] = vendor_symbol
    fred = BUILTIN_FRED_POLICIES.get(canonical)
    if fred is not None:
        bindings["fred"] = str(fred["series"])
    return MappingProxyType(bindings)


__all__ = [
    "BUILTIN_ALPACA_ALLOWED_DIVIDEND_SUBTYPES",
    "BUILTIN_ALPACA_DIVIDEND_FREQUENCIES",
    "BUILTIN_BINANCE_MIDPOINT_SYMBOLS",
    "BUILTIN_BINANCE_STAKING_RATE_POLICIES",
    "BUILTIN_BINANCE_SYMBOLS",
    "BUILTIN_COINGECKO_COIN_IDS",
    "BUILTIN_COINGECKO_COMPONENT_SKEW_SECONDS",
    "BUILTIN_COINGECKO_HISTORY_SYMBOLS",
    "BUILTIN_COINGECKO_NORMALIZATION_COIN_ID",
    "BUILTIN_COINGECKO_NORMALIZATION_COMPONENT_SYMBOL",
    "BUILTIN_ETHEREUM_EXCHANGE_RATE_POLICIES",
    "BUILTIN_FRED_POLICIES",
    "BUILTIN_FX_HUB_MAX_AGE_SECONDS",
    "BUILTIN_FX_PROVIDER_SYMBOLS",
    "BUILTIN_FX_REQUIREMENTS",
    "BUILTIN_KRAKEN_MAX_QUOTE_AGE_SECONDS",
    "BUILTIN_KRAKEN_SYMBOLS",
    "BUILTIN_LIDO_CHAIN_ID",
    "BUILTIN_LIDO_CONTRACT_ADDRESS",
    "BUILTIN_LIDO_YIELD_POLICIES",
    "BUILTIN_LISTED_PROVIDER_SYMBOLS",
    "BUILTIN_OKX_INTERNAL_ALIASES",
    "BUILTIN_OKX_MARKETS",
    "BUILTIN_OKX_YIELD_SYMBOLS",
    "BUILTIN_PROVIDER_ROUTES",
    "BUILTIN_STAKING_BACKING_QUOTE_POLICIES",
    "BUILTIN_STAKING_RATIO_POLICIES",
    "BUILTIN_SYNTHETIC_RECIPES",
    "BUILTIN_TWELVE_FX_CACHE_FLOOR_SECONDS",
    "BUILTIN_TWELVE_FX_POLL_SETTING",
    "COINGECKO_SUPPORTED_QUOTE_ASSETS",
    "LATEST_REGULAR_CASH_DIVIDEND_STRATEGY",
    "SUPPORTED_DIVIDEND_STRATEGIES",
    "SUPPORTED_TREASURY_PROXY_FRED_SERIES",
    "TREASURY_3M_FRED_SERIES",
    "BuiltinEthereumExchangeRatePolicy",
    "BuiltinStakingRatioPolicy",
    "BuiltinSyntheticRecipePolicy",
    "builtin_provider_symbols",
]
