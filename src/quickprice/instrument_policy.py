"""Shared controlled identifiers used by catalog and provider compilation."""

from __future__ import annotations

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

__all__ = [
    "COINGECKO_SUPPORTED_QUOTE_ASSETS",
    "LATEST_REGULAR_CASH_DIVIDEND_STRATEGY",
    "SUPPORTED_DIVIDEND_STRATEGIES",
    "SUPPORTED_TREASURY_PROXY_FRED_SERIES",
    "TREASURY_3M_FRED_SERIES",
]
