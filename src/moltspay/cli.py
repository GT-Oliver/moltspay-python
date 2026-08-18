#!/usr/bin/env python3
"""MoltsPay command-line interface."""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .chains import CHAINS, is_testnet, list_chains
from .client import MoltsPay
from .wallet import DEFAULT_WALLET_PATH, Wallet
from .exceptions import InteractiveRailRequiresLifecycle, MoltsPayError

DEFAULT_CONFIG_DIR = DEFAULT_WALLET_PATH.parent


def config_dir(args) -> Path:
    return Path(getattr(args, "config_dir", None) or DEFAULT_CONFIG_DIR).expanduser()


def client_for(args, **kwargs) -> MoltsPay:
    root = config_dir(args)
    kwargs.setdefault("alipay_framework", getattr(args, "framework", None))
    return MoltsPay(
        chain=getattr(args, "chain", None) or "base",
        config_dir=str(root),
        wallet_path=str(root / "wallet.json"),
        solana_wallet_path=str(root / "wallet-solana.json"),
        **kwargs,
    )


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
        wallet = SolanaWallet(wallet_path=str(path), create_if_missing=True)
        if args.max_per_tx is not None or args.max_per_day is not None:
            # Solana wallets do not currently persist EVM spending limits.
            print("Warning: spending limits apply to the EVM wallet only", file=sys.stderr)
        output({"address": wallet.address, "chain": chain, "path": str(path)})
        return 0
    path = Path(args.config_dir).expanduser() / "wallet.json" if args.config_dir else DEFAULT_WALLET_PATH
    wallet = Wallet(wallet_path=str(path), chain=chain)
    if args.max_per_tx is not None or args.max_per_day is not None:
        wallet.set_limits(max_per_tx=args.max_per_tx, max_per_day=args.max_per_day)
    output({"address": wallet.address, "chain": chain, "path": str(path)})
    return 0


def cmd_status(args) -> int:
    client = client_for(args)
    balances = {"base": client.balance("base")}
    result = {"address": client.address, "config": client.get_config(), "balances": balances}
    result["balance"] = balances["base"]
    output(result)
    return 0


def cmd_faucet(args) -> int:
    if not is_testnet(args.chain):
        print("Faucet requires a testnet chain", file=sys.stderr)
        return 1
    client = client_for(args)
    if getattr(args, "address", None) and args.address.lower() != client.address.lower():
        print("--address is not supported for the configured wallet", file=sys.stderr)
        return 1
    result = client.faucet()
    output(result)
    return 0 if result.success else 1


def cmd_pay(args) -> int:
    framework = getattr(args, "framework", None) or os.environ.get("AIPAY_FRAMEWORK")
    if args.rail == "alipay" and str(framework or "").lower() == "openclaw":
        raise InteractiveRailRequiresLifecycle(
            "Interactive Alipay payments in OpenClaw require the non-blocking "
            "'moltspay alipay start' lifecycle",
            details={"command": "moltspay alipay start"},
        )
    params = json.loads(args.params or "{}") if isinstance(args.params, str) else (args.params or {})
    if args.image is not None:
        params["image"] = args.image
    if args.data is not None:
        data = json.loads(args.data)
        if not isinstance(data, dict):
            raise ValueError("--data must contain a JSON object")
        params.update(data)
    if args.prompt is not None:
        params["prompt"] = args.prompt
    client = client_for(args)

    def show_topup(pack: str, code_url: str) -> None:
        print(f"Provider balance top-up required: CNY {pack}", file=sys.stderr)
        print_wechat_qr(code_url)

    result = client.pay(
        args.server, args.service, token=args.token, chain=args.chain,
        rail=args.rail, payment_params=params,
        rail_options={
            "buyer_id": getattr(args, "buyer", None),
            "topup_pack": getattr(args, "pack", None),
            "topup_mode": getattr(args, "topup_mode", "auto"),
            "auto_topup": not getattr(args, "no_auto_topup", False),
            "max_topup_attempts": getattr(args, "max_topup_attempts", 10),
            "topup_poll_interval": getattr(args, "topup_poll_interval", 2.0),
            "topup_rail": getattr(args, "topup_rail", "wechat"),
            "on_topup_required": show_topup,
            "intent_summary": getattr(args, "intent_summary", None),
            "business_session_id": getattr(args, "session_id", None),
            "timeout": getattr(args, "timeout", None),
            "poll_interval": getattr(args, "poll_interval", 3.0),
        },
    )
    output(result)
    return 0 if result.success else 1


def cmd_alipay(args) -> int:
    client = client_for(args)
    command = args.alipay_command
    if command == "start":
        params = json.loads(args.params or "{}") if isinstance(args.params, str) else (args.params or {})
        if not isinstance(params, dict):
            raise ValueError("params must contain a JSON object")
        session = client.start_alipay_payment(
            args.server,
            args.service,
            params,
            intent_summary=args.intent_summary,
            timeout=args.timeout,
            request_id=args.request_id,
            business_session_id=args.session_id,
        )
        output(session)
        for media_path in session.media_paths:
            print(f"MEDIA: {media_path}")
        return 0 if session.status in {"pending", "processing", "completed"} else 1
    if command == "check-wallet":
        output(client.check_alipay_wallet())
    elif command == "status":
        output(client.get_alipay_payment_status(args.identifier))
    elif command == "resume":
        session = client.resume_alipay_payment(args.identifier)
        output(session)
        return 0 if session.status in {"completed", "pending", "processing"} else 1
    elif command == "list":
        output(client.list_alipay_payment_sessions(limit=args.limit))
    return 0


def cmd_approve(args) -> int:
    from web3 import Web3
    chain = args.chain
    if chain not in ("bnb", "bnb_testnet"):
        print("--chain must be bnb or bnb_testnet", file=sys.stderr)
        return 1
    root = config_dir(args)
    wallet = Wallet(wallet_path=str(root / "wallet.json"), chain=chain)
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
    if not args.url:
        raise ValueError("The Python SDK does not bundle a service registry; provide a provider URL")
    output(client_for(args).get_services(args.url))
    return 0


def cmd_fund(args) -> int:
    result = client_for(args).fund_qr(args.amount, args.chain)
    output(result)
    return 0 if result.success else 1


def cmd_transfer(args) -> int:
    if not getattr(args, "yes", False) and sys.stdin.isatty():
        answer = input(f"Transfer {args.amount} {args.token} to {args.to}? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            return 1
    result = client_for(args).transfer(args.to, args.amount, args.token, args.chain)
    output(result)
    return 0 if result.success else 1


def cmd_config(args) -> int:
    client = client_for(args)
    changed = any(value is not None for value in (args.max_per_tx, args.max_per_day))
    value = client.update_config(
        max_per_tx=args.max_per_tx, max_per_day=args.max_per_day,
    ) if changed else client.get_config()
    output(value)
    return 0


def cmd_list(args) -> int:
    # The Python client does not yet persist a Node-compatible transaction
    # history. Keep the command available and machine-readable until that
    # storage is implemented rather than exposing a different command tree.
    output([])
    return 0


def cmd_validate(args) -> int:
    path = Path(args.path).expanduser()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("services"), list):
        raise ValueError("manifest must be an object with a services array")
    output({"valid": True, "path": str(path)})
    return 0


def cmd_server(args) -> int:
    if args.server_command == "stop":
        pid_file = config_dir(args) / "server.pid"
        if not pid_file.exists():
            raise ValueError("No running MoltsPay server found")
        pid_file.unlink()
        return 0
    from .server.server import MoltsPayServer
    server = MoltsPayServer(*args.paths, port=args.port, host=args.host)
    server.listen()
    return 0


def cmd_limits(args) -> int:
    client = client_for(args)
    if args.max_per_tx is not None or args.max_per_day is not None:
        client.set_limits(max_per_tx=args.max_per_tx, max_per_day=args.max_per_day)
    output(client.limits())
    return 0


def cmd_balance(args) -> int:
    buyer = getattr(args, "buyer", None)
    client = client_for(args, buyer_id=buyer)
    if args.balance_command == "query":
        output(client.get_buyer_balance(args.server, buyer_id=buyer))
    elif args.balance_command == "transactions":
        output(client.list_balance_transactions(args.server, buyer_id=buyer, limit=args.limit, offset=args.offset))
    elif args.balance_command == "set-buyer":
        client.set_buyer_id(args.id)
        output(client.get_config())
    elif args.balance_command == "whoami":
        result = {"buyerId": client._buyer_id, "address": client.get_balance_signer_address()}
        if args.server:
            result["balance"] = client.get_buyer_balance(args.server)
        output(result)
    elif args.balance_command == "bind":
        output(client.topup_balance_pack(args.server, pack=args.pack, buyer_id=args.buyer))
    elif args.balance_command == "topup":
        output(client.topup_balance(args.server, args.amount, args.rail, buyer_id=args.buyer,
                                    tx_hash=args.tx_hash, chain=args.chain,
                                    trade_no=args.trade_no, out_trade_no=args.out_trade_no))
    elif args.balance_command == "topup-order":
        result = client.create_balance_topup_order(args.server, pack=args.pack, buyer_id=args.buyer, rail=getattr(args, "rail", "wechat"))
        output({"status": "topup_required", "out_trade_no": result["outTradeNo"],
                "code_url": result.get("codeUrl"), "payment_session_id": result.get("paymentSessionId"), "rail": result.get("rail", "wechat"), "pack": result["pack"], "server_url": args.server})
    elif args.balance_command == "topup-confirm":
        output(client.confirm_balance_topup(args.id, server_url=getattr(args, "server", None)))
    elif args.balance_command == "topup-status":
        output(client.get_balance_topup_session(args.id))
    elif args.balance_command == "topup-list":
        output(client.list_balance_topup_sessions())
    elif args.balance_command == "topup-pack":
        output(client.topup_balance_pack(args.server, pack=args.pack, buyer_id=args.buyer, rail=getattr(args, "rail", "wechat")))
    return 0


def cmd_wechat(args) -> int:
    client = client_for(args)
    if args.wechat_command == "start":
        params = json.loads(args.params or "{}") if args.params else {}
        if getattr(args, "prompt", None) is not None:
            params["prompt"] = args.prompt
        if getattr(args, "image", None) is not None:
            params["image"] = args.image
        if getattr(args, "data", None) is not None:
            params.update(json.loads(args.data))
        session = client.start_wechat_payment(args.server, args.service, params)
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="moltspay",
        description="MoltsPay - Payment infrastructure for AI Agents",
    )
    parser.add_argument("--version", action="version", version=f"moltspay {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    chain_choices = sorted(list_chains())
    testnet_choices = [chain for chain in chain_choices if is_testnet(chain)]
    config_default = str(DEFAULT_CONFIG_DIR)

    command = sub.add_parser("init", help="Create a wallet", description="Create an EVM or Solana wallet")
    command.add_argument("--chain", default="base", choices=chain_choices)
    command.add_argument("--max-per-tx", type=float)
    command.add_argument("--max-per-day", type=float)
    command.add_argument("--config-dir", default=config_default)

    command = sub.add_parser("status", help="Show wallet configuration and balance", description="Show wallet configuration and balance")
    command.add_argument("--config-dir", default=config_default)
    command.add_argument("--json", action="store_true")

    command = sub.add_parser("faucet", help="Request testnet tokens", description="Request free tokens from a supported testnet faucet")
    command.add_argument("--chain", default="base_sepolia", choices=testnet_choices)
    command.add_argument("--address")
    command.add_argument("--config-dir", default=config_default)

    command = sub.add_parser("pay", help="Pay for a service", description="Discover and pay for a provider service")
    command.add_argument("server")
    command.add_argument("service")
    command.add_argument("params", nargs="?")
    command.add_argument("--prompt")
    command.add_argument("--image")
    command.add_argument("--data")
    command.add_argument("--token", default="USDC")
    command.add_argument("--chain", choices=chain_choices)
    command.add_argument("--rail")
    command.add_argument("--buyer")
    command.add_argument("--pack")
    command.add_argument("--topup-mode", choices=["auto", "manual"], default="auto")
    command.add_argument("--no-auto-topup", action="store_true")
    command.add_argument("--max-topup-attempts", type=int, default=10)
    command.add_argument("--topup-poll-interval", type=float, default=2.0)
    command.add_argument("--topup-rail", choices=["wechat", "alipay"], default="wechat")
    command.add_argument("--intent-summary", help="Human-readable purpose passed to the official Alipay CLI")
    command.add_argument(
        "--session-id",
        help=(
            "Real runtime business session ID for Alipay; defaults to AIPAY_SESSION_ID. "
            "Local mpay_alipay_* IDs are rejected."
        ),
    )
    command.add_argument(
        "--framework",
        help="Runtime framework name for Alipay; defaults to AIPAY_FRAMEWORK or moltspay",
    )
    command.add_argument("--timeout", type=float, help="Maximum Alipay interaction time in seconds")
    command.add_argument("--poll-interval", type=float, default=3.0, help="Alipay resume polling interval")
    command.add_argument("--config-dir", default=config_default)
    command.add_argument("--json", action="store_true")

    command = sub.add_parser("approve", help="Approve BNB token spending", description="Approve USDC/USDT spending on BNB Chain")
    command.add_argument("--chain", choices=["bnb", "bnb_testnet"], default="bnb_testnet")
    command.add_argument("--spender")
    command.add_argument("--config-dir", default=config_default)

    command = sub.add_parser("services", help="Discover provider services", description="List services exposed by a provider")
    command.add_argument("url", nargs="?")
    command.add_argument("-q", "--query")
    command.add_argument("--max-price", type=float)
    command.add_argument("--type")
    command.add_argument("--tag")
    command.add_argument("--json", action="store_true")

    command = sub.add_parser("fund", help="Fund wallet with fiat", description="Generate a debit-card/Apple Pay funding QR code")
    command.add_argument("amount", type=float)
    command.add_argument("--chain", default="base", choices=chain_choices)
    command.add_argument("--config-dir", default=config_default)

    command = sub.add_parser("transfer", help="Transfer tokens", description="Transfer USDC or USDT to an EVM address")
    command.add_argument("to")
    command.add_argument("amount")
    command.add_argument("--token", choices=["USDC", "USDT"], default="USDC")
    command.add_argument("--chain", default="base", choices=chain_choices)
    command.add_argument("--yes", action="store_true")
    command.add_argument("--json", action="store_true")
    command.add_argument("--config-dir", default=config_default)
    
    command = sub.add_parser("config", help="Read or update client configuration", description="Update MoltsPay settings")
    command.add_argument("--max-per-tx", type=float)
    command.add_argument("--max-per-day", type=float)
    command.add_argument("--config-dir", default=config_default)

    command = sub.add_parser("balance", help="Use a provider balance account", description="Query balance-rail accounts and transactions")
    children = command.add_subparsers(dest="balance_command", required=True)
    child = children.add_parser("query")
    child.add_argument("server")
    child.add_argument("--buyer")
    child.add_argument("--config-dir", default=config_default)
    child.add_argument("--json", action="store_true")
    child = children.add_parser("whoami")
    child.add_argument("server", nargs="?")
    child.add_argument("--buyer")
    child.add_argument("--config-dir", default=config_default)
    child.add_argument("--json", action="store_true")
    child = children.add_parser("bind")
    child.add_argument("server")
    child.add_argument("--pack")
    child.add_argument("--buyer")
    child.add_argument("--config-dir", default=config_default)
    child.add_argument("--json", action="store_true")
    child = children.add_parser("topup")
    child.add_argument("server")
    child.add_argument("amount")
    child.add_argument("--buyer")
    child.add_argument("--tx-hash")
    child.add_argument("--chain", default="base", choices=chain_choices)
    child.add_argument("--trade-no")
    child.add_argument("--out-trade-no")
    child.add_argument("--rail", required=True)
    child.add_argument("--config-dir", default=config_default)
    child.add_argument("--json", action="store_true")
    child = children.add_parser("transactions")
    child.add_argument("server")
    child.add_argument("--limit", type=int, default=20)
    child.add_argument("--offset", type=int, default=0)
    child.add_argument("--buyer")
    child.add_argument("--config-dir", default=config_default)
    child.add_argument("--json", action="store_true")
    child = children.add_parser("set-buyer")
    child.add_argument("id")
    child.add_argument("--config-dir", default=config_default)
    child = children.add_parser("topup-order")
    child.add_argument("server")
    child.add_argument("--pack")
    child.add_argument("--buyer")
    child.add_argument("--rail", choices=["wechat", "alipay"], default="wechat")
    child.add_argument("--config-dir", default=config_default)
    child.add_argument("--json", action="store_true")
    child = children.add_parser("topup-confirm")
    child.add_argument("id", help="out_trade_no")
    child.add_argument("--server")
    child.add_argument("--wait", type=float, default=0)
    child.add_argument("--config-dir", default=config_default)
    child.add_argument("--json", action="store_true")
    child = children.add_parser("topup-status")
    child.add_argument("id", help="out_trade_no")
    child.add_argument("--config-dir", default=config_default)
    child.add_argument("--json", action="store_true")
    child = children.add_parser("topup-list")
    child.add_argument("--config-dir", default=config_default)
    child.add_argument("--json", action="store_true")
    child = children.add_parser("topup-pack")
    child.add_argument("server")
    child.add_argument("--pack")
    child.add_argument("--buyer")
    child.add_argument("--rail", choices=["wechat", "alipay"], default="wechat")
    child.add_argument("--config-dir", default=config_default)
    child.add_argument("--json", action="store_true")

    command = sub.add_parser("wechat", help="Manage WeChat payments", description="Start and manage WeChat Native payment sessions")
    children = command.add_subparsers(dest="wechat_command", required=True)
    child = children.add_parser("start")
    child.add_argument("server")
    child.add_argument("service")
    child.add_argument("params", nargs="?")
    child.add_argument("--prompt")
    child.add_argument("--image")
    child.add_argument("--data")
    child.add_argument("--config-dir", default=config_default)
    child.add_argument("--json", action="store_true")

    for name in ("status", "fulfill", "cancel"):
        child = children.add_parser(name)
        child.add_argument("identifier")
        child.add_argument("--config-dir", default=config_default)
        child.add_argument("--json", action="store_true")
    child = children.add_parser("list")
    child.add_argument("--config-dir", default=config_default)
    child.add_argument("--json", action="store_true")

    command = sub.add_parser("alipay", help="Manage Alipay A402 payments", description="Start and recover Alipay AI Pay sessions")
    children = command.add_subparsers(dest="alipay_command", required=True)
    child = children.add_parser("start", help="Start one recoverable Alipay payment without polling")
    child.add_argument("server")
    child.add_argument("service")
    child.add_argument("params", nargs="?")
    child.add_argument("--session-id", help="Real runtime business session ID; defaults to AIPAY_SESSION_ID")
    child.add_argument("--framework", help="Runtime framework name; defaults to AIPAY_FRAMEWORK or moltspay")
    child.add_argument("--intent-summary")
    child.add_argument("--timeout", type=float)
    child.add_argument("--request-id")
    child.add_argument("--config-dir", default=config_default)
    child.add_argument("--json", action="store_true")
    child = children.add_parser("check-wallet", help="Read official Alipay AI wallet readiness")
    child.add_argument("--config-dir", default=config_default)
    child.add_argument("--json", action="store_true")
    for name in ("status", "resume"):
        child = children.add_parser(name, help=("Resume a payment; this may retry the provider request" if name == "resume" else "Read a local payment session"))
        child.add_argument("identifier")
        child.add_argument("--config-dir", default=config_default)
        child.add_argument("--json", action="store_true")
    child = children.add_parser("list", help="List local Alipay payment sessions")
    child.add_argument("--limit", type=int, default=100)
    child.add_argument("--config-dir", default=config_default)
    child.add_argument("--json", action="store_true")
    command = sub.add_parser("list", help="List recent transactions")
    command.add_argument("--days", default="7")
    command.add_argument("--chain", default="all")
    command.add_argument("--limit", default="20")
    command.add_argument("--config-dir", default=config_default)

    command = sub.add_parser("validate", help="Validate a service manifest")
    command.add_argument("path")

    command = sub.add_parser("server", help="Manage a MoltsPay server")
    children = command.add_subparsers(dest="server_command", required=True)
    child = children.add_parser("start")
    child.add_argument("paths", nargs="+")
    child.add_argument("-p", "--port", type=int, default=3000)
    child.add_argument("--host", default="0.0.0.0")
    child.add_argument("--facilitator")
    child.add_argument("--config-dir", default=config_default)
    child = children.add_parser("stop")
    child.add_argument("--config-dir", default=config_default)

    return parser


COMMANDS = {
    "init": cmd_init, "status": cmd_status, "faucet": cmd_faucet,
    "pay": cmd_pay, "approve": cmd_approve, "services": cmd_services,
    "fund": cmd_fund, "transfer": cmd_transfer, "config": cmd_config,
    "balance": cmd_balance, "wechat": cmd_wechat, "alipay": cmd_alipay,
    "list": cmd_list, "validate": cmd_validate, "server": cmd_server,
}


def main() -> int:
    configure_stdio()
    args = build_parser().parse_args()
    try:
        return COMMANDS[args.command](args)
    except Exception as exc:
        if isinstance(exc, MoltsPayError):
            payload = {
                "success": False, "error": {"code": str(exc.code).lower(), "message": str(exc),
                "retryable": str(exc.code).lower() in {"alipay_payment_timeout", "alipay_payment_state_unknown", "alipay_verify_unavailable"},
                "details": getattr(exc, "details", {}) or {}},
            }
            if getattr(args, "json", False):
                output(payload)
            else:
                print(f"Error [{payload['error']['code']}]: {exc}", file=sys.stderr)
                session_id = payload["error"]["details"].get("paymentSessionId")
                if session_id:
                    print(f"Resume with: moltspay alipay resume {session_id}", file=sys.stderr)
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
