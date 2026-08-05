#!/usr/bin/env python3
"""
MoltsPay CLI - Command-line interface for MoltsPay Python SDK.

Commands:
    init        Initialize wallet (EVM or Solana)
    status      Show wallet status and balances
    faucet      Request testnet tokens
    pay         Pay for a service
    approve     Approve spender for BNB chain
"""

import argparse
import sys
import json
from pathlib import Path

from .client import MoltsPay
from .wallet import Wallet, DEFAULT_WALLET_PATH, CHAINS as WALLET_CHAINS
from .chains import CHAINS, get_chain, is_testnet
from .exceptions import PaymentError, WalletError


def cmd_init(args):
    """Initialize a new wallet."""
    chain = args.chain
    
    # Solana wallet
    if chain and chain.startswith("solana"):
        try:
            from .wallet_solana import SolanaWallet, DEFAULT_SOLANA_WALLET_PATH
            
            wallet_path = args.config_dir or str(DEFAULT_SOLANA_WALLET_PATH)
            
            # Check if exists
            if Path(wallet_path).exists() and not args.force:
                print(f"⚠️  Solana wallet already exists: {wallet_path}")
                print("   Use --force to overwrite")
                return 1
            
            wallet = SolanaWallet(wallet_path=wallet_path, create_if_missing=True)
            
            print("\n🔐 MoltsPay Solana Wallet Created\n")
            print(f"   Address: {wallet.public_key}")
            print(f"   Saved to: {wallet_path}")
            print(f"\n💡 Get testnet USDC:")
            print(f"   moltspay faucet --chain solana_devnet\n")
            
        except ImportError:
            print("❌ Solana support requires 'solders' package.")
            print("   Install with: pip install solders")
            return 1
    
    # EVM wallet
    else:
        wallet_path = args.config_dir or str(DEFAULT_WALLET_PATH)
        
        if Path(wallet_path).exists() and not args.force:
            print(f"⚠️  EVM wallet already exists: {wallet_path}")
            print("   Use --force to overwrite")
            return 1
        
        wallet = Wallet(wallet_path=wallet_path)
        
        print("\n🔐 MoltsPay EVM Wallet Created\n")
        print(f"   Address: {wallet.address}")
        print(f"   Chain: {chain or 'base'}")
        print(f"   Saved to: {wallet_path}")
        
        if is_testnet(chain or "base"):
            print(f"\n💡 Get testnet USDC:")
            print(f"   moltspay faucet --chain {chain}\n")
        else:
            print(f"\n💡 Fund your wallet:")
            print(f"   moltspay fund --amount 10\n")
    
    return 0


def cmd_status(args):
    """Show wallet status."""
    chain = args.chain or "base"
    
    print("\n📊 MoltsPay Wallet Status\n")
    
    # EVM wallet
    try:
        wallet = Wallet(chain=chain)
        print(f"EVM Wallet:")
        print(f"   Address: {wallet.address}")
        print(f"   Chain: {chain}")
        limits = wallet.limits
        print(f"   Limits: ${limits.max_per_tx}/tx, ${limits.max_per_day}/day")
    except Exception as e:
        print(f"EVM Wallet: Not found ({e})")
    
    # Solana wallet
    try:
        from .wallet_solana import SolanaWallet
        solana_wallet = SolanaWallet(create_if_missing=False)
        if solana_wallet.exists:
            print(f"\nSolana Wallet:")
            print(f"   Address: {solana_wallet.public_key}")
    except ImportError:
        pass
    except Exception:
        pass
    
    print("")
    return 0


def cmd_faucet(args):
    """Request testnet tokens."""
    chain = args.chain or "base_sepolia"
    
    if not is_testnet(chain):
        print(f"❌ Faucet only works on testnets. Use one of:")
        print("   base_sepolia, bnb_testnet, tempo_moderato, solana_devnet")
        return 1
    
    print(f"\n🚰 Requesting testnet tokens on {chain}...\n")
    
    try:
        client = MoltsPay(chain=chain)
        result = client.faucet()
        
        if result.success:
            print(f"✅ Received {result.amount} {result.token or 'tokens'}!")
            if result.tx_hash:
                print(f"   TX: {result.tx_hash}")
        else:
            print(f"❌ {result.error}")
            return 1
            
    except Exception as e:
        print(f"❌ Error: {e}")
        return 1
    
    print("")
    return 0


def cmd_pay(args):
    """Pay for a service."""
    service_url = args.url
    service_id = args.service
    chain = args.chain or "base"
    prompt = args.prompt
    
    if not prompt:
        print("❌ --prompt is required")
        return 1
    
    print(f"\n💰 Paying for {service_id} on {chain}...\n")
    
    try:
        client = MoltsPay(chain=chain, timeout=120.0)
        
        # Build params
        params = {"prompt": prompt}
        if args.params:
            params.update(json.loads(args.params))
        
        result = client.pay(service_url, service_id, **params)
        
        if result.success:
            print(f"✅ Payment successful!")
            print(f"   Amount: ${result.amount} {result.token}")
            if result.tx_hash:
                print(f"   TX: {result.tx_hash}")
            if result.explorer_url:
                print(f"   Explorer: {result.explorer_url}")
            print(f"\n📦 Result:")
            if isinstance(result.result, dict):
                print(json.dumps(result.result, indent=2))
            else:
                print(result.result)
        else:
            print(f"❌ Payment failed: {result.error}")
            return 1
            
    except Exception as e:
        print(f"❌ Error: {e}")
        return 1
    
    print("")
    return 0


def cmd_approve(args):
    """Approve spender for BNB chain."""
    # Default spender is the MoltsPay faucet wallet (also used as BNB facilitator)
    DEFAULT_SPENDER = "0x145E00f48b98E2829f803Be53418230e47943a8A"
    
    chain = args.chain
    spender = args.spender or DEFAULT_SPENDER
    
    if not chain or not chain.startswith("bnb"):
        print("❌ --chain must be 'bnb' or 'bnb_testnet'")
        return 1
    
    print(f"\n🔓 Approving {spender[:10]}... on {chain}...\n")
    
    try:
        from web3 import Web3
        from eth_account import Account
        
        wallet = Wallet(chain=chain)
        chain_config = CHAINS[chain]
        
        w3 = Web3(Web3.HTTPProvider(chain_config["rpc"]))
        account = wallet._account
        
        # ERC20 approve ABI
        approve_abi = [{
            "name": "approve",
            "type": "function",
            "inputs": [
                {"name": "spender", "type": "address"},
                {"name": "amount", "type": "uint256"},
            ],
            "outputs": [{"type": "bool"}],
        }]
        
        # Approve both USDC and USDT
        for token_name, token_config in chain_config["tokens"].items():
            token_address = token_config["address"]
            token = w3.eth.contract(
                address=Web3.to_checksum_address(token_address),
                abi=approve_abi,
            )
            
            nonce = w3.eth.get_transaction_count(account.address)
            max_amount = 2**256 - 1  # Max uint256
            
            tx = token.functions.approve(
                Web3.to_checksum_address(spender),
                max_amount,
            ).build_transaction({
                "chainId": chain_config["chainId"],
                "gas": 100000,
                "gasPrice": w3.eth.gas_price,
                "nonce": nonce,
            })
            
            signed_tx = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
            
            if receipt["status"] == 1:
                print(f"✅ {token_name} approved: {tx_hash.hex()}")
            else:
                print(f"❌ {token_name} approval failed")
        
        print(f"\n✅ Approval complete! You can now pay on {chain}.\n")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        return 1
    
    return 0


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="MoltsPay CLI - Agent Payments SDK",
        prog="moltspay",
    )
    parser.add_argument("--version", action="version", version="moltspay 0.6.1")
    
    subparsers = parser.add_subparsers(dest="command", help="Commands")
    
    # init
    init_parser = subparsers.add_parser("init", help="Initialize wallet")
    init_parser.add_argument("--chain", help="Chain (base, solana_devnet, etc.)")
    init_parser.add_argument("--config-dir", help="Config directory")
    init_parser.add_argument("--force", action="store_true", help="Overwrite existing")
    
    # status
    status_parser = subparsers.add_parser("status", help="Show wallet status")
    status_parser.add_argument("--chain", help="Chain to check")
    
    # faucet
    faucet_parser = subparsers.add_parser("faucet", help="Request testnet tokens")
    faucet_parser.add_argument("--chain", help="Testnet chain", default="base_sepolia")
    
    # pay
    pay_parser = subparsers.add_parser("pay", help="Pay for a service")
    pay_parser.add_argument("url", help="Service URL")
    pay_parser.add_argument("service", help="Service ID")
    pay_parser.add_argument("--chain", help="Chain to pay on", default="base")
    pay_parser.add_argument("--prompt", help="Prompt for the service")
    pay_parser.add_argument("--params", help="Additional params as JSON")
    
    # approve
    approve_parser = subparsers.add_parser("approve", help="Approve spender (BNB)")
    approve_parser.add_argument("--chain", help="BNB chain", required=True)
    approve_parser.add_argument("--spender", help="Spender address (default: MoltsPay faucet)")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 0
    
    commands = {
        "init": cmd_init,
        "status": cmd_status,
        "faucet": cmd_faucet,
        "pay": cmd_pay,
        "approve": cmd_approve,
    }
    
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
