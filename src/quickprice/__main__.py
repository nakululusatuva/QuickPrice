"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import secrets
from urllib.parse import quote, urlencode, urlsplit

import uvicorn

from .admin_security import (
    create_admin_key_verifier,
    generate_admin_key,
    generate_totp_secret,
)
from .auth import hash_api_key
from .config import Settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quickprice")
    subparsers = parser.add_subparsers(dest="command")
    serve = subparsers.add_parser("serve", help="run the HTTP service")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    key = subparsers.add_parser("hash-key", help="hash an API key without placing it in argv")
    key.add_argument("--generate", action="store_true", help="generate a new high-entropy key")
    admin = subparsers.add_parser(
        "admin-credentials",
        help="generate the local-only administrator key and TOTP enrollment values",
    )
    admin.add_argument("--origin", required=True, help="exact public origin, including scheme")
    admin.add_argument("--account", default="admin", help="TOTP account label")
    plugins = subparsers.add_parser("plugins", help="inspect trusted instrument plugins")
    plugin_commands = plugins.add_subparsers(dest="plugin_command", required=True)
    plugin_commands.add_parser("list", help="list enabled plugins and their instruments")
    plugin_commands.add_parser("validate", help="validate enabled plugin declarations")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "hash-key":
        raw_key = secrets.token_urlsafe(32) if args.generate else getpass.getpass("API key: ")
        if args.generate:
            print(f"API key (save it now): {raw_key}")
        print(f"Configured hash: {hash_api_key(raw_key)}")
        return
    if args.command == "admin-credentials":
        origin = args.origin.rstrip("/")
        parsed_origin = urlsplit(origin)
        try:
            _ = parsed_origin.port
        except ValueError as exc:
            raise SystemExit("administrator origin has an invalid port") from exc
        local_http = parsed_origin.scheme == "http" and parsed_origin.hostname in {
            "127.0.0.1",
            "::1",
            "localhost",
        }
        if (
            (parsed_origin.scheme != "https" and not local_http)
            or not parsed_origin.hostname
            or parsed_origin.username is not None
            or parsed_origin.password is not None
            or parsed_origin.path
            or parsed_origin.query
            or parsed_origin.fragment
        ):
            raise SystemExit("administrator origin must use HTTPS outside local development")
        raw_key = generate_admin_key()
        totp_secret = generate_totp_secret()
        verifier = create_admin_key_verifier(raw_key)
        label = quote(f"QuickPrice:{args.account}")
        query = urlencode(
            {
                "secret": totp_secret,
                "issuer": "QuickPrice",
                "algorithm": "SHA1",
                "digits": "6",
                "period": "30",
            }
        )
        print(f"Administrator key (save it now): {raw_key}")
        print(f"TOTP secret (enroll it now): {totp_secret}")
        print(f"TOTP URI: otpauth://totp/{label}?{query}")
        print(f"QUICKPRICE_ADMIN_KEY_VERIFIER={verifier}")
        print(f"QUICKPRICE_ADMIN_TOTP_SECRET={totp_secret}")
        print(f"QUICKPRICE_ADMIN_ORIGIN={origin}")
        return
    settings = Settings.from_env()
    if args.command == "plugins":
        from .registry import build_registry

        registry = build_registry(settings.enabled_plugins)
        if args.plugin_command == "list":
            for plugin in registry.plugins:
                print(
                    f"{plugin.plugin_id}\t{plugin.version}\t{len(plugin.instruments)} instruments"
                )
            return
        from .providers.wiring import build_provider_graph

        async def validate() -> None:
            graph = build_provider_graph(settings, registry, strict=True)
            await graph.close()

        asyncio.run(validate())
        print(f"Validated {len(registry.plugins)} plugins and {len(registry)} instruments.")
        return
    uvicorn.run(
        "quickprice.api:app",
        host=getattr(args, "host", None) or settings.host,
        port=getattr(args, "port", None) or settings.port,
        workers=1,
        loop="asyncio",
        http="h11",
        log_level=settings.log_level,
        access_log=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
