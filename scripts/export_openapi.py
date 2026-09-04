"""Export FastAPI OpenAPI schema to JSON.

A0 owns this script. It serialises the schema for the frontend.
"""

import json
import sys
from pathlib import Path

from pydantic import BaseModel
from pydantic.json_schema import models_json_schema

API_DIR = Path(__file__).parent.parent / "apps" / "api"
sys.path.insert(0, str(API_DIR))

from app.main import create_app
import app.contracts

OUT_FILE = API_DIR / "openapi.json"


def replace_refs(obj: dict | list | str | float | int | bool | None) -> dict | list | str | float | int | bool | None:
    """Recursively replace #/$defs/ with #/components/schemas/ in JSON schema refs."""
    if isinstance(obj, dict):
        new_obj = {}
        for k, v in obj.items():
            if k == "$ref" and isinstance(v, str) and v.startswith("#/$defs/"):
                new_obj[k] = v.replace("#/$defs/", "#/components/schemas/")
            else:
                new_obj[k] = replace_refs(v)
        return new_obj
    elif isinstance(obj, list):
        return [replace_refs(item) for item in obj]
    else:
        return obj


def main() -> None:
    """Generate openapi.json and exit."""
    application = create_app()
    openapi_schema = application.openapi()

    # Collect all contract models
    models = [
        getattr(app.contracts, name)
        for name in app.contracts.__all__
        if isinstance(getattr(app.contracts, name), type)
        and issubclass(getattr(app.contracts, name), BaseModel)
    ]

    # Generate JSON schemas for the contract models
    _, schemas = models_json_schema(
        [(m, "validation") for m in models], 
        title="Mandate Guardian API Contracts"
    )

    # Fix the $ref pointers inside the generated schemas
    schemas = replace_refs(schemas)  # type: ignore

    # Inject into OpenAPI spec
    components = openapi_schema.setdefault("components", {})
    components_schemas = components.setdefault("schemas", {})
    
    if "$defs" in schemas:
        components_schemas.update(schemas["$defs"])

    if not openapi_schema.get("components", {}).get("schemas"):
        print("Error: No schemas found in OpenAPI spec. Contract models missing?", file=sys.stderr)
        sys.exit(1)

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(openapi_schema, f, indent=2)
        f.write("\n")

    print(f"Successfully generated {OUT_FILE}")


if __name__ == "__main__":
    main()
