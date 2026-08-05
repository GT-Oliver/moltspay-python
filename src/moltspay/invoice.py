"""Invoice creation and payment verification helper."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlencode

from .chains import CHAINS
from .models import Invoice, VerifyPaymentResult
from .verify import verify_payment


class PaymentAgent:
    def __init__(self, chain: str = "base", wallet_address: Optional[str] = None, private_key: Optional[str] = None):
        self.chain = chain
        self.config = CHAINS[chain]
        if wallet_address:
            self.address = wallet_address
        elif private_key:
            from eth_account import Account
            self.address = Account.from_key(private_key).address
        else:
            raise ValueError("wallet_address or private_key is required")

    def create_invoice(
        self,
        order_id: str,
        amount: float,
        service: str,
        description: Optional[str] = None,
        expires_minutes: int = 30,
        metadata: Optional[Dict[str, Any]] = None,
        token: str = "USDC",
    ) -> Invoice:
        expires = datetime.now(timezone.utc) + timedelta(minutes=expires_minutes)
        query = urlencode({"to": self.address, "amount": amount, "token": token, "chain": self.chain, "order_id": order_id})
        return Invoice(
            order_id=order_id, amount=str(amount), service=service, description=description,
            token=token, chain=self.chain, chain_id=self.config["chainId"], recipient=self.address,
            expires_at=expires.isoformat(), deep_link=f"moltspay://pay?{query}",
            explorer_url=self.config["explorer"], metadata=metadata or {},
        )

    def verify_payment(self, tx_hash: str, expected_amount: float, token: str = "USDC") -> VerifyPaymentResult:
        return verify_payment(tx_hash, expected_amount, self.address, self.chain, token)
