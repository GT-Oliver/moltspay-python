"""MCP tools wrapping the MoltsPay Python client."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

from ..client import MoltsPay


def create_mcp_server(dry_run: bool = False, config_dir: Optional[str] = None):
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError("MCP support requires: pip install moltspay[mcp]") from exc

    root = Path(config_dir).expanduser() if config_dir else Path.home() / ".moltspay"
    wallet_path = root / "wallet.json"
    if not wallet_path.exists():
        raise RuntimeError("MoltsPay wallet not found. Run `moltspay init` before starting the MCP server.")
    client = MoltsPay(wallet_path=str(wallet_path), config_dir=str(root))
    server = FastMCP("moltspay")

    @server.tool(description="Return wallet address, balances and spending limits.")
    def moltspay_status() -> Dict[str, Any]:
        return {
            "address": client.address,
            "defaultChain": client.get_config()["chain"],
            "balances": client.get_all_balances(),
            "limits": client.get_config()["limits"],
        }

    @server.tool(description="List services and prices from a MoltsPay provider.")
    def moltspay_services(url: str, max_price: Optional[float] = None, query: Optional[str] = None) -> Dict[str, Any]:
        response = client.get_services(url)
        services = response.services
        if max_price is not None:
            services = [service for service in services if service.price <= max_price]
        if query:
            needle = query.lower()
            services = [
                service for service in services
                if needle in " ".join(filter(None, [service.id, service.name, service.description])).lower()
            ]
        return {"provider": response.provider.model_dump() if response.provider else None, "services": [item.model_dump() for item in services]}

    @server.tool(description="Pay for and execute a MoltsPay provider service.")
    def moltspay_pay(
        url: str,
        service: str,
        params: Dict[str, Any],
        chain: Optional[str] = None,
        token: str = "USDC",
        rail: Optional[str] = None,
        confirmed: bool = False,
    ) -> Dict[str, Any]:
        intent = {"url": url, "service": service, "params": params, "chain": chain, "token": token, "rail": rail}
        if dry_run:
            return {"dryRun": True, "message": "No payment executed", "intent": intent}
        if os.environ.get("MOLTSPAY_MCP_REQUIRE_CONFIRM") == "1" and not confirmed:
            raise ValueError("Confirmation required; call again with confirmed=true after reviewing the price")
        result = client.pay(url, service, token=token, chain=chain, rail=rail, payment_params=params)
        return result.model_dump()

    @server.tool(description="Read or update MoltsPay spending limits.")
    def moltspay_config(max_per_tx: Optional[float] = None, max_per_day: Optional[float] = None) -> Dict[str, Any]:
        if max_per_tx is not None or max_per_day is not None:
            client.update_config(max_per_tx=max_per_tx, max_per_day=max_per_day)
        return client.get_config()

    return server
