"""EIP-2612 permit and allowance wallet helpers."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Dict, Optional

from .chains import CHAINS
from .models import TransferResult
from .wallet import Wallet


PERMIT_ABI = [
    {"name": "permit", "type": "function", "stateMutability": "nonpayable", "inputs": [
        {"name": "owner", "type": "address"}, {"name": "spender", "type": "address"},
        {"name": "value", "type": "uint256"}, {"name": "deadline", "type": "uint256"},
        {"name": "v", "type": "uint8"}, {"name": "r", "type": "bytes32"}, {"name": "s", "type": "bytes32"},
    ], "outputs": []},
    {"name": "transferFrom", "type": "function", "stateMutability": "nonpayable", "inputs": [
        {"name": "from", "type": "address"}, {"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"},
    ], "outputs": [{"name": "", "type": "bool"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view", "inputs": [
        {"name": "owner", "type": "address"}, {"name": "spender", "type": "address"},
    ], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view", "inputs": [{"name": "owner", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "nonces", "type": "function", "stateMutability": "view", "inputs": [{"name": "owner", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
]


class PermitWallet:
    def __init__(self, private_key: Optional[str] = None, wallet_path: Optional[str] = None, password: Optional[str] = None, chain: str = "base", rpc_url: Optional[str] = None):
        from web3 import Web3
        self.chain = chain
        self.config = CHAINS[chain]
        self.wallet = Wallet(wallet_path=wallet_path, private_key=private_key, password=password, chain=chain)
        self.address = self.wallet.address
        self.w3 = Web3(Web3.HTTPProvider(rpc_url or self.config["rpc"]))
        token = self.config.get("tokens", {}).get("USDC")
        if not token:
            raise ValueError(f"USDC is not configured on {chain}")
        self.decimals = int(token["decimals"])
        self.contract = self.w3.eth.contract(address=Web3.to_checksum_address(token["address"]), abi=PERMIT_ABI)

    def check_permit_allowance(self, owner: str) -> str:
        raw = self.contract.functions.allowance(owner, self.address).call()
        return f"{Decimal(raw) / (Decimal(10) ** self.decimals):.{self.decimals}f}"

    def _execute(self, function) -> tuple[str, Any]:
        tx = function.build_transaction({
            "from": self.address, "nonce": self.w3.eth.get_transaction_count(self.address),
            "chainId": self.config["chainId"], "gasPrice": self.w3.eth.gas_price,
        })
        tx.setdefault("gas", self.w3.eth.estimate_gas(tx))
        signed = self.wallet._account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return tx_hash.hex(), self.w3.eth.wait_for_transaction_receipt(tx_hash)

    def transfer_with_permit(self, to: str, amount: float, permit: Dict[str, Any]) -> TransferResult:
        try:
            owner = self.w3.to_checksum_address(permit["owner"])
            recipient = self.w3.to_checksum_address(to)
            if self.w3.to_checksum_address(permit["spender"]).lower() != self.address.lower():
                raise ValueError("Permit spender does not match this wallet")
            if int(permit["deadline"]) < int(time.time()):
                raise ValueError("Permit has expired")
            atomic = int(Decimal(str(amount)) * (Decimal(10) ** self.decimals))
            if atomic <= 0 or atomic > int(permit["value"]):
                raise ValueError("Permit value is below transfer amount")
            permit_hash = None
            allowance = self.contract.functions.allowance(owner, self.address).call()
            if allowance < atomic:
                permit_hash, receipt = self._execute(self.contract.functions.permit(
                    owner, self.address, int(permit["value"]), int(permit["deadline"]),
                    int(permit["v"]), permit["r"], permit["s"],
                ))
                if int(receipt["status"]) != 1:
                    raise RuntimeError("Permit transaction failed")
            transfer_hash, receipt = self._execute(self.contract.functions.transferFrom(owner, recipient, atomic))
            return TransferResult(
                success=int(receipt["status"]) == 1, tx_hash=transfer_hash,
                permit_tx_hash=permit_hash, transfer_tx_hash=transfer_hash,
                from_address=owner, to_address=recipient, amount=amount,
                token="USDC", chain=self.chain, gas_used=int(receipt["gasUsed"]),
                block_number=int(receipt["blockNumber"]),
                explorer_url=f"{self.config['explorer']}/tx/{transfer_hash}",
            )
        except Exception as exc:
            return TransferResult(success=False, to_address=to, amount=amount, token="USDC", chain=self.chain, error=str(exc))

    def get_gas_balance(self) -> str:
        return str(self.w3.from_wei(self.w3.eth.get_balance(self.address), "ether"))

    def has_enough_gas(self, minimum: float = 0.001) -> bool:
        return Decimal(self.get_gas_balance()) >= Decimal(str(minimum))


class AllowanceWallet(PermitWallet):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.permits: Dict[str, Dict[str, Any]] = {}

    def store_permit(self, permit: Dict[str, Any]) -> None:
        self.permits[permit["owner"].lower()] = dict(permit)

    def get_permit(self, owner: str) -> Optional[Dict[str, Any]]:
        return self.permits.get(owner.lower())

    def check_allowance(self, owner: str) -> Dict[str, Any]:
        owner_address = self.w3.to_checksum_address(owner)
        allowance = self.contract.functions.allowance(owner_address, self.address).call()
        balance = self.contract.functions.balanceOf(owner_address).call()
        gas = self.get_gas_balance()
        scale = Decimal(10) ** self.decimals
        return {
            "owner": owner_address, "agent": self.address,
            "allowance": str(Decimal(allowance) / scale),
            "owner_balance": str(Decimal(balance) / scale),
            "agent_gas_balance": gas,
            "can_spend": allowance > 0 and Decimal(gas) >= Decimal("0.0001"),
            "chain": self.chain,
        }

    def spend(self, to: str, amount: float, permit: Optional[Dict[str, Any]] = None) -> TransferResult:
        selected = permit
        if selected:
            self.store_permit(selected)
        if selected is None:
            atomic = int(Decimal(str(amount)) * (Decimal(10) ** self.decimals))
            for owner, candidate in self.permits.items():
                if self.contract.functions.allowance(owner, self.address).call() >= atomic:
                    selected = candidate
                    break
        if selected is None:
            return TransferResult(success=False, to_address=to, amount=amount, token="USDC", chain=self.chain, error="No valid permit found")
        result = self.transfer_with_permit(to, amount, selected)
        if result.success:
            owner = selected["owner"]
            remaining = self.contract.functions.allowance(owner, self.address).call()
            result.remaining_allowance = str(Decimal(remaining) / (Decimal(10) ** self.decimals))
        return result


def generate_permit_instructions(owner: str, agent: str, amount: float, chain: str = "base", deadline_hours: int = 24) -> Dict[str, Any]:
    config = CHAINS[chain]
    token = config["tokens"]["USDC"]
    deadline = int(time.time()) + deadline_hours * 3600
    value = str(int(Decimal(str(amount)) * (Decimal(10) ** int(token["decimals"]))))
    typed_data = {
        "types": {"Permit": [
            {"name": "owner", "type": "address"}, {"name": "spender", "type": "address"},
            {"name": "value", "type": "uint256"}, {"name": "nonce", "type": "uint256"},
            {"name": "deadline", "type": "uint256"},
        ]},
        "primaryType": "Permit",
        "domain": {"name": "USD Coin", "version": "2", "chainId": config["chainId"], "verifyingContract": token["address"]},
        "message": {"owner": owner, "spender": agent, "value": value, "nonce": "<QUERY_CONTRACT>", "deadline": deadline},
    }
    return {"typed_data": typed_data, "deadline": deadline, "value": value}
