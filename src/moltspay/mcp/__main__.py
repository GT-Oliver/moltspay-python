"""stdio entrypoint for ``moltspay-mcp``."""

import argparse

from .server import create_mcp_server


def main() -> None:
    parser = argparse.ArgumentParser(prog="moltspay-mcp")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--config-dir")
    args = parser.parse_args()
    create_mcp_server(dry_run=args.dry_run, config_dir=args.config_dir).run(transport="stdio")


if __name__ == "__main__":
    main()
