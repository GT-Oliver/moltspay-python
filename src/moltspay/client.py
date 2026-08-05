"""MoltsPay client - main interface."""

from typing import Any, Optional, List, Dict
from concurrent.futures import ThreadPoolExecutor, as_completed
import httpx

from .wallet import Wallet
from .x402 import X402Client, AsyncX402Client
from .models import Service, Balance, Limits, PaymentResult, TokenSymbol, FundingResult, FaucetResult
from .exceptions import InsufficientFunds, LimitExceeded, PaymentError
from .chains import CHAINS, get_protocol

# ERC20 ABI for balanceOf and allowance
ERC20_BALANCE_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]

ERC20_ALLOWANCE_ABI = [
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"}
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]

# Lazy import for Solana (optional dependency)
_solana_wallet_module = None
_solana_facilitator_module = None

def _get_solana_wallet():
    """Lazy import SolanaWallet to avoid requiring solders for EVM-only users."""
    global _solana_wallet_module
    if _solana_wallet_module is None:
        from . import wallet_solana as _solana_wallet_module
    return _solana_wallet_module

def _get_solana_facilitator():
    """Lazy import Solana facilitator."""
    global _solana_facilitator_module
    if _solana_facilitator_module is None:
        from .facilitators import solana as _solana_facilitator_module
    return _solana_facilitator_module

# Server-side APIs
ONRAMP_API = "https://moltspay.com/api/v1/onramp"
FAUCET_API = "https://moltspay.com/api/v1/faucet"


class MoltsPay:
    """
    MoltsPay client for paying for agent services.
    
    Usage:
        from moltspay import MoltsPay
        
        # Initialize (auto-creates wallet if not exists)
        client = MoltsPay()
        
        # Pay for a service
        result = client.pay(
            "https://juai8.com/zen7",
            "text-to-video",
            prompt="a cat dancing"
        )
        print(result.result)
    """
    
    def __init__(
        self,
        wallet_path: Optional[str] = None,
        private_key: Optional[str] = None,
        chain: str = "base",
        timeout: float = None,
        solana_wallet_path: Optional[str] = None,
    ):
        """
        Initialize MoltsPay client.
        
        Args:
            wallet_path: Path to EVM wallet file (default: ~/.moltspay/wallet.json)
            private_key: EVM private key (if provided, ignores wallet_path)
            chain: Default chain for direct operations
            timeout: HTTP timeout in seconds (None = no timeout, like Node.js)
            solana_wallet_path: Path to Solana wallet (default: ~/.moltspay/wallet-solana.json)
        """
        self._wallet = Wallet(
            wallet_path=wallet_path,
            private_key=private_key,
            chain=chain,
        )
        self._x402 = X402Client(timeout=timeout)
        self._chain = chain
        self._timeout = timeout
        
        # Solana wallet (lazy loaded)
        self._solana_wallet = None
        self._solana_wallet_path = solana_wallet_path
    
    def _is_solana_chain(self, chain: str = None) -> bool:
        """Check if chain is a Solana chain."""
        chain = chain or self._chain
        return chain in ("solana", "solana_devnet")
    
    def _get_solana_wallet(self):
        """Get or create Solana wallet (lazy loading)."""
        if self._solana_wallet is None:
            wallet_mod = _get_solana_wallet()
            self._solana_wallet = wallet_mod.SolanaWallet(
                wallet_path=self._solana_wallet_path,
                create_if_missing=True,
            )
        return self._solana_wallet
    
    @property
    def address(self) -> str:
        """Get wallet address for current chain."""
        if self._is_solana_chain():
            return self.solana_address
        return self._wallet.address
    
    @property
    def evm_address(self) -> str:
        """Get EVM wallet address."""
        return self._wallet.address
    
    @property
    def solana_address(self) -> Optional[str]:
        """Get Solana wallet address (creates wallet if needed)."""
        try:
            wallet = self._get_solana_wallet()
            return wallet.address
        except Exception:
            return None
    
    def discover(self, service_url: str) -> List[Service]:
        """
        Discover available services from a provider.
        
        Args:
            service_url: Base URL of the service provider
        
        Returns:
            List of available services
        """
        return self._x402.discover_services(service_url)
    
    def balance(self, chain: str = None) -> Balance:
        """
        Get wallet balance on a specific chain.
        
        Args:
            chain: Chain to query (default: client's chain)
        
        Returns:
            Balance object with USDC, USDT, and native token amounts
        """
        chain = chain or self._chain
        
        # Solana chains use different balance query
        if self._is_solana_chain(chain):
            balances = self.get_solana_balances(chain)
            return Balance(
                address=self.solana_address or "",
                usdc=balances.get("usdc", 0.0),
                usdt=0.0,  # Solana doesn't have USDT in our config
                eth=balances.get("sol", 0.0),  # SOL as native
                chain=chain,
            )
        
        # EVM chains
        balances = self._get_chain_balance(chain)
        return Balance(
            address=self.evm_address,
            usdc=balances.get("usdc", 0.0),
            usdt=balances.get("usdt", 0.0),
            eth=balances.get("native", 0.0),
            chain=chain,
        )
    
    def _get_chain_balance(self, chain: str) -> Dict[str, float]:
        """
        Query token balances on an EVM chain via RPC.
        
        Returns:
            Dict with token balances. Always includes 'usdc', 'usdt', 'native'.
            Tempo also includes 'pathUSD', 'alphaUSD', 'betaUSD', 'thetaUSD'.
            For Tempo: pathUSD maps to usdc, alphaUSD maps to usdt.
        """
        chain_config = CHAINS.get(chain)
        if not chain_config:
            return {"usdc": 0.0, "usdt": 0.0, "native": 0.0}
        
        try:
            from web3 import Web3
            
            w3 = Web3(Web3.HTTPProvider(chain_config["rpc"]))
            address = Web3.to_checksum_address(self.evm_address)
            
            # Get native balance
            native_wei = w3.eth.get_balance(address)
            native = float(w3.from_wei(native_wei, 'ether'))
            
            result = {"native": native, "usdc": 0.0, "usdt": 0.0}
            
            # Get all token balances from chain config
            tokens = chain_config.get("tokens", {})
            for token_name, token_config in tokens.items():
                try:
                    token_contract = w3.eth.contract(
                        address=Web3.to_checksum_address(token_config["address"]),
                        abi=ERC20_BALANCE_ABI
                    )
                    raw_balance = token_contract.functions.balanceOf(address).call()
                    balance = raw_balance / (10 ** token_config["decimals"])
                    
                    # Store with original name
                    if token_name in ("USDC", "USDT"):
                        result[token_name.lower()] = balance
                    else:
                        result[token_name] = balance
                        
                        # Tempo mapping: pathUSD → usdc, alphaUSD → usdt
                        if chain == "tempo_moderato":
                            if token_name == "pathUSD":
                                result["usdc"] = balance
                            elif token_name == "alphaUSD":
                                result["usdt"] = balance
                                
                except Exception:
                    if token_name in ("USDC", "USDT"):
                        result[token_name.lower()] = 0.0
                    else:
                        result[token_name] = 0.0
            
            return result
            
        except Exception as e:
            # Return zeros if query fails
            return {"usdc": 0.0, "usdt": 0.0, "native": 0.0}
    
    def get_all_balances(self) -> Dict[str, Dict[str, float]]:
        """
        Get wallet balances on all supported chains.
        
        Queries all EVM chains in parallel for speed.
        
        Returns:
            Dict mapping chain name to balance dict:
            {
                "base": {"usdc": 10.0, "usdt": 0.0, "native": 0.001},
                "polygon": {"usdc": 5.0, "usdt": 0.0, "native": 0.0},
                ...
            }
        """
        evm_chains = ["base", "polygon", "base_sepolia", "bnb", "bnb_testnet", "tempo_moderato"]
        results = {}
        
        # Query EVM chains in parallel
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(self._get_chain_balance, chain): chain 
                for chain in evm_chains
            }
            for future in as_completed(futures):
                chain = futures[future]
                try:
                    results[chain] = future.result()
                except Exception:
                    results[chain] = {"usdc": 0.0, "usdt": 0.0, "native": 0.0}
        
        return results
    
    def get_solana_balances(self, chain: str = "solana_devnet") -> Dict[str, float]:
        """
        Get Solana wallet balances (SOL + USDC).
        
        Args:
            chain: "solana" or "solana_devnet"
        
        Returns:
            Dict with 'sol' and 'usdc' balances
        """
        try:
            solana_addr = self.solana_address
            if not solana_addr:
                return {"sol": 0.0, "usdc": 0.0}
            
            chain_config = CHAINS.get(chain)
            if not chain_config:
                return {"sol": 0.0, "usdc": 0.0}
            
            from solana.rpc.api import Client as SolanaClient
            from solders.pubkey import Pubkey
            
            client = SolanaClient(chain_config["rpc"])
            pubkey = Pubkey.from_string(solana_addr)
            
            # Get SOL balance
            sol_resp = client.get_balance(pubkey)
            sol = sol_resp.value / 1e9 if sol_resp.value else 0.0
            
            # Get USDC balance (SPL token)
            usdc = 0.0
            usdc_config = chain_config.get("tokens", {}).get("USDC")
            if usdc_config:
                from spl.token.instructions import get_associated_token_address
                
                usdc_mint = Pubkey.from_string(usdc_config["address"])
                ata = get_associated_token_address(pubkey, usdc_mint)
                
                try:
                    token_resp = client.get_token_account_balance(ata)
                    if token_resp.value:
                        usdc = float(token_resp.value.ui_amount or 0)
                except Exception:
                    # ATA might not exist yet
                    usdc = 0.0
            
            return {"sol": sol, "usdc": usdc}
            
        except Exception as e:
            return {"sol": 0.0, "usdc": 0.0}
    
    def check_bnb_approvals(self, chain: str = "bnb") -> Dict[str, Any]:
        """
        Check BNB chain approval status for pay-for-success flow.
        
        Args:
            chain: "bnb" or "bnb_testnet"
        
        Returns:
            Dict with 'usdc', 'usdt' (bool), and 'spender' (str or None)
        """
        if chain not in ("bnb", "bnb_testnet"):
            return {"usdc": False, "usdt": False, "spender": None}
        
        result = {"usdc": False, "usdt": False, "spender": None}
        
        # Read spender from wallet config (saved during approve command)
        try:
            import json
            from pathlib import Path
            
            wallet_path = self._wallet._wallet_path
            if wallet_path and Path(wallet_path).exists():
                with open(wallet_path) as f:
                    wallet_data = json.load(f)
                result["spender"] = wallet_data.get("approvals", {}).get(chain)
        except Exception:
            pass
        
        if not result["spender"]:
            return result
        
        # Check allowances
        try:
            from web3 import Web3
            
            chain_config = CHAINS.get(chain)
            if not chain_config:
                return result
            
            w3 = Web3(Web3.HTTPProvider(chain_config["rpc"]))
            owner = Web3.to_checksum_address(self.evm_address)
            spender = Web3.to_checksum_address(result["spender"])
            
            for token_name in ["USDC", "USDT"]:
                token_config = chain_config.get("tokens", {}).get(token_name)
                if token_config:
                    contract = w3.eth.contract(
                        address=Web3.to_checksum_address(token_config["address"]),
                        abi=ERC20_ALLOWANCE_ABI
                    )
                    allowance = contract.functions.allowance(owner, spender).call()
                    result[token_name.lower()] = allowance > 0
        except Exception:
            pass
        
        return result
    
    def limits(self) -> Limits:
        """Get current spending limits."""
        return self._wallet.limits
    
    def set_limits(self, max_per_tx: float = None, max_per_day: float = None):
        """
        Update spending limits.
        
        Args:
            max_per_tx: Maximum amount per transaction
            max_per_day: Maximum daily spending
        """
        self._wallet.set_limits(max_per_tx=max_per_tx, max_per_day=max_per_day)
    
    def fund(self, amount: float, chain: str = None) -> FundingResult:
        """
        Generate a funding URL to add USDC to wallet via debit card/Apple Pay.
        
        Args:
            amount: Amount in USD to fund (minimum $5)
            chain: Chain to fund on ("base" or "polygon", default: wallet's chain)
        
        Returns:
            FundingResult with URL to open/scan as QR code
        
        Example:
            result = client.fund(10)
            if result.success:
                print(f"Scan QR or open: {result.url}")
        """
        chain = chain or self._chain
        
        if amount < 5:
            return FundingResult(
                success=False,
                amount=amount,
                chain=chain,
                error="Minimum funding amount is $5"
            )
        
        valid_chains = ("base", "polygon", "solana", "bnb", "tempo_moderato")
        if chain not in valid_chains:
            return FundingResult(
                success=False,
                amount=amount,
                chain=chain,
                error=f"Invalid chain: {chain}. Use one of: {', '.join(valid_chains)}"
            )
        
        # Use Solana address for Solana chain
        wallet_address = self.solana_address if chain == "solana" else self.evm_address
        
        try:
            response = httpx.post(
                f"{ONRAMP_API}/create",
                json={
                    "address": wallet_address,
                    "amount": amount,
                    "chain": chain,
                },
                timeout=30.0,
            )
            
            if response.status_code != 200:
                error_data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
                return FundingResult(
                    success=False,
                    amount=amount,
                    chain=chain,
                    error=error_data.get("error", f"Server error: {response.status_code}")
                )
            
            data = response.json()
            return FundingResult(
                success=True,
                url=data["url"],
                amount=amount,
                chain=chain,
                expires_in=data.get("expires_in", 300),
            )
            
        except Exception as e:
            return FundingResult(
                success=False,
                amount=amount,
                chain=chain,
                error=str(e)
            )
    
    def fund_qr(self, amount: float, chain: str = None) -> FundingResult:
        """
        Generate funding URL and print QR code to terminal.
        
        Args:
            amount: Amount in USD to fund (minimum $5)
            chain: Chain to fund on ("base" or "polygon")
        
        Returns:
            FundingResult with URL
        
        Example:
            client.fund_qr(10)  # Prints QR code to terminal
        """
        result = self.fund(amount, chain)
        
        if result.success and result.url:
            try:
                import qrcode
                qr = qrcode.QRCode(border=1)
                qr.add_data(result.url)
                qr.make(fit=True)
                
                print(f"\n💳 Fund your wallet\n")
                print(f"   Wallet: {self.address}")
                print(f"   Chain: {result.chain}")
                print(f"   Amount: ${result.amount:.2f}\n")
                print("   Scan to pay (US debit card / Apple Pay):\n")
                qr.print_ascii(invert=True)
                print(f"\n   ⏱️  QR code expires in {result.expires_in // 60} minutes\n")
            except ImportError:
                print(f"\n💳 Fund your wallet")
                print(f"   Open this URL to pay: {result.url}")
                print(f"   (Install 'qrcode' for QR code: pip install qrcode)\n")
        else:
            print(f"❌ {result.error}")
        
        return result
    
    def faucet(self) -> FaucetResult:
        """
        Request free testnet USDC from MoltsPay faucet.
        
        Only works on testnet chains (base_sepolia). Returns 1 USDC per request,
        limited to once per 24 hours per wallet address.
        
        Returns:
            FaucetResult with transaction details
        
        Example:
            client = MoltsPay(chain="base_sepolia")
            result = client.faucet()
            if result.success:
                print(f"Received {result.amount} USDC!")
                print(f"TX: {result.tx_hash}")
        
        Note:
            For mainnet USDC, use fund() or fund_qr() instead.
        """
        # Check if on testnet
        valid_testnets = ("base_sepolia", "bnb_testnet", "tempo_moderato", "solana_devnet")
        if self._chain not in valid_testnets:
            return FaucetResult(
                success=False,
                amount=0,
                chain=self._chain,
                error=f"Faucet only works on testnets. Current chain: {self._chain}. "
                      f"Use MoltsPay(chain='base_sepolia') for testnet, or fund() for mainnet."
            )
        
        try:
            response = httpx.post(
                FAUCET_API,
                json={"address": self.address, "chain": self._chain},
                timeout=30.0,
            )
            
            if response.status_code == 200:
                data = response.json()
                # Parse amount - handle comma-formatted numbers (e.g., "1,000,000" from Tempo)
                raw_amount = data.get("amount", 1.0)
                if isinstance(raw_amount, str):
                    raw_amount = float(raw_amount.replace(",", ""))
                return FaucetResult(
                    success=True,
                    amount=float(raw_amount),
                    chain=self._chain,
                    tx_hash=data.get("tx_hash"),
                )
            elif response.status_code == 429:
                return FaucetResult(
                    success=False,
                    amount=0,
                    chain=self._chain,
                    error="Rate limited: You can only request once per 24 hours. Try again later."
                )
            else:
                error_msg = response.json().get("error", response.text)
                return FaucetResult(
                    success=False,
                    amount=0,
                    chain=self._chain,
                    error=f"Faucet request failed: {error_msg}"
                )
        except httpx.TimeoutException:
            return FaucetResult(
                success=False,
                amount=0,
                chain=self._chain,
                error="Request timed out. Please try again."
            )
        except Exception as e:
            return FaucetResult(
                success=False,
                amount=0,
                chain=self._chain,
                error=f"Faucet request failed: {str(e)}"
            )
    
    def pay(
        self,
        service_url: str,
        service_id: str,
        token: str = "USDC",
        chain: str = None,
        **params,
    ) -> PaymentResult:
        """
        Pay for and call a service.
        
        Args:
            service_url: Base URL of the service provider
            service_id: Service ID to call
            token: Token to pay with ("USDC" or "USDT", default: "USDC")
            chain: Override chain for this payment (default: client's chain)
            **params: Service parameters
        
        Returns:
            PaymentResult with service response
        
        Raises:
            InsufficientFunds: Not enough balance
            LimitExceeded: Transaction exceeds limits
            PaymentError: Payment or service failed
        """
        # Use provided chain or default
        chain = chain or self._chain
        
        # Normalize token
        token = token.upper()
        if token not in ("USDC", "USDT"):
            raise PaymentError(f"Unsupported token: {token}. Use USDC or USDT.")
        
        # USDT requires gas for on-chain approval (no EIP-2612 support) - EVM only
        if token == "USDT" and not self._is_solana_chain(chain):
            bal = self.balance()
            if bal.native < 0.0001:
                raise PaymentError(
                    f"USDT requires ETH for gas (~$0.01 on Base). "
                    f"Your ETH balance: {bal.native:.6f} ETH. "
                    f"Please add a small amount of ETH to your wallet, or use USDC (gasless)."
                )
            import warnings
            warnings.warn("USDT requires gas (~$0.01). USDC is gasless and recommended.", UserWarning)
        
        # Discover service to get price
        services = self.discover(service_url)
        service = next((s for s in services if s.id == service_id), None)
        
        if not service:
            raise PaymentError(f"Service not found: {service_id}")
        
        # Check if token is accepted
        accepted = service.accepts
        if token not in accepted:
            raise PaymentError(f"Token {token} not accepted. Accepted: {', '.join(accepted)}")
        
        # Check limits
        ok, error = self._wallet.check_limits(service.price)
        if not ok:
            if "per-transaction" in error:
                raise LimitExceeded("per_tx", self._wallet.limits.max_per_tx, service.price)
            else:
                raise LimitExceeded("daily", self._wallet.limits.max_per_day, service.price)
        
        try:
            # Route to appropriate facilitator based on chain
            if self._is_solana_chain(chain):
                result = self._pay_solana(service_url, service_id, service.price, token, chain, params)
            else:
                result = self._pay_evm(service_url, service_id, service.price, token, chain, params)
            
            # Record spend on success
            if result.success:
                self._wallet.record_spend(service.price)
            
            return result
            
        except PaymentError:
            raise
        except Exception as e:
            return PaymentResult(
                success=False,
                amount=service.price,
                token=token,
                service_id=service_id,
                error=str(e),
            )
    
    def _pay_evm(
        self,
        service_url: str,
        service_id: str,
        price: float,
        token: str,
        chain: str,
        params: dict,
    ) -> PaymentResult:
        """Execute payment on EVM chains (Base, Polygon, etc.)."""
        payment_response = self._x402.pay_and_call(
            service_url,
            service_id,
            params,
            self._wallet._account,
            token=token,
            chain=chain,
        )
        
        # Build explorer URL only for real on-chain tx_hash
        explorer_url = None
        if payment_response.tx_hash and not payment_response.tx_hash.startswith("moltspay:"):
            chain_config = CHAINS.get(chain, {})
            if chain_config:
                explorer_url = f"{chain_config['explorer']}/tx/{payment_response.tx_hash}"
        
        return PaymentResult(
            success=True,
            tx_hash=payment_response.tx_hash,
            amount=price,
            token=token,
            service_id=service_id,
            result=payment_response.result,
            explorer_url=explorer_url,
        )
    
    def _pay_solana(
        self,
        service_url: str,
        service_id: str,
        price: float,
        token: str,
        chain: str,
        params: dict,
    ) -> PaymentResult:
        """Execute payment on Solana chains."""
        import base64
        import json as json_lib
        
        # Get Solana wallet
        solana_wallet = self._get_solana_wallet()
        keypair = solana_wallet.keypair
        
        # Make request to get 402 response with payment requirements
        with httpx.Client(timeout=self._timeout) as client:
            response = client.post(
                f"{service_url}/execute",
                json={"service": service_id, "params": params, "chain": chain},
            )
            
            if response.status_code != 402:
                if response.is_success:
                    return PaymentResult(
                        success=True,
                        amount=price,
                        token=token,
                        service_id=service_id,
                        result=response.json().get("result"),
                    )
                raise PaymentError(f"Unexpected response: {response.status_code}")
            
            # Parse X-Payment-Required header (base64 encoded JSON)
            payment_header = response.headers.get("x-payment-required")
            if not payment_header:
                raise PaymentError("Missing x-payment-required header in 402 response")
            
            try:
                decoded = base64.b64decode(payment_header).decode("utf-8")
                parsed = json_lib.loads(decoded)
                
                # Handle both v1 (array) and v2 (object with accepts) formats
                if isinstance(parsed, list):
                    requirements = parsed
                elif isinstance(parsed, dict) and "accepts" in parsed:
                    requirements = parsed["accepts"]
                else:
                    requirements = [parsed]
            except Exception as e:
                raise PaymentError(f"Invalid x-payment-required header: {e}")
            
            # Find Solana requirement
            network = "solana:mainnet" if chain == "solana" else "solana:devnet"
            payment_details = None
            for req in requirements:
                if req.get("network") == network:
                    payment_details = req
                    break
            
            if not payment_details:
                raise PaymentError(f"No payment requirement found for {chain}")
        
        # Execute Solana payment
        solana_mod = _get_solana_facilitator()
        result = solana_mod.handle_solana_payment(
            server_url=service_url,
            service=service_id,
            params=params,
            payment_details=payment_details,
            keypair=keypair,
            chain_name=chain,
        )
        
        # Build explorer URL
        tx_hash = result.get("payment", {}).get("transaction") if isinstance(result, dict) else None
        explorer_url = None
        if tx_hash:
            cluster = "" if chain == "solana" else "?cluster=devnet"
            explorer_url = f"https://solscan.io/tx/{tx_hash}{cluster}"
        
        return PaymentResult(
            success=True,
            tx_hash=tx_hash,
            amount=price,
            token=token,
            service_id=service_id,
            result=result,
            explorer_url=explorer_url,
        )
    
    def close(self):
        """Close the client."""
        self._x402.close()
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        self.close()


class AsyncMoltsPay:
    """
    Async version of MoltsPay client.
    
    Usage:
        import asyncio
        from moltspay import AsyncMoltsPay
        
        async def main():
            async with AsyncMoltsPay() as client:
                result = await client.pay(
                    "https://juai8.com/zen7",
                    "text-to-video",
                    prompt="a cat dancing"
                )
                print(result.result)
        
        asyncio.run(main())
    """
    
    def __init__(
        self,
        wallet_path: Optional[str] = None,
        private_key: Optional[str] = None,
        chain: str = "base",
        timeout: float = None,
    ):
        """Initialize async MoltsPay client. timeout=None means no timeout (like Node.js)."""
        self._wallet = Wallet(
            wallet_path=wallet_path,
            private_key=private_key,
            chain=chain,
        )
        self._x402 = AsyncX402Client(timeout=timeout)
        self._chain = chain
    
    @property
    def address(self) -> str:
        return self._wallet.address
    
    async def discover(self, service_url: str) -> List[Service]:
        """Discover available services."""
        return await self._x402.discover_services(service_url)
    
    def balance(self) -> Balance:
        """Get wallet balance (sync - no RPC call yet)."""
        return Balance(
            address=self._wallet.address,
            usdc=0.0,
            usdt=0.0,
            eth=0.0,
            chain=self._chain,
        )
    
    def limits(self) -> Limits:
        """Get spending limits."""
        return self._wallet.limits
    
    def set_limits(self, max_per_tx: float = None, max_per_day: float = None):
        """Update spending limits."""
        self._wallet.set_limits(max_per_tx=max_per_tx, max_per_day=max_per_day)
    
    async def pay(
        self,
        service_url: str,
        service_id: str,
        token: str = "USDC",
        **params,
    ) -> PaymentResult:
        """
        Pay for and call a service (async).
        
        Args:
            service_url: Base URL of the service provider
            service_id: Service ID to call
            token: Token to pay with ("USDC" or "USDT", default: "USDC")
            **params: Service parameters
        """
        # Normalize token
        token = token.upper()
        if token not in ("USDC", "USDT"):
            raise PaymentError(f"Unsupported token: {token}. Use USDC or USDT.")
        
        # USDT requires gas for on-chain approval (no EIP-2612 support)
        if token == "USDT":
            bal = await self.balance()
            if bal.native < 0.0001:
                raise PaymentError(
                    f"USDT requires ETH for gas (~$0.01 on Base). "
                    f"Your ETH balance: {bal.native:.6f} ETH. "
                    f"Please add a small amount of ETH to your wallet, or use USDC (gasless)."
                )
            import warnings
            warnings.warn("USDT requires gas (~$0.01). USDC is gasless and recommended.", UserWarning)
        
        services = await self.discover(service_url)
        service = next((s for s in services if s.id == service_id), None)
        
        if not service:
            raise PaymentError(f"Service not found: {service_id}")
        
        # Check if token is accepted
        accepted = service.accepts
        if token not in accepted:
            raise PaymentError(f"Token {token} not accepted. Accepted: {', '.join(accepted)}")
        
        ok, error = self._wallet.check_limits(service.price)
        if not ok:
            if "per-transaction" in error:
                raise LimitExceeded("per_tx", self._wallet.limits.max_per_tx, service.price)
            else:
                raise LimitExceeded("daily", self._wallet.limits.max_per_day, service.price)
        
        try:
            payment_response = await self._x402.pay_and_call(
                service_url,
                service_id,
                params,
                self._wallet._account,
                token=token,
                chain=self._chain,
            )
            
            self._wallet.record_spend(service.price)
            
            # Build explorer URL only for real on-chain tx_hash
            # (not internal IDs like "moltspay:xxx")
            explorer_url = None
            if payment_response.tx_hash and not payment_response.tx_hash.startswith("moltspay:"):
                chain_config = self._wallet.chain_config
                explorer_url = f"{chain_config['explorer']}{payment_response.tx_hash}"
            
            return PaymentResult(
                success=True,
                tx_hash=payment_response.tx_hash,
                amount=service.price,
                token=token,
                service_id=service_id,
                result=payment_response.result,
                explorer_url=explorer_url,
            )
            
        except PaymentError:
            raise
        except Exception as e:
            return PaymentResult(
                success=False,
                amount=service.price,
                token=token,
                service_id=service_id,
                error=str(e),
            )
    
    async def close(self):
        """Close the client."""
        await self._x402.close()
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, *args):
        await self.close()
