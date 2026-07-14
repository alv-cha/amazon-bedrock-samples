import json

import pytest

from cdk.pricing_resolver import handler as resolver


def _product(
    sku: str,
    inference_type: str,
    usd_per_1k: str,
    *,
    feature: str = "",
    service_tier: str = "",
) -> str:
    attributes = {
        "model": "gpt-oss-20b",
        "regionCode": "us-east-1",
        "inferenceType": inference_type,
    }
    if feature:
        attributes["feature"] = feature
    if service_tier:
        attributes["service_tier"] = service_tier
    return json.dumps({
        "product": {"sku": sku, "attributes": attributes},
        "terms": {
            "OnDemand": {
                f"{sku}.term": {
                    "priceDimensions": {
                        f"{sku}.dimension": {
                            "unit": "1K tokens",
                            "pricePerUnit": {"USD": usd_per_1k},
                        }
                    }
                }
            }
        },
    })


class _FakePricing:
    def __init__(self, products):
        self.products = products
        self.calls = []

    def get_paginator(self, operation):
        assert operation == "get_products"
        return self

    def paginate(self, **kwargs):
        self.calls.append(kwargs)
        return [{"PriceList": self.products}]


def test_snapshot_uses_standard_prices_and_maps_all_model_ids():
    pricing = _FakePricing([
        _product("native-in", "Input tokens", "0.0000700000",
                 feature="On-demand Inference"),
        _product("mantle-in", "Input tokens", "0.0000700000",
                 service_tier="standard"),
        _product("native-out", "Output tokens", "0.0003000000",
                 feature="On-demand Inference"),
        _product("mantle-out", "Output tokens", "0.0003000000",
                 service_tier="standard"),
        _product("flex", "Input tokens flex", "0.0000350000"),
        _product("priority", "Output tokens priority", "0.0005250000"),
        _product("batch", "Input tokens", "0.0000350000",
                 feature="Batch Inference"),
    ])

    snapshot = resolver.resolve_snapshot(
        pricing,
        "us-east-1",
        {
            "gpt-oss-20b": [
                "openai.gpt-oss-20b",
                "openai.gpt-oss-20b-1:0",
            ]
        },
        {
            "anthropic.claude-opus-4-7": {
                "input_per_mtok": 15,
                "output_per_mtok": 75,
            }
        },
    )

    expected = {"input_per_mtok": 0.07, "output_per_mtok": 0.3}
    assert snapshot["openai.gpt-oss-20b"] == expected
    assert snapshot["openai.gpt-oss-20b-1:0"] == expected
    assert snapshot["anthropic.claude-opus-4-7"] == {
        "input_per_mtok": 15.0,
        "output_per_mtok": 75.0,
    }
    assert pricing.calls[0]["ServiceCode"] == "AmazonBedrock"


def test_snapshot_rejects_ambiguous_standard_price():
    pricing = _FakePricing([
        _product("input-a", "Input tokens", "0.0000700000"),
        _product("input-b", "Input tokens", "0.0000800000"),
        _product("output", "Output tokens", "0.0003000000"),
    ])

    with pytest.raises(ValueError, match="one standard on-demand input price"):
        resolver.resolve_snapshot(
            pricing,
            "us-east-1",
            {"gpt-oss-20b": ["openai.gpt-oss-20b"]},
            {},
        )


def test_delete_does_not_query_pricing(monkeypatch):
    monkeypatch.setattr(
        resolver.boto3,
        "client",
        lambda *args, **kwargs: pytest.fail("Pricing must not run on delete"),
    )
    result = resolver.handler(
        {
            "RequestType": "Delete",
            "ResourceProperties": {"RegionCode": "us-east-1"},
        },
        None,
    )
    assert result == {"PhysicalResourceId": "bedrock-model-prices-us-east-1"}
