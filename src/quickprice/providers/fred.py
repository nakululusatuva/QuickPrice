"""FRED DGS3MO adapter for the BOXX Treasury proxy yield."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from ._models import component, decimal_value, utc_datetime, yield_metric
from .base import (
    HttpProvider,
    MalformedResponse,
    ProviderUnavailable,
    UnsupportedInstrument,
    require_mapping,
)


class FredProvider(HttpProvider):
    name = "fred"
    base_url = "https://api.stlouisfed.org/fred/series/observations"
    series_id = "DGS3MO"
    expense_ratio_percentage_points = Decimal("0.1949")

    def __init__(
        self,
        api_key: str,
        *,
        series_bindings: Mapping[str, str] | None = None,
        expense_ratios: Mapping[str, Decimal | str | int | float] | None = None,
        method_bindings: Mapping[str, str] | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.api_key = api_key
        self.series_bindings = {
            symbol.strip().upper(): series.strip().upper()
            for symbol, series in (
                {"BOXX:USD": type(self).series_id} if series_bindings is None else series_bindings
            ).items()
        }
        requested_expenses = (
            {"BOXX:USD": type(self).expense_ratio_percentage_points}
            if expense_ratios is None
            else expense_ratios
        )
        self.expense_ratios = {
            symbol.strip().upper(): decimal_value(value)
            for symbol, value in requested_expenses.items()
        }
        if self.series_bindings.keys() != self.expense_ratios.keys():
            raise ValueError("FRED series and expense-ratio bindings must have the same symbols")
        requested_methods = method_bindings or {
            symbol: (
                "treasury_3m_proxy_minus_expense"
                if series == "DGS3MO"
                else "treasury_series_proxy_minus_expense"
            )
            for symbol, series in self.series_bindings.items()
        }
        self.method_bindings = {
            symbol.strip().upper(): method.strip().lower()
            for symbol, method in requested_methods.items()
        }
        if self.series_bindings.keys() != self.method_bindings.keys():
            raise ValueError("FRED method and series bindings must have the same symbols")
        if not set(self.method_bindings.values()).issubset(
            {
                "treasury_3m_proxy_minus_expense",
                "treasury_series_proxy_minus_expense",
            }
        ):
            raise ValueError("unsupported FRED yield method binding")

    async def get_yield(self, symbol: str):
        normalized = symbol.strip().upper()
        if normalized not in self.series_bindings:
            raise UnsupportedInstrument(self.name, f"unsupported yield symbol {normalized}")
        series_id = self.series_bindings[normalized]
        payload = await self._request_json(
            "GET",
            self.base_url,
            params={
                "series_id": series_id,
                "api_key": self.api_key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 20,
            },
        )
        document = require_mapping(payload, self.name)
        if "error_code" in document:
            raise ProviderUnavailable(self.name, "upstream returned an error")
        observations = document.get("observations")
        if not isinstance(observations, Sequence) or isinstance(observations, (str, bytes)):
            raise MalformedResponse(self.name, "observations must be an array")
        selected: tuple[Any, Decimal] | None = None
        for observation in observations:
            if not isinstance(observation, Mapping) or observation.get("value") in (None, ".", ""):
                continue
            try:
                selected = observation, decimal_value(observation["value"])
                break
            except KeyError, ValueError:
                continue
        if selected is None:
            raise ProviderUnavailable(self.name, "no valid Treasury-series observation")
        observation, treasury_yield = selected
        try:
            as_of = utc_datetime(observation["date"])
        except (KeyError, ValueError) as exc:
            raise MalformedResponse(self.name, "invalid observation date") from exc
        estimate = treasury_yield - self.expense_ratios[normalized]
        components = (
            component(
                symbol=series_id,
                provider=self.name,
                price=treasury_yield,
                as_of=as_of,
                feed="fred_daily",
                role=(
                    "treasury_3m_yield_percent"
                    if series_id == "DGS3MO"
                    else "treasury_yield_percent"
                ),
            ),
        )
        return yield_metric(
            symbol=normalized,
            value=estimate,
            as_of=as_of,
            method=self.method_bindings[normalized],
            provider=self.name,
            is_proxy=True,
            components=components,
        )
