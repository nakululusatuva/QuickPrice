# QuickPrice Native Operations Guide

This guide covers one QuickPrice process on a Linux VPS with a local persistent
SQLite database. Docker is not supported. Never run two QuickPrice processes
against the same database.

The supplied systemd unit reads non-secret runtime settings and provider
credentials from separate files. Expected ownership and modes are:

```text
/etc/quickprice/quickprice.env                 root:quickprice 0640
/etc/quickprice/admin-auth.env                 root:quickprice 0640
/var/lib/quickprice/config/quickprice.env      quickprice      0600
/var/lib/quickprice/config/provider-keys.env   quickprice      0600
/var/lib/quickprice/config/instruments.json    quickprice      0600
/var/lib/quickprice                            quickprice      0700
```

The reverse proxy must not inherit either application file. QuickPrice is
proxy-agnostic and expects TLS termination in a separately managed HTTP server.

An outbound provider proxy is independent from that inbound reverse proxy.
`QUICKPRICE_PROVIDER_PROXY_URL` alone applies to every provider. Define
`QUICKPRICE_PROVIDER_PROXY_NAMES` as a comma-separated allowlist when some
providers should remain direct. Validate both REST and WebSocket endpoints from
the service host before enabling a provider; Binance streaming additionally
requires CONNECT access to port 9443. After a change, inspect source and
fallback metadata rather than assuming that an HTTP 200 liveness response
proves provider reachability.

## Normal baseline

```bash
systemctl status quickprice
journalctl -u quickprice --since '15 minutes ago'
curl --fail https://price.example.com/health/live
curl --fail https://price.example.com/health/ready
curl --fail \
  -H "X-API-Key: ${QUICKPRICE_API_KEY}" \
  https://price.example.com/internal/readiness
curl --fail \
  -H "X-API-Key: ${QUICKPRICE_API_KEY}" \
  https://price.example.com/internal/metrics
```

A healthy service has:

- HTTP 200 liveness and readiness;
- `py_gil_disabled=true` and `gil_enabled=false`;
- a writer queue that returns toward zero;
- bounded database and WAL files;
- process memory that settles after bounded, streaming history restoration;
- no sustained event-loop lag;
- expected provider quotas, circuits, and fallback levels;
- snapshot age consistent with market status and provider cadence.

The supplied systemd unit uses `MemoryTHP=disable` on systemd 260 or newer.
This is a per-process policy: it prevents CPython 3.14t's mimalloc arenas from
retaining disproportionate 2 MiB transparent huge pages without changing the
host-wide THP setting. Older systemd versions may omit this optional directive;
QuickPrice remains functional with a potentially higher resident set.

Suggested alerts:

| Signal | Warning | Critical |
|---|---:|---:|
| Readiness | One failure | Three failures in two minutes |
| Request p95 | Above 100 ms for five minutes | Above 250 ms for five minutes |
| Unexpected 5xx | Above 0.1% for five minutes | Above 1% for five minutes |
| Event-loop lag | Above 100 ms for five minutes | Above 500 ms for two minutes |
| SQLite writer queue | Does not drain for five minutes | Grows for 15 minutes |
| Disk free | Below 20% | Below 10% or 2 GB |
| Binance/Kraken spot quote age | Above 15 seconds | Above 60 seconds |
| CoinGecko staking-token quote age | Above 15 minutes | Above 30 minutes |
| Open equity quote age | Above 30 seconds | Above five minutes |
| USD/CNH quote age | Above five minutes | Above 15 minutes |

Closed equity markets do not produce trades. Evaluate quote age together with
`market_status`. Yield quality is independent of price quality; alert on both.

## Dashboard and live logs

`/dashboard` is a static operator shell. Loading the shell is not an
authentication event and returns no market data or secrets. Its catalog,
quotes, and Live log tab use the same QuickPrice API-key boundary as other
clients. Treat a dashboard session as an authenticated administrative session:
use a trusted browser profile, close the tab when finished, and never place the
key in a bookmark, URL, reverse-proxy rule, or shared browser storage.

The Live log tab reads `/internal/logs/stream` as an authenticated Server-Sent
Events connection. Operational properties are intentionally bounded:

- the in-process backlog retains at most 500 events and each connected client
  has a 100-event queue;
- `QUICKPRICE_DASHBOARD_MAX_LOG_STREAMS` limits concurrent streams per process
  and defaults to 8; excess authenticated connections receive HTTP 429;
- when a client falls behind, older queued events are discarded rather than
  allowing memory growth or blocking application logging;
- the backlog is cleared on process restart and is not an audit log, durable
  journal, or replacement for host log collection;
- configured credential values, credential-like assignments, and URL query
  strings are redacted before delivery. Redaction is defense in depth: code
  must still avoid logging secrets in the first place;
- the server sends a heartbeat every 15 seconds. After an interruption, the
  dashboard retries after two seconds and supplies the last received event
  identifier when possible. Events older than the bounded backlog are not
  replayable.

Configure the selected reverse proxy specifically for the SSE path while
remaining otherwise proxy-agnostic. It must preserve `X-API-Key` and
`Last-Event-ID`, forward a streaming-capable connection, disable response
buffering and shared caching, avoid compression or transformations that delay
flushes, and disable or extend upstream read/idle timeouts so a healthy stream
is not terminated. Do not encode the key in a proxy-generated query parameter.
Validate reconnect and redaction behavior through the public HTTPS origin after
every proxy or application upgrade.

## Administrator control plane

`/admin` is not an extension of dashboard authentication. It requires the
dedicated scrypt-verified administrator key and TOTP factor generated by
`quickprice admin-credentials`. Keep the raw key and TOTP seed in separate
protected records. Never copy either value into the web-managed files.

Production acceptance must verify all of the following through the public
origin:

- the cookie uses the `__Host-quickprice_admin` name with `Secure`, `HttpOnly`,
  `SameSite=Strict`, and `Path=/`, and has no `Domain` attribute;
- a quote API key receives HTTP 401 from every `/admin-api/*` data endpoint;
- missing or wrong CSRF tokens, sibling origins, `Origin: null`, non-JSON
  mutation bodies, and `Sec-Fetch-Site: same-site` are rejected;
- the reverse proxy overwrites forwarding headers, restores a visitor address
  only from trusted peers, caps ordinary admin bodies at 64 KiB, permits at
  most 8 MiB only on `/admin-api/instrument-catalog/import`, and never caches
  the admin shell or responses;
- `QUICKPRICE_ADMIN_TRUSTED_PROXY_IPS` lists only the exact Nginx peer address;
  QuickPrice is started through `quickprice serve`, which disables Uvicorn's
  implicit proxy-header trust;
- direct origin access is blocked when an edge proxy is part of the trust
  boundary, and the edge-to-origin TLS mode validates the origin certificate;
- administrator changes produce durable, redacted authorization-intent and
  completion events around file mutations.

Provider credentials are write-only. Status responses may disclose the
credential name, configured state, management source, and restart state, but
never a value, fingerprint derived from a weak secret, request body, provider
response, or exception text. Web-managed files are atomically replaced below
`/var/lib/quickprice/config`; the service remains unable to write `/etc`, the
application tree, Nginx, or systemd and has no self-restart endpoint.
Provider files may also contain host-managed network destinations such as
Ethereum RPC URLs. Those entries are preserved but never exposed or modified
by web administration.

Instrument administration is declarative. The UI can stage custom instruments
supported by installed providers, adjust compatible routes and bounded
collection intervals, validate the full catalog, perform a shadow warm-up, and
atomically activate or roll back a runtime generation. It cannot load entry
points, upload wheels, specify import paths, create arbitrary URLs or headers,
or execute code. A completely new provider still requires a reviewed plugin
deployment.

The catalog file maintains active, staged, and last-known-good generations.
Before an application upgrade, back up `instruments.json` together with SQLite.
Version 1 policy files migrate only after the complete application startup has
succeeded while retaining disabled symbols and interval overrides. QuickPrice
also writes `instruments.json.v1-backup`; keep it until the previous release is
outside the rollback window. After migration, confirm that the active revision
contains all built-ins before activating any new draft.

A policy that intentionally disabled every version 1 symbol remains empty after
migration. This is a ready state when the other readiness gates pass; the
detailed readiness response reports zero active instruments. The default quote
and instrument batches are HTTP 200 empty arrays, while explicit disabled-symbol
requests still fail as unknown symbols.

During activation, monitor the job in `/admin`, provider quota state, and the
service journal. Only changed instruments are warmed. Every changed instrument
must produce a valid price, and bond or staking instruments must also produce
their mandatory income metric. A failed job intentionally leaves the staged
revision available for correction and does not degrade the active generation
or `/health/ready`.

Large listed-security imports are constrained by the Alpaca stream prefix and
shared REST rate gate. Adjust `QUICKPRICE_ALPACA_STREAM_SYMBOL_LIMIT`,
`QUICKPRICE_ALPACA_REST_CALLS_PER_MINUTE`, and
`QUICKPRICE_CATALOG_WARM_TIMEOUT_SECONDS` only to values supported by the
attached account. The old generation remains active until the candidate
collector acknowledges startup. The configured warm timeout is a floor.
Validation exposes a scale-aware effective deadline derived from the changed
capabilities, bounded warm concurrency, compiled fallback routing timeouts, and
known provider rate gates; inspect that plan before activating a large catalog.

Provider statistics are process-lifetime operational observations. Credit
values are local request reservations unless a provider explicitly reports a
quota; they are not vendor billing statements. `Not tracked` is distinct from
zero usage. Success is the result rate of provider operations, not exchange
uptime; upstream HTTP calls are a separate latency surface and are not added to
the operation count. Stream state and reconnects are shown separately. Success
rate and latency have no value before the first observed attempt. Quota
snapshots refresh every 60 seconds and carry their own observation timestamp;
the bounded 2,048-sample percentile window and process-lifetime counters reset
on restart. Admission uses committed primary demand. Validation reports
uncapped fallback demand separately, while provider quota gates enforce the
reported hard-capped amount at runtime; do not read worst-case fallback demand
as simultaneously reserved credit.

## Routine checks

Daily:

1. Request the configured workbook symbols and inspect `partial`, `errors`,
   `quality`, and `fallback_level`.
2. Check OKX, Finnhub, Twelve Data, Alpha Vantage, CoinGecko, Ethereum RPC, and
   Binance staking route metrics.
3. Review 401, 429, 5xx, circuit-breaker, and WebSocket reconnect counts.
4. Check free disk space and journal growth.

Weekly:

1. Create an online SQLite backup and copy it away from the VPS.
2. Run `scripts/smoke_test.py` from another host.
3. Review reverse-proxy certificate renewal, NTP, and operating-system updates.
4. Verify that 1-minute, 5-minute, and daily retention is bounded at 48 hours,
   45 days, and 400 days.

Monthly, restore a backup into an isolated directory, run
`PRAGMA integrity_check`, and start an offline test instance.

## Online backup

The backup script uses the SQLite online backup API and runs an integrity check:

```bash
sudo -u quickprice /opt/quickprice/.venv/bin/python \
  /opt/quickprice/scripts/backup_sqlite.py \
  /var/lib/quickprice/quickprice.sqlite3 \
  --output-dir /var/lib/quickprice/backups
```

Copy the result to another machine or encrypted object storage. Do not copy only
an active `.sqlite3` file because committed data may still be in `-wal`.

On Windows, the script cannot replace NTFS ACL management. Store backups in a
directory restricted to the current account and verify the ACL separately.

## Restore

Restoration causes downtime. Verify the backup first:

```bash
/opt/quickprice/.venv/bin/python - <<'PY'
import sqlite3

path = "/secure/backups/quickprice-YYYYMMDDTHHMMSSZ.sqlite3"
with sqlite3.connect(path) as database:
    print(database.execute("PRAGMA integrity_check").fetchone()[0])
PY
```

The result must be `ok`. Then stop the writer and replace the database:

```bash
sudo systemctl stop quickprice
sudo install -o quickprice -g quickprice -m 0600 \
  /secure/backups/quickprice-YYYYMMDDTHHMMSSZ.sqlite3 \
  /var/lib/quickprice/quickprice.sqlite3.new
sudo -u quickprice cp \
  /var/lib/quickprice/quickprice.sqlite3 \
  /var/lib/quickprice/quickprice.sqlite3.before-restore
sudo rm -f \
  /var/lib/quickprice/quickprice.sqlite3-wal \
  /var/lib/quickprice/quickprice.sqlite3-shm
sudo mv \
  /var/lib/quickprice/quickprice.sqlite3.new \
  /var/lib/quickprice/quickprice.sqlite3
sudo systemctl start quickprice
```

Wait for registry validation, history restoration, and gap backfill. Verify
readiness and workbook symbols before deleting the pre-restore copy.

## Upgrade and rollback

Before an upgrade:

1. Make and export an online backup.
2. Save the current Git commit and virtual environment path.
3. Validate all enabled plugin wheels against the new core.
4. Use a maintenance window because the single instance briefly stops.

Example upgrade:

```bash
cd /opt/quickprice
git fetch --tags origin
git checkout <reviewed-commit>
UV_PROJECT_ENVIRONMENT=.venv uv sync --locked --no-dev \
  --python /opt/python-3.14.6t/bin/python3.14t
UV_PROJECT_ENVIRONMENT=.venv uv run quickprice plugins validate
sudo systemctl restart quickprice
```

Reload the independently managed reverse proxy only when its own configuration
changed. Run the smoke test. If startup fails, save the journal, return to the
previous commit and lockfile, synchronize again, and restart. If a schema
migration was applied, follow that release's migration notes; a code rollback
alone may not be sufficient.

For a Python patch upgrade, update the version and Python.org SHA-256 together.
Never bypass the source checksum or post-build GIL assertion.

## Failure procedures

### Liveness failure

1. Inspect `systemctl status quickprice` and the exit status.
2. Read `journalctl -u quickprice -b` for import, memory, and startup errors.
3. Check disk space and inodes before touching SQLite files.
4. Stop abusive Excel refreshes or load tests if event-loop lag is high.

HTTP requests must not wait on providers or SQLite. If API latency follows
provider latency, treat it as a release-blocking regression.

### Readiness failure

Check:

- the runtime is 3.14t with the GIL disabled after full imports;
- the durable API-key catalog contains at least one active, unexpired key;
- administrator factors and the exact public origin are complete when the
  public control plane is enabled;
- `/var/lib/quickprice` is writable by the service account;
- enabled plugins pass `quickprice plugins validate`;
- every required bond and staking income route has produced data;
- provider credentials and RPC chain IDs are correct;
- the background coordinator has not exited.

### One symbol is stale or unavailable

1. Inspect the symbol error, source components, price quality, and yield quality.
2. Check provider quotas, circuits, last success, and fallback counts.
3. Confirm DNS, TLS, provider status, and the relevant market session.
4. Wait for the 60-second half-open probe instead of restart-looping around a
   provider 429.

Synthetic prices reject over-age or over-skew components. A last cached value
must remain explicitly stale. WBETH yield may fall back from signed Binance APR
to the contract exchange-rate estimate and finally the declared 30-day
WBETH/ETH market-ratio proxy; the method and proxy flags must reveal the
fallback. Assets whose rewards increase units rather than unit value must use a
compatible provider-reported metric; QuickPrice rejects a market-ratio proxy
that cannot observe those units.

### Provider quota exhausted

- Do not rotate keys to bypass a free plan.
- Twelve Data reserves 765 of its 790 daily credits for FX quotes. History and
  listed-security fallback share the 25-credit general pool and cannot consume
  the quote reserve; the five cached USD hubs require at most 744 quote credits.
- Finnhub enforces a durable 60-call-per-minute local gate. Listed-security
  polling scales with the catalog and successful or expected-failure responses
  are cached briefly; do not lower the cadence below the calculated floor. A
  separate 29-call-per-second sliding gate protects cold starts, recent stream
  trades suppress REST polling, and closed-market checks use a 15-minute floor.
- Alpaca and Finnhub recovery probes continue at Finnhub's quota-safe floor
  after a listed-security route falls through. Twelve Data and Alpha Vantage
  fallback values or expected errors are cached per symbol until the next
  scheduled US session open; closed sessions use a 15-minute route-wide floor.
- Alpaca streams at most 30 symbols by default. Remaining symbols use the
  catalog-scaled REST cadence behind the shared 180-call-per-minute gate.
- FX scheduling continues to probe Twelve Data every 240 seconds for USD/CNH
  and every 900 seconds for the other USD hubs. Alpha FX fallback values and
  expected Alpha errors are cached per hub for six hours, so those primary
  probes do not consume another Alpha credit on every scheduler cycle.
- CoinGecko uses one successful all-symbol request no more than every ten
  minutes. Identifiable connection, TLS, DNS, proxy, and timeout failures retry
  indefinitely every five minutes, while explicit upstream and quota failures
  retain their protected backoff and hard credit ceiling.
- Binance's primary WBETH APR route uses only a read-only USER_DATA key.
- Accept a disclosed stale value or upgrade to an authorized paid plan.

### SQLite queue or WAL growth

Check disk space and passive checkpoint status:

```bash
sudo -u quickprice /opt/quickprice/.venv/bin/python -c \
  "import sqlite3; db=sqlite3.connect('/var/lib/quickprice/quickprice.sqlite3'); print(db.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone())"
```

Do not delete `-wal`, run `VACUUM` under load, or hide persistence failures.
Stop load generation, add disk capacity, and perform a graceful stop before a
cold repair.

### HTTPS failure

1. Check the selected reverse proxy's status, logs, certificate, and upstream
   health result.
2. Confirm that its upstream is the loopback QuickPrice listener and that it
   forwards `X-API-Key` unchanged.
3. Verify `/health/live` directly on loopback, then through the public origin.
4. Do not expose the application listener or disable TLS verification as a
   workaround.

### Key rotation

For the QuickPrice client credential:

1. Generate a new raw key and hash.
2. Temporarily configure both hashes.
3. Update Excel and run the smoke test.
4. Remove the old hash and restart QuickPrice.
5. Review sanitized reverse-proxy source IP logs if compromise is suspected.

Never solve Power Query authentication by putting the key in the URL.

For provider credentials, prepare a complete replacement
`provider-keys.env`, validate it with `quickprice plugins validate`, install it
atomically at the configured path, and restart QuickPrice. Confirm provider
routes and quota checkpoints before revoking the previous credentials. Do not
copy provider values into `quickprice.env` or the reverse-proxy environment.

## Provider fallback drill

1. Record current source, quality, components, quotas, and circuits.
2. In a controlled test configuration, disable one primary provider.
3. Confirm that three failures open its circuit and the HTTP cache remains fast.
   Network-class failures must continue fixed short half-open probes without an
   exponential retry ceiling; explicit HTTP and quota failures retain bounded
   exponential backoff.
4. Confirm fallback metadata and stale behavior.
5. Restore the provider, wait for the half-open probe, and confirm primary
   recovery without a request storm or quota reset.
6. Check writer queue, duplicate history, and WAL growth.

## Load and soak acceptance

Run authenticated requests in an isolated window. Temporarily set:

```dotenv
QUICKPRICE_RATE_LIMIT_ENABLED=false
```

Restart QuickPrice, then run from a separate client:

```bash
export QUICKPRICE_API_KEY='<raw-key>'
python scripts/load_test.py https://price.example.com \
  --concurrency 500 --rps 300 --duration 86400 \
  --max-p95-ms 100 --max-error-percent 0.1
```

Collect CPU, RSS, threads, file descriptors, TCP states, event-loop lag,
snapshots, provider quotas, circuits, SQLite queue, database size, WAL size, and
reverse-proxy TLS errors. Memory, file descriptors, connections, tasks, queues,
and WAL must not grow without bound. Restore rate limiting and run the smoke
test after the soak.

## Security incident

Rotate a leaked QuickPrice key immediately. Revoke and replace leaked provider
or RPC credentials using least privilege. Binance credentials must never have
trade or withdrawal permission. Preserve relevant journals and host audit logs,
but never enable logging that records request headers or secrets.

If untrusted third parties obtained access, also review provider redistribution
licenses and stop public access until the license boundary is understood.
