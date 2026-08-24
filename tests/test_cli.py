from __future__ import annotations

import json
from pathlib import Path

from market_scanner.cli import main
from market_scanner.config import load_config


def test_demo_cli_writes_all_reports(tmp_path: Path) -> None:
    code = main(["scan", "--provider", "demo", "--output-dir", str(tmp_path)])
    assert code == 0
    assert {path.suffix for path in tmp_path.iterdir()} == {".json", ".csv", ".md", ".html"}
    report = json.loads((tmp_path / "market-scan.json").read_text(encoding="utf-8"))
    # The CLI reads config/scanner.toml, so compare against that same file
    # rather than the dataclass default.
    assert report["candidate_count"] == load_config()[0].watchlist_size
    assert report["provider"] == "demo"
    # The demo pipeline must stay fully offline, so no SEC fundamentals are
    # attached even when the shipped config enables them.
    assert all(candidate["fundamental_score"] is None for candidate in report["candidates"])
    assert all(candidate["passed_filters"] for candidate in report["candidates"])


def test_live_cli_fails_closed_without_credentials(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    assert main(["scan", "--provider", "alpaca", "--output-dir", str(tmp_path)]) == 2
    assert not list(tmp_path.iterdir())


def test_blowing_stocks_cli_writes_reports_and_a_dated_archive(tmp_path: Path) -> None:
    code = main(
        [
            "blowing-stocks",
            "--provider",
            "demo",
            "--symbols",
            "AAA,BBB",
            "--output-dir",
            str(tmp_path),
        ]
    )
    assert code == 0
    written = {path.name for path in tmp_path.iterdir() if path.is_file()}
    assert written == {
        "blowing-stocks.json",
        "blowing-stocks.csv",
        "blowing-stocks.md",
        "blowing-stocks.html",
    }
    archive = list((tmp_path / "history").glob("blowing-stocks-*.json"))
    assert len(archive) == 1

    report = json.loads((tmp_path / "blowing-stocks.json").read_text(encoding="utf-8"))
    assert report["report"] == "blowing-stocks"
    assert report["screener"] == "BlowingStocksScreener"
    assert [row["symbol"] for row in report["low_float"]] == ["AAA", "BBB"]
    # The demo path must stay offline: no float provider, no EDGAR, and every
    # row labelled so a synthetic run can never be mistaken for a real one.
    assert all("DEMO" in row["float_basis"] for row in report["low_float"])
    assert any("DEMO DATA" in warning for warning in report["warnings"])


def test_blowing_stocks_cli_fails_closed_without_credentials(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    code = main(
        [
            "blowing-stocks",
            "--provider",
            "alpaca",
            "--symbols",
            "AAA",
            "--output-dir",
            str(tmp_path),
        ]
    )
    assert code == 2
    assert not list(tmp_path.iterdir())


def test_blowing_stocks_cli_honours_the_retention_override(tmp_path: Path) -> None:
    """The archive window is an operator decision, so the flag has to reach it."""
    stale = tmp_path / "history" / "blowing-stocks-2020-01-01.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}", encoding="utf-8")
    code = main(
        [
            "blowing-stocks",
            "--provider",
            "demo",
            "--symbols",
            "AAA",
            "--retention-days",
            "1",
            "--output-dir",
            str(tmp_path),
        ]
    )
    assert code == 0
    assert not stale.exists()
