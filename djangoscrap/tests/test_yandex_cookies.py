"""Tests for Netscape-format Yandex session cookies (Playwright payload conversion)."""

from __future__ import annotations

from pathlib import Path


def test_playwright_cookie_list_matches_page_host(tmp_path):
    from djangoscrap.ingestion_cookies import yandex_playwright_cookie_list

    cookies = tmp_path / "cookies.txt"
    cookies.write_text(
        "# Netscape HTTP Cookie File\n"
        ".yandex.ru\tTRUE\t/\tTRUE\t1999999999\tx\ty\n",
        encoding="utf-8",
    )
    items = yandex_playwright_cookie_list(Path(cookies), "https://yandex.ru/images/")
    assert len(items) == 1
    assert items[0]["name"] == "x"
