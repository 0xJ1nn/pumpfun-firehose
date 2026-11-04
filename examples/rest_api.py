"""Run the dashboard and API from Python.

    python examples/rest_api.py

Then open http://127.0.0.1:8000.
"""

from __future__ import annotations

import uvicorn

from pumpfire import CoinBook, Firehose, MetadataFetcher, SolPrice
from pumpfire.api import create_app


def main() -> None:
    book = CoinBook()
    metadata = MetadataFetcher(book)
    firehose = Firehose(book, metadata=metadata)
    solprice = SolPrice(book)
    app = create_app(
        book,
        firehose=firehose,
        solprice=solprice,
        metadata=metadata,
    )
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
