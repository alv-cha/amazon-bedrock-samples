"""Resolve standard on-demand Bedrock token prices during stack deployment."""

import json
from decimal import Decimal

import boto3

SERVICE_CODE = "AmazonBedrock"
PRICING_API_REGION = "us-east-1"


def _dimension_rate(product: dict) -> Decimal:
    rates = set()
    for term in product.get("terms", {}).get("OnDemand", {}).values():
        for dimension in term.get("priceDimensions", {}).values():
            if dimension.get("unit") != "1K tokens":
                continue
            rates.add(Decimal(dimension["pricePerUnit"]["USD"]) * 1000)
    if len(rates) != 1:
        sku = product.get("product", {}).get("sku")
        raise ValueError(
            f"Expected one USD-per-MTok rate for SKU {sku}; "
            f"found {sorted(rates)}"
        )
    return rates.pop()


def _catalog_price(pricing_client, region_code: str, catalog_model: str) -> dict:
    filters = [
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region_code},
        {"Type": "TERM_MATCH", "Field": "model", "Value": catalog_model},
    ]
    rates: dict[str, set[Decimal]] = {"input": set(), "output": set()}
    paginator = pricing_client.get_paginator("get_products")
    for page in paginator.paginate(ServiceCode=SERVICE_CODE, Filters=filters):
        for raw_product in page.get("PriceList", []):
            product = json.loads(raw_product)
            attributes = product["product"]["attributes"]
            feature = attributes.get("feature", "").lower()
            tier = attributes.get("service_tier", "").lower()
            if feature not in ("", "on-demand inference"):
                continue
            if tier not in ("", "standard"):
                continue

            inference_type = attributes.get("inferenceType", "").lower()
            if inference_type == "input tokens":
                rates["input"].add(_dimension_rate(product))
            elif inference_type == "output tokens":
                rates["output"].add(_dimension_rate(product))

    for token_type, candidates in rates.items():
        if len(candidates) != 1:
            raise ValueError(
                f"Expected one standard on-demand {token_type} price for "
                f"{catalog_model} in {region_code}; found {sorted(candidates)}"
            )

    return {
        "input_per_mtok": float(rates["input"].pop()),
        "output_per_mtok": float(rates["output"].pop()),
    }


def resolve_snapshot(
    pricing_client,
    region_code: str,
    catalog_models: dict[str, list[str]],
    pinned_prices: dict[str, dict],
) -> dict:
    snapshot = {
        model_id: {
            "input_per_mtok": float(price["input_per_mtok"]),
            "output_per_mtok": float(price["output_per_mtok"]),
        }
        for model_id, price in pinned_prices.items()
    }
    for catalog_model, model_ids in catalog_models.items():
        price = _catalog_price(pricing_client, region_code, catalog_model)
        for model_id in model_ids:
            if model_id in snapshot:
                raise ValueError(f"Duplicate model price mapping for {model_id}")
            snapshot[model_id] = price
    return snapshot


def conservative_fallback(snapshot: dict, configured: dict) -> dict:
    """Keep unknown models at least as expensive as every known model."""
    fallback = {}
    for field in ("input_per_mtok", "output_per_mtok"):
        candidates = [float(configured[field])]
        candidates.extend(float(price[field]) for price in snapshot.values())
        fallback[field] = max(candidates)
    return fallback


def handler(event, _context):
    properties = event["ResourceProperties"]
    physical_id = f"bedrock-model-prices-{properties['RegionCode']}"
    if event["RequestType"] == "Delete":
        return {"PhysicalResourceId": physical_id}

    client = boto3.client("pricing", region_name=PRICING_API_REGION)
    snapshot = resolve_snapshot(
        client,
        properties["RegionCode"],
        properties["CatalogModels"],
        properties.get("PinnedPrices", {}),
    )
    fallback = conservative_fallback(
        snapshot,
        properties.get("FallbackPrice", {
            "input_per_mtok": 15.0,
            "output_per_mtok": 75.0,
        }),
    )
    return {
        "PhysicalResourceId": physical_id,
        "Data": {
            "ModelPricesJson": json.dumps(
                snapshot, sort_keys=True, separators=(",", ":")
            ),
            "FallbackPriceJson": json.dumps(
                fallback, sort_keys=True, separators=(",", ":")
            ),
        },
    }
