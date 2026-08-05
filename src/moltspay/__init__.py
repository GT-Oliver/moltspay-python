"""
MoltsPay Python SDK - Agent-to-Agent Payments

Usage:
    from moltspay import MoltsPay
    
    client = MoltsPay()  # Auto-creates wallet if not exists
    result = client.pay("https://juai8.com/zen7", "text-to-video", prompt="a cat")
"""

from .client import MoltsPay, AsyncMoltsPay
from .wallet import Wallet, create_wallet, load_wallet
from .models import Service, Balance, Limits, PaymentResult, FundingResult, FaucetResult
from .exceptions import (
    MoltsPayError,
    PaymentError,
    InsufficientFunds,
    LimitExceeded,
    WalletError,
)

__version__ = "0.6.1"
__all__ = [
    "MoltsPay",
    "AsyncMoltsPay",
    "Wallet",
    "create_wallet",
    "load_wallet",
    "Service",
    "Balance",
    "Limits",
    "PaymentResult",
    "FundingResult",
    "FaucetResult",
    "MoltsPayError",
    "PaymentError",
    "InsufficientFunds",
    "LimitExceeded",
    "WalletError",
]
