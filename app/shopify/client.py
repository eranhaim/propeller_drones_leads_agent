"""Shopify Storefront API client -- read-only product search.

Uses the Storefront API (not Admin API) so we only need a public
storefront access token. Configured via:
    SHOPIFY_STOREFRONT_TOKEN  -- Storefront API access token
    SHOPIFY_STOREFRONT_URL    -- https://<shop>.myshopify.com/api/2024-01/graphql.json

If either env var is empty, all calls return an empty list and the tool
falls back to directing the lead to a sales rep.
"""

from __future__ import annotations

from typing import Optional

import httpx
from loguru import logger

from app.config import get_settings

_SEARCH_QUERY = """
query SearchProducts($query: String!, $first: Int!) {
  search(query: $query, first: $first, types: PRODUCT) {
    edges {
      node {
        ... on Product {
          title
          handle
          priceRange {
            minVariantPrice { amount currencyCode }
            maxVariantPrice { amount currencyCode }
          }
          totalInventory
          description
        }
      }
    }
  }
}
"""


def search_shopify_products(query: str, limit: int = 3) -> list[dict]:
    """Search Shopify for products matching *query*.

    Returns a list of dicts with keys:
        title, price_min, price_max, in_stock, description, url

    Returns [] if Shopify is not configured or on any error.
    """
    settings = get_settings()
    token = (settings.shopify_storefront_token or "").strip()
    url = (settings.shopify_storefront_url or "").strip()

    if not token or not url:
        logger.debug("[shopify] not configured (token or url empty), skipping search")
        return []

    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.post(
                url,
                headers={
                    "X-Shopify-Storefront-Access-Token": token,
                    "Content-Type": "application/json",
                },
                json={
                    "query": _SEARCH_QUERY,
                    "variables": {"query": query, "first": limit},
                },
            )
        resp.raise_for_status()
        body = resp.json()
    except Exception:
        logger.exception("[shopify] search failed for query={!r}", query)
        return []

    edges = (
        body.get("data", {})
        .get("search", {})
        .get("edges", [])
    )

    results: list[dict] = []
    shop_base = url.split("/api/")[0]  # e.g. https://propeller-drones.myshopify.com

    for edge in edges:
        node = edge.get("node", {})
        if not node.get("title"):
            continue

        price_range = node.get("priceRange", {})
        min_price = price_range.get("minVariantPrice", {}).get("amount", "")
        max_price = price_range.get("maxVariantPrice", {}).get("amount", "")

        try:
            min_f = float(min_price)
            max_f = float(max_price)
            price_min = int(min_f)
            price_max = int(max_f)
        except (ValueError, TypeError):
            price_min = price_max = None

        inventory = node.get("totalInventory")
        in_stock = isinstance(inventory, int) and inventory > 0

        handle = node.get("handle", "")
        product_url = f"{shop_base}/products/{handle}" if handle else ""

        results.append({
            "title": node.get("title", ""),
            "price_min": price_min,
            "price_max": price_max,
            "in_stock": in_stock,
            "description": (node.get("description") or "")[:200],
            "url": product_url,
        })

    return results
