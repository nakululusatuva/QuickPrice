"""Strict, immutable models for the administrator-managed instrument catalog.

The catalog is deliberately data-only. It cannot carry network locations,
Python import paths, request headers, or executable expressions. Provider
descriptors and route compilation remain trusted application code.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    field_validator,
    model_validator,
)

from .domain import RewardAccrualMode
from .instrument_policy import (
    SUPPORTED_DIVIDEND_STRATEGIES,
    SUPPORTED_TREASURY_PROXY_FRED_SERIES,
    TREASURY_3M_FRED_SERIES,
)
from .plugin_api import (
    AssetClass,
    InstrumentPlugin,
    InstrumentSpec,
    MarketCalendar,
    YieldStrategy,
)

CATALOG_SCHEMA_VERSION = 2
MAX_CUSTOM_INSTRUMENTS = 2_000
MAX_PROVIDER_CHAIN = 4
MAX_SYNTHETIC_INPUTS = 2
MAX_SYNTHETIC_DEPTH = 4
MAX_CATALOG_IMPORT_BYTES = 8 * 1024 * 1024

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]*:[A-Z0-9][A-Z0-9._-]*$")
_ASSET_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,63}$")
_PROVIDER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_VENDOR_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_TOKEN_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_CAPABILITIES = ("quote", "history", "dividend", "yield")

SafeFloat = Annotated[float, Field(ge=0, le=1_000_000)]
PositiveInterval = Annotated[float, Field(ge=0.25, le=604_800)]
PositiveAge = Annotated[float, Field(gt=0, le=604_800)]


class CatalogValidationError(ValueError):
    """Raised when an untrusted catalog payload violates the managed schema."""


class InstrumentOwnership(StrEnum):
    BUILTIN = "builtin"
    CUSTOM = "custom"


class SyntheticOperation(StrEnum):
    INVERSE = "inverse"
    MULTIPLY = "multiply"
    DIVIDE = "divide"


class _CatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)


def _require_safe_text(value: str, *, field_name: str, maximum: int) -> str:
    if not value or value != value.strip() or len(value) > maximum:
        raise ValueError(
            f"{field_name} must be non-empty, trimmed, and at most {maximum} characters"
        )
    if any(ord(character) < 32 for character in value) or "<" in value or ">" in value:
        raise ValueError(f"{field_name} contains an unsafe character")
    return value


def _canonical_symbol(value: str, *, field_name: str = "symbol") -> str:
    normalized = value.strip().upper().replace("/", ":")
    if value != normalized or not _SYMBOL_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a canonical BASE:QUOTE symbol")
    return value


class ProviderSymbolBinding(_CatalogModel):
    """A safe vendor identifier associated with one installed provider."""

    provider: StrictStr
    symbol: StrictStr

    @field_validator("provider")
    @classmethod
    def _validate_provider(cls, value: str) -> str:
        if value != value.strip().lower() or not _PROVIDER_PATTERN.fullmatch(value):
            raise ValueError("provider must be a lowercase installed-provider identifier")
        return value

    @field_validator("symbol")
    @classmethod
    def _validate_vendor_symbol(cls, value: str) -> str:
        if value != value.strip() or not _VENDOR_SYMBOL_PATTERN.fullmatch(value):
            raise ValueError("vendor symbol contains unsupported characters")
        lowered = value.lower()
        if "://" in lowered or lowered.startswith(("http:", "https:", "file:")):
            raise ValueError("vendor symbol cannot be a URL")
        return value


class CapabilityRoute(_CatalogModel):
    """An ordered provider fallback chain for one provider capability."""

    capability: Literal["quote", "history", "dividend", "yield"]
    providers: tuple[StrictStr, ...]

    @field_validator("providers")
    @classmethod
    def _validate_providers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not 1 <= len(value) <= MAX_PROVIDER_CHAIN:
            raise ValueError(f"provider chain must contain 1 to {MAX_PROVIDER_CHAIN} providers")
        if len(set(value)) != len(value):
            raise ValueError("provider chain cannot contain duplicates")
        for provider in value:
            if provider != provider.strip().lower() or not _PROVIDER_PATTERN.fullmatch(provider):
                raise ValueError("provider chain contains an invalid provider identifier")
        return value


class HistoryCollectionPolicy(_CatalogModel):
    enabled: StrictBool = True
    poll_seconds: Annotated[float, Field(ge=1, le=86_400)] | None = None
    backfill_days: Annotated[int, Field(ge=1, le=3_650)] | None = None


class IncomePolicy(_CatalogModel):
    """Controlled income semantics; no executable formula is accepted."""

    yield_strategy: YieldStrategy | None = None
    dividend_strategy: StrictStr | None = None
    reward_accrual_mode: RewardAccrualMode | None = None
    underlying_asset: StrictStr | None = None
    fred_series: StrictStr | None = None
    expense_ratio_percent: Annotated[float, Field(ge=0, le=25)] | None = None
    fallback_ratio_days: Annotated[int, Field(ge=7, le=365)] | None = None

    @field_validator("dividend_strategy")
    @classmethod
    def _validate_dividend_strategy(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in SUPPORTED_DIVIDEND_STRATEGIES:
            raise ValueError("dividend strategy is not supported")
        return value

    @field_validator("underlying_asset")
    @classmethod
    def _validate_underlying(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value != value.strip().upper() or not _ASSET_PATTERN.fullmatch(value):
            raise ValueError("underlying asset must be an uppercase asset identifier")
        return value

    @field_validator("fred_series")
    @classmethod
    def _validate_fred_series(cls, value: str | None) -> str | None:
        if value is not None and value not in SUPPORTED_TREASURY_PROXY_FRED_SERIES:
            raise ValueError("FRED series is not in the controlled Treasury allowlist")
        return value

    @model_validator(mode="after")
    def _validate_strategy_parameters(self) -> Self:
        treasury = self.yield_strategy in {
            YieldStrategy.TREASURY_3M_PROXY_MINUS_EXPENSE,
            YieldStrategy.TREASURY_PROXY_MINUS_EXPENSE,
        }
        if treasury and (self.fred_series is None or self.expense_ratio_percent is None):
            raise ValueError("Treasury proxy yield requires a FRED series and expense ratio")
        if not treasury and (
            self.fred_series is not None or self.expense_ratio_percent is not None
        ):
            raise ValueError("Treasury proxy parameters require the Treasury proxy yield strategy")
        if (
            self.fallback_ratio_days is not None
            and self.reward_accrual_mode is not RewardAccrualMode.VALUE_ACCRUING
        ):
            raise ValueError("ratio fallback is only valid for value-accruing tokens")
        if (
            self.yield_strategy is YieldStrategy.TREASURY_3M_PROXY_MINUS_EXPENSE
            and self.fred_series != TREASURY_3M_FRED_SERIES
        ):
            raise ValueError("the three-month Treasury strategy requires DGS3MO")
        return self


class SyntheticRecipeDefinition(_CatalogModel):
    """A bounded declarative expression over one or two catalog symbols."""

    operation: SyntheticOperation
    inputs: tuple[StrictStr, ...]
    max_skew_seconds: SafeFloat = 2.0
    input_max_age_seconds: tuple[PositiveAge | None, ...] = ()

    @field_validator("inputs")
    @classmethod
    def _validate_inputs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for symbol in value:
            _canonical_symbol(symbol, field_name="synthetic input")
        if len(set(value)) != len(value):
            raise ValueError("synthetic inputs cannot contain duplicates")
        return value

    @model_validator(mode="after")
    def _validate_arity(self) -> Self:
        required = 1 if self.operation is SyntheticOperation.INVERSE else 2
        if len(self.inputs) != required or len(self.inputs) > MAX_SYNTHETIC_INPUTS:
            raise ValueError(f"{self.operation.value} requires exactly {required} input symbols")
        if self.input_max_age_seconds and len(self.input_max_age_seconds) != len(self.inputs):
            raise ValueError("synthetic input age limits must match the input count")
        return self


class ManagedInstrumentDefinition(_CatalogModel):
    """One complete provider-neutral instrument declaration."""

    id: StrictStr
    symbol: StrictStr
    base: StrictStr
    quote: StrictStr
    name: StrictStr
    description: StrictStr
    asset_class: AssetClass
    asset_type: StrictStr
    price_basis: StrictStr
    change_basis: StrictStr = "unadjusted_market_price"
    ownership: InstrumentOwnership
    enabled: StrictBool = True
    archived: StrictBool = False
    aliases: tuple[StrictStr, ...] = ()
    market_calendar: MarketCalendar = MarketCalendar.ALWAYS_OPEN
    quote_poll_seconds: PositiveInterval = 5.0
    stale_after_seconds: Annotated[float, Field(ge=1, le=604_800)] = 10.0
    history: HistoryCollectionPolicy = Field(default_factory=HistoryCollectionPolicy)
    routes: tuple[CapabilityRoute, ...] = ()
    provider_symbols: tuple[ProviderSymbolBinding, ...] = ()
    income: IncomePolicy | None = None
    synthetic: SyntheticRecipeDefinition | None = None

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if value != value.strip().lower() or not _ID_PATTERN.fullmatch(value):
            raise ValueError("instrument id must be a lowercase stable identifier")
        return value

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        return _canonical_symbol(value)

    @field_validator("base", "quote")
    @classmethod
    def _validate_asset(cls, value: str) -> str:
        if value != value.strip().upper() or not _ASSET_PATTERN.fullmatch(value):
            raise ValueError("base and quote must be uppercase asset identifiers")
        return value

    @field_validator("name", "description")
    @classmethod
    def _validate_human_text(cls, value: str, info: Any) -> str:
        maximum = 160 if info.field_name == "name" else 1_000
        return _require_safe_text(value, field_name=info.field_name, maximum=maximum)

    @field_validator("asset_type", "price_basis", "change_basis")
    @classmethod
    def _validate_classification_token(cls, value: str, info: Any) -> str:
        if value != value.strip().lower() or not _TOKEN_PATTERN.fullmatch(value):
            raise ValueError(f"{info.field_name} must be a lowercase controlled identifier")
        return value

    @field_validator("aliases")
    @classmethod
    def _validate_aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 32 or len(set(value)) != len(value):
            raise ValueError("instrument aliases are duplicated or exceed the limit")
        for alias in value:
            _canonical_symbol(alias, field_name="alias")
        return value

    @field_validator("routes")
    @classmethod
    def _validate_routes(cls, value: tuple[CapabilityRoute, ...]) -> tuple[CapabilityRoute, ...]:
        capabilities = tuple(item.capability for item in value)
        if len(set(capabilities)) != len(capabilities):
            raise ValueError("instrument cannot declare duplicate capability routes")
        return value

    @field_validator("provider_symbols")
    @classmethod
    def _validate_bindings(
        cls, value: tuple[ProviderSymbolBinding, ...]
    ) -> tuple[ProviderSymbolBinding, ...]:
        providers = tuple(item.provider for item in value)
        if len(set(providers)) != len(providers):
            raise ValueError("instrument cannot declare duplicate provider symbols")
        return value

    @model_validator(mode="after")
    def _validate_definition(self) -> Self:
        if self.ownership is InstrumentOwnership.BUILTIN and not self.id.startswith("builtin-"):
            raise ValueError("built-in instrument id must use the builtin namespace")
        if self.ownership is InstrumentOwnership.CUSTOM and not self.id.startswith("custom-"):
            raise ValueError("custom instrument id must use the custom namespace")
        if self.symbol != f"{self.base}:{self.quote}":
            raise ValueError("instrument symbol must match base and quote")
        if self.symbol in self.aliases:
            raise ValueError("instrument cannot alias its canonical symbol")
        if self.archived and self.enabled:
            raise ValueError("an archived instrument cannot be enabled")
        if self.stale_after_seconds < self.quote_poll_seconds:
            raise ValueError("stale threshold cannot be shorter than quote polling")
        is_staking = "staking" in self.asset_type
        if self.asset_class is AssetClass.BOND and (
            self.income is None or self.income.yield_strategy is None
        ):
            raise ValueError("bond instruments require a yield strategy")
        if is_staking and (
            self.income is None
            or self.income.yield_strategy is None
            or self.income.reward_accrual_mode is None
            or self.income.underlying_asset is None
        ):
            raise ValueError(
                "staking instruments require yield, underlying asset, and accrual mode"
            )
        if self.income is not None and self.income.reward_accrual_mode is not None:
            if self.income.yield_strategy is None or self.income.underlying_asset is None:
                raise ValueError("staking income policy is incomplete")
        if self.synthetic is not None and self.symbol in self.synthetic.inputs:
            raise ValueError("synthetic instrument cannot directly depend on itself")
        return self

    @classmethod
    def from_instrument_spec(
        cls,
        item: InstrumentSpec,
        *,
        instrument_id: str,
        ownership: InstrumentOwnership = InstrumentOwnership.BUILTIN,
        enabled: bool = True,
        routes: Iterable[CapabilityRoute] = (),
        provider_symbols: Iterable[ProviderSymbolBinding] = (),
        synthetic: SyntheticRecipeDefinition | None = None,
    ) -> Self:
        income = None
        if (
            item.yield_strategy is not None
            or item.dividend_strategy is not None
            or item.reward_accrual_mode is not None
            or item.underlying_asset is not None
        ):
            fred_series = None
            expense_ratio_percent = None
            fallback_ratio_days = None
            if item.yield_strategy is YieldStrategy.TREASURY_3M_PROXY_MINUS_EXPENSE:
                fred_series = "DGS3MO"
                expense_ratio_percent = 0.1949
            if item.reward_accrual_mode is RewardAccrualMode.VALUE_ACCRUING:
                fallback_ratio_days = 30
            income = IncomePolicy(
                yield_strategy=item.yield_strategy,
                dividend_strategy=item.dividend_strategy,
                reward_accrual_mode=item.reward_accrual_mode,
                underlying_asset=item.underlying_asset,
                fred_series=fred_series,
                expense_ratio_percent=expense_ratio_percent,
                fallback_ratio_days=fallback_ratio_days,
            )
        return cls(
            id=instrument_id,
            symbol=item.symbol,
            base=item.base,
            quote=item.quote,
            name=item.name,
            description=item.description,
            asset_class=item.asset_class,
            asset_type=item.asset_type,
            price_basis=item.price_basis,
            change_basis=item.change_basis,
            ownership=ownership,
            enabled=enabled,
            aliases=item.aliases,
            market_calendar=item.market_calendar,
            quote_poll_seconds=item.quote_poll_seconds,
            stale_after_seconds=item.stale_after_seconds,
            history=HistoryCollectionPolicy(
                enabled=item.history_enabled,
                poll_seconds=item.history_poll_seconds,
            ),
            routes=tuple(routes),
            provider_symbols=tuple(provider_symbols),
            income=income,
            synthetic=synthetic,
        )

    def to_instrument_spec(self) -> InstrumentSpec:
        income = self.income
        return InstrumentSpec(
            symbol=self.symbol,
            base=self.base,
            quote=self.quote,
            name=self.name,
            description=self.description,
            asset_class=self.asset_class,
            asset_type=self.asset_type,
            price_basis=self.price_basis,
            change_basis=self.change_basis,
            yield_strategy=None if income is None else income.yield_strategy,
            dividend_strategy=None if income is None else income.dividend_strategy,
            reward_accrual_mode=None if income is None else income.reward_accrual_mode,
            underlying_asset=None if income is None else income.underlying_asset,
            aliases=self.aliases,
            market_calendar=self.market_calendar,
            stale_after_seconds=self.stale_after_seconds,
            quote_poll_seconds=self.quote_poll_seconds,
            history_enabled=self.history.enabled,
            history_poll_seconds=self.history.poll_seconds,
        )

    @property
    def history_enabled(self) -> bool:
        return self.history.enabled

    @property
    def history_poll_seconds(self) -> float:
        return 3_600.0 if self.history.poll_seconds is None else self.history.poll_seconds

    @property
    def dividend_strategy(self) -> str | None:
        return None if self.income is None else self.income.dividend_strategy

    @property
    def yield_strategy(self) -> YieldStrategy | None:
        return None if self.income is None else self.income.yield_strategy

    @property
    def reward_accrual_mode(self) -> RewardAccrualMode | None:
        return None if self.income is None else self.income.reward_accrual_mode

    @property
    def underlying_asset(self) -> str | None:
        return None if self.income is None else self.income.underlying_asset


def _generation_content(instruments: Iterable[ManagedInstrumentDefinition]) -> dict[str, Any]:
    return {
        "instruments": [item.model_dump(mode="json") for item in instruments],
    }


def catalog_revision(instruments: Iterable[ManagedInstrumentDefinition]) -> str:
    encoded = json.dumps(
        _generation_content(instruments),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class CatalogGeneration(_CatalogModel):
    """A complete, content-addressed catalog generation."""

    revision: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    instruments: tuple[ManagedInstrumentDefinition, ...]

    @model_validator(mode="after")
    def _validate_generation(self) -> Self:
        if not self.instruments:
            raise ValueError("catalog generation cannot be empty")
        if self.revision != catalog_revision(self.instruments):
            raise ValueError("catalog generation revision does not match its content")
        ids: set[str] = set()
        symbols: set[str] = set()
        aliases: set[str] = set()
        custom_count = 0
        active_symbols: set[str] = set()
        synthetic_by_symbol: dict[str, SyntheticRecipeDefinition] = {}
        for item in self.instruments:
            if item.id in ids or item.symbol in symbols or item.symbol in aliases:
                raise ValueError("catalog contains a duplicate instrument identity")
            ids.add(item.id)
            symbols.add(item.symbol)
            if item.ownership is InstrumentOwnership.CUSTOM:
                custom_count += 1
            for alias in item.aliases:
                if alias in aliases or alias in symbols:
                    raise ValueError("catalog contains a duplicate instrument alias")
                aliases.add(alias)
            if item.enabled and not item.archived:
                active_symbols.add(item.symbol)
                if item.synthetic is not None:
                    synthetic_by_symbol[item.symbol] = item.synthetic
        if custom_count > MAX_CUSTOM_INSTRUMENTS:
            raise ValueError(
                f"catalog exceeds the {MAX_CUSTOM_INSTRUMENTS} custom instrument limit"
            )
        for symbol, recipe in synthetic_by_symbol.items():
            missing = [
                dependency for dependency in recipe.inputs if dependency not in active_symbols
            ]
            if missing:
                raise ValueError(
                    f"synthetic instrument {symbol} has unavailable inputs: {', '.join(missing)}"
                )

        visiting: set[str] = set()
        depths: dict[str, int] = {}

        def depth(symbol: str) -> int:
            if symbol in depths:
                return depths[symbol]
            if symbol in visiting:
                raise ValueError("catalog contains a synthetic dependency cycle")
            recipe = synthetic_by_symbol.get(symbol)
            if recipe is None:
                return 0
            visiting.add(symbol)
            result = 1 + max((depth(dependency) for dependency in recipe.inputs), default=0)
            visiting.remove(symbol)
            if result > MAX_SYNTHETIC_DEPTH:
                raise ValueError(
                    f"synthetic dependency depth exceeds the limit of {MAX_SYNTHETIC_DEPTH}"
                )
            depths[symbol] = result
            return result

        for symbol in synthetic_by_symbol:
            depth(symbol)
        return self

    @classmethod
    def build(cls, instruments: Iterable[ManagedInstrumentDefinition]) -> Self:
        items = tuple(instruments)
        return cls(revision=catalog_revision(items), instruments=items)

    @property
    def definitions(self) -> tuple[ManagedInstrumentDefinition, ...]:
        return self.instruments

    def by_id(self) -> Mapping[str, ManagedInstrumentDefinition]:
        return {item.id: item for item in self.instruments}

    def by_symbol(self) -> Mapping[str, ManagedInstrumentDefinition]:
        return {item.symbol: item for item in self.instruments}

    def to_registry(
        self,
        provider_installer: str
        | Any = "quickprice.providers.wiring:install_builtin_provider_routes",
    ) -> Any:
        """Build a metadata registry; provider routes are compiled separately."""

        from .registry import InstrumentRegistry

        instruments = tuple(
            item.to_instrument_spec()
            for item in self.instruments
            if item.enabled and not item.archived
        )
        plugin = InstrumentPlugin(
            plugin_id="managed-catalog",
            version=str(CATALOG_SCHEMA_VERSION),
            instruments=instruments,
            provider_installer=provider_installer or _install_noop,
        )
        return InstrumentRegistry((plugin,))


class ManagedCatalogDocument(_CatalogModel):
    version: Literal[2] = CATALOG_SCHEMA_VERSION
    active: CatalogGeneration
    staged: CatalogGeneration | None = None
    last_known_good: CatalogGeneration | None = None


def _install_noop(_context: Any) -> None:
    """Satisfy registry provenance validation; route compilation is external."""


def builtin_instrument_id(symbol: str) -> str:
    canonical = _canonical_symbol(symbol)
    return f"builtin-{canonical.lower().replace(':', '-')}"


def definition_from_payload(payload: Mapping[str, Any]) -> ManagedInstrumentDefinition:
    if not isinstance(payload, Mapping):
        raise CatalogValidationError("instrument definition must be a JSON object")
    try:
        return ManagedInstrumentDefinition.model_validate_json(_json_payload_bytes(dict(payload)))
    except (TypeError, ValueError) as exc:
        raise CatalogValidationError(str(exc)) from exc


def generation_from_payload(payload: Mapping[str, Any]) -> CatalogGeneration:
    if not isinstance(payload, Mapping):
        raise CatalogValidationError("catalog generation must be a JSON object")
    try:
        return CatalogGeneration.model_validate_json(_json_payload_bytes(dict(payload)))
    except (TypeError, ValueError) as exc:
        raise CatalogValidationError(str(exc)) from exc


def document_from_payload(payload: Mapping[str, Any]) -> ManagedCatalogDocument:
    if not isinstance(payload, Mapping):
        raise CatalogValidationError("managed catalog must be a JSON object")
    try:
        return ManagedCatalogDocument.model_validate_json(_json_payload_bytes(dict(payload)))
    except (TypeError, ValueError) as exc:
        raise CatalogValidationError(str(exc)) from exc


def _json_payload_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "MAX_CATALOG_IMPORT_BYTES",
    "MAX_CUSTOM_INSTRUMENTS",
    "MAX_PROVIDER_CHAIN",
    "MAX_SYNTHETIC_DEPTH",
    "CapabilityRoute",
    "CatalogGeneration",
    "CatalogValidationError",
    "HistoryCollectionPolicy",
    "IncomePolicy",
    "InstrumentOwnership",
    "ManagedCatalogDocument",
    "ManagedInstrumentDefinition",
    "ProviderSymbolBinding",
    "SyntheticOperation",
    "SyntheticRecipeDefinition",
    "builtin_instrument_id",
    "catalog_revision",
    "definition_from_payload",
    "document_from_payload",
    "generation_from_payload",
]
