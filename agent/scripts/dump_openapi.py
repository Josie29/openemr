import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from copilot.openapi import OPENAPI_PATH, build_openapi_spec, dump_spec  # noqa: E402

# Write the committed OpenAPI spec (agent/openapi.json) from the live FastAPI app.
#
# Run this after any change to an endpoint or its response model:
#     python scripts/dump_openapi.py
# The contract test (tests/test_openapi_contract.py) fails on drift, so this is the single command
# that resolves that failure.


def main() -> None:
    spec = build_openapi_spec()
    OPENAPI_PATH.write_text(dump_spec(spec))
    print(f"wrote {OPENAPI_PATH} ({len(spec.get('paths', {}))} paths)")


if __name__ == "__main__":
    main()
