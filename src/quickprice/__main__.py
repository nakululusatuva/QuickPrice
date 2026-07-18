"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import secrets

import uvicorn

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
    )


if __name__ == "__main__":
    main()
