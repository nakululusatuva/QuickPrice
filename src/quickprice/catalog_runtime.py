"""Validated shadow warm-up and atomic managed-catalog activation."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from .catalog import CatalogGeneration, ManagedInstrumentDefinition
from .domain import DividendEvent, ProviderQuote, YieldMetric, utc_now
from .market import scheduled_market_status
from .plugin_api import YieldStrategy
from .providers.base import Capability
from .providers.compiler import (
    CompiledRoutePlan,
    RouteCompileError,
    build_compiled_provider_graph,
    instrument_route_input_from_definition,
    required_capabilities,
)
from .providers.descriptors import (
    ProviderBindingVerificationError,
    verify_provider_bindings,
)

_LOGGER = logging.getLogger(__name__)
_MAX_RETAINED_JOBS = 100
_WARM_CONCURRENCY = 4
_MAX_WARM_CONCURRENCY = 32
_DEFAULT_WARM_TIMEOUT_SECONDS = 180.0
_WARM_TIMEOUT_MARGIN_SECONDS = 15.0


class CatalogRuntimeError(RuntimeError):
    """A catalog operation could not safely reach the runtime."""


class CatalogActivationBusyError(CatalogRuntimeError):
    """Only one validation-changing activation job may run at once."""


class CatalogJobNotFoundError(CatalogRuntimeError):
    """The requested bounded in-memory job record no longer exists."""


class CatalogWarmError(CatalogRuntimeError):
    """One symbol failed a bounded capability warm-up."""

    def __init__(self, symbol: str, capability: Capability, cause: BaseException) -> None:
        super().__init__("instrument warm-up failed")
        self.symbol = symbol
        self.capability = capability
        self.cause_type = type(cause).__name__


class CatalogRouteError(RouteCompileError):
    """A required compiled route is absent for one safe catalog identity."""

    def __init__(self, code: str, symbol: str, capability: Capability) -> None:
        super().__init__("managed provider route is invalid")
        self.code = code
        self.symbol = symbol
        self.capability = capability


@dataclass(frozen=True, slots=True)
class CreditBudgetViolation:
    provider: str
    proposed: float
    limit: float
    effective_limit: float
    grandfathered_unchanged: float
    scope: str | None = None


class CatalogBudgetError(RouteCompileError):
    """A candidate route plan exceeds one or more configured credit limits."""

    def __init__(self, violations: tuple[CreditBudgetViolation, ...]) -> None:
        super().__init__("managed provider credit budget is exceeded")
        self.violations = violations


class CatalogJobState(StrEnum):
    QUEUED = "queued"
    VALIDATING = "validating"
    WARMING = "warming"
    ACTIVATING = "activating"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class WarmedInstrument:
    definition: ManagedInstrumentDefinition
    quote: Any
    dividend: Any = None
    yield_metric: Any = None


@dataclass(frozen=True, slots=True)
class WarmExecutionPlan:
    """Deterministic activation capacity estimate derived from compiled routes."""

    symbol_count: int
    capability_operations: int
    worst_case_provider_attempts: int
    concurrency: int
    configured_timeout_seconds: float
    effective_timeout_seconds: float
    provider_attempts: tuple[tuple[str, int], ...] = ()
    primary_operations: tuple[tuple[str, int], ...] = ()
    provider_rate_floor_seconds: tuple[tuple[str, float], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol_count": self.symbol_count,
            "capability_operations": self.capability_operations,
            "worst_case_provider_attempts": self.worst_case_provider_attempts,
            "concurrency": self.concurrency,
            "configured_timeout_seconds": self.configured_timeout_seconds,
            "effective_timeout_seconds": self.effective_timeout_seconds,
            "provider_attempts": dict(self.provider_attempts),
            "primary_operations": dict(self.primary_operations),
            "provider_rate_floor_seconds": dict(self.provider_rate_floor_seconds),
        }


class _WarmRatePacer:
    """Space shadow requests before adapters with explicit minute-rate gates."""

    def __init__(self, interval_seconds: float) -> None:
        self.interval_seconds = interval_seconds
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def acquire(self) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            now = loop.time()
            scheduled = max(now, self._next_at)
            self._next_at = scheduled + self.interval_seconds
        delay = scheduled - now
        if delay > 0:
            await asyncio.sleep(delay)


@dataclass(slots=True)
class CatalogActivationJob:
    job_id: str
    operation: Literal["activate", "rollback"]
    requested_revision: str
    target_generation_revision: str
    state: CatalogJobState = CatalogJobState.QUEUED
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    changed_symbols: tuple[str, ...] = ()
    collector_handoff: str | None = None
    binding_verification: dict[str, Any] | None = None
    warm_plan: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    def update(self, state: CatalogJobState) -> None:
        self.state = state
        self.updated_at = datetime.now(UTC)

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "operation": self.operation,
            "requested_revision": self.requested_revision,
            "target_generation_revision": self.target_generation_revision,
            "state": self.state.value,
            "created_at": self.created_at.isoformat().replace("+00:00", "Z"),
            "updated_at": self.updated_at.isoformat().replace("+00:00", "Z"),
            "changed_symbols": list(self.changed_symbols),
            "collector_handoff": self.collector_handoff,
            "binding_verification": self.binding_verification,
            "warm_plan": self.warm_plan,
            "error": self.error,
        }


type AuditSink = Callable[[str, str, Mapping[str, Any], Any], Awaitable[None]]


class InstrumentCatalogRuntime:
    """Own validation jobs while the service remains the data-plane authority."""

    def __init__(
        self,
        service: Any,
        settings: Any,
        store: Any,
        *,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self.service = service
        self.settings = settings
        self.store = store
        self.audit_sink = audit_sink
        self._request_lock = asyncio.Lock()
        self._active_task: asyncio.Task[None] | None = None
        self._jobs: OrderedDict[str, CatalogActivationJob] = OrderedDict()
        configured_timeout = getattr(
            settings,
            "catalog_warm_timeout_seconds",
            _DEFAULT_WARM_TIMEOUT_SECONDS,
        )
        self.warm_timeout_seconds = max(1.0, float(configured_timeout))

    @staticmethod
    def _definition_fingerprint(item: ManagedInstrumentDefinition) -> str:
        return item.model_dump_json(exclude_none=False)

    @classmethod
    def _changed_symbols(
        cls,
        current: CatalogGeneration,
        target: CatalogGeneration,
    ) -> tuple[str, ...]:
        current_by_id = current.by_id()
        changed: list[str] = []
        for item in target.instruments:
            if not item.enabled or item.archived:
                continue
            previous = current_by_id.get(item.id)
            if previous is None or cls._definition_fingerprint(
                previous
            ) != cls._definition_fingerprint(item):
                changed.append(item.symbol)
        return tuple(changed)

    @staticmethod
    def _credit_limits(settings: Any) -> dict[str, float]:
        return {
            "twelve_data": float(settings.twelve_daily_credits),
            "alpha_vantage": float(settings.alpha_vantage_daily_credits),
            # A 31-day divisor makes every rolling calendar month safe.
            "coingecko": float(settings.coingecko_monthly_credits // 31),
            "finnhub": float(settings.finnhub_calls_per_minute * 1_440),
        }

    @staticmethod
    def _credit_line_identity(
        line: Any,
    ) -> tuple[str, str, str, tuple[str, ...], tuple[str, ...]]:
        bases = tuple(
            sorted(
                basis for basis in line.bases if not str(basis).startswith("shared_batch_count:")
            )
        )
        capability = getattr(line.capability, "value", str(line.capability))
        quota_scopes = tuple(sorted(getattr(line, "quota_scopes", ("general",))))
        return line.provider, capability, line.symbol, bases, quota_scopes

    @classmethod
    def _retained_committed_credits(
        cls,
        active: CompiledRoutePlan,
        candidate: CompiledRoutePlan,
    ) -> tuple[dict[str, Decimal], dict[str, dict[str, Decimal]]]:
        """Return unchanged committed demand eligible for grandfathering.

        Existing deployments can legitimately predate tighter credit admission
        policy. Only the common portion of the exact same upstream demand is
        retained; a replacement route, a new symbol, or a faster polling delta
        must fit the configured budget.
        """

        active_lines: dict[tuple[str, str, str, tuple[str, ...], tuple[str, ...]], Decimal] = {}
        candidate_lines: dict[tuple[str, str, str, tuple[str, ...], tuple[str, ...]], Decimal] = {}
        for target, plan in ((active_lines, active), (candidate_lines, candidate)):
            for line in plan.credit_estimates:
                if not line.committed:
                    continue
                identity = cls._credit_line_identity(line)
                target[identity] = target.get(identity, Decimal(0)) + Decimal(
                    line.estimated_credits_per_day
                )
        retained: dict[str, Decimal] = {}
        retained_by_scope: dict[str, dict[str, Decimal]] = {}
        for identity, active_value in active_lines.items():
            candidate_value = candidate_lines.get(identity)
            if candidate_value is None:
                continue
            provider = identity[0]
            common = min(
                active_value,
                candidate_value,
            )
            retained[provider] = retained.get(provider, Decimal(0)) + common
            scopes = retained_by_scope.setdefault(provider, {})
            for scope in identity[-1]:
                scopes[scope] = scopes.get(scope, Decimal(0)) + common
        return retained, retained_by_scope

    @classmethod
    def _credit_admission_context(
        cls,
        settings: Any,
        active: CompiledRoutePlan,
        candidate: CompiledRoutePlan,
    ) -> tuple[
        dict[str, Decimal],
        dict[str, Decimal],
        dict[str, dict[str, Decimal]],
        dict[str, dict[str, Decimal]],
    ]:
        retained, retained_by_scope = cls._retained_committed_credits(active, candidate)
        limits = {
            provider: Decimal(str(limit))
            for provider, limit in cls._credit_limits(settings).items()
        }
        effective = {
            provider: max(limit, retained.get(provider, Decimal(0)))
            for provider, limit in limits.items()
        }
        twelve_limit = Decimal(int(settings.twelve_daily_credits))
        twelve_reserve = Decimal(
            min(
                int(settings.twelve_fx_reserve_credits),
                max(0, int(settings.twelve_daily_credits) - 1),
            )
        )
        configured_general = twelve_limit - twelve_reserve
        effective_by_scope = {
            "twelve_data": {
                "general": max(
                    configured_general,
                    retained_by_scope.get("twelve_data", {}).get("general", Decimal(0)),
                )
            }
        }
        return retained, effective, retained_by_scope, effective_by_scope

    @classmethod
    def _catalog_diff(
        cls,
        current: CatalogGeneration,
        target: CatalogGeneration,
    ) -> dict[str, Any]:
        current_by_id = current.by_id()
        target_by_id = target.by_id()
        added: list[str] = []
        changed: list[str] = []
        archived_or_disabled: list[str] = []
        for instrument_id in sorted(set(current_by_id) | set(target_by_id)):
            before = current_by_id.get(instrument_id)
            after = target_by_id.get(instrument_id)
            before_active = before is not None and before.enabled and not before.archived
            after_active = after is not None and after.enabled and not after.archived
            if after_active and not before_active:
                added.append(after.symbol)
            elif before_active and not after_active:
                archived_or_disabled.append(before.symbol)
            elif (
                before_active
                and after_active
                and cls._definition_fingerprint(before) != cls._definition_fingerprint(after)
            ):
                changed.append(after.symbol)
        return {
            "added": added,
            "changed": changed,
            "archived_or_disabled": archived_or_disabled,
            "counts": {
                "added": len(added),
                "changed": len(changed),
                "archived_or_disabled": len(archived_or_disabled),
                "total": len(added) + len(changed) + len(archived_or_disabled),
            },
        }

    def _compile(
        self,
        generation: CatalogGeneration,
        *,
        strict: bool,
    ) -> tuple[Any, Any, CompiledRoutePlan]:
        registry = generation.to_registry()
        graph, plan = build_compiled_provider_graph(
            self.settings,
            registry,
            generation.definitions,
            metrics=self.service.metrics,
            strict=strict,
        )
        return registry, graph, plan

    def _strict_runtime_validation(self) -> bool:
        return bool(self.settings.production and self.settings.background_enabled)

    async def _verify_bindings(
        self,
        plan: CompiledRoutePlan,
        symbols: tuple[str, ...],
    ) -> dict[str, Any]:
        """Verify changed vendor identities without exposing configurable I/O."""

        if not bool(self.settings.production):
            return {
                "verified": False,
                "mode": "skipped_non_production",
                "providers": {},
                "warnings": [{"code": "upstream_verification_not_run"}],
            }
        return await verify_provider_bindings(
            self.settings,
            plan,
            symbols=symbols,
            credit_reserver=self.service.reserve_provider_search_credit,
        )

    @staticmethod
    def _collector_definition_payload(item: ManagedInstrumentDefinition) -> dict[str, Any]:
        payload = item.model_dump(mode="json")
        for field_name in ("name", "description", "aliases", "stale_after_seconds"):
            payload.pop(field_name, None)
        return payload

    def _can_reuse_coordinator(
        self,
        current: CatalogGeneration,
        target: CatalogGeneration,
        plan: CompiledRoutePlan,
    ) -> bool:
        coordinator = getattr(self.service, "_coordinator", None)
        if coordinator is None or not bool(getattr(coordinator, "is_running", False)):
            return False
        if getattr(coordinator, "fatal_error", None) is not None:
            return False
        runtime = self.service.capture_generation()
        if runtime.revision != current.revision or runtime.route_plan is None:
            return False
        current_plan_snapshot = getattr(runtime.route_plan, "as_dict", None)
        if not callable(current_plan_snapshot) or current_plan_snapshot() != plan.as_dict():
            return False
        current_active = {
            item.id: self._collector_definition_payload(item)
            for item in current.instruments
            if item.enabled and not item.archived
        }
        target_active = {
            item.id: self._collector_definition_payload(item)
            for item in target.instruments
            if item.enabled and not item.archived
        }
        return current_active == target_active

    @staticmethod
    def _provider_runtime_footprints(
        generation: CatalogGeneration,
        plan: CompiledRoutePlan,
    ) -> dict[str, tuple[tuple[Any, ...], ...]]:
        """Describe only definition fields that alter provider instances."""

        definitions = generation.by_symbol()
        records: dict[str, list[tuple[Any, ...]]] = {}
        provider_fields = {
            "symbol",
            "base",
            "quote",
            "asset_class",
            "asset_type",
            "routes",
            "provider_symbols",
            "income",
            "synthetic",
        }
        for item in plan.instruments.values():
            definition = definitions.get(item.symbol)
            if definition is None:
                continue
            capabilities: dict[str, tuple[str, ...]] = {}
            providers = set(item.provider_symbols)
            for capability, chain in item.routes.items():
                for provider in chain:
                    providers.add(provider)
                    capabilities.setdefault(provider, ())
                    capabilities[provider] = (
                        *capabilities[provider],
                        capability.value,
                    )
            policy = definition.model_dump_json(
                include=provider_fields,
                exclude_none=False,
            )
            for provider in providers:
                records.setdefault(provider, []).append(
                    (
                        item.symbol,
                        item.provider_symbols.get(provider),
                        tuple(sorted(capabilities.get(provider, ()))),
                        policy,
                    )
                )
        return {provider: tuple(sorted(items)) for provider, items in records.items()}

    @classmethod
    def _reusable_provider_names(
        cls,
        current: CatalogGeneration,
        target: CatalogGeneration,
        active_plan: CompiledRoutePlan,
        candidate_plan: CompiledRoutePlan,
    ) -> tuple[str, ...]:
        active = cls._provider_runtime_footprints(current, active_plan)
        candidate = cls._provider_runtime_footprints(target, candidate_plan)
        return tuple(
            sorted(
                provider
                for provider in set(active) & set(candidate)
                if active[provider] == candidate[provider]
            )
        )

    def _can_reconcile_coordinator(
        self,
        current: CatalogGeneration,
        active_plan: CompiledRoutePlan,
        candidate_coordinator: Any,
        reusable_provider_names: tuple[str, ...],
    ) -> bool:
        coordinator = getattr(self.service, "_coordinator", None)
        can_reconcile = getattr(coordinator, "can_reconcile", None)
        if not callable(can_reconcile):
            return False
        runtime = self.service.capture_generation()
        snapshot = getattr(runtime.route_plan, "as_dict", None)
        if runtime.revision != current.revision or not callable(snapshot):
            return False
        if snapshot() != active_plan.as_dict():
            return False
        return bool(can_reconcile(candidate_coordinator, reusable_provider_names))

    @staticmethod
    def _require_changed_routes(
        generation: CatalogGeneration,
        plan: CompiledRoutePlan,
        changed_symbols: tuple[str, ...],
    ) -> None:
        changed = set(changed_symbols)
        for definition in generation.definitions:
            if definition.symbol not in changed or not definition.enabled or definition.archived:
                continue
            route_input = instrument_route_input_from_definition(definition)
            for capability in required_capabilities(route_input):
                if not plan.providers_for(definition.symbol, capability):
                    raise CatalogRouteError(
                        "provider_route_missing",
                        definition.symbol,
                        capability,
                    )

    async def validate(self, expected_revision: str) -> dict[str, Any]:
        await asyncio.to_thread(self.store.assert_revision, expected_revision)
        generation = self.store.staged_generation()
        if generation is None:
            raise CatalogRuntimeError("there is no staged catalog to validate")
        await asyncio.to_thread(
            self.store.assert_runtime_target,
            expected_revision,
            "activate",
            generation.revision,
        )
        structural = await asyncio.to_thread(self.store.validate, expected_revision)
        active = self.store.active_generation()
        changed = self._changed_symbols(active, generation)
        graph = None
        try:
            _, graph, plan = await asyncio.to_thread(
                self._compile,
                generation,
                strict=self._strict_runtime_validation(),
            )
            self._require_changed_routes(generation, plan, changed)
            _, active_graph, active_plan = await asyncio.to_thread(
                self._compile,
                active,
                strict=False,
            )
            await active_graph.close()
            over_budget = self._incremental_budget_errors(active_plan, plan)
            if over_budget:
                raise CatalogBudgetError(over_budget)
            await asyncio.to_thread(
                self.store.assert_runtime_target,
                expected_revision,
                "activate",
                generation.revision,
            )
            binding_verification = await self._verify_bindings(plan, changed)
            warm_plan = self._warm_execution_plan(graph, generation, changed)
            limits = self._credit_limits(self.settings)
            worst_case_credits = {
                provider: float(value) for provider, value in plan.worst_case_daily_credits.items()
            }
            hard_capped_credits = {
                provider: float(value) for provider, value in plan.hard_capped_daily_credits.items()
            }
            worst_case_within_budget = all(
                provider not in limits or value <= limits[provider]
                for provider, value in worst_case_credits.items()
            )
            hard_capped_within_budget = all(
                provider not in limits or value <= limits[provider]
                for provider, value in hard_capped_credits.items()
            )
            (
                grandfathered,
                effective_limits,
                grandfathered_by_scope,
                effective_limits_by_scope,
            ) = self._credit_admission_context(
                self.settings,
                active_plan,
                plan,
            )
            return {
                **structural,
                "provider_routes": plan.as_dict(),
                "binding_verification": binding_verification,
                "warm_plan": warm_plan.as_dict(),
                "diff": self._catalog_diff(active, generation),
                "credit_plan": {
                    "estimated_daily_credits": dict(plan.estimated_daily_credits),
                    "active_daily_credits": dict(active_plan.estimated_daily_credits),
                    "limits": limits,
                    "grandfathered_unchanged_daily_credits": dict(grandfathered),
                    "effective_admission_limits": dict(effective_limits),
                    "grandfathered_unchanged_daily_credits_by_scope": {
                        provider: dict(scopes)
                        for provider, scopes in grandfathered_by_scope.items()
                    },
                    "effective_admission_limits_by_scope": {
                        provider: dict(scopes)
                        for provider, scopes in effective_limits_by_scope.items()
                    },
                    "worst_case_daily_credits": worst_case_credits,
                    "hard_capped_daily_credits": hard_capped_credits,
                    "admission_basis": "committed_primary_demand",
                    "fallback_requests_hard_gated": True,
                    "worst_case_within_configured_budget": worst_case_within_budget,
                    "within_hard_budget": hard_capped_within_budget,
                    "within_budget": True,
                },
                "warnings": structural.get("warnings", []),
            }
        finally:
            if graph is not None:
                await graph.close()

    def _incremental_budget_errors(
        self,
        active: CompiledRoutePlan,
        candidate: CompiledRoutePlan,
    ) -> tuple[CreditBudgetViolation, ...]:
        errors: list[CreditBudgetViolation] = []
        (
            grandfathered,
            effective_limits,
            grandfathered_by_scope,
            effective_limits_by_scope,
        ) = self._credit_admission_context(
            self.settings,
            active,
            candidate,
        )
        for provider, limit in self._credit_limits(self.settings).items():
            proposed = Decimal(candidate.committed_daily_credits.get(provider, 0))
            effective_limit = effective_limits[provider]
            if proposed > effective_limit:
                errors.append(
                    CreditBudgetViolation(
                        provider,
                        float(proposed),
                        limit,
                        float(effective_limit),
                        float(grandfathered.get(provider, Decimal(0))),
                    )
                )
        proposed_general = Decimal(
            candidate.committed_daily_credits_by_scope.get("twelve_data", {}).get(
                "general",
                0,
            )
        )
        configured_general = max(
            0.0,
            float(self.settings.twelve_daily_credits)
            - min(
                float(self.settings.twelve_fx_reserve_credits),
                max(0.0, float(self.settings.twelve_daily_credits) - 1.0),
            ),
        )
        effective_general = effective_limits_by_scope["twelve_data"]["general"]
        if proposed_general > effective_general:
            errors.append(
                CreditBudgetViolation(
                    "twelve_data",
                    float(proposed_general),
                    configured_general,
                    float(effective_general),
                    float(
                        grandfathered_by_scope.get("twelve_data", {}).get(
                            "general",
                            Decimal(0),
                        )
                    ),
                    "general",
                )
            )
        return tuple(errors)

    async def request_activation(
        self,
        expected_revision: str,
        *,
        audit: Any = None,
    ) -> dict[str, Any]:
        await asyncio.to_thread(self.store.assert_revision, expected_revision)
        staged = self.store.staged_generation()
        if staged is None:
            raise CatalogRuntimeError("there is no staged catalog to activate")
        return await self._request_job(
            "activate",
            expected_revision,
            staged,
            audit=audit,
        )

    async def request_rollback(
        self,
        expected_revision: str,
        *,
        audit: Any = None,
    ) -> dict[str, Any]:
        await asyncio.to_thread(self.store.assert_revision, expected_revision)
        target = self.store.last_known_good_generation()
        if target is None:
            raise CatalogRuntimeError("there is no last-known-good catalog")
        return await self._request_job(
            "rollback",
            expected_revision,
            target,
            audit=audit,
        )

    async def _request_job(
        self,
        operation: Literal["activate", "rollback"],
        expected_revision: str,
        target: CatalogGeneration,
        *,
        audit: Any,
    ) -> dict[str, Any]:
        async with self._request_lock:
            if self._active_task is not None and not self._active_task.done():
                raise CatalogActivationBusyError("an instrument activation job is already running")
            job = CatalogActivationJob(
                job_id=str(uuid.uuid7()),
                operation=operation,
                requested_revision=expected_revision,
                target_generation_revision=target.revision,
            )
            self._jobs[job.job_id] = job
            while len(self._jobs) > _MAX_RETAINED_JOBS:
                self._jobs.popitem(last=False)
            self._active_task = asyncio.create_task(
                self._run_job(job, target, audit=audit),
                name=f"catalog-{operation}:{job.job_id}",
            )
            return job.as_dict()

    def job(self, job_id: str) -> dict[str, Any]:
        job = self._jobs.get(job_id)
        if job is None:
            raise CatalogJobNotFoundError("instrument catalog job was not found")
        return job.as_dict()

    async def stop(self) -> None:
        """Cancel and join the one activation job before service resources stop."""

        async with self._request_lock:
            task = self._active_task
            if task is None or task.done():
                return
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _run_job(
        self,
        job: CatalogActivationJob,
        target: CatalogGeneration,
        *,
        audit: Any,
    ) -> None:
        graph = None
        candidate_coordinator = None
        stage = "validating"
        try:
            job.update(CatalogJobState.VALIDATING)
            await asyncio.to_thread(
                self.store.assert_runtime_target,
                job.requested_revision,
                job.operation,
                target.revision,
            )
            current = self.store.active_generation()
            registry, graph, plan = await asyncio.to_thread(
                self._compile,
                target,
                strict=self._strict_runtime_validation(),
            )
            changed = self._changed_symbols(current, target)
            self._require_changed_routes(target, plan, changed)
            _, active_graph, active_plan = await asyncio.to_thread(
                self._compile,
                current,
                strict=False,
            )
            try:
                budget_errors = self._incremental_budget_errors(active_plan, plan)
            finally:
                await active_graph.close()
            if budget_errors:
                raise CatalogBudgetError(budget_errors)
            job.changed_symbols = changed
            generation_id = str(uuid.uuid7())
            from .collectors import MarketDataCoordinator

            candidate_coordinator = MarketDataCoordinator(
                self.service,
                self.settings,
                registry,
                generation_id=generation_id,
                graph=graph,
                catalog=target,
            )
            graph = None
            stage = "preparing_collectors"
            await asyncio.to_thread(
                self.store.assert_runtime_target,
                job.requested_revision,
                job.operation,
                target.revision,
            )
            # Preparation restores and shares durable quota ledgers, but does
            # not open streams, start schedulers, or publish candidate data.
            await candidate_coordinator.prepare()
            stage = "verifying_bindings"
            job.binding_verification = await self._verify_bindings(plan, changed)
            execution_plan = self._warm_execution_plan(
                candidate_coordinator.graph,
                target,
                changed,
            )
            job.warm_plan = execution_plan.as_dict()
            job.update(CatalogJobState.WARMING)
            stage = "warming"
            warmed = await self._warm(candidate_coordinator.graph, target, changed)
            job.update(CatalogJobState.ACTIVATING)
            stage = "activating"
            reuse_coordinator = self.settings.background_enabled and self._can_reuse_coordinator(
                current,
                target,
                plan,
            )
            reusable_provider_names = self._reusable_provider_names(
                current,
                target,
                active_plan,
                plan,
            )
            reconcile_coordinator = (
                self.settings.background_enabled
                and not reuse_coordinator
                and self._can_reconcile_coordinator(
                    current,
                    active_plan,
                    candidate_coordinator,
                    reusable_provider_names,
                )
            )
            if reuse_coordinator:
                handoff_coordinator = self.service._coordinator
                job.collector_handoff = "reused_metadata_only"
                reconcile_provider_names = None
            elif reconcile_coordinator:
                handoff_coordinator = candidate_coordinator
                reconcile_provider_names = reusable_provider_names
                job.collector_handoff = "reconciled"
            elif self.settings.background_enabled:
                handoff_coordinator = candidate_coordinator
                reconcile_provider_names = None
                job.collector_handoff = "reconnected"
            else:
                handoff_coordinator = None
                reconcile_provider_names = None
                job.collector_handoff = "disabled"
            await self.service._commit_catalog_activation(
                operation=job.operation,
                expected_file_revision=job.requested_revision,
                target=target,
                registry=registry,
                route_plan=plan,
                generation_id=generation_id,
                warmed=warmed,
                candidate_coordinator=handoff_coordinator,
                reconcile_provider_names=reconcile_provider_names,
            )
            if handoff_coordinator is candidate_coordinator and not reconcile_coordinator:
                candidate_coordinator = None
            job.update(CatalogJobState.SUCCEEDED)
            await self._audit(
                f"instrument_catalog.{job.operation}_succeeded",
                job,
                audit,
            )
        except asyncio.CancelledError as exc:
            job.error = self._safe_error(exc, stage=stage, cancelled=True)
            job.update(CatalogJobState.CANCELLED)
            _LOGGER.info(
                "Instrument catalog job cancelled job_id=%s operation=%s stage=%s",
                job.job_id,
                job.operation,
                stage,
            )
            await asyncio.shield(
                self._audit(
                    f"instrument_catalog.{job.operation}_cancelled",
                    job,
                    audit,
                )
            )
            raise
        except Exception as exc:
            job.error = self._safe_error(exc, stage=stage)
            job.update(CatalogJobState.FAILED)
            _LOGGER.warning(
                "Instrument catalog job failed job_id=%s operation=%s error_type=%s",
                job.job_id,
                job.operation,
                type(exc).__name__,
            )
            await self._audit(
                f"instrument_catalog.{job.operation}_failed",
                job,
                audit,
            )
        finally:
            if candidate_coordinator is not None:
                await candidate_coordinator.stop(persist_checkpoints=False)
            if graph is not None:
                await graph.close()

    @staticmethod
    def _safe_error(
        exc: BaseException,
        *,
        stage: str,
        cancelled: bool = False,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": (
                "catalog_activation_cancelled"
                if cancelled
                else "catalog_warm_timeout"
                if stage == "warming" and isinstance(exc, TimeoutError)
                else "catalog_activation_failed"
            ),
            "message": (
                "catalog activation was cancelled during service shutdown"
                if cancelled
                else "catalog validation, warm-up, or activation failed"
            ),
            "type": type(exc).__name__,
            "stage": stage,
        }
        warm_error = InstrumentCatalogRuntime._find_warm_error(exc)
        if warm_error is not None:
            result["code"] = "catalog_warm_failed"
            result.update(
                {
                    "symbol": warm_error.symbol,
                    "capability": warm_error.capability.value,
                    "cause_type": warm_error.cause_type,
                }
            )
        elif isinstance(exc, CatalogRouteError):
            result.update(
                {
                    "code": exc.code,
                    "symbol": exc.symbol,
                    "capability": exc.capability.value,
                }
            )
        elif isinstance(exc, CatalogBudgetError):
            result.update(
                {
                    "code": "provider_credit_budget_exceeded",
                    "budgets": [
                        {
                            "provider": item.provider,
                            "proposed": item.proposed,
                            "limit": item.limit,
                            "effective_limit": item.effective_limit,
                            "grandfathered_unchanged": item.grandfathered_unchanged,
                            "scope": item.scope,
                        }
                        for item in exc.violations
                    ],
                }
            )
        elif isinstance(exc, ProviderBindingVerificationError):
            verification = exc.as_dict()
            result.update(
                {
                    "code": "provider_binding_verification_failed",
                    "binding_failures": verification["failures"],
                }
            )
        elif isinstance(exc, RouteCompileError):
            result.update(InstrumentCatalogRuntime._safe_compile_context(str(exc)))
        return result

    @staticmethod
    def _safe_compile_context(message: str) -> dict[str, str]:
        symbol_capability = re.search(
            r"(?P<symbol>[A-Z0-9._-]+:[A-Z0-9._-]+)/"
            r"(?P<capability>quote|history|dividend|yield)",
            message,
        )
        if symbol_capability is not None:
            return {
                "code": "provider_route_invalid",
                "symbol": symbol_capability.group("symbol"),
                "capability": symbol_capability.group("capability"),
            }
        binding = re.search(
            r"(?P<symbol>[A-Z0-9._-]+:[A-Z0-9._-]+)[/:]"
            r"\s*(?P<provider>[a-z][a-z0-9_]*)",
            message,
        )
        if binding is not None:
            return {
                "code": "provider_binding_invalid",
                "symbol": binding.group("symbol"),
                "provider": binding.group("provider"),
            }
        provider = re.search(r"provider is not configured: (?P<provider>[a-z][a-z0-9_]*)", message)
        if provider is not None:
            return {
                "code": "provider_not_configured",
                "provider": provider.group("provider"),
            }
        lowered = message.casefold()
        if "synthetic" in lowered:
            return {"code": "synthetic_route_invalid"}
        if "treasury" in lowered or "yield strategy" in lowered or "dividend" in lowered:
            return {"code": "income_policy_invalid"}
        return {"code": "route_compile_failed"}

    @staticmethod
    def _find_warm_error(exc: BaseException) -> CatalogWarmError | None:
        if isinstance(exc, CatalogWarmError):
            return exc
        if isinstance(exc, BaseExceptionGroup):
            for nested in exc.exceptions:
                found = InstrumentCatalogRuntime._find_warm_error(nested)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _warm_capabilities(
        definition: ManagedInstrumentDefinition,
    ) -> tuple[Capability, ...]:
        capabilities = [Capability.QUOTE]
        income = definition.income
        if income is not None and (
            income.dividend_strategy is not None
            or income.yield_strategy is YieldStrategy.LATEST_DISTRIBUTION_ANNUALIZED
        ):
            capabilities.append(Capability.DIVIDEND)
        if income is not None and income.yield_strategy not in {
            None,
            YieldStrategy.LATEST_DISTRIBUTION_ANNUALIZED,
        }:
            capabilities.append(Capability.YIELD)
        return tuple(capabilities)

    def _warm_provider_rate_per_minute(self, name: str, provider: Any) -> float | None:
        setting_names = {
            "alpaca": ("alpaca_rest_calls_per_minute", 180),
            "finnhub": ("finnhub_calls_per_minute", 60),
            "twelve_data": ("twelve_calls_per_minute", 8),
        }
        setting = setting_names.get(name)
        if setting is not None:
            value = float(getattr(self.settings, setting[0], setting[1]))
            return value if math.isfinite(value) and value > 0 else None
        minimum_interval = getattr(provider, "minimum_request_interval_seconds", None)
        if minimum_interval is None:
            return None
        interval = float(minimum_interval)
        if not math.isfinite(interval) or interval <= 0:
            return None
        return 60.0 / interval

    def _warm_execution_plan(
        self,
        graph: Any,
        generation: CatalogGeneration,
        symbols: tuple[str, ...],
    ) -> WarmExecutionPlan:
        """Size a shadow warm-up without consuming credits or sleeping."""

        router = graph.router
        providers_for = getattr(router, "providers_for", None)
        if not callable(providers_for):
            # Small contract-test routers intentionally expose only the methods
            # under test. Preserve their explicitly configured total deadline.
            return WarmExecutionPlan(
                symbol_count=len(symbols),
                capability_operations=len(symbols),
                worst_case_provider_attempts=len(symbols),
                concurrency=_WARM_CONCURRENCY,
                configured_timeout_seconds=self.warm_timeout_seconds,
                effective_timeout_seconds=self.warm_timeout_seconds,
            )

        by_symbol = generation.by_symbol()
        concurrency = min(
            _MAX_WARM_CONCURRENCY,
            max(_WARM_CONCURRENCY, math.ceil(max(1, len(symbols)) / 64)),
        )
        operation_count = 0
        attempt_count = 0
        total_routing_timeout = 0.0
        maximum_routing_timeout = float(getattr(self.settings, "provider_timeout_seconds", 8.0))
        provider_attempts: dict[str, int] = {}
        primary_operations: dict[str, int] = {}
        provider_instances: dict[str, Any] = {}
        router_timeout = float(
            getattr(router, "timeout_seconds", maximum_routing_timeout) or maximum_routing_timeout
        )

        for symbol in symbols:
            definition = by_symbol[symbol]
            for capability in self._warm_capabilities(definition):
                operation_count += 1
                chain = tuple(providers_for(symbol, capability))
                if chain:
                    primary_name = str(getattr(chain[0], "name", chain[0].__class__.__name__))
                    primary_operations[primary_name] = primary_operations.get(primary_name, 0) + 1
                for provider in chain:
                    name = str(getattr(provider, "name", provider.__class__.__name__))
                    provider_instances.setdefault(name, provider)
                    provider_attempts[name] = provider_attempts.get(name, 0) + 1
                    attempt_count += 1
                    timeout = getattr(provider, "routing_timeout_seconds", router_timeout)
                    timeout = router_timeout if timeout is None else float(timeout)
                    if not math.isfinite(timeout) or timeout <= 0:
                        timeout = router_timeout
                    maximum_routing_timeout = max(maximum_routing_timeout, timeout)
                    total_routing_timeout += timeout

        rate_floors: dict[str, float] = {}
        for name, count in provider_attempts.items():
            rate = self._warm_provider_rate_per_minute(name, provider_instances[name])
            if rate is not None:
                rate_floors[name] = count * 60.0 / rate

        routing_floor = total_routing_timeout / concurrency
        rate_floor = max(rate_floors.values(), default=0.0)
        effective_timeout = max(
            self.warm_timeout_seconds,
            routing_floor + maximum_routing_timeout + _WARM_TIMEOUT_MARGIN_SECONDS,
            rate_floor + maximum_routing_timeout + _WARM_TIMEOUT_MARGIN_SECONDS,
        )
        return WarmExecutionPlan(
            symbol_count=len(symbols),
            capability_operations=operation_count,
            worst_case_provider_attempts=attempt_count,
            concurrency=concurrency,
            configured_timeout_seconds=self.warm_timeout_seconds,
            effective_timeout_seconds=float(math.ceil(effective_timeout)),
            provider_attempts=tuple(sorted(provider_attempts.items())),
            primary_operations=tuple(sorted(primary_operations.items())),
            provider_rate_floor_seconds=tuple(
                (name, float(math.ceil(seconds))) for name, seconds in sorted(rate_floors.items())
            ),
        )

    async def _warm(
        self,
        graph: Any,
        generation: CatalogGeneration,
        symbols: tuple[str, ...],
        *,
        execution_plan: WarmExecutionPlan | None = None,
    ) -> tuple[WarmedInstrument, ...]:
        execution_plan = execution_plan or self._warm_execution_plan(
            graph,
            generation,
            symbols,
        )
        by_symbol = generation.by_symbol()
        semaphore = asyncio.Semaphore(execution_plan.concurrency)
        pacers: dict[str, _WarmRatePacer] = {}
        results: list[WarmedInstrument | None] = [None] * len(symbols)

        async def pace(symbol: str, capability: Capability) -> None:
            providers_for = getattr(graph.router, "providers_for", None)
            if not callable(providers_for):
                return
            chain = tuple(providers_for(symbol, capability))
            if not chain:
                return
            provider = chain[0]
            name = str(getattr(provider, "name", provider.__class__.__name__))
            # CoinGecko coalesces all configured coin IDs behind its own shared
            # batch refresh, so per-symbol pacing would defeat that adapter.
            if name == "coingecko":
                return
            rate = self._warm_provider_rate_per_minute(name, provider)
            if rate is None:
                return
            pacer = pacers.setdefault(name, _WarmRatePacer(60.0 / rate))
            await pacer.acquire()

        async def warm_one(symbol: str) -> WarmedInstrument:
            definition = by_symbol[symbol]
            async with semaphore:
                try:
                    await pace(symbol, Capability.QUOTE)
                    quote = await graph.router.get_quote(symbol)
                    self._validate_quote(definition, quote)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    raise CatalogWarmError(symbol, Capability.QUOTE, exc) from None
                dividend = None
                yield_metric = None
                income = definition.income
                if income is not None and (
                    income.dividend_strategy is not None
                    or income.yield_strategy is YieldStrategy.LATEST_DISTRIBUTION_ANNUALIZED
                ):
                    try:
                        if not graph.router.configured(symbol, Capability.DIVIDEND):
                            raise RouteCompileError("required dividend route is missing")
                        await pace(symbol, Capability.DIVIDEND)
                        dividend = await graph.router.get_latest_dividend(symbol)
                        self._validate_dividend(symbol, dividend)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        raise CatalogWarmError(symbol, Capability.DIVIDEND, exc) from None
                if income is not None and income.yield_strategy not in {
                    None,
                    YieldStrategy.LATEST_DISTRIBUTION_ANNUALIZED,
                }:
                    try:
                        if not graph.router.configured(symbol, Capability.YIELD):
                            raise RouteCompileError("required yield route is missing")
                        await pace(symbol, Capability.YIELD)
                        yield_metric = await graph.router.get_yield(symbol)
                        self._validate_yield(symbol, yield_metric)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        raise CatalogWarmError(symbol, Capability.YIELD, exc) from None
                return WarmedInstrument(definition, quote, dividend, yield_metric)

        async def store_result(index: int, symbol: str) -> None:
            results[index] = await warm_one(symbol)

        async with asyncio.timeout(execution_plan.effective_timeout_seconds):
            async with asyncio.TaskGroup() as group:
                for index, symbol in enumerate(symbols):
                    group.create_task(
                        store_result(index, symbol),
                        name=f"catalog-warm:{symbol}",
                    )
        if any(item is None for item in results):
            raise CatalogRuntimeError("catalog warm-up completed without a result")
        return tuple(item for item in results if item is not None)

    @staticmethod
    def _validate_quote(
        definition: ManagedInstrumentDefinition,
        quote: Any,
    ) -> None:
        if not isinstance(quote, ProviderQuote):
            raise TypeError("quote provider returned an invalid domain value")
        if quote.symbol != definition.symbol:
            raise ValueError("quote provider returned the wrong symbol")
        now = utc_now()
        if quote.as_of > now + timedelta(seconds=60):
            raise ValueError("quote timestamp is in the future")
        age = max(0.0, (now - quote.as_of).total_seconds())
        scheduled_closed = scheduled_market_status(definition.market_calendar, now) == "closed"
        status_age = (
            None
            if quote.market_status_as_of is None
            else (now - quote.market_status_as_of).total_seconds()
        )
        provider_confirms_closed = (
            definition.market_calendar.value != "always_open"
            and quote.market_status == "closed"
            and status_age is not None
            and 0 <= status_age <= 300
        )
        if (
            age > definition.stale_after_seconds
            and not scheduled_closed
            and not provider_confirms_closed
        ):
            raise ValueError("quote is too stale for activation")

    @staticmethod
    def _validate_dividend(symbol: str, dividend: Any) -> None:
        if not isinstance(dividend, DividendEvent):
            raise TypeError("dividend provider returned an invalid domain value")
        if dividend.symbol != symbol:
            raise ValueError("dividend provider returned the wrong symbol")
        if dividend.event_type != "regular_cash":
            raise ValueError("latest mandatory dividend is not a regular cash event")

    def _validate_yield(self, symbol: str, metric: Any) -> None:
        if not isinstance(metric, YieldMetric):
            raise TypeError("yield provider returned an invalid domain value")
        if metric.symbol != symbol:
            raise ValueError("yield provider returned the wrong symbol")
        now = utc_now()
        if metric.as_of > now + timedelta(seconds=60):
            raise ValueError("yield timestamp is in the future")
        threshold = self.service._default_yield_stale_after_seconds(metric)
        if (now - metric.as_of).total_seconds() > threshold:
            raise ValueError("yield metric is too stale for activation")

    async def _audit(
        self,
        action: str,
        job: CatalogActivationJob,
        audit: Any,
    ) -> None:
        if self.audit_sink is None:
            return
        try:
            await self.audit_sink(
                action,
                job.job_id,
                {
                    "operation": job.operation,
                    "state": job.state.value,
                    "target_generation_revision": job.target_generation_revision,
                    "changed_symbol_count": len(job.changed_symbols),
                    "error_type": None if job.error is None else job.error["type"],
                    "error_code": None if job.error is None else job.error["code"],
                    "error_stage": None if job.error is None else job.error.get("stage"),
                    "error_symbol": None if job.error is None else job.error.get("symbol"),
                    "error_capability": (
                        None if job.error is None else job.error.get("capability")
                    ),
                },
                audit,
            )
        except Exception:
            _LOGGER.error(
                "Instrument catalog audit failed job_id=%s action=%s",
                job.job_id,
                action,
            )


__all__ = [
    "CatalogActivationBusyError",
    "CatalogActivationJob",
    "CatalogJobNotFoundError",
    "CatalogJobState",
    "CatalogRuntimeError",
    "InstrumentCatalogRuntime",
    "WarmedInstrument",
]
