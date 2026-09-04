"""Contract and schema validation tests. A0 owns this module."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

import app.contracts

FIXTURES_DIR = Path(__file__).parent.parent.parent.parent.parent / "data" / "fixtures" / "contracts"


def load_manifest() -> dict[str, dict[str, Any]]:
    """Load the fixture manifest to drive parameterized tests."""
    manifest_path = FIXTURES_DIR / "manifest.json"
    with manifest_path.open(encoding="utf-8") as f:
        data = json.load(f)
    return data["fixtures"]


MANIFEST = load_manifest()


def get_model_class(import_path: str) -> type[BaseModel]:
    """Map a manifest model name to a model imported from ``app.contracts``.

    The manifest uses fully-qualified submodule paths (e.g.
    ``app.contracts.risk_session.RiskSession``); the class itself must be
    exported from the canonical ``app.contracts`` package.
    """
    class_name = import_path.rsplit(".", 1)[-1]
    model = getattr(app.contracts, class_name, None)
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        raise KeyError(f"Model {import_path!r} is not exported from app.contracts.")
    return model


@pytest.mark.parametrize("filename, meta", MANIFEST.items(), ids=list(MANIFEST.keys()))
def test_contract_fixture(filename: str, meta: dict[str, Any]) -> None:
    """Validate each fixture against its declared model.

    Must:
    1. Validate valid JSON.
    2. Raise ValidationError for invalid JSON.
    3. Ensure valid models survive a JSON round-trip.
    4. Assert schema has additionalProperties: false.
    5. Assert schema_version serializes as "1.0" for event models.
    """
    fixture_path = FIXTURES_DIR / filename
    with open(fixture_path, encoding="utf-8") as f:
        payload = json.load(f)

    model_cls = get_model_class(meta["model"])
    is_valid = meta["valid"]

    if is_valid:
        # 1. Validate valid JSON
        instance = model_cls.model_validate(payload)

        # 3. Ensure valid models survive a JSON round-trip
        serialized = instance.model_dump_json(by_alias=True)
        round_tripped = model_cls.model_validate_json(serialized)
        assert instance == round_tripped

        # 5. Assert schema_version serializes as "1.0" for event models
        if "schema_version" in model_cls.model_fields:
            assert json.loads(serialized).get("schema_version") == "1.0"
    else:
        # 2. Raise ValidationError for invalid JSON
        with pytest.raises(ValidationError):
            model_cls.model_validate(payload)

    # 4. Assert schema has additionalProperties: false on the top-level object
    schema = model_cls.model_json_schema()
    assert schema.get("additionalProperties") is False, (
        f"Model {meta['model']} must forbid additional properties."
    )
