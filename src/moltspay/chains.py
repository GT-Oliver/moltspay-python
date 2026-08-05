"""Chain configurations for MoltsPay multi-chain support."""

from typing import TypedDict, Optional, Dict

class TokenConfig(TypedDict):
    address: str
    decimals: int

class ChainConfig(TypedDict):
    name: str
    chainId: int
    rpc: str
    explorer: str
    type: str  # 'evm', 'tempo', 'solana'
    protocol: str  # 'x402', 'mpp', 'solana'
    tokens: Dict[str, TokenConfig]

# EVM Chain Configurations
CHAINS: Dict[str, ChainConfig] = {
    # === Mainnet ===
    "base": {
        "name": "Base",
        "chainId": 8453,
        "rpc": "https://mainnet.base.org",
        "explorer": "https://basescan.org",
        "type": "evm",
        "protocol": "x402",
        "tokens": {
            "USDC": {"address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6},
        }
    },
    "polygon": {
        "name": "Polygon",
        "chainId": 137,
        "rpc": "https://polygon-rpc.com",
        "explorer": "https://polygonscan.com",
        "type": "evm",
        "protocol": "x402",
        "tokens": {
            "USDC": {"address": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", "decimals": 6},
        }
    },
    
    # === Testnets ===
    "base_sepolia": {
        "name": "Base Sepolia",
        "chainId": 84532,
        "rpc": "https://sepolia.base.org",
        "explorer": "https://sepolia.basescan.org",
        "type": "evm",
        "protocol": "x402",
        "tokens": {
            "USDC": {"address": "0x036CbD53842c5426634e7929541eC2318f3dCF7e", "decimals": 6},
        }
    },
    
    # === Tempo (MPP Protocol) ===
    "tempo_moderato": {
        "name": "Tempo Moderato",
        "chainId": 42431,
        "rpc": "https://rpc.moderato.tempo.xyz",
        "explorer": "https://explore.testnet.tempo.xyz",
        "type": "tempo",
        "protocol": "mpp",
        "tokens": {
            "pathUSD": {"address": "0x20c0000000000000000000000000000000000000", "decimals": 6},
            "alphaUSD": {"address": "0x20c0000000000000000000000000000000000001", "decimals": 6},
            "betaUSD": {"address": "0x20c0000000000000000000000000000000000002", "decimals": 6},
            "thetaUSD": {"address": "0x20c0000000000000000000000000000000000003", "decimals": 6},
        }
    },
    
    # === BNB Chain ===
    "bnb": {
        "name": "BNB Chain",
        "chainId": 56,
        "rpc": "https://bsc-dataseed.binance.org",
        "explorer": "https://bscscan.com",
        "type": "evm",
        "protocol": "bnb",
        "tokens": {
            "USDT": {"address": "0x55d398326f99059fF775485246999027B3197955", "decimals": 18},
            "USDC": {"address": "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d", "decimals": 18},
        }
    },
    "bnb_testnet": {
        "name": "BNB Testnet",
        "chainId": 97,
        "rpc": "https://data-seed-prebsc-1-s1.binance.org:8545",
        "explorer": "https://testnet.bscscan.com",
        "type": "evm",
        "protocol": "bnb",
        "tokens": {
            "USDC": {"address": "0x64544969ed7EBf5f083679233325356EbE738930", "decimals": 18},
            "USDT": {"address": "0x337610d27c682E347C9cD60BD4b3b107C9d34dDd", "decimals": 18},
        }
    },
    
    # === Solana ===
    "solana": {
        "name": "Solana",
        "chainId": 0,  # Not used for Solana
        "rpc": "https://api.mainnet-beta.solana.com",
        "explorer": "https://solscan.io",
        "type": "solana",
        "protocol": "solana",
        "tokens": {
            "USDC": {"address": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "decimals": 6},
        }
    },
    "solana_devnet": {
        "name": "Solana Devnet",
        "chainId": 0,
        "rpc": "https://api.devnet.solana.com",
        "explorer": "https://solscan.io",
        "type": "solana",
        "protocol": "solana",
        "tokens": {
            "USDC": {"address": "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU", "decimals": 6},
        }
    },
}

# Chain ID to name mapping
CHAIN_IDS = {chain["chainId"]: name for name, chain in CHAINS.items() if chain["chainId"] > 0}

def get_chain(name: str) -> Optional[ChainConfig]:
    """Get chain configuration by name."""
    return CHAINS.get(name)

def get_chain_by_id(chain_id: int) -> Optional[str]:
    """Get chain name by chain ID."""
    return CHAIN_IDS.get(chain_id)

def is_testnet(chain: str) -> bool:
    """Check if chain is a testnet."""
    return chain in ("base_sepolia", "bnb_testnet", "solana_devnet", "tempo_moderato")

def get_protocol(chain: str) -> str:
    """Get protocol for chain (x402, mpp, bnb, solana)."""
    config = CHAINS.get(chain)
    return config["protocol"] if config else "x402"
