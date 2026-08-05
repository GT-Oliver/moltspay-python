"""Pydantic models for MoltsPay."""

from typing import Optional, Any, List, Literal
from pydantic import BaseModel

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
