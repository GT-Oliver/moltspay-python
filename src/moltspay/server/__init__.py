"""
MoltsPay Server - Accept x402 payments for your AI services.

Usage:
    # CLI
    moltspay-server ./my_skill --port 8402
    
    # Or with multiple skills
    moltspay-server ./video_gen ./transcription ./image_gen
    
    # Programmatic
    from moltspay.server import MoltsPayServer
    
    server = MoltsPayServer("./my_skill")
    server.listen(8402)

Skill Structure:
    my_skill/
    ├── moltspay.services.json    # Service definitions (includes chain config)
    └── __init__.py               # Python functions

Example moltspay.services.json:
    {
      "provider": {
        "name": "My AI Service",
        "wallet": "0xYourWalletAddress",
        "chains": [
          {"chain": "base", "tokens": ["USDC", "USDT"]},
          {"chain": "polygon", "tokens": ["USDC"]},
          {"chain": "base_sepolia", "tokens": ["USDC"]}
        ]
      },
      "services": [{
        "id": "my-service",
        "name": "My Service",
        "price": 0.99,
        "currency": "USDC",
        "function": "my_function",
        "input": {"prompt": {"type": "string", "required": true}}
      }]
    }

Example __init__.py:
    async def my_function(params):
        prompt = params.get("prompt")
        # Do work...
        return {"result": "..."}

Environment Variables (in ~/.moltspay/.env):
    CDP_API_KEY_ID=xxx            # CDP API Key ID
    CDP_API_KEY_SECRET=xxx        # CDP API Key Secret
"""

from .server import MoltsPayServer
from .facilitators import (
    FacilitatorRegistry,
    CDPFacilitator,
    BNBFacilitator,
    TempoFacilitator,
    SolanaFacilitator,
)
from .types import (
    ServicesManifest,
    ServiceConfig,
    ProviderConfig,
    ChainConfig,
    RegisteredSkill,
    X402PaymentPayload,
    X402PaymentRequirements,
    CHAIN_TO_NETWORK,
    TOKEN_ADDRESSES,
    SOLANA_CHAINS,
)

__all__ = [
    "MoltsPayServer",
    "FacilitatorRegistry",
    "CDPFacilitator",
    "BNBFacilitator",
    "TempoFacilitator",
    "SolanaFacilitator",
    "ServicesManifest",
    "ServiceConfig",
    "ProviderConfig",
    "ChainConfig",
    "RegisteredSkill",
    "X402PaymentPayload",
    "X402PaymentRequirements",
    "CHAIN_TO_NETWORK",
    "TOKEN_ADDRESSES",
    "SOLANA_CHAINS",
]
