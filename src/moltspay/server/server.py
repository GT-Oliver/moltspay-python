"""
MoltsPay Server - Payment infrastructure for AI Agents (Python).

Usage:
    moltspay-server ./my_skill1 ./my_skill2 --port 8402
    
Or programmatically:
    from moltspay.server import MoltsPayServer
    
    server = MoltsPayServer("./my_skill")
    server.listen(8402)
"""

import asyncio
import base64
import importlib.util
import inspect
import json
import os
import sys
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote, quote

from .types import (
    ServicesManifest,
    ServiceConfig,
    ChainConfig,
    RegisteredSkill,
    X402PaymentPayload,
    X402PaymentRequirements,
    TOKEN_ADDRESSES,
    TOKEN_DECIMALS,
    TOKEN_DOMAINS,
    get_token_domain,
    CHAIN_TO_NETWORK,
    SOLANA_CHAINS,
    X402_VERSION,
)
from .facilitators import FacilitatorRegistry
from .facilitators.cdp import load_env_file
from .facilitators.balance import BalanceFacilitator
from .facilitators.wechat import WechatFacilitator
from .facilitators.alipay import AlipayFacilitator, ALIPAY_NETWORK, normalize_cny_amount
from .alipay_store import AlipayOrderStore, proof_hash
from ..alipay import encode_a402_json
from ..exceptions import AlipayProtocolError
from ..balance import from_sat, to_sat


class MoltsPayServer:
    """
    MoltsPay x402 Payment Server.
    
    Loads Python skills and serves them with x402 payment handling.
    
    Example:
        server = MoltsPayServer("./video_gen", "./transcription")
        server.listen(8402)
    """
    
    def __init__(
        self,
        *skill_paths: str,
        port: int = 8402,
        host: str = "0.0.0.0",
    ):
        """
        Initialize MoltsPay Server.
        
        Args:
            *skill_paths: Paths to skill directories containing moltspay.services.json
            port: Server port (default: 8402)
            host: Server host (default: 0.0.0.0)
        """
        # Load env first
        load_env_file()
        
        self.port = port
        self.host = host
        self.skills: Dict[str, RegisteredSkill] = {}
        self.manifests: List[ServicesManifest] = []
        self._balance_topup_orders: Dict[str, Dict[str, Any]] = {}
        
        # Initialize facilitator registry
        self.registry = FacilitatorRegistry()
        
        # Load all skill paths
        for skill_path in skill_paths:
            self._load_skill(skill_path)
        
        # Provider info (from first manifest)
        self.provider = self.manifests[0].provider if self.manifests else None

        if self.provider and self.provider.balance:
            balance_config = self.provider.balance
            self.registry.register("balance", BalanceFacilitator(
                db_path=balance_config["db_path"],
                currency=balance_config.get("currency", "USD"),
                single_limit=balance_config.get("single_limit", "5.00"),
                daily_limit=balance_config.get("daily_limit", "10.00"),
                auth_mode=balance_config.get("auth_mode", "off"),
            ))
        if self.provider and self.provider.wechat:
            self.registry.register("wechat", WechatFacilitator(self.provider.wechat))
        self.alipay = None
        self.alipay_store = None
        if self.provider and self.provider.alipay:
            self.alipay = AlipayFacilitator(self.provider.alipay)
            self.alipay_store = AlipayOrderStore(self.provider.alipay.get("order_db_path", "data/alipay-a402.sqlite"))
            self.registry.register("alipay", self.alipay)
        
        # Get configured chains
        self.chains = self._get_provider_chains()
        self.supported_networks = [c.network for c in self.chains]
        
        # Log startup info
        total_services = sum(len(m.services) for m in self.manifests)
        chain_names = ", ".join(c.chain for c in self.chains)
        
        print(f"[MoltsPay] Loaded {total_services} services from {len(self.manifests)} skill(s)")
        if self.provider:
            print(f"[MoltsPay] Provider: {self.provider.name}")
            print(f"[MoltsPay] Receive wallet: {self.provider.wallet}")
        print(f"[MoltsPay] Chains: {chain_names} (multi-chain enabled)")
        print(f"[MoltsPay] Facilitators: {', '.join(self.registry.list_facilitators())}")
        print(f"[MoltsPay] Supported networks: {', '.join(self.registry.list_supported_networks())}")
        print(f"[MoltsPay] Protocol: x402 + MPP")

    @staticmethod
    def _balance_topup_order_is_active(
        order: Dict[str, Any], now: Optional[datetime] = None,
    ) -> bool:
        """Return whether a cached top-up order still has a scannable QR code."""
        expires_at = order.get("expires_at")
        if not isinstance(expires_at, str) or not expires_at:
            # Entries created by older versions did not carry their real expiry.
            return False
        try:
            expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        if expires.tzinfo is None:
            return False
        current = now or datetime.now(timezone.utc)
        return current.astimezone(timezone.utc) < expires.astimezone(timezone.utc)

    def _get_cached_balance_topup_order(self, cache_key: str) -> Optional[Dict[str, Any]]:
        cached = self._balance_topup_orders.get(cache_key)
        if cached is None:
            return None
        if self._balance_topup_order_is_active(cached):
            return cached
        # Never return a QR code after WeChat's Native-order expiry.
        self._balance_topup_orders.pop(cache_key, None)
        return None
    
    def _get_provider_chains(self) -> List[ChainConfig]:
        """Get supported chains from provider config."""
        if self.provider and self.provider.chains:
            # Use get_chains() to handle both string and object formats
            chains = self.provider.get_chains()
            result = []
            for c in chains:
                # Determine network from chain name
                if c.chain in ("balance", "wechat", "alipay"):
                    network = c.network or c.chain
                    wallet = c.wallet
                elif c.chain.startswith("solana"):
                    # Solana chains use different network format
                    network = c.network or SOLANA_CHAINS.get(c.chain, {}).get("network", "solana:devnet")
                    # Use solana_wallet if available
                    wallet = c.wallet or (self.provider.solana_wallet if self.provider else None)
                else:
                    # EVM chains
                    network = c.network or CHAIN_TO_NETWORK.get(c.chain, "eip155:8453")
                    wallet = c.wallet or (self.provider.wallet if self.provider else None)
                
                result.append(ChainConfig(
                    chain=c.chain,
                    network=network,
                    tokens=c.tokens,
                    wallet=wallet,
                ))
            return result
        
        # Fallback: single chain from legacy 'chain' field
        chain = self.provider.chain if self.provider else "base"
        network = CHAIN_TO_NETWORK.get(chain, "eip155:8453")
        return [ChainConfig(chain=chain, network=network, tokens=["USDC"])]
    
    def _load_skill(self, skill_path: str) -> None:
        """Load a skill from a directory path."""
        path = Path(skill_path).resolve()
        
        if not path.is_dir():
            raise ValueError(f"Skill path is not a directory: {path}")
        
        # Load services manifest
        manifest_path = path / "moltspay.services.json"
        if not manifest_path.exists():
            raise ValueError(f"No moltspay.services.json found in {path}")
        
        manifest_data = json.loads(manifest_path.read_text())
        manifest = ServicesManifest(**manifest_data)
        self.manifests.append(manifest)
        
        # Load Python module
        init_path = path / "__init__.py"
        if not init_path.exists():
            raise ValueError(f"No __init__.py found in {path}")
        
        # Import the module
        module_name = path.name
        spec = importlib.util.spec_from_file_location(module_name, init_path)
        if spec is None or spec.loader is None:
            raise ValueError(f"Could not load module from {init_path}")
        
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        
        print(f"[MoltsPay] Loading skill from {path}")
        
        # Register each service's handler
        for service in manifest.services:
            func_name = service.function
            
            if not hasattr(module, func_name):
                print(f"[MoltsPay] WARNING: Function '{func_name}' not found in {module_name}")
                continue
            
            handler = getattr(module, func_name)
            if not callable(handler):
                print(f"[MoltsPay] WARNING: '{func_name}' is not callable")
                continue
            
            self.skills[service.id] = RegisteredSkill(
                id=service.id,
                config=service,
                handler=handler,
            )
            print(f"[MoltsPay]   Registered: {service.id} -> {func_name}()")
    
    def _build_payment_requirements(
        self,
        config: ServiceConfig,
        network: str,
        wallet: Optional[str] = None,
        token: Optional[str] = None,
    ) -> X402PaymentRequirements:
        """Build x402 payment requirements for a service."""
        amount_units = str(int(config.price * 1e6))
        accepted = config.accepted_currencies
        
        selected_token = token if token and token in accepted else accepted[0]
        token_addresses = TOKEN_ADDRESSES.get(network, {})
        token_address = token_addresses.get(selected_token, "")
        token_domain = get_token_domain(network, selected_token)
        
        return X402PaymentRequirements(
            scheme="exact",
            network=network,
            asset=token_address,
            amount=amount_units,
            payTo=wallet or (self.provider.wallet if self.provider else ""),
            maxTimeoutSeconds=300,
            extra=token_domain,
        )
    
    def _detect_payment_token(self, payment: X402PaymentPayload, network: str) -> Optional[str]:
        """Detect which token is being used in the payment."""
        asset = None
        if payment.accepted:
            asset = payment.accepted.get("asset")
        if not asset and isinstance(payment.payload, dict):
            asset = payment.payload.get("asset")
        
        if not asset:
            return None
        
        token_addresses = TOKEN_ADDRESSES.get(network, {})
        for symbol, address in token_addresses.items():
            if address.lower() == asset.lower():
                return symbol
        return None
    
    async def _execute_handler(
        self,
        handler: Callable,
        params: Dict[str, Any],
    ) -> Any:
        """Execute a skill handler (sync or async)."""
        if inspect.iscoroutinefunction(handler):
            return await handler(params)
        else:
            # Run sync handler in thread pool
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, handler, params)
    
    def listen(self, port: Optional[int] = None) -> None:
        """
        Start the HTTP server.
        
        Args:
            port: Override port (optional)
        """
        port = port or self.port
        server = self
        
        class RequestHandler(BaseHTTPRequestHandler):
            """HTTP request handler for MoltsPay."""
            
            def log_message(self, format, *args):
                """Suppress default logging."""
                pass
            
            def _send_json(self, status: int, data: Any, headers: Dict[str, str] = None):
                """Send JSON response."""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Payment, Accept-Payment-Rail, Payment-Proof, Idempotency-Key")
                self.send_header("Access-Control-Expose-Headers", "X-Payment-Required, X-Payment-Response, Payment-Needed, Payment-Validation")
                if headers:
                    for key, value in headers.items():
                        self.send_header(key, value)
                self.end_headers()
                self.wfile.write(json.dumps(data, indent=2).encode())
            
            def _send_402(self, config: ServiceConfig, client_chain: str = None):
                """Send 402 Payment Required response with all supported chains.
                
                Args:
                    config: Service configuration
                    client_chain: Client's requested chain (only adds MPP header if tempo_moderato)
                """
                accepts = []
                
                # Get BNB spender address if available
                bnb_spender = server.registry.get_bnb_spender_address()
                
                # Get Solana fee payer if available
                solana_fee_payer = server.registry.get_solana_fee_payer()
                
                # Build accepts for ALL chains and ALL tokens
                for chain_config in server.chains:
                    if chain_config.network == "balance":
                        balance = server.registry.get("balance")
                        if isinstance(balance, BalanceFacilitator) and config.balance is not None:
                            balance_price = str(config.balance.get("price", config.price))
                            accepts.append(balance.create_requirements(balance_price, config.id))
                        continue
                    if chain_config.network == "wechat":
                        wechat = server.registry.get("wechat")
                        if isinstance(wechat, WechatFacilitator) and config.wechat:
                            accepts.append(wechat.create_payment_requirements(
                                price_cny=str(config.wechat["price_cny"]),
                                description=str(config.wechat["description"]),
                            ))
                        continue
                    token_addresses = TOKEN_ADDRESSES.get(chain_config.network, {})
                    # Get decimals for this network (default 6, BNB uses 18)
                    decimals = TOKEN_DECIMALS.get(chain_config.network, 6)
                    amount_units = str(int(config.price * (10 ** decimals)))
                    
                    # Determine wallet: use solana_wallet for Solana networks
                    if chain_config.network.startswith("solana:"):
                        wallet = server.provider.solana_wallet if server.provider else ""
                    else:
                        wallet = server.provider.wallet if server.provider else ""
                    
                    # Use service's accepted currencies, filtered by chain's supported tokens
                    for token in config.accepted_currencies:
                        if token in chain_config.tokens and token in token_addresses:
                            accept_entry = {
                                "scheme": "exact",
                                "network": chain_config.network,
                                "asset": token_addresses[token],
                                "amount": amount_units,
                                "payTo": wallet,
                                "maxTimeoutSeconds": 300,
                                "extra": get_token_domain(chain_config.network, token),
                            }
                            # Add bnbSpender for BNB networks
                            if chain_config.network in ("eip155:56", "eip155:97") and bnb_spender:
                                accept_entry["extra"] = {
                                    **accept_entry.get("extra", {}),
                                    "bnbSpender": bnb_spender,
                                }
                            # Add solanaFeePayer for Solana networks
                            if chain_config.network.startswith("solana:") and solana_fee_payer:
                                accept_entry["extra"] = {
                                    **accept_entry.get("extra", {}),
                                    "solanaFeePayer": solana_fee_payer,
                                }
                            accepts.append(accept_entry)
                
                payment_required = {
                    "x402Version": X402_VERSION,
                    "accepts": accepts,
                    "resource": {
                        "url": f"/execute?service={config.id}",
                        "description": f"{config.name} - ${config.price} {config.currency}",
                        "mimeType": "application/json",
                    },
                }
                
                encoded = base64.b64encode(json.dumps(payment_required).encode()).decode()
                
                self.send_response(402)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("X-Payment-Required", encoded)
                
                # Add MPP WWW-Authenticate header ONLY if client requested tempo_moderato
                # This prevents MPP from overriding x402 on other chains
                if client_chain == "tempo_moderato":
                    tempo = server.registry.get_tempo_facilitator()
                    tempo_chain = next((c for c in server.chains if c.network == "eip155:42431"), None)
                    if tempo and tempo_chain:
                        mpp_challenge = tempo.generate_mpp_challenge(
                            service_id=config.id,
                            service_name=config.name,
                            price=config.price,
                            wallet=server.provider.wallet if server.provider else "",
                            provider_name=server.provider.name if server.provider else "MoltsPay",
                        )
                        self.send_header("WWW-Authenticate", mpp_challenge["header"])
                
                self.end_headers()
                
                response = {
                    "error": "Payment required",
                    "message": f"Service requires ${config.price} {config.currency}",
                    "acceptedCurrencies": config.accepted_currencies,
                    "supportedChains": [c.chain for c in server.chains],
                    "x402": payment_required,
                }
                self.wfile.write(json.dumps(response, indent=2).encode())
            
            def do_OPTIONS(self):
                """Handle CORS preflight."""
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Payment, Accept-Payment-Rail, Payment-Proof, Idempotency-Key")
                self.send_header("Access-Control-Expose-Headers", "X-Payment-Required, X-Payment-Response, Payment-Needed, Payment-Validation")
                self.end_headers()
            
            def do_GET(self):
                """Handle GET requests."""
                parsed = urlparse(self.path)
                
                if parsed.path == "/services":
                    return self._handle_get_services()
                elif parsed.path == "/.well-known/agent-services.json":
                    return self._handle_agent_services()
                elif parsed.path == "/health":
                    return self._handle_health()
                elif parsed.path == "/balance/query":
                    return self._handle_balance_query(parsed)
                elif parsed.path == "/balance/transactions":
                    return self._handle_balance_transactions(parsed)
                elif parsed.path == "/balance":
                    return self._handle_balance_query(parsed)
                elif parsed.path.startswith("/payments/wechat/"):
                    return self._handle_wechat_status(parsed.path)
                elif parsed.path.startswith("/payments/alipay/"):
                    return self._handle_alipay_status(parsed.path)
                else:
                    self._send_json(404, {"error": "Not found"})
            
            def do_POST(self):
                """Handle POST requests."""
                parsed = urlparse(self.path)
                
                # Read body
                content_length = int(self.headers.get("Content-Length", 0))
                body = {}
                if content_length > 0:
                    raw_body = self.rfile.read(content_length)
                    try:
                        body = json.loads(raw_body)
                    except json.JSONDecodeError:
                        return self._send_json(400, {"error": "Invalid JSON"})
                
                # Get payment header
                payment_header = self.headers.get("X-Payment")
                payment_proof = self.headers.get("Payment-Proof")
                idempotency_key = self.headers.get("Idempotency-Key")
                
                if parsed.path == "/execute":
                    return self._handle_execute(body, payment_header, payment_proof, idempotency_key)
                elif parsed.path == "/balance/topup":
                    return self._handle_balance_topup(body)
                elif parsed.path == "/balance/topup/order":
                    return self._handle_balance_topup_order(body)
                elif parsed.path == "/balance/topup/confirm":
                    return self._handle_balance_topup_confirm(body)
                elif parsed.path == "/balance/refund":
                    return self._handle_balance_refund(body)
                else:
                    self._send_json(404, {"error": "Not found"})

            def _balance_facilitator(self):
                facilitator = server.registry.get("balance")
                return facilitator if isinstance(facilitator, BalanceFacilitator) else None

            def _handle_wechat_status(self, path: str):
                """Read WeChat order state without executing the paid service."""
                facilitator = server.registry.get("wechat")
                if not isinstance(facilitator, WechatFacilitator):
                    return self._send_json(404, {"error": "WeChat payment rail is not configured"})
                trade_no = unquote(path.removeprefix("/payments/wechat/"))
                if not trade_no or len(trade_no) > 128 or not all(ch.isalnum() or ch in "._-" for ch in trade_no):
                    return self._send_json(400, {"error": "Invalid WeChat trade number"})
                try:
                    result = facilitator.query_order(trade_no)
                except Exception as exc:
                    return self._send_json(502, {"status": "unknown", "error": str(exc)})
                trade_state = str(result.get("trade_state", "UNKNOWN")).upper()
                status = {
                    "SUCCESS": "paid", "NOTPAY": "pending", "USERPAYING": "pending",
                    "CLOSED": "failed", "REVOKED": "failed", "PAYERROR": "failed",
                }.get(trade_state, "unknown")
                return self._send_json(200, {
                    "status": status, "tradeState": trade_state, "outTradeNo": trade_no,
                    "transactionId": result.get("transaction_id"),
                })

            def _handle_alipay_status(self, path: str):
                """Read one durable provider order without exposing proof data."""
                if not server.alipay_store:
                    return self._send_json(404, {"error": "Alipay payment rail is not configured"})
                out_trade_no = unquote(path.removeprefix("/payments/alipay/"))
                if (
                    not out_trade_no or len(out_trade_no) > 128
                    or not all(ch.isalnum() or ch in "._-" for ch in out_trade_no)
                ):
                    return self._send_json(400, {"error": "Invalid Alipay merchant order number"})
                order = server.alipay_store.public_status(out_trade_no)
                if order is None:
                    return self._send_json(404, {
                        "code": "alipay_order_not_found",
                        "error": "Alipay order was not found",
                    })
                return self._send_json(200, order, {"Cache-Control": "no-store"})

            def _handle_balance_query(self, parsed):
                facilitator = self._balance_facilitator()
                if not facilitator:
                    return self._send_json(404, {"error": "Balance rail not configured"})
                balance_config = server.provider.balance if server.provider and server.provider.balance else {}
                topup_policy = {
                    "topupPacks": [str(item) for item in balance_config.get("topup_packs", [])],
                    "customTopupMax": balance_config.get("auto_topup_max"),
                }
                buyer_id = (parse_qs(parsed.query).get("buyer_id") or [""])[0]
                buyer = facilitator.ledger.get_buyer(buyer_id)
                if not buyer:
                    return self._send_json(200, {
                        "buyer_id": buyer_id, "balance": "0.00", "currency": facilitator.currency,
                        "exists": False, **topup_policy,
                    })
                self._send_json(200, {
                    "buyer_id": buyer_id, "currency": facilitator.currency,
                    "balance": from_sat(buyer["balance_sat"]),
                    "spent_today": from_sat(facilitator.ledger.spent_today_sat(buyer_id)),
                    "today_spent": from_sat(facilitator.ledger.spent_today_sat(buyer_id)),
                    "single_limit": from_sat(buyer["single_limit_sat"]),
                    "daily_limit": from_sat(buyer["daily_limit_sat"]),
                    "status": buyer["status"],
                    **topup_policy,
                })

            def _handle_balance_transactions(self, parsed):
                facilitator = self._balance_facilitator()
                if not facilitator:
                    return self._send_json(404, {"error": "Balance rail not configured"})
                query = parse_qs(parsed.query)
                buyer_id = (query.get("buyer_id") or [""])[0]
                limit = int((query.get("limit") or [20])[0])
                offset = int((query.get("offset") or [0])[0])
                self._send_json(200, {"transactions": facilitator.ledger.list_transactions(buyer_id, limit, offset)})

            def _handle_balance_topup(self, body):
                facilitator = self._balance_facilitator()
                if not facilitator:
                    return self._send_json(404, {"error": "Balance rail not configured"})
                if isinstance(body, dict) and str(body.get("rail") or "").lower() == "alipay":
                    return self._send_json(400, {"error": "Alipay balance top-ups are not supported"})
                if not self._balance_admin_authorized():
                    return self._send_json(401, {"error": "Balance admin authorization required"})
                try:
                    result = facilitator.ledger.topup(
                        buyer_id=body["buyer_id"], amount_sat=to_sat(body["amount"]),
                        external_ref=body["external_ref"], description=body.get("description"),
                    )
                    self._send_json(200, {**result, "balance": from_sat(result["balance_sat"])})
                except (KeyError, ValueError) as exc:
                    self._send_json(400, {"error": str(exc)})

            def _handle_balance_topup_order(self, body):
                facilitator = self._balance_facilitator()
                wechat = server.registry.get("wechat")
                if not facilitator or not isinstance(wechat, WechatFacilitator):
                    return self._send_json(400, {"error": "WeChat rail not configured on this server"})
                buyer_id = body.get("buyer_id") if isinstance(body, dict) else None
                if not isinstance(buyer_id, str) or not buyer_id:
                    return self._send_json(400, {"error": "buyer_id is required"})
                config = server.provider.balance if server.provider and server.provider.balance else {}
                pack = body.get("pack") or config.get("default_pack")
                packs = config.get("topup_packs", [])
                if not pack:
                    return self._send_json(400, {"error": "no pack specified and no default_pack configured"})
                try:
                    pack_sat = to_sat(str(pack))
                    max_pack = to_sat(str(config["auto_topup_max"])) if config.get("auto_topup_max") else None
                except ValueError as exc:
                    return self._send_json(400, {"error": f"Invalid pack: {exc}"})
                if pack_sat <= 0:
                    return self._send_json(400, {"error": "top-up pack must be greater than zero"})
                if str(pack) not in [str(item) for item in packs] and (max_pack is None or pack_sat > max_pack):
                    return self._send_json(400, {"error": f'pack "{pack}" is not an offered top-up pack'})
                signer = body.get("signer_address")
                attach = {"buyer_id": buyer_id, "signer": signer.lower()} if isinstance(signer, str) and signer.startswith("0x") else {"buyer_id": buyer_id, "nonce": secrets.token_hex(8)}
                cache_key = f"{buyer_id}|{pack}|{signer or ''}"
                cached = server._get_cached_balance_topup_order(cache_key)
                if cached:
                    return self._send_json(200, cached)
                try:
                    requirement = wechat.create_payment_requirements(
                        str(pack), f"Balance top-up {pack}", attach=attach,
                    )
                except Exception as exc:
                    return self._send_json(502, {"error": str(exc)})
                extra = requirement.get("extra", {})
                timeout_seconds = int(requirement.get("maxTimeoutSeconds", 300) or 300)
                created_at = datetime.now(timezone.utc)
                expires_at = created_at + timedelta(seconds=timeout_seconds)
                result = {
                    "code_url": extra.get("code_url"), "out_trade_no": extra.get("out_trade_no"),
                    "pack": str(pack), "max_timeout_seconds": timeout_seconds,
                    "created_at": created_at.isoformat().replace("+00:00", "Z"),
                    "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
                }
                server._balance_topup_orders[cache_key] = result
                self._send_json(200, result)

            def _handle_balance_topup_confirm(self, body):
                facilitator = self._balance_facilitator()
                wechat = server.registry.get("wechat")
                trade_no = body.get("out_trade_no") if isinstance(body, dict) else None
                if not facilitator or not isinstance(wechat, WechatFacilitator):
                    return self._send_json(400, {"error": "WeChat rail not configured on this server"})
                if not trade_no:
                    return self._send_json(400, {"error": "out_trade_no is required"})
                requirement = {"amount": "0", "extra": {"out_trade_no": trade_no}}
                payload = {"payload": {"out_trade_no": trade_no}}
                try:
                    check = asyncio.run(wechat.verify(payload, requirement))
                except Exception as exc:
                    return self._send_json(200, {"credited": False, "pending": True, "reason": str(exc)})
                if not check.valid:
                    return self._send_json(200, {"credited": False, "pending": True, "reason": check.error})
                details = check.details or {}
                amount = details.get("amount") or {}
                paid_fen = int(amount.get("payer_total") or amount.get("total") or 0)
                attach = details.get("attach")
                try:
                    attach_data = json.loads(attach) if isinstance(attach, str) else (attach or {})
                except json.JSONDecodeError:
                    attach_data = {}
                buyer_id = attach_data.get("buyer_id")
                if not buyer_id or paid_fen <= 0:
                    return self._send_json(422, {"error": "top-up order has no buyer binding or paid amount"})
                credited = facilitator.ledger.topup(
                    buyer_id, paid_fen, f"wechat:{trade_no}",
                    description=f"wechat topup out_trade_no={trade_no}",
                )
                for key, value in list(server._balance_topup_orders.items()):
                    if value.get("out_trade_no") == trade_no:
                        del server._balance_topup_orders[key]
                self._send_json(200, {"credited": True, "buyer_id": buyer_id,
                                      "tx_id": credited["tx_id"], "balance": from_sat(credited["balance_sat"]),
                                      "replayed": credited.get("replayed", False)})

            def _handle_balance_refund(self, body):
                facilitator = self._balance_facilitator()
                if not facilitator:
                    return self._send_json(404, {"error": "Balance rail not configured"})
                if not self._balance_admin_authorized():
                    return self._send_json(401, {"error": "Balance admin authorization required"})
                result = facilitator.refund(body.get("transaction", ""), body.get("reason"))
                self._send_json(200 if result.get("success") else 400, result)

            def _balance_admin_authorized(self):
                expected = os.environ.get("MOLTSPAY_BALANCE_ADMIN_TOKEN")
                if not expected:
                    return False
                return self.headers.get("Authorization", "") == f"Bearer {expected}"
            
            def _handle_get_services(self):
                """GET /services - List available services."""
                all_services = []
                for manifest in server.manifests:
                    for svc in manifest.services:
                        all_services.append({
                            "id": svc.id,
                            "name": svc.name,
                            "description": svc.description,
                            "price": svc.price,
                            "currency": svc.currency,
                            "acceptedCurrencies": svc.accepted_currencies,
                            "input": {k: v.model_dump() for k, v in svc.input.items()},
                            "output": svc.output,
                            "available": svc.id in server.skills,
                            "paymentRails": ({"alipay": {"available": True, "interactive": True, "protocol": "a402", "currency": "CNY", "amount": normalize_cny_amount(svc.alipay["price_cny"])}} if svc.alipay and server.alipay else {}),
                        })
                
                self._send_json(200, {
                    "provider": {
                        "name": server.provider.name if server.provider else "Unknown",
                        "description": server.provider.description if server.provider else None,
                        "wallet": server.provider.wallet if server.provider else None,
                        "chains": [c.model_dump() for c in server.chains],
                    },
                    "services": all_services,
                    "x402": {
                        "version": X402_VERSION,
                        "schemes": ["exact"],
                        "mainnet": True,
                    },
                })
            
            def _handle_agent_services(self):
                """GET /.well-known/agent-services.json - Standard discovery."""
                all_services = []
                for manifest in server.manifests:
                    for svc in manifest.services:
                        all_services.append({
                            "id": svc.id,
                            "name": svc.name,
                            "description": svc.description,
                            "price": svc.price,
                            "currency": svc.currency,
                            "acceptedCurrencies": svc.accepted_currencies,
                            "available": svc.id in server.skills,
                            "paymentRails": ({"alipay": {"available": True, "interactive": True, "protocol": "a402", "currency": "CNY", "amount": normalize_cny_amount(svc.alipay["price_cny"])}} if svc.alipay and server.alipay else {}),
                        })
                
                self._send_json(200, {
                    "version": "1.0",
                    "provider": {
                        "name": server.provider.name if server.provider else "Unknown",
                        "description": server.provider.description if server.provider else None,
                        "wallet": server.provider.wallet if server.provider else None,
                        "chains": [c.model_dump() for c in server.chains],
                    },
                    "services": all_services,
                    "endpoints": {
                        "services": "/services",
                        "execute": "/execute",
                        "health": "/health",
                    },
                    "payment": {
                        "protocol": "x402",
                        "version": X402_VERSION,
                        "schemes": ["exact"],
                        "mainnet": True,
                    },
                })
            
            def _handle_health(self):
                """GET /health - Health check."""
                total_services = sum(len(m.services) for m in server.manifests)
                
                self._send_json(200, {
                    "status": "healthy",
                    "chains": [c.chain for c in server.chains],
                    "facilitators": server.registry.list_facilitators(),
                    "supported_networks": server.registry.list_supported_networks(),
                    "services": total_services,
                    "registered": len(server.skills),
                })
            
            def _send_alipay_402(self, skill: RegisteredSkill, request_id: Optional[str]):
                if not server.alipay or not server.alipay_store or not skill.config.alipay:
                    return self._send_json(400, {"code": "alipay_not_configured", "error": "Alipay rail is not configured for this service"})
                alipay_config = skill.config.alipay
                amount = str(alipay_config.get("price_cny", ""))
                resource_id = str(alipay_config.get("resource_id") or f"/execute?service={quote(skill.id, safe='')}")
                service_id = alipay_config.get("service_id")
                if not isinstance(service_id, str) or not service_id.strip():
                    return self._send_json(500, {
                        "code": "alipay_config_invalid",
                        "error": f"Alipay service_id is required for service '{skill.id}'",
                    })
                service_id = service_id.strip()
                try:
                    from decimal import Decimal
                    amount_fen = int(Decimal(normalize_cny_amount(amount)) * 100)
                    order = server.alipay_store.create_order(
                        request_id=request_id or f"req_{secrets.token_hex(12)}", kind="service", amount_fen=amount_fen,
                        resource_id=resource_id, goods_name=str(alipay_config.get("goods_name") or skill.config.name),
                        pay_before="", service_id=service_id,
                    )
                    bill = server.alipay.create_payment_needed(
                        out_trade_no=order["out_trade_no"], amount=amount,
                        goods_name=str(alipay_config.get("goods_name") or skill.config.name),
                        resource_id=resource_id, service_id=service_id,
                        timeout_seconds=int(alipay_config.get("pay_timeout_seconds") or server.alipay.config.get("default_timeout_seconds", 1800)),
                    )
                except Exception as exc:
                    return self._send_json(500, {"code": "alipay_config_invalid", "error": str(exc)})
                # Keep the signed expiry and exact amount in the durable order.
                server.alipay_store.db.execute("UPDATE alipay_orders SET pay_before=?,updated_at=? WHERE out_trade_no=?", (bill["pay_before"], datetime.now(timezone.utc).isoformat(), order["out_trade_no"]))
                response = {"code": "payment_needed", "message": "Payment is required to access this resource", "resourceId": resource_id, "requestId": request_id}
                return self._send_json(402, response, {
                    "Payment-Needed": bill["header"], "Cache-Control": "no-store",
                })

            def _handle_alipay_execute(self, skill: RegisteredSkill, body: Dict[str, Any], payment_proof: str):
                if not server.alipay or not server.alipay_store:
                    return self._send_json(400, {"code": "alipay_not_configured", "error": "Alipay rail is not configured"})
                try:
                    proof = server.alipay.parse_payment_proof(payment_proof)
                except Exception:
                    return self._send_json(400, {"code": "alipay_proof_malformed", "error": "Payment-Proof is malformed"})
                try:
                    verified = server.alipay.verify_payment(proof)
                except Exception as exc:
                    code = getattr(exc, "code", "alipay_verify_unavailable")
                    status = 502 if code == "alipay_response_signature_invalid" else 503
                    return self._send_json(status, {"code": code, "error": code, "retryable": status >= 500})
                if str(verified.get("code")) != "10000" or verified.get("active") is not True:
                    return self._send_json(402, {"code": "alipay_proof_inactive", "error": "Payment-Proof is inactive"})
                out_trade_no = str(verified.get("out_trade_no") or "")
                order = server.alipay_store.get(out_trade_no)
                if not order:
                    return self._send_json(404, {"code": "alipay_order_not_found", "error": "Alipay order was not found"})
                try:
                    amount_matches = normalize_cny_amount(verified.get("amount")) == f"{order['amount_fen'] / 100:.2f}"
                except Exception:
                    amount_matches = False
                if not amount_matches or str(verified.get("currency", "CNY")) != "CNY":
                    return self._send_json(409, {"code": "alipay_amount_mismatch", "error": "Alipay amount does not match the order"})
                resource_id = str(verified.get("resource_id") or "")
                if resource_id != order["resource_id"]:
                    return self._send_json(403, {"code": "alipay_resource_mismatch", "error": "Alipay resource does not match the order"})
                claimed = server.alipay_store.claim_execution(out_trade_no, trade_no=str(verified.get("trade_no") or proof["trade_no"]), digest=proof_hash(payment_proof), resource_id=resource_id)
                if claimed["state"] == "completed":
                    cached = claimed["order"]
                    return self._send_json(200, {"success": True, "result": cached.get("result"), "replayed": True}, {"Payment-Validation": encode_a402_json({"trade_no": cached.get("trade_no"), "out_trade_no": out_trade_no, "validated": True, "resource_id": resource_id})})
                if claimed["state"] == "executing":
                    return self._send_json(409, {"code": "alipay_execution_in_progress", "error": "Alipay execution is already in progress", "retryable": True})
                if claimed["state"] == "replay":
                    reason = claimed.get("reason") or "replay_detected"
                    code = {
                        "trade_mismatch": "alipay_trade_order_mismatch",
                        "trade_used_by_other_order": "alipay_trade_reused",
                        "proof_changed_before_completion": "alipay_proof_changed",
                    }.get(reason, "alipay_replay_detected")
                    return self._send_json(409, {
                        "code": code,
                        "error": "Alipay payment evidence conflicts with the durable order",
                        "reason": reason,
                    })
                if claimed["state"] != "claimed":
                    return self._send_json(403, {"code": "alipay_resource_mismatch", "error": "Alipay order cannot be used for this resource"})
                try:
                    loop = asyncio.new_event_loop()
                    try:
                        result = loop.run_until_complete(asyncio.wait_for(server._execute_handler(skill.handler, body.get("params", {})), timeout=int(os.environ.get("SKILL_TIMEOUT_SECONDS", "1200"))))
                    finally:
                        loop.close()
                except Exception as exc:
                    server.alipay_store.fail_delivery(out_trade_no, "service_execution_failed_after_payment", {"error": "service execution failed"})
                    return self._send_json(500, {"code": "service_execution_failed_after_payment", "error": "Service execution failed after payment"})
                completed = server.alipay_store.complete(out_trade_no, result)
                try:
                    server.alipay.confirm_fulfillment(str(verified.get("trade_no") or proof["trade_no"]))
                    server.alipay_store.mark_outbox(str(verified.get("trade_no") or proof["trade_no"]), "confirmed")
                except Exception:
                    # The business result is durable; the outbox remains retryable.
                    pass
                return self._send_json(200, {"success": True, "result": result, "payment": {"status": "validated", "network": "alipay"}}, {"Payment-Validation": encode_a402_json({"trade_no": verified.get("trade_no"), "out_trade_no": out_trade_no, "validated": True, "resource_id": resource_id})})

            def _handle_execute(self, body: Dict[str, Any], payment_header: Optional[str], payment_proof: Optional[str] = None, idempotency_key: Optional[str] = None):
                """POST /execute - Execute service with x402 payment."""
                service_id = body.get("service")
                params = body.get("params", {})
                
                if not service_id:
                    return self._send_json(400, {"error": "Missing service"})
                
                skill = server.skills.get(service_id)
                if not skill:
                    return self._send_json(404, {"error": f"Service '{service_id}' not found"})

                requested_rail = (self.headers.get("Accept-Payment-Rail") or body.get("rail") or "").lower()
                if requested_rail == "alipay" or payment_proof:
                    if payment_proof:
                        return self._handle_alipay_execute(skill, body, payment_proof)
                    return self._send_alipay_402(skill, idempotency_key)
                
                # Validate required params
                for key, field in skill.config.input.items():
                    if field.required and key not in params:
                        return self._send_json(400, {"error": f"Missing required param: {key}"})
                
                # If no payment, return 402
                if not payment_header:
                    return self._send_402(skill.config, client_chain=body.get("chain"))
                
                # Parse payment payload
                try:
                    decoded = base64.b64decode(payment_header).decode()
                    payment_data = json.loads(decoded)
                    payment = X402PaymentPayload(
                        x402Version=payment_data.get("x402Version", 2),
                        payload=payment_data.get("payload", {}),
                        accepted=payment_data.get("accepted"),
                        resource=payment_data.get("resource"),
                        scheme=payment_data.get("scheme"),
                        network=payment_data.get("network"),
                    )
                except Exception as e:
                    return self._send_json(400, {"error": f"Invalid X-Payment header: {e}"})
                
                # Validate payment
                if payment.x402Version != X402_VERSION:
                    return self._send_json(402, {"error": f"Unsupported x402 version: {payment.x402Version}"})
                
                scheme = payment.accepted.get("scheme") if payment.accepted else payment.scheme
                network = payment.accepted.get("network") if payment.accepted else payment.network
                
                if scheme not in ("exact", "balance", "wechatpay-native"):
                    return self._send_json(402, {"error": f"Unsupported scheme: {scheme}"})
                
                # Validate network is one of our supported chains
                if network not in server.supported_networks:
                    supported = ", ".join(server.supported_networks)
                    return self._send_json(402, {"error": f"Network {network} not supported. Supported: {supported}"})
                
                # Detect payment token
                payment_token = None if network in ("balance", "wechat") else server._detect_payment_token(payment, network)
                if payment_token and payment_token not in skill.config.accepted_currencies:
                    accepted = skill.config.accepted_currencies
                    return self._send_json(402, {
                        "error": f"Token {payment_token} not accepted. Accepted: {', '.join(accepted)}"
                    })
                
                # Build requirements
                if network == "balance":
                    facilitator = server.registry.get("balance")
                    if not isinstance(facilitator, BalanceFacilitator) or skill.config.balance is None:
                        return self._send_json(402, {"error": "Balance rail not enabled for this service"})
                    req_data = facilitator.create_requirements(str(skill.config.balance.get("price", skill.config.price)), skill.config.id)
                    requirements = X402PaymentRequirements(**req_data)
                elif network == "wechat":
                    if skill.config.wechat is None:
                        return self._send_json(402, {"error": "WeChat rail not enabled for this service"})
                    accepted = payment.accepted or {}
                    requirements = X402PaymentRequirements(
                        scheme="wechatpay-native", network="wechat", asset="CNY",
                        amount=str(skill.config.wechat["price_cny"]),
                        payTo=str(server.provider.wechat["mchid"]),
                        maxTimeoutSeconds=int(accepted.get("maxTimeoutSeconds", 300)),
                        extra=accepted.get("extra", {}),
                    )
                else:
                    requirements = server._build_payment_requirements(skill.config, network=network, token=payment_token)
                
                # Verify payment using registry
                print(f"[MoltsPay] Verifying payment on {network}...")
                
                # Build payment payload dict for registry
                payment_dict = {
                    "x402Version": payment.x402Version,
                    "payload": payment.payload,
                    "accepted": payment.accepted,
                    "resource": payment.resource,
                    "scheme": payment.scheme,
                    "network": network,
                }
                requirements_dict = {
                    "scheme": requirements.scheme,
                    "network": requirements.network,
                    "asset": requirements.asset,
                    "amount": requirements.amount,
                    "payTo": requirements.payTo,
                    "maxTimeoutSeconds": requirements.maxTimeoutSeconds,
                    "extra": requirements.extra,
                }
                
                # Run async verify
                verify_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(verify_loop)
                try:
                    verify_result = verify_loop.run_until_complete(
                        server.registry.verify(payment_dict, requirements_dict)
                    )
                finally:
                    verify_loop.close()
                
                if not verify_result.valid:
                    if network == "balance" and verify_result.error == "insufficient_balance":
                        balance_config = server.provider.balance if server.provider and server.provider.balance else {}
                        details = dict(verify_result.details or {})
                        if details.get("balance_sat") is not None:
                            details["balance"] = from_sat(int(details["balance_sat"]))
                        details.update({
                            "required": str(requirements.amount),
                            "currency": balance_config.get("currency", "CNY"),
                            "topupPacks": [str(item) for item in balance_config.get("topup_packs", [])],
                            "customTopupMax": balance_config.get("auto_topup_max"),
                        })
                        return self._send_json(402, {
                            "code": "insufficient_balance",
                            "error": "insufficient_balance",
                            "details": details,
                        })
                    return self._send_json(402, {
                        "error": f"Payment verification failed: {verify_result.error}",
                    })
                print(f"[MoltsPay] Payment verified")
                
                # Check if Solana - must settle BEFORE skill execution (blockhash expiry)
                is_solana = network.startswith("solana:")
                is_balance = network == "balance"
                settlement = None
                
                if is_solana or is_balance:
                    print(f"[MoltsPay] Solana detected - settling payment FIRST (blockhash expiry protection)")
                    settle_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(settle_loop)
                    try:
                        settlement = settle_loop.run_until_complete(
                            server.registry.settle(payment_dict, requirements_dict)
                        )
                    finally:
                        settle_loop.close()
                    
                    if not settlement.success:
                        return self._send_json(402, {
                            "error": f"Payment settlement failed: {settlement.error}",
                        })
                    print(f"[MoltsPay] Payment settled: {settlement.transaction}")
                
                # Execute skill
                timeout_seconds = int(os.environ.get("SKILL_TIMEOUT_SECONDS", "1200"))
                print(f"[MoltsPay] Executing skill: {service_id} (timeout: {timeout_seconds}s)")
                
                try:
                    # Run async handler
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        result = loop.run_until_complete(
                            asyncio.wait_for(
                                server._execute_handler(skill.handler, params),
                                timeout=timeout_seconds,
                            )
                        )
                    finally:
                        loop.close()
                except asyncio.TimeoutError:
                    print(f"[MoltsPay] Skill timeout after {timeout_seconds}s")
                    if is_balance and settlement and settlement.success:
                        server.registry.get("balance").refund(settlement.transaction, "skill timeout")
                    return self._send_json(500, {
                        "error": "Service execution failed",
                        "message": f"Timeout after {timeout_seconds}s",
                    })
                except Exception as e:
                    print(f"[MoltsPay] Skill execution failed: {e}")
                    if is_balance and settlement and settlement.success:
                        server.registry.get("balance").refund(settlement.transaction, str(e))
                    return self._send_json(500, {
                        "error": "Service execution failed",
                        "message": str(e),
                    })
                
                # Settle payment (skip if already done for Solana)
                if not is_solana and not is_balance:
                    print(f"[MoltsPay] Skill succeeded, settling payment...")
                    settle_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(settle_loop)
                    try:
                        settlement = settle_loop.run_until_complete(
                            server.registry.settle(payment_dict, requirements_dict)
                        )
                    finally:
                        settle_loop.close()
                    
                    if settlement.success:
                        print(f"[MoltsPay] Payment settled: {settlement.transaction}")
                    else:
                        print(f"[MoltsPay] Settlement warning: {settlement.error}")
                
                # Build response
                extra_headers = {}
                if settlement.success:
                    response_payload = {
                        "success": True,
                        "transaction": settlement.transaction,
                        "network": network,
                    }
                    extra_headers["X-Payment-Response"] = base64.b64encode(
                        json.dumps(response_payload).encode()
                    ).decode()
                
                self._send_json(200, {
                    "success": True,
                    "result": result,
                    "payment": {
                        "transaction": settlement.transaction,
                        "status": "settled" if settlement.success else "pending",
                        "network": network,
                    } if settlement.success else {"status": "pending"},
                }, extra_headers)
        
        # Start server
        httpd = HTTPServer((self.host, port), RequestHandler)
        print(f"[MoltsPay] Server listening on http://{self.host}:{port}")
        print(f"[MoltsPay] Endpoints:")
        print(f"  GET  /services                      - List available services")
        print(f"  GET  /.well-known/agent-services.json - Service discovery")
        print(f"  POST /execute                       - Execute service (x402 payment)")
        print(f"  GET  /health                        - Health check")
        
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[MoltsPay] Shutting down...")
            httpd.shutdown()
