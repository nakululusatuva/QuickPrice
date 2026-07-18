# QuickPrice Native Operations Guide

This guide covers one QuickPrice process on a Linux VPS with a local persistent
SQLite database. Docker is not supported. Never run two QuickPrice processes
against the same database.

QuickPrice reads `/etc/quickprice/quickprice.env`. Caddy reads only
`/etc/caddy/quickprice.env`; it must never inherit provider credentials or the
QuickPrice API-key hashes. Expected ownership and modes are:

```text
/etc/quickprice/quickprice.env  root:quickprice 0640
/etc/caddy/quickprice.env       root:caddy      0640
/var/lib/quickprice             quickprice      0700
```

## Normal baseline

```bash
systemctl status quickprice caddy
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
- no sustained event-loop lag;
- expected provider quotas, circuits, and fallback levels;
- snapshot age consistent with market status and provider cadence.

Suggested alerts:

| Signal | Warning | Critical |
|---|---:|---:|
| Readiness | One failure | Three failures in two minutes |
| Request p95 | Above 100 ms for five minutes | Above 250 ms for five minutes |
| Unexpected 5xx | Above 0.1% for five minutes | Above 1% for five minutes |
| Event-loop lag | Above 100 ms for five minutes | Above 500 ms for two minutes |
| SQLite writer queue | Does not drain for five minutes | Grows for 15 minutes |
| Disk free | Below 20% | Below 10% or 2 GB |
| Active crypto quote age | Above 15 seconds | Above 60 seconds |
| Open equity quote age | Above 30 seconds | Above five minutes |
| USD/CNH quote age | Above five minutes | Above 15 minutes |

Closed equity markets do not produce trades. Evaluate quote age together with
`market_status`. Yield quality is independent of price quality; alert on both.

## Routine checks

Daily:

1. Request the configured workbook symbols and inspect `partial`, `errors`,
   `quality`, and `fallback_level`.
2. Check Twelve Data, Alpha Vantage, CoinGecko, Ethereum RPC, and Binance
   staking route metrics.
3. Review 401, 429, 5xx, circuit-breaker, and WebSocket reconnect counts.
4. Check free disk space and journal growth.

Weekly:

1. Create an online SQLite backup and copy it away from the VPS.
2. Run `scripts/smoke_test.py` from another host.
3. Review certificate renewal, NTP, and operating-system updates.
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
sudo systemctl restart caddy
```

Run the smoke test. If startup fails, save the journal, return to the previous
commit and lockfile, synchronize again, and restart. If a schema migration was
applied, follow that release's migration notes; a code rollback alone may not be
sufficient.

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
- `QUICKPRICE_API_KEY_HASHES` contains a valid `sha256:<64 hex>` value;
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
must remain explicitly stale. WBETH yield may fall back from contract index to
Binance rate history and finally the declared 30-day WBETH/ETH market-ratio
proxy; the method and proxy flags must reveal the fallback. For staking assets
whose rewards increase units rather than unit value, the same declared
price-ratio fallback can omit those units and must remain a low-confidence
market proxy rather than a protocol-reported rate.

### Provider quota exhausted

- Do not rotate keys to bypass a free plan.
- Twelve Data reserves its final credits for FX components.
- Alpha FX uses a six-hour emergency cadence.
- CoinGecko uses one all-symbol request no more than every five minutes.
- Binance staking fallback uses only a read-only USER_DATA key.
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

1. Confirm A/AAAA records, inbound TCP 80/443, and system time.
2. Read `journalctl -u caddy`.
3. Remove an invalid AAAA record if IPv6 is not correctly routed.
4. Do not expose port 8080 or disable TLS verification as a workaround.

### Key rotation

1. Generate a new raw key and hash.
2. Temporarily configure both hashes.
3. Update Excel and run the smoke test.
4. Remove the old hash and restart QuickPrice.
5. Review Caddy source IP logs if compromise is suspected.

Never solve Power Query authentication by putting the key in the URL.

## Provider fallback drill

1. Record current source, quality, components, quotas, and circuits.
2. In a controlled test configuration, disable one primary provider.
3. Confirm that three failures open its circuit and the HTTP cache remains fast.
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
Caddy TLS errors. Memory, file descriptors, connections, tasks, queues, and WAL
must not grow without bound. Restore rate limiting and run the smoke test after
the soak.

## Security incident

Rotate a leaked QuickPrice key immediately. Revoke and replace leaked provider
or RPC credentials using least privilege. Binance credentials must never have
trade or withdrawal permission. Preserve relevant journals and host audit logs,
but never enable logging that records request headers or secrets.

If untrusted third parties obtained access, also review provider redistribution
licenses and stop public access until the license boundary is understood.
