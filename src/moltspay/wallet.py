"""Wallet management - create, load, sign."""

import os
import json
import hashlib
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple, Union
from decimal import Decimal, InvalidOperation

from eth_account import Account
from eth_account.messages import encode_typed_data

from .models import WalletData, Limits, TransferResult
from .exceptions import WalletError

DEFAULT_WALLET_PATH = Path.home() / ".moltspay" / "wallet.json"

# Chain configs (wallet works on all EVM chains with same address)
CHAINS = {
    "base": {
        "chain_id": 8453,
        "usdc": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "usdt": "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2",
        "rpc": "https://mainnet.base.org",
        "explorer": "https://basescan.org/tx/",
    },
    "polygon": {
        "chain_id": 137,
        "usdc": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
        "usdt": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
        "rpc": "https://polygon-bor-rpc.publicnode.com",
        "explorer": "https://polygonscan.com/tx/",
    },
    "base_sepolia": {
        "chain_id": 84532,
        "usdc": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
        "usdt": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",  # Same as USDC on testnet
        "rpc": "https://sepolia.base.org",
        "explorer": "https://sepolia.basescan.org/tx/",
        "testnet": True,
    },
}


class Wallet:
    """MoltsPay wallet - load/create and sign permits."""
    
    def __init__(
        self,
        wallet_path: Optional[str] = None,
        private_key: Optional[str] = None,
        chain: str = "base",
        password: Optional[str] = None,
        label: Optional[str] = None,
    ):
        self.chain = chain
        self._password = password
        self._label = label
        self.chain_config = CHAINS.get(chain, CHAINS["base"])
        self._wallet_path = Path(wallet_path).expanduser() if wallet_path else DEFAULT_WALLET_PATH
        self._limits = Limits(max_per_tx=10, max_per_day=100)
        self._spent_today = 0.0
        self._today = datetime.now().strftime("%Y-%m-%d")
        
        if private_key:
            self._account = Account.from_key(private_key)
            self.address = self._account.address
        else:
            self._load_or_create()
    
    def _load_or_create(self):
        """Load existing wallet or create new one."""
        if self._wallet_path.exists():
            self._load_wallet()
        else:
            self._create_wallet()
    
    def _load_wallet(self):
        """Load wallet from file."""
        try:
            with open(self._wallet_path, "r") as f:
                data = json.load(f)
            
            wallet_data = WalletData(**data)
            
            if wallet_data.encrypted:
                if not self._password:
                    raise WalletError("Wallet is encrypted. Password required.")
                if not wallet_data.iv or not wallet_data.salt:
                    raise WalletError("Encrypted wallet is missing iv or salt")
                private_key = _decrypt_private_key(
                    wallet_data.privateKey, self._password, wallet_data.iv, wallet_data.salt
                )
            else:
                private_key = wallet_data.privateKey
            
            self._account = Account.from_key(private_key)
            self.address = self._account.address
            
            # Load limits if present
            if wallet_data.limits:
                self._limits = Limits(
                    max_per_tx=wallet_data.limits.get("maxPerTx", 10),
                    max_per_day=wallet_data.limits.get("maxPerDay", 100),
                )
            
            # Load spending if present and same day
            if wallet_data.spending:
                if wallet_data.spending.get("today") == self._today:
                    self._spent_today = wallet_data.spending.get("amount", 0)
                    
        except json.JSONDecodeError as e:
            raise WalletError(f"Invalid wallet file: {e}")
        except Exception as e:
            raise WalletError(f"Failed to load wallet: {e}")
    
    def _create_wallet(self):
        """Create new wallet and save to file."""
        # Generate new account
        self._account = Account.create()
        self.address = self._account.address
        
        # Prepare wallet data
        stored_private_key = self._account.key.hex()
        encrypted = False
        iv = None
        salt = None
        if self._password:
            stored_private_key, iv, salt = _encrypt_private_key(stored_private_key, self._password)
            encrypted = True
        wallet_data = WalletData(
            address=self.address,
            privateKey=stored_private_key,
            chain=self.chain,
            encrypted=encrypted,
            iv=iv,
            salt=salt,
            label=self._label,
            createdAt=datetime.now().isoformat(),
            limits={"maxPerTx": 10, "maxPerDay": 100},
            spending={"today": self._today, "amount": 0},
        )
        
        # Ensure directory exists
        self._wallet_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Save with restricted permissions
        with open(self._wallet_path, "w") as f:
            json.dump(wallet_data.model_dump(), f, indent=2)
        
        # Set file permissions (owner read/write only)
        os.chmod(self._wallet_path, 0o600)
    
    def _save_spending(self):
        """Save updated spending to wallet file."""
        if not self._wallet_path.exists():
            return
        
        try:
            with open(self._wallet_path, "r") as f:
                data = json.load(f)
            
            data["spending"] = {
                "today": self._today,
                "amount": self._spent_today,
            }
            
            with open(self._wallet_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass  # Non-critical, continue
    
    @property
    def limits(self) -> Limits:
        """Get current spending limits."""
        # Reset if new day
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._today:
            self._today = today
            self._spent_today = 0.0
        
        return Limits(
            max_per_tx=self._limits.max_per_tx,
            max_per_day=self._limits.max_per_day,
            spent_today=self._spent_today,
        )
    
    def set_limits(self, max_per_tx: float = None, max_per_day: float = None):
        """Update spending limits."""
        if max_per_tx is not None:
            self._limits.max_per_tx = max_per_tx
        if max_per_day is not None:
            self._limits.max_per_day = max_per_day
        
        # Save to file
        if self._wallet_path.exists():
            try:
                with open(self._wallet_path, "r") as f:
                    data = json.load(f)
                data["limits"] = {
                    "maxPerTx": self._limits.max_per_tx,
                    "maxPerDay": self._limits.max_per_day,
                }
                with open(self._wallet_path, "w") as f:
                    json.dump(data, f, indent=2)
            except Exception:
                pass
    
    def check_limits(self, amount: float) -> Tuple[bool, Optional[str]]:
        """Check if amount is within limits. Returns (ok, error_message)."""
        limits = self.limits
        
        if amount > limits.max_per_tx:
            return False, f"Exceeds per-transaction limit: {amount} > {limits.max_per_tx}"
        
        if limits.spent_today + amount > limits.max_per_day:
            return False, f"Exceeds daily limit: {limits.spent_today} + {amount} > {limits.max_per_day}"
        
        return True, None
    
    def record_spend(self, amount: float):
        """Record a successful spend."""
        self._spent_today += amount
        self._save_spending()
    
    def sign_permit(
        self,
        spender: str,
        amount: float,
        deadline_minutes: int = 30,
        nonce: int = 0,
    ) -> dict:
        """
        Sign an EIP-2612 permit for USDC spending.
        
        Args:
            spender: Address authorized to spend
            amount: Amount in USDC
            deadline_minutes: Permit validity in minutes
            nonce: USDC permit nonce (query from contract)
        
        Returns:
            Permit data with signature {owner, spender, value, deadline, nonce, v, r, s}
        """
        deadline = int(datetime.now().timestamp()) + (deadline_minutes * 60)
        value = int(amount * 1e6)  # USDC has 6 decimals
        
        # EIP-712 domain for USDC
        domain = {
            "name": "USD Coin",
            "version": "2",
            "chainId": self.chain_config["chain_id"],
            "verifyingContract": self.chain_config["usdc"],
        }
        
        # Permit message
        message = {
            "owner": self.address,
            "spender": spender,
            "value": value,
            "nonce": nonce,
            "deadline": deadline,
        }
        
        # EIP-712 types
        types = {
            "Permit": [
                {"name": "owner", "type": "address"},
                {"name": "spender", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "nonce", "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
            ],
        }
        
        # Sign
        signable = encode_typed_data(domain, types, message)
        signed = self._account.sign_message(signable)
        
        return {
            "owner": self.address,
            "spender": spender,
            "value": str(value),
            "deadline": deadline,
            "nonce": nonce,
            "v": signed.v,
            "r": hex(signed.r),
            "s": hex(signed.s),
        }

    def transfer(
        self,
        to: str,
        amount: Union[str, float, int],
        token: str = "USDC",
        chain: Optional[str] = None,
    ) -> TransferResult:
        """Send an ERC-20 token using exact decimal-to-atomic conversion."""
        from .chains import CHAINS as CHAIN_CONFIGS
        from web3 import Web3

        chain_name = chain or self.chain
        symbol = token.upper()
        config = CHAIN_CONFIGS.get(chain_name)
        if not config or config.get("type") == "solana":
            return TransferResult(success=False, amount=float(amount), token=symbol, chain=chain_name, error=f"Unsupported EVM chain: {chain_name}")
        token_config = config.get("tokens", {}).get(symbol)
        if not token_config:
            return TransferResult(success=False, amount=float(amount), token=symbol, chain=chain_name, error=f"Token {symbol} not supported on {chain_name}")
        try:
            value = Decimal(str(amount))
            decimals = int(token_config["decimals"])
            atomic = value * (Decimal(10) ** decimals)
            if value <= 0 or atomic != atomic.to_integral_value():
                raise ValueError(f"amount must be positive with at most {decimals} decimal places")
            w3 = Web3(Web3.HTTPProvider(config["rpc"]))
            recipient = Web3.to_checksum_address(to)
            contract = w3.eth.contract(
                address=Web3.to_checksum_address(token_config["address"]),
                abi=[{
                    "name": "transfer", "type": "function", "stateMutability": "nonpayable",
                    "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
                    "outputs": [{"name": "", "type": "bool"}],
                }, {
                    "name": "balanceOf", "type": "function", "stateMutability": "view",
                    "inputs": [{"name": "account", "type": "address"}],
                    "outputs": [{"name": "", "type": "uint256"}],
                }],
            )
            atomic_int = int(atomic)
            balance = contract.functions.balanceOf(self.address).call()
            if balance < atomic_int:
                return TransferResult(success=False, amount=float(value), token=symbol, chain=chain_name, error=f"Insufficient {symbol} balance")
            nonce = w3.eth.get_transaction_count(self.address)
            tx = contract.functions.transfer(recipient, atomic_int).build_transaction({
                "from": self.address,
                "nonce": nonce,
                "chainId": config["chainId"],
                "gasPrice": w3.eth.gas_price,
            })
            tx.setdefault("gas", w3.eth.estimate_gas(tx))
            signed = self._account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
            hash_hex = tx_hash.hex()
            return TransferResult(
                success=int(receipt["status"]) == 1,
                tx_hash=hash_hex,
                from_address=self.address,
                to_address=recipient,
                amount=float(value),
                token=symbol,
                chain=chain_name,
                gas_used=int(receipt["gasUsed"]),
                block_number=int(receipt["blockNumber"]),
                explorer_url=f"{config['explorer']}/tx/{hash_hex}",
                error=None if int(receipt["status"]) == 1 else "Transaction reverted",
            )
        except (InvalidOperation, ValueError) as exc:
            return TransferResult(success=False, token=symbol, chain=chain_name, error=str(exc))
        except Exception as exc:
            return TransferResult(success=False, token=symbol, chain=chain_name, error=str(exc))


def create_wallet(
    wallet_path: Optional[str] = None,
    chain: str = "base",
    password: Optional[str] = None,
    label: Optional[str] = None,
) -> Wallet:
    """Create a new wallet."""
    path = Path(wallet_path).expanduser() if wallet_path else DEFAULT_WALLET_PATH
    if path.exists():
        raise WalletError(f"Wallet already exists at {path}")
    return Wallet(wallet_path=str(path), chain=chain, password=password, label=label)


def load_wallet(
    wallet_path: Optional[str] = None,
    chain: str = "base",
    password: Optional[str] = None,
) -> Wallet:
    """Load an existing wallet."""
    path = Path(wallet_path).expanduser() if wallet_path else DEFAULT_WALLET_PATH
    if not path.exists():
        raise WalletError(f"Wallet not found at {path}")
    return Wallet(wallet_path=str(path), chain=chain, password=password)


def _encrypt_private_key(private_key: str, password: str) -> Tuple[str, str, str]:
    """Node-compatible scrypt + AES-256-CBC wallet encryption."""
    from cryptography.hazmat.primitives import padding as symmetric_padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    salt = os.urandom(16)
    iv = os.urandom(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    padder = symmetric_padding.PKCS7(128).padder()
    padded = padder.update(private_key.encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return encrypted.hex(), iv.hex(), salt.hex()


def _decrypt_private_key(encrypted: str, password: str, iv_hex: str, salt_hex: str) -> str:
    from cryptography.hazmat.primitives import padding as symmetric_padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=16384, r=8, p=1, dklen=32)
    decryptor = Cipher(algorithms.AES(key), modes.CBC(bytes.fromhex(iv_hex))).decryptor()
    padded = decryptor.update(bytes.fromhex(encrypted)) + decryptor.finalize()
    unpadder = symmetric_padding.PKCS7(128).unpadder()
    try:
        return (unpadder.update(padded) + unpadder.finalize()).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise WalletError("Invalid wallet password or corrupted wallet") from exc
