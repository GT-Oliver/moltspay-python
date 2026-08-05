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
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

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
        
        # Initialize facilitator registry
        self.registry = FacilitatorRegistry()
        
        # Load all skill paths
        for skill_path in skill_paths:
            self._load_skill(skill_path)
        
        # Provider info (from first manifest)
        self.provider = self.manifests[0].provider if self.manifests else None
        
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
    
    def _get_provider_chains(self) -> List[ChainConfig]:
        """Get supported chains from provider config."""
        if self.provider and self.provider.chains:
            # Use get_chains() to handle both string and object formats
            chains = self.provider.get_chains()
            result = []
            for c in chains:
                # Determine network from chain name
                if c.chain.startswith("solana"):
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
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Payment")
                self.send_header("Access-Control-Expose-Headers", "X-Payment-Required, X-Payment-Response")
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
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Payment")
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
                
                if parsed.path == "/execute":
                    return self._handle_execute(body, payment_header)
                else:
                    self._send_json(404, {"error": "Not found"})
            
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
            
            def _handle_execute(self, body: Dict[str, Any], payment_header: Optional[str]):
                """POST /execute - Execute service with x402 payment."""
                service_id = body.get("service")
                params = body.get("params", {})
                
                if not service_id:
                    return self._send_json(400, {"error": "Missing service"})
                
                skill = server.skills.get(service_id)
                if not skill:
                    return self._send_json(404, {"error": f"Service '{service_id}' not found"})
                
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
                
                if scheme != "exact":
                    return self._send_json(402, {"error": f"Unsupported scheme: {scheme}"})
                
                # Validate network is one of our supported chains
                if network not in server.supported_networks:
                    supported = ", ".join(server.supported_networks)
                    return self._send_json(402, {"error": f"Network {network} not supported. Supported: {supported}"})
                
                # Detect payment token
                payment_token = server._detect_payment_token(payment, network)
                if payment_token and payment_token not in skill.config.accepted_currencies:
                    accepted = skill.config.accepted_currencies
                    return self._send_json(402, {
                        "error": f"Token {payment_token} not accepted. Accepted: {', '.join(accepted)}"
                    })
                
                # Build requirements
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
                
                # Debug logging
                print(f"[MoltsPay DEBUG] payment_dict: {json.dumps(payment_dict, indent=2)}")
                print(f"[MoltsPay DEBUG] requirements_dict: {json.dumps(requirements_dict, indent=2)}")
                
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
                    return self._send_json(402, {
                        "error": f"Payment verification failed: {verify_result.error}",
                    })
                print(f"[MoltsPay] Payment verified")
                
                # Check if Solana - must settle BEFORE skill execution (blockhash expiry)
                is_solana = network.startswith("solana:")
                settlement = None
                
                if is_solana:
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
                    return self._send_json(500, {
                        "error": "Service execution failed",
                        "message": f"Timeout after {timeout_seconds}s",
                    })
                except Exception as e:
                    print(f"[MoltsPay] Skill execution failed: {e}")
                    return self._send_json(500, {
                        "error": "Service execution failed",
                        "message": str(e),
                    })
                
                # Settle payment (skip if already done for Solana)
                if not is_solana:
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
