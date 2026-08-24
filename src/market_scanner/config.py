"""TOML configuration and symbol-universe loading."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from market_scanner.blowing_stocks import BlowingStocksConfig, config_from_mapping
from market_scanner.models import ScanConfig
from market_scanner.multibagger import MultibaggerConfig

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DATA = Path(__file__).with_name("data")


def _read_config_payload(path: str | Path | None = None) -> dict:
    """Locate and parse the TOML config both loaders share.

    A non-editable install resolves ROOT into site-packages, where no
    ``config/`` exists, so the packaged copy is the fallback.
    """
    requested = path or os.environ.get("MARKET_SCANNER_CONFIG")
    if requested:
        config_path = Path(requested)
        if not config_path.exists():
            raise FileNotFoundError(f"config file not found: {config_path}")
    else:
        repository_config = ROOT / "config/scanner.toml"
        config_path = (
            repository_config if repository_config.exists() else PACKAGE_DATA / "scanner.toml"
        )
    with config_path.open("rb") as handle:
        return tomllib.load(handle)


def load_config(path: str | Path | None = None) -> tuple[ScanConfig, dict, dict]:
    payload = _read_config_payload(path)
    scanner_values = payload.get("scanner", {})
    known = set(ScanConfig.__dataclass_fields__)
    unknown = set(scanner_values) - known
    if unknown:
        raise ValueError(f"Unknown scanner config keys: {', '.join(sorted(unknown))}")
    return ScanConfig(**scanner_values), payload.get("provider", {}), payload.get("output", {})


def load_multibagger_config(path: str | Path | None = None) -> MultibaggerConfig:
    """Read the optional ``[multibagger]`` section.

    Kept separate from :func:`load_config` so adding the multibagger report
    does not change that function's established return shape.
    """
    payload = _read_config_payload(path)
    values = payload.get("multibagger", {})
    known = set(MultibaggerConfig.__dataclass_fields__)
    unknown = set(values) - known
    if unknown:
        raise ValueError(f"Unknown multibagger config keys: {', '.join(sorted(unknown))}")
    if "horizons_years" in values:
        values = {**values, "horizons_years": tuple(values["horizons_years"])}
    return MultibaggerConfig(**values)


def load_blowing_stocks_config(path: str | Path | None = None) -> BlowingStocksConfig:
    """Read the optional ``[blowing_stocks]`` section.

    Separate from :func:`load_config` for the same reason the multibagger
    loader is: the screener has its own gates, its own nested screens, and its
    own defaults, and adding it must not change what the scan loader returns.
    """
    payload = _read_config_payload(path)
    return config_from_mapping(payload.get("blowing_stocks", {}))


def load_symbols(path: str | Path | None = None) -> list[str]:
    requested = path or os.environ.get("MARKET_SCANNER_UNIVERSE")
    if requested:
        universe_path = Path(requested)
        if not universe_path.exists():
            raise FileNotFoundError(f"universe file not found: {universe_path}")
    else:
        repository_universe = ROOT / "config/universe.txt"
        universe_path = (
            repository_universe if repository_universe.exists() else PACKAGE_DATA / "universe.txt"
        )
    lines = universe_path.read_text(encoding="utf-8").splitlines()
    symbols = [line.strip().upper() for line in lines if line.strip() and not line.startswith("#")]
    if not symbols:
        raise ValueError("symbol universe is empty")
    return list(dict.fromkeys(symbols))
