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

    def __init__(self, api_key: str, **kwargs):
        super().__init__(**kwargs)
        self.api_key = api_key

    async def get_yield(self, symbol: str):
        normalized = symbol.strip().upper()
        if normalized != "BOXX:USD":
            raise UnsupportedInstrument(self.name, f"unsupported yield symbol {normalized}")
        payload = await self._request_json(
            "GET",
            self.base_url,
            params={
                "series_id": self.series_id,
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
            raise ProviderUnavailable(self.name, "no valid DGS3MO observation")
        observation, treasury_yield = selected
        try:
            as_of = utc_datetime(observation["date"])
        except (KeyError, ValueError) as exc:
            raise MalformedResponse(self.name, "invalid observation date") from exc
        estimate = treasury_yield - self.expense_ratio_percentage_points
        components = (
            component(
                symbol=self.series_id,
                provider=self.name,
                price=treasury_yield,
                as_of=as_of,
                feed="fred_daily",
                role="treasury_3m_yield_percent",
            ),
        )
        return yield_metric(
            symbol=normalized,
            value=estimate,
            as_of=as_of,
            method="treasury_3m_proxy_minus_expense",
            provider=self.name,
            is_proxy=True,
            components=components,
        )
