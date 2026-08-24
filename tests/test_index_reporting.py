from __future__ import annotations

from datetime import date
from pathlib import Path

from market_scanner.index_reporting import (
    INDEX_FILENAME,
    ReportEntry,
    discover_reports,
    render_index,
    write_index,
)


def _touch(directory: Path, name: str) -> None:
    (directory / name).write_text("x", encoding="utf-8")


def test_discover_reports_on_a_missing_directory_is_empty(tmp_path: Path) -> None:
    assert discover_reports(tmp_path / "absent") == []


def test_discover_reports_groups_formats_for_the_same_screener_and_date(
    tmp_path: Path,
) -> None:
    for ext in ("json", "csv", "md", "html"):
        _touch(tmp_path, f"market-scan-23-08-2026.{ext}")

    entries = discover_reports(tmp_path)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.screener == "market-scan"
    assert entry.run_date == date(2026, 8, 23)
    assert set(entry.formats) == {"json", "csv", "md", "html"}
    assert entry.formats["html"] == "market-scan-23-08-2026.html"


def test_discover_reports_treats_high_volatility_as_its_own_screener(
    tmp_path: Path,
) -> None:
    for ext in ("json", "csv", "md", "html"):
        _touch(tmp_path, f"High-volatility-23-08-2026.{ext}")
    _touch(tmp_path, "market-scan-23-08-2026.html")

    entries = {entry.screener: entry for entry in discover_reports(tmp_path)}

    assert entries["High-volatility"].run_date == date(2026, 8, 23)
    assert set(entries["High-volatility"].formats) == {"json", "csv", "md", "html"}
    assert entries["market-scan"].screener != "High-volatility"


def test_discover_reports_ignores_unrelated_and_malformed_files(tmp_path: Path) -> None:
    for name in (
        "notes.txt",
        "float-cache.json",
        "market-scan.json",  # undated, not this module's pattern
        "market-scan-not-a-date.json",
        "market-scan-32-13-2026.json",  # no such calendar date
        "index.html",
    ):
        _touch(tmp_path, name)

    assert discover_reports(tmp_path) == []


def test_discover_reports_separates_different_screeners_and_dates(tmp_path: Path) -> None:
    _touch(tmp_path, "market-scan-23-08-2026.html")
    _touch(tmp_path, "market-scan-22-08-2026.html")
    _touch(tmp_path, "blowing-stocks-23-08-2026.html")

    entries = discover_reports(tmp_path)

    assert len(entries) == 3
    keys = {(entry.screener, entry.run_date) for entry in entries}
    assert keys == {
        ("market-scan", date(2026, 8, 23)),
        ("market-scan", date(2026, 8, 22)),
        ("blowing-stocks", date(2026, 8, 23)),
    }


def test_render_index_marks_the_newest_report_as_latest() -> None:
    entries = [
        ReportEntry("market-scan", date(2026, 8, 23), {"html": "a.html"}),
        ReportEntry("market-scan", date(2026, 8, 20), {"html": "b.html"}),
    ]
    page = render_index(entries, generated_at="2026-08-23T00:00:00+00:00")

    assert page.index("Latest") < page.index("20 Aug 2026")
    assert "23 Aug 2026" in page
    assert "a.html" in page
    assert "b.html" in page


def test_render_index_reports_empty_screeners_honestly() -> None:
    page = render_index([], generated_at="2026-08-23T00:00:00+00:00")

    # market-scan, High-volatility, multibagger, blowing-stocks
    assert page.count("No reports found yet.") == 4
    assert "Market Scan" in page
    assert "High Volatility" in page
    assert "Multibagger" in page
    assert "Blowing Stocks" in page


def test_render_index_links_the_high_volatility_report() -> None:
    entries = [
        ReportEntry(
            "High-volatility",
            date(2026, 8, 23),
            {"html": "High-volatility-23-08-2026.html", "csv": "High-volatility-23-08-2026.csv"},
        )
    ]
    page = render_index(entries, generated_at="2026-08-23T00:00:00+00:00")

    assert "High Volatility" in page
    assert "High-volatility-23-08-2026.html" in page
    assert "High-volatility-23-08-2026.csv" in page


def test_write_index_writes_the_file_and_rebuilds_from_disk_each_call(tmp_path: Path) -> None:
    _touch(tmp_path, "market-scan-23-08-2026.html")

    path = write_index(tmp_path)

    assert path == tmp_path / INDEX_FILENAME
    assert path.exists()
    first = path.read_text(encoding="utf-8")
    assert "market-scan-23-08-2026.html" in first

    # A second run reflects new files without needing anything carried over
    # from the first — the index has no state of its own.
    _touch(tmp_path, "multibagger-23-08-2026.html")
    write_index(tmp_path)
    second = path.read_text(encoding="utf-8")
    assert "multibagger-23-08-2026.html" in second


def test_the_index_file_itself_is_never_listed_as_a_report(tmp_path: Path) -> None:
    _touch(tmp_path, "market-scan-23-08-2026.html")
    write_index(tmp_path)

    # Rebuilding again must not pick up index.html as if it were a report.
    entries = discover_reports(tmp_path)
    assert all(INDEX_FILENAME not in entry.formats.values() for entry in entries)
