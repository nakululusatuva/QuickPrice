#!/usr/bin/env python3
"""Run the public HTTPS deployment acceptance smoke test."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from urllib.parse import urlencode, urljoin, urlparse


def request_json(base_url: str, path: str, api_key: str) -> tuple[int, dict]:
    url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    request = urllib.request.Request(
        url,
        headers={"X-API-Key": api_key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(
            request, timeout=15, context=ssl.create_default_context()
        ) as response:
            raw = response.read()
            try:
                body = json.loads(raw)
            except json.JSONDecodeError, UnicodeDecodeError:
                body = {"raw": raw.decode("utf-8", errors="replace")[:500]}
            return response.status, body
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            body = json.loads(raw)
        except json.JSONDecodeError, UnicodeDecodeError:
            body = {
                "detail": exc.reason,
                "raw": raw.decode("utf-8", errors="replace")[:500],
            }
        return exc.code, body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", help="for example https://price.example.com")
    parser.add_argument(
        "--allow-http",
        action="store_true",
        help="permit plain HTTP for a local-only test",
    )
    args = parser.parse_args()

    parsed = urlparse(args.base_url)
    if parsed.scheme != "https" and not args.allow_http:
        parser.error("public acceptance tests require an https:// URL")

    api_key = os.environ.get("QUICKPRICE_API_KEY") or getpass.getpass(
        "QuickPrice API key (input is hidden): "
    )
    if not api_key:
        parser.error("QUICKPRICE_API_KEY is empty")

    failures: list[str] = []

    for health_path in ("/health/live", "/health/ready"):
        status, body = request_json(args.base_url, health_path, api_key)
        if status != 200:
            failures.append(f"{health_path}: expected 200, got {status}: {body}")

    symbols: list[str] = []
    status, body = request_json(args.base_url, "/v1/instruments", api_key)
    if status != 200:
        failures.append(f"/v1/instruments: expected 200, got {status}: {body}")
    else:
        instruments = body.get("data") or []
        symbols = [
            item.get("symbol")
            for item in instruments
            if isinstance(item, dict) and isinstance(item.get("symbol"), str)
        ]
        if not symbols:
            failures.append("/v1/instruments returned no configured symbols")
        for item in instruments:
            if not isinstance(item, dict):
                failures.append("instrument item is not a JSON object")
                continue
            for field in ("symbol", "name", "description", "asset_class", "asset_type"):
                if not item.get(field):
                    failures.append(f"{item.get('symbol', '?')}: missing instrument {field}")

    returned: set[str] = set()
    for offset in range(0, len(symbols), 100):
        batch = symbols[offset : offset + 100]
        path = f"/v1/quotes?{urlencode({'symbols': ','.join(batch)})}"
        status, body = request_json(args.base_url, path, api_key)
        if status != 200:
            failures.append(f"{path}: expected 200, got {status}: {body}")
            continue
        required_envelope = {
            "schema_version",
            "request_id",
            "generated_at",
            "partial",
            "data",
            "errors",
        }
        missing_envelope = required_envelope.difference(body)
        if missing_envelope:
            failures.append(f"missing envelope fields: {sorted(missing_envelope)}")
        if body.get("schema_version") != "1.1":
            failures.append(f"unexpected schema_version: {body.get('schema_version')!r}")

        quotes = body.get("data") or []
        returned.update(item.get("symbol") for item in quotes if isinstance(item, dict))

        for item in quotes:
            if not isinstance(item, dict):
                failures.append("quote item is not a JSON object")
                continue
            for field in (
                "symbol",
                "name",
                "description",
                "asset_class",
                "asset_type",
                "price",
                "source",
                "quality",
            ):
                if field not in item:
                    failures.append(f"{item.get('symbol', '?')}: missing {field}")
            changes = item.get("changes") or {}
            if "1y" not in changes:
                failures.append(f"{item.get('symbol', '?')}: missing changes.1y")
            if "staking" in str(item.get("asset_type", "")):
                if not item.get("reward_accrual_mode") or not item.get("underlying_asset"):
                    failures.append(f"{item.get('symbol', '?')}: missing staking classification")
                annual_yield = item.get("estimated_annual_yield")
                if not isinstance(annual_yield, dict):
                    failures.append(f"{item.get('symbol', '?')}: missing required staking yield")
                else:
                    for field in (
                        "percent",
                        "rate_type",
                        "accrual_mode",
                        "fallback_level",
                        "quality",
                    ):
                        if annual_yield.get(field) is None:
                            failures.append(f"{item.get('symbol', '?')}: missing yield {field}")

    missing_symbols = set(symbols).difference(returned)
    if missing_symbols:
        failures.append(f"missing usable quotes: {sorted(missing_symbols)}")

    # Authentication must fail closed. Deliberately use a fixed invalid value.
    invalid_path = "/v1/quotes?symbols=BTC%3AUSDC"
    invalid_status, _ = request_json(args.base_url, invalid_path, "smoke-test-invalid-key")
    if invalid_status != 401:
        failures.append(f"invalid API key: expected 401, got {invalid_status}")

    if failures:
        print("QuickPrice smoke test FAILED", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    transport = "HTTP (local test)" if parsed.scheme == "http" else "HTTPS"
    print(
        f"QuickPrice smoke test passed: {transport}, readiness, authentication, "
        f"schema, and {len(symbols)} configured symbols are usable."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
