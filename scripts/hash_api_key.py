#!/usr/bin/env python3
"""Generate or hash a QuickPrice API key without putting it in shell history."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import secrets


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a QuickPrice key and its sha256:<hex> server-side hash."
    )
    parser.add_argument(
        "--generate",
        action="store_true",
        help="generate a new 256-bit URL-safe API key instead of prompting",
    )
    args = parser.parse_args()

    if args.generate:
        key = secrets.token_urlsafe(32)
    else:
        key = getpass.getpass("API key to hash (input is hidden): ")
        if not key:
            parser.error("the API key must not be empty")

    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()

    if args.generate:
        print("QuickPrice API key - save it in Excel now; it is shown only here:")
        print(key)
        print()
    print("Server-side .env value:")
    print(f"QUICKPRICE_API_KEY_HASHES=sha256:{digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
