"""On-chain payment verification compatible with the Node.js SDK."""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Union

from .chains import CHAINS, get_chain_by_id
from .models import VerifyPaymentResult


def _chain_name(chain: Union[str, int]) -> Optional[str]:
    return get_chain_by_id(chain) if isinstance(chain, int) else chain


def verify_payment(
    tx_hash: str,
    expected_amount: float,
    expected_to: Optional[str] = None,
    chain: Union[str, int] = "base",
    expected_token: Optional[str] = None,
    tolerance: float = 0.0,
) -> VerifyPaymentResult:
    """Verify an ERC-20 Transfer event in a confirmed transaction receipt."""
    name = _chain_name(chain)
    config = CHAINS.get(name or "")
    if not config or config.get("type") == "solana":
        return VerifyPaymentResult(verified=False, tx_hash=tx_hash, error=f"Unsupported chain: {chain}")
    try:
        from web3 import Web3
        transfer_topic = Web3.keccak(text="Transfer(address,address,uint256)").hex().lower()
        w3 = Web3(Web3.HTTPProvider(config["rpc"]))
        receipt = w3.eth.get_transaction_receipt(tx_hash)
        if receipt is None:
            return VerifyPaymentResult(verified=False, tx_hash=tx_hash, pending=True, error="Transaction not found or not confirmed")
        if int(receipt["status"]) != 1:
            return VerifyPaymentResult(verified=False, tx_hash=tx_hash, block_number=int(receipt["blockNumber"]), error="Transaction failed")

        accepted: Dict[str, tuple[str, dict]] = {}
        for symbol, token_config in config.get("tokens", {}).items():
            normalized = symbol.upper()
            if expected_token and normalized != expected_token.upper():
                continue
            if normalized in ("USDC", "USDT", "PATHUSD", "ALPHAUSD"):
                accepted[token_config["address"].lower()] = (normalized, token_config)

        for log in receipt["logs"]:
            address = str(log["address"]).lower()
            detected = accepted.get(address)
            topics = log["topics"]
            if not detected or len(topics) < 3 or topics[0].hex().lower() != transfer_topic:
                continue
            symbol, token_config = detected
            sender = "0x" + topics[1].hex()[-40:]
            recipient = "0x" + topics[2].hex()[-40:]
            if expected_to and recipient.lower() != expected_to.lower():
                continue
            raw = int(log["data"].hex(), 16)
            amount = raw / (10 ** int(token_config.get("decimals", 6)))
            minimum = expected_amount * (1 - max(0.0, tolerance))
            verified = amount >= minimum
            current_block = w3.eth.block_number
            return VerifyPaymentResult(
                verified=verified,
                tx_hash=tx_hash,
                amount=f"{amount:.{int(token_config.get('decimals', 6))}f}",
                token=symbol,
                sender=Web3.to_checksum_address(sender),
                recipient=Web3.to_checksum_address(recipient),
                block_number=int(receipt["blockNumber"]),
                confirmations=max(0, current_block - int(receipt["blockNumber"]) + 1),
                explorer_url=f"{config['explorer']}/tx/{tx_hash}",
                error=None if verified else f"Insufficient amount: received {amount}, expected {expected_amount}",
            )
        label = expected_token.upper() if expected_token else "USDC/USDT"
        return VerifyPaymentResult(verified=False, tx_hash=tx_hash, error=f"No {label} transfer found")
    except Exception as exc:
        return VerifyPaymentResult(verified=False, tx_hash=tx_hash, error=str(exc))


def get_transaction_status(tx_hash: str, chain: Union[str, int] = "base") -> Dict[str, Any]:
    """Return pending, confirmed, failed, or not_found for a transaction."""
    name = _chain_name(chain)
    config = CHAINS.get(name or "")
    if not config or config.get("type") == "solana":
        return {"status": "not_found"}
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(config["rpc"]))
        receipt = w3.eth.get_transaction_receipt(tx_hash)
        if receipt is None:
            tx = w3.eth.get_transaction(tx_hash)
            return {"status": "pending" if tx else "not_found"}
        block_number = int(receipt["blockNumber"])
        return {
            "status": "confirmed" if int(receipt["status"]) == 1 else "failed",
            "block_number": block_number,
            "confirmations": max(0, w3.eth.block_number - block_number + 1),
        }
    except Exception:
        return {"status": "not_found"}


def wait_for_transaction(
    tx_hash: str,
    chain: Union[str, int] = "base",
    confirmations: int = 1,
    timeout: float = 60.0,
    poll_interval: float = 1.0,
) -> VerifyPaymentResult:
    """Wait until a transaction reaches the requested confirmation count."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = get_transaction_status(tx_hash, chain)
        if status["status"] == "failed":
            return VerifyPaymentResult(verified=False, tx_hash=tx_hash, block_number=status.get("block_number"), error="Transaction failed")
        if status["status"] == "confirmed" and status.get("confirmations", 0) >= confirmations:
            return VerifyPaymentResult(
                verified=True,
                tx_hash=tx_hash,
                block_number=status.get("block_number"),
                confirmations=status.get("confirmations"),
            )
        time.sleep(max(0.05, poll_interval))
    return VerifyPaymentResult(verified=False, tx_hash=tx_hash, pending=True, error="Timeout waiting for transaction")
