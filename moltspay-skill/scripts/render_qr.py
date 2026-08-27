#!/usr/bin/env python3
"""Render a QR PNG for a payment code URL without making network requests."""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a QR payload to a PNG file")
    parser.add_argument("payload", help="Exact QR payload returned by MoltsPay")
    parser.add_argument("output", help="Destination .png path")
    args = parser.parse_args()

    output = Path(args.output).expanduser().resolve()
    if output.suffix.lower() != ".png":
        raise SystemExit("output path must end in .png")
    output.parent.mkdir(parents=True, exist_ok=True)

    import qrcode

    image = qrcode.make(args.payload)
    image.save(output, format="PNG")
    print(output)


if __name__ == "__main__":
    main()
