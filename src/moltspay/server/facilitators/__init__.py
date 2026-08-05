"""
MoltsPay Server Facilitators

Facilitators handle payment verification and settlement for different blockchain networks.

Available Facilitators:
- CDPFacilitator: Base, Polygon (via Coinbase Developer Platform)
- BNBFacilitator: BNB Chain (pay-for-success with EIP-712 intents)
- TempoFacilitator: Tempo Moderato testnet (x402 + MPP protocol)
- SolanaFacilitator: Solana mainnet/devnet (SPL token transfers)

Usage:
    from moltspay.server.facilitators import FacilitatorRegistry
    
    registry = FacilitatorRegistry()
    result = await registry.verify(payment_payload, requirements)
"""

from .base import BaseFacilitator, VerifyResult, SettleResult, HealthCheckResult
from .cdp import CDPFacilitator
from .bnb import BNBFacilitator
from .tempo import TempoFacilitator
from .solana import SolanaFacilitator
from .registry import FacilitatorRegistry, NETWORK_TO_FACILITATOR

__all__ = [
    # Base
    "BaseFacilitator",
    "VerifyResult",
    "SettleResult",
    "HealthCheckResult",
    # Facilitators
    "CDPFacilitator",
    "BNBFacilitator",
    "TempoFacilitator",
    "SolanaFacilitator",
    # Registry
    "FacilitatorRegistry",
    "NETWORK_TO_FACILITATOR",
]
