#!/usr/bin/env python3
"""MoltsPay command-line interface."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .chains import CHAINS, is_testnet
from .client import MoltsPay
from .wallet import DEFAULT_WALLET_PATH, Wallet


def output(value: Any) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


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
    output({"address": client.address, "config": client.get_config(), "balance": client.balance(args.chain)})
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


def cmd_balance(args) -> int:
    client = MoltsPay(chain=args.chain, buyer_id=args.buyer_id)
    if args.balance_command == "query":
        output(client.get_buyer_balance(args.server))
    elif args.balance_command == "transactions":
        output(client.list_balance_transactions(args.server, limit=args.limit, offset=args.offset))
    elif args.balance_command == "set-buyer":
        client.set_buyer_id(args.id)
        output(client.get_config())
    return 0


def cmd_wechat(args) -> int:
    client = MoltsPay()
    if args.wechat_command == "start":
        output(client.start_wechat_payment(args.server, args.service, json.loads(args.params or "{}")))
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
    parser = argparse.ArgumentParser(prog="moltspay", description="MoltsPay agent payments SDK")
    parser.add_argument("--version", action="version", version=f"moltspay {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    command = sub.add_parser("init")
    command.add_argument("--chain", default="base")
    command.add_argument("--config-dir")
    command.add_argument("--force", action="store_true")

    command = sub.add_parser("status")
    command.add_argument("--chain", default="base")

    command = sub.add_parser("faucet")
    command.add_argument("--chain", default="base_sepolia")

    command = sub.add_parser("pay")
    command.add_argument("url")
    command.add_argument("service")
    command.add_argument("--chain", default="base")
    command.add_argument("--token", choices=["USDC", "USDT"], default="USDC")
    command.add_argument("--rail", choices=["balance", "wechat", "alipay"])
    command.add_argument("--prompt")
    command.add_argument("--params")
    command.add_argument("--timeout", type=float, default=180.0)

    command = sub.add_parser("approve")
    command.add_argument("--chain", required=True)
    command.add_argument("--spender")

    command = sub.add_parser("services")
    command.add_argument("url")
    command.add_argument("--chain", default="base")

    command = sub.add_parser("fund")
    command.add_argument("amount", type=float)
    command.add_argument("--chain", default="base")

    command = sub.add_parser("transfer")
    command.add_argument("to")
    command.add_argument("amount")
    command.add_argument("--token", choices=["USDC", "USDT"], default="USDC")
    command.add_argument("--chain", default="base")

    command = sub.add_parser("config")
    command.add_argument("--chain", default="base")
    command.add_argument("--max-per-tx", type=float)
    command.add_argument("--max-per-day", type=float)
    command.add_argument("--rail-preference")
    command.add_argument("--buyer-id")

    command = sub.add_parser("balance")
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

    command = sub.add_parser("wechat")
    children = command.add_subparsers(dest="wechat_command", required=True)
    child = children.add_parser("start")
    child.add_argument("server")
    child.add_argument("service")
    child.add_argument("--params")
    for name in ("status", "fulfill", "cancel"):
        child = children.add_parser(name)
        child.add_argument("identifier")
    children.add_parser("list")

    command = sub.add_parser("alipay")
    command.add_argument("action")
    command.add_argument("args", nargs="*")
    return parser


COMMANDS = {
    "init": cmd_init, "status": cmd_status, "faucet": cmd_faucet,
    "pay": cmd_pay, "approve": cmd_approve, "services": cmd_services,
    "fund": cmd_fund, "transfer": cmd_transfer, "config": cmd_config,
    "balance": cmd_balance, "wechat": cmd_wechat, "alipay": cmd_alipay,
}


def main() -> int:
    args = build_parser().parse_args()
    try:
        return COMMANDS[args.command](args)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
