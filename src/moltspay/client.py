"""MoltsPay client - main interface."""

from typing import Any, Optional, List, Dict, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import time
from datetime import datetime, timezone
from pathlib import Path
import httpx

from .wallet import Wallet
from .x402 import X402Client, AsyncX402Client
from .models import Service, Balance, Limits, PaymentResult, TokenSymbol, FundingResult, FaucetResult, TransferResult, ServicesResponse, BalanceTopupSession
from .exceptions import InsufficientFunds, LimitExceeded, PaymentError, UnsupportedRail
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
        config_dir: Optional[str] = None,
        rail_preference: Optional[List[str]] = None,
        buyer_id: Optional[str] = None,
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
        self._config_dir = Path(config_dir).expanduser() if config_dir else self._wallet._wallet_path.parent
        self._config_path = self._config_dir / "config.json"
        stored = self._load_config()
        self._rail_preference = rail_preference or stored.get("railPreference", [])
        self._buyer_id = buyer_id or stored.get("buyerId")
        self._balance_client = None
        self._wechat_client = None
        self._alipay_client = None
        
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
    def is_initialized(self) -> bool:
        return True

    def get_wallet(self):
        """Return the underlying EVM account for direct signing operations."""
        return self._wallet._account

    def get_balance_signer_address(self) -> str:
        return self._wallet.address.lower()
    
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

    def get_services(self, service_url: str) -> ServicesResponse:
        """Node-compatible service discovery response."""
        return self._x402.get_services(service_url)

    def _load_config(self) -> Dict[str, Any]:
        try:
            return json.loads(self._config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def get_config(self) -> Dict[str, Any]:
        return {
            "chain": self._chain,
            "limits": {"maxPerTx": self.limits().max_per_tx, "maxPerDay": self.limits().max_per_day},
            "railPreference": list(self._rail_preference),
            "buyerId": self._buyer_id,
        }

    def update_config(
        self,
        *,
        max_per_tx: Optional[float] = None,
        max_per_day: Optional[float] = None,
        rail_preference: Optional[List[str]] = None,
        buyer_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if max_per_tx is not None or max_per_day is not None:
            self.set_limits(max_per_tx=max_per_tx, max_per_day=max_per_day)
        if rail_preference is not None:
            self._rail_preference = list(rail_preference)
        if buyer_id is not None:
            self._buyer_id = buyer_id
        data = self.get_config()
        self._config_dir.mkdir(parents=True, exist_ok=True)
        self._config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return data

    def set_buyer_id(self, buyer_id: str) -> None:
        self.update_config(buyer_id=buyer_id)

    def _get_balance_client(self):
        if self._balance_client is None:
            from .balance import BalanceClient
            self._balance_client = BalanceClient(self._buyer_id, timeout=self._timeout, account=self._wallet._account)
        self._balance_client.buyer_id = self._buyer_id
        return self._balance_client

    def get_buyer_balance(self, server_url: str, buyer_id: str = None):
        return self._get_balance_client().get_balance(server_url, buyer_id)

    def list_balance_transactions(self, server_url: str, buyer_id: str = None, limit: int = 20, offset: int = 0):
        return self._get_balance_client().list_transactions(server_url, buyer_id, limit, offset)

    def topup_balance(self, server_url: str, amount: str, rail: str, buyer_id: str = None, **kwargs: Any):
        return self._get_balance_client().topup_balance(server_url, amount, rail, buyer_id=buyer_id, **kwargs)

    def create_balance_topup_order(
        self, server_url: str, pack: Optional[str] = None, buyer_id: str = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create and persist a recoverable balance top-up order."""
        buyer = buyer_id or self._buyer_id
        if buyer:
            self._get_balance_client().buyer_id = buyer
        data = self._get_balance_client().create_topup_order(server_url, pack=pack, context=context)
        now = time.time()
        session = BalanceTopupSession(
            out_trade_no=data["out_trade_no"], buyer_id=buyer or self._buyer_id or "",
            pack=str(data["pack"]), server_url=server_url.rstrip("/"), code_url=data["code_url"],
            created_at=datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z"),
            expires_at=datetime.fromtimestamp(now + float(data.get("max_timeout_seconds", 300)), timezone.utc).isoformat().replace("+00:00", "Z"),
            context=context or {},
        )
        self._save_balance_topup_session(session)
        return {"outTradeNo": session.out_trade_no, "codeUrl": session.code_url, "pack": session.pack,
                "maxTimeoutSeconds": int(data.get("max_timeout_seconds", 300))}

    def _balance_topup_dir(self) -> Path:
        return self._config_dir / "balance-topup-sessions"

    def _save_balance_topup_session(self, session: BalanceTopupSession) -> None:
        self._balance_topup_dir().mkdir(parents=True, exist_ok=True)
        (self._balance_topup_dir() / f"{session.out_trade_no}.json").write_text(
            session.model_dump_json(indent=2), encoding="utf-8"
        )

    def get_balance_topup_session(self, out_trade_no: str) -> Optional[BalanceTopupSession]:
        path = self._balance_topup_dir() / f"{out_trade_no}.json"
        if not path.exists():
            return None
        try:
            return BalanceTopupSession.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def list_balance_topup_sessions(self) -> List[BalanceTopupSession]:
        directory = self._balance_topup_dir()
        if not directory.exists():
            return []
        sessions = []
        for path in directory.glob("*.json"):
            try:
                sessions.append(BalanceTopupSession.model_validate_json(path.read_text(encoding="utf-8")))
            except Exception:
                continue
        return sorted(sessions, key=lambda item: item.created_at, reverse=True)

    def confirm_balance_topup(self, out_trade_no: str, server_url: str = None) -> Dict[str, Any]:
        session = self.get_balance_topup_session(out_trade_no)
        url = server_url or (session.server_url if session else None)
        if not url:
            return {"credited": False, "reason": f"No server URL for {out_trade_no}: pass server_url or run topup-order first"}
        data = self._get_balance_client().confirm_topup(url, out_trade_no)
        if data.get("credited") and session:
            session.status = "credited"
            session.tx_id = data.get("tx_id")
            session.balance = data.get("balance")
            self._save_balance_topup_session(session)
        return {"credited": bool(data.get("credited")), "pending": data.get("pending"),
                "balance": data.get("balance"), "txId": data.get("tx_id"), "reason": data.get("reason")}

    def topup_balance_pack(
        self, server_url: str, pack: Optional[str] = None, buyer_id: str = None,
        poll_interval: float = 2.0, timeout: Optional[float] = None,
        on_code_url: Optional[Callable[[str, str], None]] = None,
    ) -> Dict[str, Any]:
        order = self.create_balance_topup_order(server_url, pack=pack, buyer_id=buyer_id)
        if on_code_url:
            on_code_url(order["pack"], order["codeUrl"])
        deadline = time.time() + float(timeout or order["maxTimeoutSeconds"])
        while time.time() < deadline:
            result = self.confirm_balance_topup(order["outTradeNo"], server_url=server_url)
            if result.get("credited"):
                return {"balance": result.get("balance"), "outTradeNo": order["outTradeNo"], "txId": result.get("txId")}
            time.sleep(max(0.05, poll_interval))
        raise PaymentError("Top-up timed out before the payment was confirmed")

    def _get_wechat_client(self):
        if self._wechat_client is None:
            from .wechat import WechatClient
            self._wechat_client = WechatClient(config_dir=str(self._config_dir), timeout=self._timeout)
        return self._wechat_client

    def _rail_challenge(self, service_url: str, service_id: str, params: Dict[str, Any], rail: str):
        from .x402 import parse_402_response
        url = f"{service_url.rstrip('/')}/execute"
        body = {"service": service_id, "params": params, "rail": rail}
        response = httpx.post(
            url, json=body, headers={"Accept-Payment-Rail": rail},
            timeout=self._timeout,
        )
        if response.status_code != 402:
            if response.is_success:
                return url, body, None, response
            raise PaymentError(f"Service error: {response.status_code} {response.text}")
        parsed = parse_402_response(response, service_id)
        aliases = {"wechat": {"wechatpay-native", "wechat"}, "alipay": {"alipay-aipay", "alipay"}}
        requirement = next((item for item in parsed.accepts if item.get("scheme") in aliases.get(rail, {rail}) or item.get("network") == rail), None)
        if not requirement:
            raise UnsupportedRail(f"Server does not offer payment rail: {rail}")
        return url, body, requirement, response

    def start_wechat_payment(
        self,
        service_url: str,
        service_id: str,
        params: Optional[Dict[str, Any]] = None,
        **options: Any,
    ):
        url, body, requirement, response = self._rail_challenge(service_url, service_id, params or {}, "wechat")
        if requirement is None:
            raise PaymentError("Service completed without requiring a WeChat payment")
        return self._get_wechat_client().start_402(
            resource_url=url, requirement=requirement, data=json.dumps(body),
            context={"server_url": service_url, "service_id": service_id}, **options,
        )

    def get_wechat_payment_status(self, identifier: str):
        return self._get_wechat_client().status(identifier)

    def fulfill_wechat_payment(self, identifier: str):
        return self._get_wechat_client().fulfill(identifier)

    def cancel_wechat_payment(self, identifier: str):
        return self._get_wechat_client().cancel(identifier)

    def list_wechat_payment_sessions(self):
        return self._get_wechat_client().list_sessions()

    def check_alipay_wallet(self, executable: str = "alipay-bot") -> None:
        """Check the locally installed Alipay wallet dependency."""
        from .alipay import AlipayClient
        client = self._alipay_client
        if client is None or client.executable != executable:
            client = AlipayClient(config_dir=str(self._config_dir), executable=executable)
            self._alipay_client = client
        return client.check_wallet()

    def _pay_wechat(self, service_url: str, service_id: str, params: Dict[str, Any], amount: float, **options: Any) -> PaymentResult:
        session = self.start_wechat_payment(service_url, service_id, params, **{k: v for k, v in options.items() if k in {"timeout", "on_payment_pending"}})
        completed = self._get_wechat_client().poll_session(
            session.payment_session_id,
            poll_interval=float(options.get("poll_interval", 3.0)),
            timeout=float(options.get("timeout", 300.0)),
        )
        if completed.status != "completed":
            raise PaymentError(completed.last_error or f"WeChat payment ended with {completed.status}")
        try:
            result = json.loads(completed.result_body or "{}")
        except json.JSONDecodeError:
            result = completed.result_body
        return PaymentResult(
            success=True, amount=amount, token="CNY", service_id=service_id,
            result=result.get("result", result) if isinstance(result, dict) else result,
            facilitator="wechat", network="wechat",
            payment={"out_trade_no": completed.out_trade_no, "session_id": completed.payment_session_id},
        )

    def _pay_alipay(self, service_url: str, service_id: str, params: Dict[str, Any], amount: float, **options: Any) -> PaymentResult:
        url, body, requirement, response = self._rail_challenge(service_url, service_id, params, "alipay")
        if requirement is None:
            data = response.json()
            return PaymentResult(success=True, amount=amount, token="CNY", service_id=service_id, result=data.get("result", data))
        if self._alipay_client is None:
            from .alipay import AlipayClient
            self._alipay_client = AlipayClient(
                config_dir=str(self._config_dir),
                framework=options.get("framework", "openclaw"),
            )
        elif options.get("framework"):
            self._alipay_client.framework = options["framework"]
        result = self._alipay_client.pay_402(
            resource_url=url, requirement=requirement, data=json.dumps(body),
            intent_summary=options.get("intent_summary"), timeout=options.get("timeout"),
            poll_interval=float(options.get("poll_interval", 3.0)),
            on_payment_pending=options.get("on_payment_pending"),
        )
        payment = result["payment"]
        return PaymentResult(
            success=True, amount=amount, token="CNY", service_id=service_id,
            result=result["body"], facilitator="alipay", network="alipay",
            tx_hash=f"alipay:{payment['trade_no']}", payment=payment,
        )
    
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

    def transfer(self, to: str, amount: Any, token: str = "USDC", chain: str = None) -> TransferResult:
        """Transfer USDC/USDT to an EVM address."""
        return self._wallet.transfer(to=to, amount=amount, token=token, chain=chain or self._chain)
    
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
                
                print("\nFund your wallet\n")
                print(f"   Wallet: {self.address}")
                print(f"   Chain: {result.chain}")
                print(f"   Amount: ${result.amount:.2f}\n")
                print("   Scan to pay (US debit card / Apple Pay):\n")
                qr.print_ascii(invert=True)
                print(f"\n   QR code expires in {result.expires_in // 60} minutes\n")
            except ImportError:
                print("\nFund your wallet")
                print(f"   Open this URL to pay: {result.url}")
                print(f"   (Install 'qrcode' for QR code: pip install qrcode)\n")
        else:
            print(f"Error: {result.error}")
        
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
        rail: str = None,
        payment_params: Optional[Dict[str, Any]] = None,
        rail_options: Optional[Dict[str, Any]] = None,
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
        if payment_params:
            params = {**payment_params, **params}

        # Use provided chain or default
        chain = chain or self._chain

        selected_rail = rail
        if selected_rail is None and self._rail_preference:
            selected_rail = self._rail_preference[0]
        
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

        if selected_rail == "balance":
            options = rail_options or {}
            buyer_id = options.get("buyer_id") or self._buyer_id
            if buyer_id:
                self._get_balance_client().buyer_id = buyer_id
            try:
                return self._get_balance_client().pay(service_url, service_id, params, service.price, buyer_id=buyer_id)
            except PaymentError as exc:
                message = str(exc)
                fundable = any(item in message.lower() for item in ("insufficient", "unknown buyer", "buyer_not_found"))
                if not fundable or options.get("auto_topup", True) is False:
                    raise
                if options.get("topup_mode") == "manual":
                    order = self.create_balance_topup_order(
                        service_url, pack=options.get("topup_pack"), buyer_id=buyer_id,
                        context={"service": service_id},
                    )
                    if options.get("on_topup_required"):
                        options["on_topup_required"](order["pack"], order["codeUrl"])
                    return PaymentResult(
                        success=False, amount=service.price, token="BALANCE", service_id=service_id,
                        result={"status": "topup_required", "out_trade_no": order["outTradeNo"],
                                "code_url": order["codeUrl"], "pack": order["pack"], "server_url": service_url},
                    )
                self.topup_balance_pack(
                    service_url, pack=options.get("topup_pack"), buyer_id=buyer_id,
                    poll_interval=float(options.get("topup_poll_interval", 2.0)),
                )
                return self._get_balance_client().pay(service_url, service_id, params, service.price, buyer_id=buyer_id)
        if selected_rail == "wechat":
            return self._pay_wechat(service_url, service_id, params, service.price, **(rail_options or {}))
        if selected_rail == "alipay":
            return self._pay_alipay(service_url, service_id, params, service.price, **(rail_options or {}))
        if selected_rail and selected_rail not in CHAINS:
            raise UnsupportedRail(f"Unsupported payment rail: {selected_rail}")
        
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
        for extra_client in (self._balance_client, self._wechat_client):
            if extra_client is not None:
                extra_client.close()
    
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
        config_dir: Optional[str] = None,
        rail_preference: Optional[List[str]] = None,
        buyer_id: Optional[str] = None,
    ):
        """Initialize async MoltsPay client. timeout=None means no timeout (like Node.js)."""
        self._wallet = Wallet(
            wallet_path=wallet_path,
            private_key=private_key,
            chain=chain,
        )
        self._x402 = AsyncX402Client(timeout=timeout)
        self._chain = chain
        self._timeout = timeout
        self._config_dir = config_dir
        self._rail_preference = rail_preference
        self._buyer_id = buyer_id
        self._sync_client = None

    def _get_sync_client(self) -> MoltsPay:
        if self._sync_client is None:
            private_key = self._wallet._account.key.hex()
            self._sync_client = MoltsPay(
                private_key=private_key, chain=self._chain, timeout=self._timeout,
                config_dir=self._config_dir, rail_preference=self._rail_preference,
                buyer_id=self._buyer_id,
            )
        return self._sync_client
    
    @property
    def address(self) -> str:
        return self._wallet.address
    
    async def discover(self, service_url: str) -> List[Service]:
        """Discover available services."""
        return await self._x402.discover_services(service_url)
    
    def balance(self) -> Balance:
        """Get wallet balance using the same RPC-backed implementation as sync client."""
        return self._get_sync_client().balance(self._chain)

    async def get_balance(self, chain: str = None) -> Balance:
        import asyncio
        return await asyncio.to_thread(self._get_sync_client().balance, chain or self._chain)

    async def get_all_balances(self) -> Dict[str, Dict[str, float]]:
        import asyncio
        return await asyncio.to_thread(self._get_sync_client().get_all_balances)

    def get_config(self) -> Dict[str, Any]:
        return self._get_sync_client().get_config()

    def update_config(self, **kwargs: Any) -> Dict[str, Any]:
        return self._get_sync_client().update_config(**kwargs)
    
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
        chain: str = None,
        rail: str = None,
        payment_params: Optional[Dict[str, Any]] = None,
        rail_options: Optional[Dict[str, Any]] = None,
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
        if payment_params:
            params = {**payment_params, **params}
        if rail:
            import asyncio
            return await asyncio.to_thread(
                self._get_sync_client().pay,
                service_url, service_id, token, chain or self._chain, rail,
                params, rail_options,
            )

        # Normalize token
        token = token.upper()
        if token not in ("USDC", "USDT"):
            raise PaymentError(f"Unsupported token: {token}. Use USDC or USDT.")
        
        # USDT requires gas for on-chain approval (no EIP-2612 support)
        if token == "USDT":
            bal = self.balance()
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
                chain=chain or self._chain,
            )
            
            self._wallet.record_spend(service.price)
            
            # Build explorer URL only for real on-chain tx_hash
            # (not internal IDs like "moltspay:xxx")
            explorer_url = None
            if payment_response.tx_hash and not payment_response.tx_hash.startswith("moltspay:"):
                chain_config = CHAINS.get(chain or self._chain, {})
                if chain_config:
                    explorer_url = f"{chain_config['explorer']}/tx/{payment_response.tx_hash}"
            
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

    async def transfer(self, to: str, amount: Any, token: str = "USDC", chain: str = None) -> TransferResult:
        import asyncio
        return await asyncio.to_thread(self._get_sync_client().transfer, to, amount, token, chain or self._chain)

    async def get_buyer_balance(self, server_url: str, buyer_id: str = None):
        import asyncio
        return await asyncio.to_thread(self._get_sync_client().get_buyer_balance, server_url, buyer_id)

    async def list_balance_transactions(self, server_url: str, buyer_id: str = None, limit: int = 20, offset: int = 0):
        import asyncio
        return await asyncio.to_thread(self._get_sync_client().list_balance_transactions, server_url, buyer_id, limit, offset)

    async def create_balance_topup_order(self, server_url: str, pack: str = None, buyer_id: str = None, context: Dict[str, Any] = None):
        import asyncio
        return await asyncio.to_thread(self._get_sync_client().create_balance_topup_order, server_url, pack, buyer_id, context)

    async def confirm_balance_topup(self, out_trade_no: str, server_url: str = None):
        import asyncio
        return await asyncio.to_thread(self._get_sync_client().confirm_balance_topup, out_trade_no, server_url)

    async def topup_balance_pack(self, server_url: str, pack: str = None, buyer_id: str = None, poll_interval: float = 2.0, timeout: float = None):
        import asyncio
        return await asyncio.to_thread(self._get_sync_client().topup_balance_pack, server_url, pack, buyer_id, poll_interval, timeout)
    
    async def close(self):
        """Close the client."""
        await self._x402.close()
        if self._sync_client is not None:
            self._sync_client.close()
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, *args):
        await self.close()
