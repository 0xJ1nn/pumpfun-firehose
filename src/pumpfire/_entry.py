"""Console entry point with a friendly message for missing optional extras."""

from __future__ import annotations

import logging
import os


def main() -> None:
    level = os.environ.get("PUMPFIRE_LOG_LEVEL")
    if level:
        logging.basicConfig(
            level=level.upper(),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
    try:
        from .cli import app
    except ImportError as exc:
        print(
            "PumpFire CLI needs optional dependencies.\n"
            "Install with: pip install 'pumpfun-firehose[cli]'\n"
            f"Missing: {exc}"
        )
        raise SystemExit(1) from exc
    app()


if __name__ == "__main__":
    main()
