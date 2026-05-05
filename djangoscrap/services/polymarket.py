"""Thin Polymarket Gamma API client.

Public, read-only — no API key needed. We only care about whether a market is
closed, which outcome won, and current YES/NO prices for live snapshotting.

Docs: https://docs.polymarket.com/
Endpoint shape: https://gamma-api.polymarket.com/markets/{market_id}
"""
from __future__ import annotations

import json
from typing import Optional

import requests


GAMMA_BASE = "https://gamma-api.polymarket.com"
TIMEOUT = 8


class PolymarketError(Exception):
    pass


def _parse_listish(value):
    """Gamma returns outcomes/prices as JSON strings *or* lists depending on endpoint."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        try:
            return list(json.loads(value))
        except (ValueError, TypeError):
            return []
    return []


def fetch_market(market_id: str, *, session: Optional[requests.Session] = None) -> dict:
    """Return a normalised dict for a Polymarket market.

    Keys: id, question, closed (bool), outcomes (list[str]), prices (list[float]),
    winning_outcome (str | None), raw (full payload).
    """
    sess = session or requests
    url = f"{GAMMA_BASE}/markets/{market_id}"
    try:
        resp = sess.get(url, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise PolymarketError(f"network error: {exc}") from exc
    if resp.status_code == 404:
        raise PolymarketError(f"market {market_id!r} not found")
    if resp.status_code >= 400:
        raise PolymarketError(f"gamma {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    outcomes = _parse_listish(data.get("outcomes"))
    raw_prices = _parse_listish(data.get("outcomePrices"))
    prices = []
    for p in raw_prices:
        try:
            prices.append(float(p))
        except (TypeError, ValueError):
            prices.append(0.0)

    closed = bool(data.get("closed"))
    winning_outcome = None
    if closed and outcomes and prices and len(outcomes) == len(prices):
        # Winner = outcome with price ≈ 1.0; pick the highest.
        idx = max(range(len(prices)), key=lambda i: prices[i])
        if prices[idx] >= 0.5:
            winning_outcome = outcomes[idx]

    return {
        "id": str(data.get("id", market_id)),
        "question": data.get("question", ""),
        "closed": closed,
        "outcomes": outcomes,
        "prices": prices,
        "winning_outcome": winning_outcome,
        "raw": data,
    }
