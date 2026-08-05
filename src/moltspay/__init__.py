"""
MoltsPay Python SDK - Agent-to-Agent Payments

Usage:
    from moltspay import MoltsPay
    
    client = MoltsPay()  # Auto-creates wallet if not exists
    result = client.pay("https://juai8.com/zen7", "text-to-video", prompt="a cat")
"""

from .client import MoltsPay, AsyncMoltsPay
from .wallet import Wallet, create_wallet, load_wallet
from .models import (
    Service, Balance, Limits, PaymentResult, FundingResult, FaucetResult,
    TransferResult, VerifyPaymentResult, BuyerBalance, ProviderInfo, ServicesResponse,
    SecurityLimits, PendingTransfer, Invoice, BalanceTopupSession,
)
from .exceptions import (
    MoltsPayError,
    PaymentError,
    InsufficientFunds,
    LimitExceeded,
    WalletError,
    UnsupportedRail,
)
from .verify import verify_payment, get_transaction_status, wait_for_transaction
from .secure_wallet import SecureWallet
from .audit import AuditLog
from .invoice import PaymentAgent
from .permit_wallet import PermitWallet, AllowanceWallet, generate_permit_instructions
from .balance import BalanceClient, BalanceLedger, to_sat, from_sat
from .wechat import WechatClient, WechatPaymentSession
from .alipay import AlipayClient
from .chains import get_chain, get_chain_by_id, list_chains, get_chain_family, is_evm_chain, is_solana_chain

__version__ = "2.4.0"
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
    "TransferResult",
    "VerifyPaymentResult",
    "BuyerBalance",
    "ProviderInfo",
    "ServicesResponse",
    "SecurityLimits",
    "PendingTransfer",
    "Invoice",
    "BalanceTopupSession",
    "MoltsPayError",
    "PaymentError",
    "InsufficientFunds",
    "LimitExceeded",
    "WalletError",
    "UnsupportedRail",
    "verify_payment",
    "get_transaction_status",
    "wait_for_transaction",
    "SecureWallet",
    "AuditLog",
    "PaymentAgent",
    "PermitWallet",
    "AllowanceWallet",
    "generate_permit_instructions",
    "BalanceClient",
    "BalanceLedger",
    "to_sat",
    "from_sat",
    "WechatClient",
    "WechatPaymentSession",
    "AlipayClient",
    "get_chain",
    "get_chain_by_id",
    "list_chains",
    "get_chain_family",
    "is_evm_chain",
    "is_solana_chain",
]
