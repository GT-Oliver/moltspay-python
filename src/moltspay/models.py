"""Pydantic models for MoltsPay."""

from typing import Optional, Any, List, Literal, Dict
from pydantic import BaseModel, ConfigDict, Field

# Supported token types
TokenSymbol = Literal["USDC", "USDT"]


class Service(BaseModel):
    """A service offered by a provider."""
    id: str
    name: Optional[str] = None
    description: Optional[str] = None
    price: float
    currency: str = "USDC"
    accepted_currencies: Optional[List[str]] = None  # ["USDC", "USDT"]
    chains: Optional[List[str]] = None  # ["base", "polygon", "base_sepolia"]
    parameters: Optional[dict] = None
    input: Dict[str, Any] = Field(default_factory=dict)
    output: Dict[str, Any] = Field(default_factory=dict)
    available: bool = True
    provider: Optional[Dict[str, Any]] = None
    endpoint: Optional[str] = None
    payment_rails: Dict[str, Any] = Field(default_factory=dict, alias="paymentRails")

    model_config = ConfigDict(populate_by_name=True, extra="allow")
    
    @property
    def accepts(self) -> List[str]:
        """Get list of accepted currencies (defaults to [currency])."""
        return self.accepted_currencies or [self.currency]


class Balance(BaseModel):
    """Wallet balance."""
    address: str
    usdc: float
    usdt: float = 0.0
    eth: float
    chain: str = "base"

    @property
    def native(self) -> float:
        """Node-compatible name for the chain's native-token balance."""
        return self.eth


class Limits(BaseModel):
    """Spending limits."""
    max_per_tx: float
    max_per_day: float
    spent_today: float = 0.0
    
    @property
    def remaining_daily(self) -> float:
        return max(0, self.max_per_day - self.spent_today)


class PaymentResult(BaseModel):
    """Result of a payment."""
    success: bool
    tx_hash: Optional[str] = None
    amount: float
    token: str = "USDC"  # Token used for payment (USDC or USDT)
    service_id: str
    result: Optional[Any] = None
    error: Optional[str] = None
    explorer_url: Optional[str] = None
    network: Optional[str] = None
    facilitator: Optional[str] = None
    payment: Optional[Dict[str, Any]] = None


class TransferResult(BaseModel):
    """Result of an ERC-20 or native-token transfer."""
    success: bool
    tx_hash: Optional[str] = None
    from_address: Optional[str] = None
    to_address: Optional[str] = None
    amount: Optional[float] = None
    token: str = "USDC"
    chain: str = "base"
    gas_used: Optional[int] = None
    block_number: Optional[int] = None
    explorer_url: Optional[str] = None
    error: Optional[str] = None
    permit_tx_hash: Optional[str] = None
    transfer_tx_hash: Optional[str] = None
    remaining_allowance: Optional[str] = None


class VerifyPaymentResult(BaseModel):
    """Normalized on-chain payment verification result."""
    verified: bool
    tx_hash: Optional[str] = None
    amount: Optional[str] = None
    token: Optional[str] = None
    sender: Optional[str] = None
    recipient: Optional[str] = None
    block_number: Optional[int] = None
    confirmations: Optional[int] = None
    explorer_url: Optional[str] = None
    pending: bool = False
    error: Optional[str] = None


class BuyerBalance(BaseModel):
    """Balance-rail account snapshot."""
    buyer_id: str
    currency: str = "USD"
    balance: str = "0.00"
    spent_today: str = "0.00"
    single_limit: Optional[str] = None
    daily_limit: Optional[str] = None
    status: str = "active"
    topup_packs: List[str] = Field(default_factory=list)
    custom_topup_max: Optional[str] = None


class BalanceTopupSession(BaseModel):
    """Recoverable balance top-up order, compatible with Node's session file."""
    out_trade_no: str
    buyer_id: str
    pack: str
    server_url: str
    code_url: str = ""
    rail: Literal["wechat"] = "wechat"
    status: Literal["pending", "credited", "expired"] = "pending"
    created_at: str
    expires_at: str
    context: Dict[str, Any] = Field(default_factory=dict)
    tx_id: Optional[str] = None
    balance: Optional[str] = None


class ProviderInfo(BaseModel):
    name: str
    username: Optional[str] = None
    description: Optional[str] = None
    wallet: Optional[str] = None
    chain: Optional[str] = None
    chains: Optional[List[Any]] = None


class ServicesResponse(BaseModel):
    provider: Optional[ProviderInfo] = None
    services: List[Service] = Field(default_factory=list)


class SecurityLimits(BaseModel):
    single_max: float = 10.0
    daily_max: float = 100.0
    require_whitelist: bool = False


class PendingTransfer(BaseModel):
    id: str
    to: str
    amount: float
    token: str = "USDC"
    reason: Optional[str] = None
    requester: Optional[str] = None
    created_at: str
    status: Literal["pending", "approved", "rejected", "executed"] = "pending"


class Invoice(BaseModel):
    type: str = "payment_request"
    version: str = "1.0"
    order_id: str
    service: str
    description: Optional[str] = None
    amount: str
    token: str = "USDC"
    chain: str
    chain_id: int
    recipient: str
    memo: Optional[str] = None
    expires_at: str
    deep_link: Optional[str] = None
    explorer_url: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class FundingResult(BaseModel):
    """Result of a funding request."""
    success: bool
    url: Optional[str] = None
    amount: float
    chain: str = "base"
    expires_in: int = 300  # seconds
    error: Optional[str] = None


class WalletData(BaseModel):
    """Wallet file format (compatible with Node.js CLI)."""
    address: str
    privateKey: str
    chain: str = "base"
    encrypted: bool = False
    iv: Optional[str] = None
    salt: Optional[str] = None
    label: Optional[str] = None
    createdAt: Optional[Any] = None  # Can be int (timestamp) or str
    limits: Optional[dict] = None
    spending: Optional[dict] = None


class FaucetResult(BaseModel):
    """Result of a testnet faucet request."""
    success: bool
    amount: float = 1.0
    chain: str = "base_sepolia"
    tx_hash: Optional[str] = None
    error: Optional[str] = None
