"""Shared fixtures: a minimal in-memory fake of the DynamoDB resource API.

The fake implements exactly the expression subset the app uses (ADD/SET
with if_not_exists, attribute_not_exists conditions, `<=` comparisons,
GSI-style query by attribute equality). Tests run with no network and no
AWS credentials.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gateway"))
sys.path.insert(0, str(ROOT / "reconciler"))

os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("MANTLE_REGION", "us-east-1")
os.environ.setdefault("USERS_TABLE", "users-test")
os.environ.setdefault("USAGE_TABLE", "usage-test")
# HS256 dev-mode JWT verification for tests (no IdP / network needed).
os.environ.setdefault("JWT_SHARED_SECRET", "test-jwt-secret")

from botocore.exceptions import ClientError  # noqa: E402


class FakeTable:
    def __init__(self, name: str, key_attrs: list[str]):
        self.name = name
        self.key_attrs = key_attrs
        self.items: dict[tuple, dict] = {}

    # -- helpers ---------------------------------------------------------
    def _key(self, key: dict) -> tuple:
        return tuple(key[a] for a in self.key_attrs)

    # -- API -------------------------------------------------------------
    def put_item(self, Item: dict):
        self.items[self._key(Item)] = dict(Item)
        return {}

    def get_item(self, Key: dict):
        item = self.items.get(self._key(Key))
        return {"Item": dict(item)} if item else {}

    def scan(self, **kwargs):
        return {"Items": [dict(v) for v in self.items.values()]}

    def query(self, IndexName=None, KeyConditionExpression=None,
              ExpressionAttributeValues=None, Limit=None, **kwargs):
        # Supports "attr = :v" equality only (what the app uses for its GSI).
        attr, _, placeholder = KeyConditionExpression.partition("=")
        attr, placeholder = attr.strip(), placeholder.strip()
        value = ExpressionAttributeValues[placeholder]
        matches = [dict(v) for v in self.items.values() if v.get(attr) == value]
        if Limit:
            matches = matches[:Limit]
        return {"Items": matches}

    def update_item(self, Key: dict, UpdateExpression: str,
                    ExpressionAttributeValues: dict | None = None,
                    ExpressionAttributeNames: dict | None = None,
                    ConditionExpression: str | None = None,
                    ReturnValues: str | None = None):
        values = ExpressionAttributeValues or {}
        names = ExpressionAttributeNames or {}
        key = self._key(Key)
        item = self.items.get(key, dict(Key))

        if ConditionExpression and not self._condition_ok(ConditionExpression, item, values):
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException",
                           "Message": "The conditional request failed"}},
                "UpdateItem",
            )

        self._apply_update(UpdateExpression, item, values, names)
        self.items[key] = item
        if ReturnValues == "ALL_NEW":
            return {"Attributes": dict(item)}
        return {}

    # -- expression evaluation (targeted subset) --------------------------
    def _condition_ok(self, expr: str, item: dict, values: dict) -> bool:
        expr = expr.strip()
        # Pattern: attribute_not_exists(f) OR (a <= :x AND b <= :y AND c <= :z)
        if expr.startswith("attribute_not_exists"):
            inner = expr[len("attribute_not_exists("):expr.index(")")]
            rest = expr[expr.index(")") + 1:].strip()
            if inner not in item:
                return True
            if rest.upper().startswith("OR"):
                return self._condition_ok(rest[2:].strip(), item, values)
            return False
        if expr.startswith("(") and expr.endswith(")"):
            expr = expr[1:-1]
        for clause in expr.split(" AND "):
            attr, op, placeholder = clause.strip().split(" ")
            assert op == "<=", f"unsupported operator in fake: {op}"
            if int(item.get(attr, 0)) > int(values[placeholder]):
                return False
        return True

    @staticmethod
    def _split_top_level(text: str) -> list[str]:
        """Split on commas that are not inside parentheses."""
        parts, depth, current = [], 0, ""
        for ch in text:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                parts.append(current.strip())
                current = ""
            else:
                current += ch
        if current.strip():
            parts.append(current.strip())
        return parts

    def _apply_update(self, expr: str, item: dict, values: dict, names: dict) -> None:
        # Split into ADD ... / SET ... sections (keywords are uppercase in the app).
        import re
        sections: dict[str, str] = {}
        for match in re.finditer(r"\b(ADD|SET)\b", expr):
            keyword = match.group(1)
            start = match.end()
            next_match = re.search(r"\b(?:ADD|SET)\b", expr[start:])
            end = start + next_match.start() if next_match else len(expr)
            sections[keyword] = expr[start:end].strip()
        if "ADD" in sections:
            for part in self._split_top_level(sections["ADD"]):
                attr, placeholder = part.split()
                item[attr] = int(item.get(attr, 0)) + int(values[placeholder])
        if "SET" in sections:
            for part in self._split_top_level(sections["SET"]):
                target, _, rhs = part.partition("=")
                target = names.get(target.strip(), target.strip())
                rhs = rhs.strip()
                if rhs.startswith("if_not_exists"):
                    inner = rhs[len("if_not_exists("):rhs.rindex(")")]
                    _attr_name, placeholder = [x.strip() for x in inner.split(",")]
                    if target not in item:
                        item[target] = values[placeholder]
                else:
                    item[target] = values[rhs]


class FakeDynamoDB:
    """Stands in for boto3.resource('dynamodb')."""

    def __init__(self):
        self.tables: dict[str, FakeTable] = {}

    def add_table(self, name: str, key_attrs: list[str]) -> FakeTable:
        self.tables[name] = FakeTable(name, key_attrs)
        return self.tables[name]

    def Table(self, name: str) -> FakeTable:  # noqa: N802 (boto3 API)
        return self.tables[name]


class FakeSNS:
    def __init__(self):
        self.published: list[dict] = []

    def publish(self, **kwargs):
        self.published.append(kwargs)
        return {"MessageId": "fake"}


@pytest.fixture
def fake_dynamodb():
    db = FakeDynamoDB()
    db.add_table(os.environ["USERS_TABLE"], ["user_id"])
    db.add_table(os.environ["USAGE_TABLE"], ["user_id", "window"])
    return db


@pytest.fixture
def fake_sns():
    return FakeSNS()
