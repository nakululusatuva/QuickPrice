"""Canonical currency universe and USD-hub topology for built-in FX pairs."""

from __future__ import annotations

from types import MappingProxyType

FX_CURRENCIES = ("USD", "EUR", "GBP", "HKD", "SGD", "CNH")
FX_CURRENCY_NAMES = MappingProxyType(
    {
        "USD": "United States Dollar",
        "EUR": "Euro",
        "GBP": "British Pound Sterling",
        "HKD": "Hong Kong Dollar",
        "SGD": "Singapore Dollar",
        "CNH": "Offshore Chinese Yuan",
    }
)
FX_HUB_SYMBOLS = tuple(f"USD:{currency}" for currency in FX_CURRENCIES if currency != "USD")
FX_SYMBOLS = tuple(
    f"{base}:{quote}" for base in FX_CURRENCIES for quote in FX_CURRENCIES if base != quote
)


def split_fx_symbol(symbol: str) -> tuple[str, str]:
    """Return a validated built-in currency pair."""

    normalized = symbol.strip().upper()
    try:
        base, quote = normalized.split(":", 1)
    except ValueError as exc:
        raise ValueError(f"invalid FX symbol {normalized}") from exc
    if base == quote or base not in FX_CURRENCIES or quote not in FX_CURRENCIES:
        raise ValueError(f"unsupported FX symbol {normalized}")
    return base, quote


def hub_symbol(currency: str) -> str:
    """Return the vendor-backed USD spoke required for a non-USD currency."""

    normalized = currency.strip().upper()
    if normalized == "USD" or normalized not in FX_CURRENCIES:
        raise ValueError(f"no USD hub symbol for {normalized}")
    return f"USD:{normalized}"


def fx_hub_requirements(symbol: str) -> tuple[str, ...]:
    """Return only root USD spokes, ordered as numerator then denominator."""

    base, quote = split_fx_symbol(symbol)
    if base == "USD":
        return (hub_symbol(quote),)
    if quote == "USD":
        return (hub_symbol(base),)
    return (hub_symbol(quote), hub_symbol(base))


__all__ = [
    "FX_CURRENCIES",
    "FX_CURRENCY_NAMES",
    "FX_HUB_SYMBOLS",
    "FX_SYMBOLS",
    "fx_hub_requirements",
    "hub_symbol",
    "split_fx_symbol",
]
