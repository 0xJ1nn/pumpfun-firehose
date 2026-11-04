"""Console entry point with a friendly message for missing optional extras."""

from __future__ import annotations


def main() -> None:
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
