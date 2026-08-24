"""Command-line entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from market_scanner.blowing_reporting import write_reports as write_blowing_reports
from market_scanner.blowing_sources import (
    AlpacaBlowingSource,
    DemoBlowingSource,
    discover_universe,
)
from market_scanner.blowing_stocks import BlowingStocksScreener
from market_scanner.config import (
    ROOT,
    load_blowing_stocks_config,
    load_config,
    load_multibagger_config,
    load_symbols,
)
from market_scanner.float_data import FloatProvider
from market_scanner.fundamentals import FundamentalsProvider
from market_scanner.index_reporting import write_index
from market_scanner.multibagger import build_result as build_multibagger_result
from market_scanner.multibagger_reporting import (
    write_reports as write_multibagger_reports,
)
from market_scanner.providers import AlpacaProvider, DemoProvider, YahooProvider
from market_scanner.providers.alpaca import ProviderError
from market_scanner.reporting import write_reports
from market_scanner.scanner import scan_market
from market_scanner.volatility_reporting import write_reports as write_volatility_reports


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="market-scanner", description="Build a filtered US equity premarket watchlist."
    )
    root.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    commands = root.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="run a market scan")
    scan.add_argument("--provider", choices=("alpaca", "yahoo", "demo"), default="yahoo")
    scan.add_argument("--config")
    scan.add_argument("--symbols", help="comma-separated symbol override")
    scan.add_argument("--universe")
    scan.add_argument("--output-dir")
    scan.add_argument("--feed", choices=("iex", "sip", "delayed_sip"))
    fundamentals = scan.add_mutually_exclusive_group()
    fundamentals.add_argument(
        "--fundamentals",
        dest="fundamentals",
        action="store_true",
        default=None,
        help="apply SEC EDGAR fundamental gates and blend them into the ranking",
    )
    fundamentals.add_argument(
        "--no-fundamentals", dest="fundamentals", action="store_false", help=argparse.SUPPRESS
    )

    baggers = commands.add_parser(
        "multibagger",
        help="report which universe names have already multiplied (historical, not a forecast)",
    )
    baggers.add_argument("--provider", choices=("yahoo",), default="yahoo")
    baggers.add_argument("--config")
    baggers.add_argument("--symbols", help="comma-separated symbol override")
    baggers.add_argument("--universe")
    baggers.add_argument("--output-dir")
    baggers.add_argument(
        "--min-multiple", type=float, help="override the minimum multiple (2.0 == doubled)"
    )

    blowing = commands.add_parser(
        "blowing-stocks",
        help="BlowingStocksScreener: low-float momentum and catalyst-breakout screens",
    )
    blowing.add_argument("--provider", choices=("alpaca", "demo"), default="alpaca")
    blowing.add_argument(
        "--feed",
        choices=("delayed_sip", "iex", "sip"),
        help="delayed_sip (default) is the full tape 15 minutes late; iex is live but "
        "carries only a few percent of volume, which understates every volume gate",
    )
    blowing.add_argument("--config")
    blowing.add_argument("--output-dir")
    blowing.add_argument("--symbols", help="comma-separated symbol override; skips discovery")
    blowing.add_argument("--universe", help="file of symbols to screen; skips discovery")
    blowing.add_argument(
        "--max-symbols",
        type=int,
        help="ceiling on symbols carried into the expensive measurement stage",
    )
    blowing.add_argument(
        "--retention-days",
        type=int,
        help="days of dated reports to keep under history/ (default 7)",
    )
    floats = blowing.add_mutually_exclusive_group()
    floats.add_argument(
        "--require-float",
        dest="require_float",
        action="store_true",
        default=None,
        help="reject symbols whose float cannot be proven (the default)",
    )
    floats.add_argument(
        "--allow-unproven-float",
        dest="require_float",
        action="store_false",
        help="keep symbols with no filed float and flag the gate as unproven",
    )
    return root


def _symbols_from(args: argparse.Namespace) -> list[str]:
    if args.symbols:
        return list(
            dict.fromkeys(item.strip().upper() for item in args.symbols.split(",") if item.strip())
        )
    return load_symbols(args.universe)


async def _multibagger(args: argparse.Namespace) -> int:
    _, provider_config, output_config = load_config(args.config)
    config = load_multibagger_config(args.config)
    if args.min_multiple is not None:
        config = replace(config, min_multiple=args.min_multiple)
    symbols = _symbols_from(args)
    if not symbols:
        raise ValueError("no symbols supplied")
    provider = YahooProvider(timeout=int(provider_config.get("request_timeout_seconds", 30)))
    history, warnings = await provider.get_long_history(symbols, config.history_years)
    result = build_multibagger_result(
        history=history,
        symbols=symbols,
        config=config,
        provider=provider.name,
        generated_at=datetime.now(UTC).isoformat(),
        warnings=warnings,
    )
    output_dir = Path(args.output_dir or output_config.get("directory") or "AllScreenersResults")
    paths = write_multibagger_reports(result, output_dir, run_date=date.today())
    index_path = write_index(output_dir)
    print(
        json.dumps(
            {
                "report": "multibagger",
                "provider": result.provider,
                "scanned": result.symbols_scanned,
                "qualified": result.symbols_qualified,
                "outputs": {name: str(path) for name, path in paths.items()},
                "index": str(index_path),
                "warnings": result.warnings,
            },
            indent=2,
        )
    )
    return 0


def _blowing_universe(args: argparse.Namespace, config, warnings: list[str]) -> list[str]:
    """Decide what population to screen, cheapest explicit source first."""
    if args.symbols:
        return list(
            dict.fromkeys(item.strip().upper() for item in args.symbols.split(",") if item.strip())
        )
    requested = args.universe or config.universe_file
    if requested:
        return load_symbols(requested)
    symbols, notes = discover_universe()
    warnings.extend(notes)
    return symbols


async def _blowing_stocks(args: argparse.Namespace) -> int:
    _, provider_config, output_config = load_config(args.config)
    config = load_blowing_stocks_config(args.config)
    if args.max_symbols is not None:
        config = replace(config, max_deep_scan_symbols=args.max_symbols)
    if args.retention_days is not None:
        config = replace(config, history_retention_days=args.retention_days)
    if args.require_float is not None:
        config = replace(config, require_float=args.require_float)

    # An explicit --output-dir is used exactly as given; the default matches
    # the other two screeners so every report lands in one shared directory.
    # Filenames are screener-prefixed and date-stamped, so nothing collides.
    output_dir = Path(
        args.output_dir or output_config.get("directory") or "AllScreenersResults"
    )
    warnings: list[str] = []
    symbols = _blowing_universe(args, config, warnings)
    if not symbols:
        raise ValueError("no symbols to screen")

    timeout = int(provider_config.get("request_timeout_seconds", 30))
    if args.provider == "demo":
        source = DemoBlowingSource()
    else:
        overrides = Path(config.float_overrides_file)
        if not overrides.is_absolute():
            overrides = ROOT / overrides
        source = AlpacaBlowingSource(
            feed=args.feed or "delayed_sip",
            timeout=timeout,
            float_provider=FloatProvider(
                timeout=timeout,
                overrides_path=overrides,
                cache_path=output_dir / "float-cache.json",
                cache_days=config.float_cache_days,
                max_age_days=config.max_float_age_days,
            ),
        )

    snapshots, source_warnings, session_date, phase, examined = await asyncio.to_thread(
        source.collect, symbols, datetime.now(UTC), config
    )
    warnings.extend(source_warnings)
    result = BlowingStocksScreener(config).run(
        snapshots,
        provider=source.name,
        generated_at=datetime.now(UTC).isoformat(),
        session_date=session_date or datetime.now(UTC).date(),
        feed=getattr(source, "feed", "demo"),
        session_phase=phase,
        universe_size=len(symbols),
        warnings=warnings,
    )
    written = write_blowing_reports(
        result, output_dir, retention_days=config.history_retention_days, run_date=date.today()
    )
    index_path = write_index(output_dir)
    print(
        json.dumps(
            {
                "report": "blowing-stocks",
                "provider": result.provider,
                "feed": result.feed,
                "session_date": result.session_date,
                "session_phase": result.session_phase,
                "universe": len(symbols),
                "measured": examined,
                "low_float": len(result.low_float),
                "catalyst": len(result.catalyst),
                "outputs": {name: str(path) for name, path in written["paths"].items()},
                "index": str(index_path),
                "pruned": [path.name for path in written["pruned"]],
                "warnings": result.warnings,
            },
            indent=2,
        )
    )
    return 0


async def _scan(args: argparse.Namespace) -> int:
    config, provider_config, output_config = load_config(args.config)
    if args.fundamentals is not None:
        config = replace(config, use_fundamentals=args.fundamentals)
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
    # The demo provider promises a deterministic, fully offline pipeline, so it
    # never reaches SEC EDGAR even when fundamentals are configured on.
    fundamentals_provider = None
    if config.use_fundamentals:
        if args.provider == "demo":
            config = replace(config, use_fundamentals=False, require_fundamentals=False)
        else:
            fundamentals_provider = FundamentalsProvider(
                timeout=int(provider_config.get("request_timeout_seconds", 30))
            )
    result = await scan_market(
        provider, symbols, config, fundamentals_provider=fundamentals_provider
    )
    output_dir = Path(args.output_dir or output_config.get("directory") or "AllScreenersResults")
    run_date = date.today()
    paths = write_reports(result.to_dict(), output_dir, run_date=run_date)
    # Always written, gated names or not: a separate report from market-scan
    # itself, so the high-volatility list never appears inside it.
    volatility_paths = write_volatility_reports(result.to_dict(), output_dir, run_date=run_date)
    index_path = write_index(output_dir)
    summary = {
        "provider": result.provider,
        "scanned": result.symbols_scanned,
        "qualified": result.symbols_qualified,
        "outputs": {name: str(path) for name, path in paths.items()},
        "volatility_outputs": {name: str(path) for name, path in volatility_paths.items()},
        "index": str(index_path),
        "warnings": result.warnings,
    }
    print(json.dumps(summary, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "scan":
            return asyncio.run(_scan(args))
        if args.command == "multibagger":
            return asyncio.run(_multibagger(args))
        if args.command == "blowing-stocks":
            return asyncio.run(_blowing_stocks(args))
    except (ProviderError, OSError, ValueError) as error:
        print(f"market-scanner: {error}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
