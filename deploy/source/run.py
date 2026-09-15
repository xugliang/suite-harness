"""Run a trusted product ASGI factory from one explicit deployment YAML file."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("deployment", type=Path)
    args = parser.parse_args()
    import yaml
    import uvicorn
    data = yaml.safe_load(args.deployment.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) - {"factory", "host", "port"}:
        raise ValueError("deployment accepts factory, host and port only")
    factory = data.get("factory")
    if not isinstance(factory, str) or factory.count(":") != 1:
        raise ValueError("factory must be a trusted Python module:callable")
    port = data.get("port", 8080)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    uvicorn.run(factory, factory=True, host=data.get("host", "127.0.0.1"), port=port)


if __name__ == "__main__":
    main()
