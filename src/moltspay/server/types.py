"""Type definitions for MoltsPay Server."""

from typing import Any, Callable, Awaitable, Optional, List, Dict, Literal, Union
from dataclasses import dataclass, field
from pydantic import BaseModel


# Type alias for skill handlers
SkillHandler = Callable[[Dict[str, Any]], Awaitable[Any]]
SyncSkillHandler = Callable[[Dict[str, Any]], Any]


class ServiceInput(BaseModel):
    """Service input parameter definition."""
    type: str = "string"
    required: bool = False
    description: Optional[str] = None


class ServiceConfig(BaseModel):
    """Service configuration from moltspay.services.json."""
    id: str
    name: str
    description: Optional[str] = None
    price: float
    currency: str = "USDC"
    acceptedCurrencies: Optional[List[str]] = None
    function: str
    input: Dict[str, ServiceInput] = {}
    output: Dict[str, Any] = {}

    @property
    def accepted_currencies(self) -> List[str]:
        """Get list of accepted currencies."""
        return self.acceptedCurrencies or [self.currency]


class ChainConfig(BaseModel):
    """Chain configuration for multi-chain support."""
    chain: str  # "base", "polygon", "base_sepolia", "bnb", "tempo_moderato", etc.
    network: Optional[str] = None  # Auto-mapped from chain if not specified
    tokens: List[str] = ["USDC"]  # ["USDC", "USDT"]
    wallet: Optional[str] = None  # Optional per-chain wallet override


class ProviderConfig(BaseModel):
    """Provider configuration from moltspay.services.json."""
    name: str
    description: Optional[str] = None
    wallet: str
    solana_wallet: Optional[str] = None  # Solana wallet address
    chain: str = "base"  # deprecated, for backward compat
    chains: Optional[List[Any]] = None  # multi-chain support (strings or ChainConfig)
    
    def get_chains(self) -> List[ChainConfig]:
        """
        Get normalized chain configs.
        
        Supports both formats (like Node.js):
        - String: "base" -> ChainConfig(chain="base", tokens=["USDC"])
        - Object: {"chain": "base", "tokens": ["USDC", "USDT"]}
        """
        if not self.chains:
            return []
        
        result = []
        for c in self.chains:
            if isinstance(c, str):
                # String format: use defaults
                result.append(ChainConfig(chain=c, tokens=["USDC"]))
            elif isinstance(c, dict):
                # Object format
                result.append(ChainConfig(**c))
            elif isinstance(c, ChainConfig):
                result.append(c)
        return result


class ServicesManifest(BaseModel):
    """Full services manifest structure."""
    provider: ProviderConfig
    services: List[ServiceConfig]


@dataclass
class RegisteredSkill:
    """A registered skill with its handler."""
    id: str
    config: ServiceConfig
    handler: Union[SkillHandler, SyncSkillHandler]


@dataclass
class X402PaymentRequirements:
    """x402 payment requirements."""
    scheme: str
    network: str
    asset: str
    amount: str
    payTo: str
    maxTimeoutSeconds: int = 300
    extra: Dict[str, str] = field(default_factory=dict)


@dataclass
class X402PaymentPayload:
    """Parsed x402 payment payload."""
    x402Version: int
    payload: Dict[str, Any]
    accepted: Optional[Dict[str, Any]] = None
    resource: Optional[Dict[str, Any]] = None
    scheme: Optional[str] = None
    network: Optional[str] = None


@dataclass
class VerifyResult:
    """Result of payment verification."""
    valid: bool
    error: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    facilitator: Optional[str] = None


@dataclass
class SettleResult:
    """Result of payment settlement."""
    success: bool
    transaction: Optional[str] = None
    status: Optional[str] = None
    error: Optional[str] = None
    facilitator: Optional[str] = None


# Chain name to network ID mapping
CHAIN_TO_NETWORK: Dict[str, str] = {
    "base": "eip155:8453",
    "base_sepolia": "eip155:84532",
    "polygon": "eip155:137",
    "bnb": "eip155:56",
    "bnb_testnet": "eip155:97",
    "tempo_moderato": "eip155:42431",
    "solana": "solana:mainnet",
    "solana_devnet": "solana:devnet",
}

# Token contract addresses by network
TOKEN_ADDRESSES: Dict[str, Dict[str, str]] = {
    # Base mainnet
    "eip155:8453": {
        "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "USDT": "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2",
    },
    # Base Sepolia (testnet)
    "eip155:84532": {
        "USDC": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    },
    # Polygon mainnet
    "eip155:137": {
        "USDC": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
        "USDT": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
    },
    # BNB mainnet (18 decimals!)
    "eip155:56": {
        "USDC": "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
        "USDT": "0x55d398326f99059fF775485246999027B3197955",
    },
    # BNB testnet (18 decimals!)
    "eip155:97": {
        "USDC": "0x64544969ed7EBf5f083679233325356EbE738930",
        "USDT": "0x337610d27c682E347C9cD60BD4b3b107C9d34dDd",
    },
    # Tempo Moderato testnet
    "eip155:42431": {
        "USDC": "0x20c0000000000000000000000000000000000000",  # pathUSD
        "USDT": "0x20c0000000000000000000000000000000000001",  # alphaUSD
    },
    # Solana mainnet (SPL token mint addresses)
    "solana:mainnet": {
        "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # Circle USDC
    },
    # Solana devnet
    "solana:devnet": {
        "USDC": "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU",  # Devnet USDC
    },
}

# Token decimals by network (default is 6, BNB uses 18)
TOKEN_DECIMALS: Dict[str, int] = {
    "eip155:56": 18,   # BNB mainnet
    "eip155:97": 18,   # BNB testnet
}

# Solana chain configuration (separate from EVM)
SOLANA_CHAINS: Dict[str, Dict[str, Any]] = {
    "solana": {
        "name": "Solana Mainnet",
        "network": "solana:mainnet",
        "cluster": "mainnet-beta",
        "rpc": "https://api.mainnet-beta.solana.com",
        "tokens": {
            "USDC": {
                "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                "decimals": 6,
            }
        },
    },
    "solana_devnet": {
        "name": "Solana Devnet",
        "network": "solana:devnet",
        "cluster": "devnet",
        "rpc": "https://api.devnet.solana.com",
        "tokens": {
            "USDC": {
                "mint": "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU",
                "decimals": 6,
            }
        },
    },
}

# EIP-712 domain info for tokens - PER NETWORK (must match on-chain exactly)
TOKEN_DOMAINS: Dict[str, Dict[str, Dict[str, str]]] = {
    # Base mainnet
    "eip155:8453": {
        "USDC": {"name": "USD Coin", "version": "2"},
        "USDT": {"name": "Tether USD", "version": "2"},
    },
    # Base Sepolia testnet - USDC uses 'USDC' not 'USD Coin'
    "eip155:84532": {
        "USDC": {"name": "USDC", "version": "2"},
    },
    # Polygon mainnet
    "eip155:137": {
        "USDC": {"name": "USD Coin", "version": "2"},
        "USDT": {"name": "(PoS) Tether USD", "version": "2"},
    },
    # Tempo Moderato testnet - TIP-20 stablecoins
    "eip155:42431": {
        "USDC": {"name": "pathUSD", "version": "1"},
        "USDT": {"name": "alphaUSD", "version": "1"},
    },
    # BNB Smart Chain mainnet
    "eip155:56": {
        "USDC": {"name": "USD Coin", "version": "1"},
        "USDT": {"name": "Tether USD", "version": "1"},
    },
    # BNB Smart Chain testnet
    "eip155:97": {
        "USDC": {"name": "USD Coin", "version": "1"},
        "USDT": {"name": "Tether USD", "version": "1"},
    },
}


def get_token_domain(network: str, token: str) -> Dict[str, str]:
    """Get EIP-712 domain for a token on a specific network."""
    network_domains = TOKEN_DOMAINS.get(network, TOKEN_DOMAINS.get("eip155:8453", {}))
    return network_domains.get(token, {"name": "USD Coin", "version": "2"})

# x402 protocol version
X402_VERSION = 2
