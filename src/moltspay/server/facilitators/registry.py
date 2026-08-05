"""
Facilitator Registry - Routes payments to the correct facilitator based on network.

Network to Facilitator mapping:
- eip155:8453, eip155:137, eip155:84532 → CDP (Base, Polygon)
- eip155:56, eip155:97 → BNB (BNB Chain)
- eip155:42431 → Tempo (Tempo Moderato testnet)
- solana:mainnet, solana:devnet → Solana
"""

from typing import Dict, Optional, List, Any

from .base import BaseFacilitator, VerifyResult, SettleResult
from .cdp import CDPFacilitator
from .bnb import BNBFacilitator
from .tempo import TempoFacilitator
from .solana import SolanaFacilitator


# Network to facilitator name mapping
NETWORK_TO_FACILITATOR: Dict[str, str] = {
    # CDP - Base and Polygon
    "eip155:8453": "cdp",     # Base mainnet
    "eip155:137": "cdp",      # Polygon mainnet
    "eip155:84532": "cdp",    # Base Sepolia
    
    # BNB
    "eip155:56": "bnb",       # BNB mainnet
    "eip155:97": "bnb",       # BNB testnet
    
    # Tempo
    "eip155:42431": "tempo",  # Tempo Moderato testnet
    
    # Solana
    "solana:mainnet": "solana",
    "solana:devnet": "solana",
}


class FacilitatorRegistry:
    """
    Central registry for payment facilitators.
    
    Manages facilitator instances and routes payments based on network.
    """
    
    def __init__(self):
        """Initialize registry with all facilitators."""
        self._facilitators: Dict[str, BaseFacilitator] = {}
        self._init_facilitators()
    
    def _init_facilitators(self):
        """Initialize all facilitator instances."""
        # CDP - for Base and Polygon
        try:
            self._facilitators["cdp"] = CDPFacilitator()
            print("[Registry] CDP facilitator initialized")
        except Exception as e:
            print(f"[Registry] Failed to init CDP facilitator: {e}")
        
        # BNB - for BNB Chain
        try:
            self._facilitators["bnb"] = BNBFacilitator()
            print("[Registry] BNB facilitator initialized")
        except Exception as e:
            print(f"[Registry] Failed to init BNB facilitator: {e}")
        
        # Tempo - for Tempo testnet
        try:
            self._facilitators["tempo"] = TempoFacilitator()
            print("[Registry] Tempo facilitator initialized")
        except Exception as e:
            print(f"[Registry] Failed to init Tempo facilitator: {e}")
        
        # Solana
        try:
            self._facilitators["solana"] = SolanaFacilitator()
            print("[Registry] Solana facilitator initialized")
        except Exception as e:
            print(f"[Registry] Failed to init Solana facilitator: {e}")
    
    def get(self, name: str) -> Optional[BaseFacilitator]:
        """Get facilitator by name."""
        return self._facilitators.get(name)
    
    def get_for_network(self, network: str) -> Optional[BaseFacilitator]:
        """Get facilitator for a specific network."""
        facilitator_name = NETWORK_TO_FACILITATOR.get(network)
        if not facilitator_name:
            return None
        return self._facilitators.get(facilitator_name)
    
    def list_facilitators(self) -> List[str]:
        """List all registered facilitator names."""
        return list(self._facilitators.keys())
    
    def list_supported_networks(self) -> List[str]:
        """List all supported networks."""
        networks = []
        for facilitator in self._facilitators.values():
            networks.extend(facilitator.supported_networks)
        return list(set(networks))
    
    def supports_network(self, network: str) -> bool:
        """Check if any facilitator supports the given network."""
        return network in NETWORK_TO_FACILITATOR
    
    async def verify(
        self,
        payment_payload: Dict[str, Any],
        requirements: Dict[str, Any],
    ) -> VerifyResult:
        """
        Verify payment using the appropriate facilitator.
        
        Automatically selects facilitator based on network in payload/requirements.
        """
        # Determine network
        network = (
            payment_payload.get("accepted", {}).get("network") or
            payment_payload.get("network") or
            requirements.get("network")
        )
        
        if not network:
            return VerifyResult(valid=False, error="No network specified in payload or requirements")
        
        facilitator = self.get_for_network(network)
        if not facilitator:
            supported = ", ".join(self.list_supported_networks())
            return VerifyResult(valid=False, error=f"Network {network} not supported. Supported: {supported}")
        
        return await facilitator.verify(payment_payload, requirements)
    
    async def settle(
        self,
        payment_payload: Dict[str, Any],
        requirements: Dict[str, Any],
    ) -> SettleResult:
        """
        Settle payment using the appropriate facilitator.
        """
        # Determine network
        network = (
            payment_payload.get("accepted", {}).get("network") or
            payment_payload.get("network") or
            requirements.get("network")
        )
        
        if not network:
            return SettleResult(success=False, error="No network specified in payload or requirements")
        
        facilitator = self.get_for_network(network)
        if not facilitator:
            return SettleResult(success=False, error=f"Network {network} not supported")
        
        return await facilitator.settle(payment_payload, requirements)
    
    # ========== Facilitator-specific getters ==========
    
    def get_bnb_spender_address(self) -> Optional[str]:
        """Get BNB facilitator spender address."""
        bnb = self._facilitators.get("bnb")
        if isinstance(bnb, BNBFacilitator):
            return bnb.get_spender_address()
        return None
    
    def get_solana_fee_payer(self) -> Optional[str]:
        """Get Solana facilitator fee payer pubkey."""
        solana = self._facilitators.get("solana")
        if isinstance(solana, SolanaFacilitator):
            return solana.get_fee_payer_pubkey()
        return None
    
    def get_tempo_facilitator(self) -> Optional[TempoFacilitator]:
        """Get Tempo facilitator for MPP support."""
        tempo = self._facilitators.get("tempo")
        if isinstance(tempo, TempoFacilitator):
            return tempo
        return None
