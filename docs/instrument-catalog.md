# Managed Instrument Catalog

QuickPrice schema version 2 stores instrument definitions as data and compiles
them into an immutable runtime generation. An operator can add any instrument
that an installed provider can supply, or a bounded synthetic instrument, from
`/admin` without editing a provider module or restarting the process.

Adding a completely new provider still requires reviewed code. Catalog data
cannot define network destinations, request headers, credentials, Python import
paths, modules, commands, or executable expressions.

## Revision model

`instruments.json` retains three generations:

- `active`: the generation visible to public APIs and collectors;
- `staged`: a private draft produced by a create, edit, archive, or import;
- `last_known_good`: the previous active generation available for rollback.

The file is serialized deterministically, replaced atomically, restricted to
the service account, and identified by a SHA-256 file revision. Every mutation
must include the latest file revision. A stale revision returns HTTP 409 instead
of overwriting another administrator's work.

A version 1 instrument policy migrates automatically only after the complete
application startup succeeds. Disabled symbols and polling or staleness
overrides are carried into the version 2 built-in seed. The original bytes are
retained beside the catalog as `instruments.json.v1-backup` for release
rollback. A version 1 policy that disables every installed symbol remains an
intentionally empty active catalog: the default quotes and instruments endpoints
return successful empty arrays, while an explicitly requested disabled symbol
remains unknown. The migration does not change public symbols or provider routing.

## Definition model

A custom instrument contains:

- canonical `BASE:QUOTE` identity, English name and description, aliases,
  asset class, asset type, and price/change basis;
- enabled and archived state, market calendar, quote interval, stale threshold,
  and history collection policy;
- ordered `quote`, `history`, `dividend`, and `yield` provider chains;
- provider-specific symbols for installed providers;
- a controlled bond, dividend, or staking income policy when applicable;
- an optional bounded synthetic recipe.

QuickPrice assigns custom IDs in the `custom-<UUIDv7>` namespace and assigns
ownership. Clients must omit `id` and `ownership` when creating an instrument.
Exported definitions contain both fields so revisions can be round-tripped.

The following request definition is a complete spot example:

```json
{
  "symbol": "DOGE:USDC",
  "base": "DOGE",
  "quote": "USDC",
  "name": "Dogecoin / USD Coin",
  "description": "Dogecoin spot market quoted in USD Coin.",
  "asset_class": "crypto",
  "asset_type": "spot_crypto",
  "price_basis": "market_price",
  "change_basis": "unadjusted_market_price",
  "enabled": true,
  "archived": false,
  "aliases": [],
  "market_calendar": "always_open",
  "quote_poll_seconds": 5,
  "stale_after_seconds": 15,
  "history": {
    "enabled": true,
    "poll_seconds": 60,
    "backfill_days": 30
  },
  "routes": [
    {"capability": "quote", "providers": ["binance", "okx"]},
    {"capability": "history", "providers": ["binance", "okx"]}
  ],
  "provider_symbols": [
    {"provider": "binance", "symbol": "DOGEUSDC"},
    {"provider": "okx", "symbol": "DOGE-USDC"}
  ],
  "income": null,
  "synthetic": null
}
```

Routes may be omitted to request compatible defaults. A market-data provider is
included only when the definition also has a valid vendor-symbol binding and
the provider supports the asset class and capability. Advanced ordering can
remove fallbacks or reorder compatible providers, but cannot make an
incompatible provider valid.

## Default route families

- Crypto spot: Binance, OKX, Kraken, and CoinGecko, filtered to providers with
  a valid symbol binding and capability.
- Listed securities: Alpaca, Finnhub, Twelve Data, and Alpha Vantage for quote;
  Alpaca, Twelve Data, and Alpha Vantage for history; Alpaca for dividends.
- FX USD spokes: Twelve Data then Alpha Vantage. Other crosses use the bounded
  USD-hub synthetic route.
- Income bond ETF: latest ordinary distribution annualized.
- Growth bond ETF: an allowlisted FRED Treasury series minus the configured
  expense ratio.
- Liquid staking: installed official rate adapters first, followed by the
  declared token/underlying ratio window when configured.

Provider descriptors expose capabilities, credential requirements, supported
asset classes, fixed upstream hosts, and vendor-symbol validation. Descriptor
responses and catalog exports never contain provider credentials.

## Income and synthetic constraints

Every bond definition must select a yield strategy. Every asset type containing
`staking` must declare an underlying asset, a reward accrual mode, and a yield
strategy. The supported accrual modes are:

- `value_accruing`;
- `rebasing_balance`;
- `distributed_units`;
- `claimable_rewards`.

The staking ratio fallback uses a declared window from 7 through 365 days. It
is available only to `value_accruing` assets, is marked as a proxy, and remains
distinct from a protocol-reported rate. Rebasing, distributed-unit, and
claimable-reward assets require a compatible provider-reported yield because a
market-price ratio does not observe their unit rewards.

Synthetic recipes permit only `inverse`, `multiply`, and `divide`. `inverse`
accepts one input; the other operations accept exactly two. The compiler rejects
cycles, duplicate inputs, excessive component age or skew, and dependency depth
above four.

## Limits

The enforced defaults are:

| Limit | Value |
|---|---:|
| Custom instruments | 2,000 |
| Providers per capability chain | 4 |
| Synthetic inputs | 2 |
| Synthetic dependency depth | 4 |
| Catalog import body | 8 MiB |
| Concurrent activation jobs | 1 |
| Retained in-memory job records | 100 |
| Alpaca streaming symbols | 30 by default |
| Alpaca REST pacing | 180 calls/minute by default |
| Shadow warm-up timeout floor | 180 seconds by default |

Ordinary administrator mutations retain the 64 KiB body limit. Provider credit
estimates are local planning values, not vendor billing statements. Activation
admission applies configured budgets to committed primary demand; the validation
response separately reports uncapped fallback demand and the amount permitted by
the runtime hard quota gates. This keeps a recommended fallback chain usable
without pretending that every fallback can consume a complete daily budget at
once. A staged catalog whose committed demand exceeds a configured provider
budget cannot activate.

`QUICKPRICE_ALPACA_STREAM_SYMBOL_LIMIT`,
`QUICKPRICE_ALPACA_REST_CALLS_PER_MINUTE`, and
`QUICKPRICE_CATALOG_WARM_TIMEOUT_SECONDS` are managed settings. Increasing a
provider limit is an operator assertion about the attached vendor plan; it does
not change QuickPrice's fixed-host or catalog security boundary.

Validation reports a deterministic warm-up execution plan. The configured
timeout is a minimum, not a fixed promise: QuickPrice raises the effective total
deadline for a large changed set using bounded concurrency, every compiled
provider attempt and routing timeout, and known provider rate gates. Primary
minute-limited routes are paced before the shadow request; CoinGecko retains its
adapter-level shared price batching. A 2,000-instrument draft can therefore be
admitted without an artificial 180-second failure, although a free provider's
real rate limit can make that activation take minutes or hours.

## Activation lifecycle

1. Create, edit, archive, or import a draft using the current file revision.
2. Validate the complete staged catalog and inspect provider and credit
   diagnostics.
3. Start activation. QuickPrice compiles the complete graph and verifies fixed-
   endpoint vendor symbols and capabilities.
4. A shadow coordinator warms only new or changed instruments. Each requires a
   valid price; bond and staking assets also require their mandatory income
   metric. Historical backfill may continue after activation.
5. QuickPrice atomically publishes the new `RuntimeGeneration` and reconciles
   collectors. Publications tagged with the retired generation ID are ignored.
6. The successful generation becomes active and the previous active generation
   becomes last-known-good.

Reconciliation carries forward quote, metadata, and history due times only when
the relevant collection policy and live provider chain are unchanged. New or
changed schedules start from the new generation without resetting unrelated
symbols into an immediate REST burst.

Public requests capture the generation once. A concurrent request therefore
sees either the complete old registry or the complete new registry. Drafts are
not public. An archived custom instrument returns 404 after activation and its
collector stops; existing SQLite history remains subject to normal retention.
The in-memory history rings are also pruned on maintenance passes, including for
symbols that no longer receive observations.

If compilation, validation, warm-up, persistence, or collector transition
fails, the old generation continues serving. The staged generation and a
sanitized job diagnostic remain available for correction. Existing readiness
is not downgraded by a failed activation.

## Import and export

`merge` updates or adds custom definitions while retaining other custom and all
built-in definitions. `replace_custom` replaces the custom set while retaining
all built-ins. Neither mode can change immutable built-in identity,
classification, or income semantics. Exports contain no API keys, provider
keys, administrator factors, URLs, or headers.

Treat exported catalogs as reviewed configuration. Keep a backup before a
large replace operation, validate the staged revision, and inspect the computed
diff and credit estimate before activation.

## Administrator endpoints

All endpoints require the dedicated administrator session. Mutations also
require the synchronizer token, exact configured origin, same-origin Fetch
metadata, and `application/json`.

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/admin-api/instrument-catalog` | Read active, staged, last-good, and file revisions |
| `POST` | `/admin-api/instrument-catalog/instruments` | Stage one custom definition |
| `PATCH` | `/admin-api/instrument-catalog/instruments/{id}` | Stage an allowed edit |
| `DELETE` | `/admin-api/instrument-catalog/instruments/{id}` | Stage custom archival |
| `POST` | `/admin-api/instrument-catalog/import` | Stage a strict merge or replace-custom import |
| `GET` | `/admin-api/instrument-catalog/export?state=active` | Export active or staged data |
| `POST` | `/admin-api/instrument-catalog/validate` | Compile and validate the staged revision |
| `POST` | `/admin-api/instrument-catalog/activate` | Start shadow warm-up and activation |
| `POST` | `/admin-api/instrument-catalog/rollback` | Start activation of last-known-good |
| `GET` | `/admin-api/instrument-catalog/jobs/{job_id}` | Read activation progress and diagnostics |
| `GET` | `/admin-api/provider-catalog` | Read safe built-in provider descriptors |
| `GET` | `/admin-api/provider-catalog/{provider}/search` | Search a fixed provider endpoint |

Create, update, archive, import, validation, activation, failure, and rollback
events are written to the redacted administrator audit log. Diagnostics never
include credentials, authentication URLs, request headers, or complete upstream
responses.

## Recovery

Rollback starts a normal validated and warmed activation of last-known-good; it
does not bypass safety checks. If the process stops during an activation, the
atomically persisted active generation remains authoritative at restart. Keep
`instruments.json` and its filesystem permissions in host backups alongside the
SQLite online backup.
