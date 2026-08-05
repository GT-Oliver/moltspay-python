#!/usr/bin/env python3
"""MoltsPay command-line interface."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .chains import CHAINS, is_testnet, list_chains
from .client import MoltsPay
from .wallet import DEFAULT_WALLET_PATH, Wallet


def configure_stdio() -> None:
    """Make CLI output safe for Windows consoles with legacy code pages."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            # Embedded callers and test capture streams may not allow their
            # encoding to be changed. Keep the original stream in that case.
            continue


def output(value: Any) -> None:
    def json_value(item: Any) -> Any:
        if hasattr(item, "model_dump"):
            return json_value(item.model_dump())
        if isinstance(item, dict):
            return {key: json_value(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [json_value(child) for child in item]
        return item

    value = json_value(value)
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def print_wechat_qr(code_url: str) -> None:
    """Render a WeChat Native code URL without corrupting JSON stdout."""
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(code_url)
        qr.make(fit=True)
        print("\nScan this QR code with WeChat to pay:\n", file=sys.stderr)
        qr.print_ascii(invert=True, out=sys.stderr)
        print(f"\nWeChat code URL: {code_url}\n", file=sys.stderr)
    except ImportError:
        print(
            f"\nWeChat code URL (install qrcode for a terminal QR code): {code_url}\n",
            file=sys.stderr,
        )


def cmd_init(args) -> int:
    chain = args.chain or "base"
    if chain.startswith("solana"):
        from .wallet_solana import DEFAULT_SOLANA_WALLET_PATH, SolanaWallet
        path = Path(args.config_dir).expanduser() / "wallet-solana.json" if args.config_dir else DEFAULT_SOLANA_WALLET_PATH
        if path.exists() and not args.force:
            print(f"Wallet already exists: {path}", file=sys.stderr)
            return 1
        wallet = SolanaWallet(wallet_path=str(path), create_if_missing=True)
        output({"address": wallet.address, "chain": chain, "path": str(path)})
        return 0
    path = Path(args.config_dir).expanduser() / "wallet.json" if args.config_dir else DEFAULT_WALLET_PATH
    if path.exists() and not args.force:
        print(f"Wallet already exists: {path}", file=sys.stderr)
        return 1
    wallet = Wallet(wallet_path=str(path), chain=chain)
    output({"address": wallet.address, "chain": chain, "path": str(path)})
    return 0


def cmd_status(args) -> int:
    client = MoltsPay(chain=args.chain)
    show_all = getattr(args, "all", False)
    balances = client.get_all_balances() if show_all else {args.chain: client.balance(args.chain)}
    if show_all:
        # get_all_balances historically covered EVM chains only.  Include both
        # Solana networks here as well so status really means all chains.
        balances.update({
            chain: client.get_solana_balances(chain)
            for chain in ("solana", "solana_devnet")
        })
    result = {"address": client.address, "config": client.get_config(), "balances": balances}
    if not show_all:
        result["balance"] = balances[args.chain]
    output(result)
    return 0


def cmd_faucet(args) -> int:
    if not is_testnet(args.chain):
        print("Faucet requires a testnet chain", file=sys.stderr)
        return 1
    result = MoltsPay(chain=args.chain).faucet()
    output(result)
    return 0 if result.success else 1


def cmd_pay(args) -> int:
    params = json.loads(args.params or "{}")
    if args.prompt is not None:
        params["prompt"] = args.prompt
    client = MoltsPay(chain=args.chain, timeout=args.timeout)
    result = client.pay(
        args.url, args.service, token=args.token, chain=args.chain,
        rail=args.rail, payment_params=params,
        rail_options={
            "buyer_id": getattr(args, "buyer_id", None),
            "topup_pack": getattr(args, "topup_pack", None),
            "topup_mode": getattr(args, "topup_mode", "auto"),
            "auto_topup": not getattr(args, "no_auto_topup", False),
        },
    )
    output(result)
    return 0 if result.success else 1


def cmd_approve(args) -> int:
    from web3 import Web3
    chain = args.chain
    if chain not in ("bnb", "bnb_testnet"):
        print("--chain must be bnb or bnb_testnet", file=sys.stderr)
        return 1
    wallet = Wallet(chain=chain)
    config = CHAINS[chain]
    w3 = Web3(Web3.HTTPProvider(config["rpc"]))
    abi = [{"name": "approve", "type": "function", "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "outputs": [{"type": "bool"}]}]
    spender = Web3.to_checksum_address(args.spender or "0x145E00f48b98E2829f803Be53418230e47943a8A")
    hashes = {}
    for symbol, token in config["tokens"].items():
        contract = w3.eth.contract(address=Web3.to_checksum_address(token["address"]), abi=abi)
        tx = contract.functions.approve(spender, 2**256 - 1).build_transaction({
            "from": wallet.address, "chainId": config["chainId"],
            "nonce": w3.eth.get_transaction_count(wallet.address), "gas": 100000,
            "gasPrice": w3.eth.gas_price,
        })
        signed = wallet._account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
        if int(receipt["status"]) != 1:
            print(f"{symbol} approval failed", file=sys.stderr)
            return 1
        hashes[symbol] = tx_hash.hex()
    output({"success": True, "spender": spender, "transactions": hashes})
    return 0


def cmd_services(args) -> int:
    output(MoltsPay(chain=args.chain).get_services(args.url))
    return 0


def cmd_fund(args) -> int:
    result = MoltsPay(chain=args.chain).fund_qr(args.amount, args.chain)
    output(result)
    return 0 if result.success else 1


def cmd_transfer(args) -> int:
    result = MoltsPay(chain=args.chain).transfer(args.to, args.amount, args.token, args.chain)
    output(result)
    return 0 if result.success else 1


def cmd_config(args) -> int:
    client = MoltsPay(chain=args.chain)
    changed = any(value is not None for value in (args.max_per_tx, args.max_per_day, args.rail_preference, args.buyer_id))
    value = client.update_config(
        max_per_tx=args.max_per_tx, max_per_day=args.max_per_day,
        rail_preference=args.rail_preference.split(",") if args.rail_preference else None,
        buyer_id=args.buyer_id,
    ) if changed else client.get_config()
    output(value)
    return 0


def cmd_limits(args) -> int:
    client = MoltsPay(chain=args.chain)
    if args.max_per_tx is not None or args.max_per_day is not None:
        client.set_limits(max_per_tx=args.max_per_tx, max_per_day=args.max_per_day)
    output(client.limits())
    return 0


def cmd_balance(args) -> int:
    client = MoltsPay(chain=args.chain, buyer_id=args.buyer_id)
    if args.balance_command == "query":
        output(client.get_buyer_balance(args.server))
    elif args.balance_command == "transactions":
        output(client.list_balance_transactions(args.server, limit=args.limit, offset=args.offset))
    elif args.balance_command == "set-buyer":
        client.set_buyer_id(args.id)
        output(client.get_config())
    elif args.balance_command == "topup-order":
        result = client.create_balance_topup_order(args.server, pack=args.pack, buyer_id=args.buyer_id)
        output({"status": "topup_required", "out_trade_no": result["outTradeNo"],
                "code_url": result["codeUrl"], "pack": result["pack"], "server_url": args.server})
    elif args.balance_command == "topup-confirm":
        output(client.confirm_balance_topup(args.id, server_url=args.server))
    elif args.balance_command == "topup-status":
        output(client.get_balance_topup_session(args.id))
    elif args.balance_command == "topup-list":
        output(client.list_balance_topup_sessions())
    elif args.balance_command == "topup-pack":
        output(client.topup_balance_pack(args.server, pack=args.pack, buyer_id=args.buyer_id))
    return 0


def cmd_wechat(args) -> int:
    client = MoltsPay()
    if args.wechat_command == "start":
        session = client.start_wechat_payment(args.server, args.service, json.loads(args.params or "{}"))
        print_wechat_qr(session.code_url)
        output(session)
    elif args.wechat_command == "status":
        output(client.get_wechat_payment_status(args.identifier))
    elif args.wechat_command == "fulfill":
        output(client.fulfill_wechat_payment(args.identifier))
    elif args.wechat_command == "cancel":
        output(client.cancel_wechat_payment(args.identifier))
    elif args.wechat_command == "list":
        output([item.model_dump() for item in client.list_wechat_payment_sessions()])
    return 0


def cmd_alipay(args) -> int:
    from .alipay import AlipayClient
    print("\n".join(AlipayClient().runner([args.action, *args.args])))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="moltspay",
        description="MoltsPay agent payments SDK (JSON output)",
    )
    parser.add_argument("--version", action="version", version=f"moltspay {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    chain_choices = sorted(list_chains())

    command = sub.add_parser("init", help="Create a wallet", description="Create an EVM or Solana wallet")
    command.add_argument("--chain", default="base", choices=chain_choices)
    command.add_argument("--config-dir")
    command.add_argument("--force", action="store_true")

    command = sub.add_parser("status", help="Show wallet configuration and balance", description="Show wallet configuration and balance")
    command.add_argument("--chain", default="base", choices=chain_choices)
    command.add_argument("--all", action="store_true", help="Query all supported chains")

    command = sub.add_parser("faucet", help="Request testnet tokens", description="Request free tokens from a supported testnet faucet")
    command.add_argument("--chain", default="base_sepolia", choices=[chain for chain in chain_choices if is_testnet(chain)])

    command = sub.add_parser("pay", help="Pay for a service", description="Discover and pay for a provider service")
    command.add_argument("url")
    command.add_argument("service")
    command.add_argument("--chain", default="base", choices=chain_choices)
    command.add_argument("--token", choices=["USDC", "USDT"], default="USDC")
    command.add_argument("--rail", choices=["balance", "wechat", "alipay"])
    command.add_argument("--prompt")
    command.add_argument("--params")
    command.add_argument("--timeout", type=float, default=180.0)
    command.add_argument("--buyer-id")
    command.add_argument("--topup-pack")
    command.add_argument("--topup-mode", choices=["auto", "manual"], default="auto")
    command.add_argument("--no-auto-topup", action="store_true")

    command = sub.add_parser("approve", help="Approve BNB token spending", description="Approve USDC/USDT spending on BNB Chain")
    command.add_argument("--chain", required=True, choices=["bnb", "bnb_testnet"])
    command.add_argument("--spender")

    command = sub.add_parser("services", help="Discover provider services", description="List services exposed by a provider")
    command.add_argument("url")
    command.add_argument("--chain", default="base", choices=chain_choices)

    command = sub.add_parser("fund", help="Fund wallet with fiat", description="Generate a debit-card/Apple Pay funding QR code")
    command.add_argument("amount", type=float)
    command.add_argument("--chain", default="base", choices=["base", "polygon"])

    command = sub.add_parser("transfer", help="Transfer tokens", description="Transfer USDC or USDT to an EVM address")
    command.add_argument("to")
    command.add_argument("amount")
    command.add_argument("--token", choices=["USDC", "USDT"], default="USDC")
    command.add_argument("--chain", default="base", choices=chain_choices)

    command = sub.add_parser("config", help="Read or update client configuration", description="Read or update rail and buyer configuration")
    command.add_argument("--chain", default="base")
    command.add_argument("--max-per-tx", type=float)
    command.add_argument("--max-per-day", type=float)
    command.add_argument("--rail-preference")
    command.add_argument("--buyer-id")

    command = sub.add_parser("limits", help="Read or update spending limits", description="Read or update per-transaction and daily spending limits")
    command.add_argument("--chain", default="base", choices=chain_choices)
    command.add_argument("--max-per-tx", type=float)
    command.add_argument("--max-per-day", type=float)

    command = sub.add_parser("balance", help="Use a provider balance account", description="Query balance-rail accounts and transactions")
    command.add_argument("--chain", default="base")
    command.add_argument("--buyer-id")
    children = command.add_subparsers(dest="balance_command", required=True)
    child = children.add_parser("query")
    child.add_argument("server")
    child = children.add_parser("transactions")
    child.add_argument("server")
    child.add_argument("--limit", type=int, default=20)
    child.add_argument("--offset", type=int, default=0)
    child = children.add_parser("set-buyer")
    child.add_argument("id")
    child = children.add_parser("topup-order")
    child.add_argument("server")
    child.add_argument("--pack")
    child.add_argument("--buyer-id")
    child = children.add_parser("topup-confirm")
    child.add_argument("id", help="out_trade_no")
    child.add_argument("--server")
    child = children.add_parser("topup-status")
    child.add_argument("id", help="out_trade_no")
    children.add_parser("topup-list")
    child = children.add_parser("topup-pack")
    child.add_argument("server")
    child.add_argument("--pack")
    child.add_argument("--buyer-id")

    command = sub.add_parser("wechat", help="Manage WeChat payments", description="Start and manage WeChat Native payment sessions")
    children = command.add_subparsers(dest="wechat_command", required=True)
    child = children.add_parser("start")
    child.add_argument("server")
    child.add_argument("service")
    child.add_argument("--params")
    for name in ("status", "fulfill", "cancel"):
        child = children.add_parser(name)
        child.add_argument("identifier")
    children.add_parser("list")

    command = sub.add_parser("alipay", help="Run Alipay buyer commands", description="Pass commands through to the official alipay-bot CLI")
    command.add_argument("action")
    command.add_argument("args", nargs="*")
    return parser


COMMANDS = {
    "init": cmd_init, "status": cmd_status, "faucet": cmd_faucet,
    "pay": cmd_pay, "approve": cmd_approve, "services": cmd_services,
    "fund": cmd_fund, "transfer": cmd_transfer, "config": cmd_config,
    "balance": cmd_balance, "limits": cmd_limits, "wechat": cmd_wechat, "alipay": cmd_alipay,
}


def main() -> int:
    configure_stdio()
    args = build_parser().parse_args()
    try:
        return COMMANDS[args.command](args)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
