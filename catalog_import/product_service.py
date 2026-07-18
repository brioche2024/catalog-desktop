from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import DEFAULT_PRODUCT_STATUS, OUTPUT_DIR
from .pfs_client import PfsApiError, PfsClient, product_weight_kg
from .session_store import PfsSession


def fetch_pfs_products(
    session: PfsSession,
    *,
    status: str = DEFAULT_PRODUCT_STATUS,
    enrich_details: bool = False,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    with PfsClient(session) as client:
        return client.fetch_catalog(
            status=status,
            enrich_details=enrich_details,
            on_progress=on_progress,
        )


def save_products_export(
    products: list[dict[str, Any]],
    raw_pages: list[dict[str, Any]],
    *,
    vendor_email: str,
    raw_variant_pages: list[dict[str, Any]] | None = None,
    output_dir: Path | None = None,
) -> Path:
    folder = output_dir or OUTPUT_DIR
    folder.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = folder / f"pfs_products_{stamp}.json"

    enriched_count = sum(
        1
        for product in products
        if product.get("detail_loaded") and product.get("variants_loaded")
    )
    with_weight_count = sum(
        1 for product in products if product.get("weight") or product_weight_kg(product)
    )

    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source": "pfs_wholesaler_api",
        "vendor_email": vendor_email,
        "product_count": len(products),
        "detail_enriched_count": enriched_count,
        "with_weight_count": with_weight_count,
        "products": products,
        "raw_api_pages": raw_pages,
        "raw_variant_pages": raw_variant_pages or [],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
