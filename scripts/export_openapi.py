from __future__ import annotations

import argparse
import json
from pathlib import Path

from dwp_agent.main import app


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "contracts" / "openapi" / "agent-public.json"


def rendered_contract() -> str:
    return json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Export the DWP Agent OpenAPI contract.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = rendered_contract()
    if args.write:
        TARGET.parent.mkdir(parents=True, exist_ok=True)
        TARGET.write_text(rendered, encoding="utf-8")
        print(f"Wrote {TARGET.relative_to(ROOT)}")
        return 0
    if not TARGET.exists() or TARGET.read_text(encoding="utf-8") != rendered:
        print("Agent OpenAPI snapshot is stale. Run scripts/export_openapi.py --write.")
        return 1
    print("PASS Agent OpenAPI snapshot matches the runtime contract.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
