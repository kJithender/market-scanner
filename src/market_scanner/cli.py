"""Command-line entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from market_scanner.config import load_config, load_symbols
from market_scanner.providers import AlpacaProvider, DemoProvider, YahooProvider
from market_scanner.providers.alpaca import ProviderError
from market_scanner.reporting import write_reports
from market_scanner.scanner import scan_market


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="market-scanner", description="Build a filtered US equity premarket watchlist."
    )
    root.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    commands = root.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="run a market scan")
    scan.add_argument("--provider", choices=("alpaca", "yahoo", "demo"), default="alpaca")
    scan.add_argument("--config")
    scan.add_argument("--symbols", help="comma-separated symbol override")
    scan.add_argument("--universe")
    scan.add_argument("--output-dir")
    scan.add_argument("--feed", choices=("iex", "sip", "delayed_sip"))
    return root


async def _scan(args: argparse.Namespace) -> int:
    config, provider_config, output_config = load_config(args.config)
    symbols = (
        list(
            dict.fromkeys(item.strip().upper() for item in args.symbols.split(",") if item.strip())
        )
        if args.symbols
        else load_symbols(args.universe)
    )
    if not symbols:
        raise ValueError("no symbols supplied")
    if args.provider == "demo":
        provider = DemoProvider()
    elif args.provider == "yahoo":
        provider = YahooProvider(
            timeout=int(provider_config.get("request_timeout_seconds", 30)),
        )
    else:
        provider = AlpacaProvider(
            feed=args.feed or provider_config.get("feed", "iex"),
            timeout=int(provider_config.get("request_timeout_seconds", 30)),
        )
    result = await scan_market(provider, symbols, config)
    output_dir = Path(args.output_dir or output_config.get("directory") or "artifacts")
    paths = write_reports(result.to_dict(), output_dir)
    summary = {
        "provider": result.provider,
        "scanned": result.symbols_scanned,
        "qualified": result.symbols_qualified,
        "outputs": {name: str(path) for name, path in paths.items()},
        "warnings": result.warnings,
    }
    print(json.dumps(summary, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "scan":
            return asyncio.run(_scan(args))
    except (ProviderError, OSError, ValueError) as error:
        print(f"market-scanner: {error}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
