"""WooCommerce REST API client -- read-only product search.

Configured via:
    WC_CONSUMER_KEY    -- WooCommerce REST API consumer key
    WC_CONSUMER_SECRET -- WooCommerce REST API consumer secret
    WC_STORE_URL       -- e.g. https://propeller-drones.shop

If any env var is empty, all calls return an empty list and the tool
falls back to directing the lead to a sales rep.
"""

from __future__ import annotations

import re

import httpx
from loguru import logger

from app.config import get_settings


def search_woocommerce_products(query: str, limit: int = 3) -> list[dict]:
    """Search WooCommerce for products matching *query*.

    Returns a list of dicts with keys:
        title, price_min, price_max, in_stock, description, url

    Returns [] if WooCommerce is not configured or on any error.
    """
    settings = get_settings()
    key = (settings.wc_consumer_key or "").strip()
    secret = (settings.wc_consumer_secret or "").strip()
    store_url = (settings.wc_store_url or "").strip().rstrip("/")

    if not key or not secret or not store_url:
        logger.debug("[woocommerce] not configured, skipping search")
        return []

    endpoint = f"{store_url}/wp-json/wc/v3/products"

    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(
                endpoint,
                auth=(key, secret),
                params={"search": query, "per_page": limit, "status": "publish"},
            )
        resp.raise_for_status()
        products = resp.json()
    except Exception:
        logger.exception("[woocommerce] search failed for query={!r}", query)
        return []

    if not isinstance(products, list):
        logger.warning("[woocommerce] unexpected response: {!r}", products)
        return []

    results: list[dict] = []
    for p in products:
        title = p.get("name", "")
        if not title:
            continue

        try:
            price = int(float(p.get("price") or 0))
            price_min = price or None
            price_max = price or None
        except (ValueError, TypeError):
            price_min = price_max = None

        in_stock = p.get("stock_status") == "instock"
        url = p.get("permalink", "")

        raw_desc = p.get("short_description") or p.get("description") or ""
        description = re.sub(r"<[^>]+>", "", raw_desc).strip()[:200]

        results.append({
            "title": title,
            "price_min": price_min,
            "price_max": price_max,
            "in_stock": in_stock,
            "description": description,
            "url": url,
        })

    return results
