"""MoltsPay exceptions."""


class MoltsPayError(Exception):
    """Base exception for MoltsPay."""
    code = "MOLTSPAY_ERROR"

    def __init__(self, message: str = "", details: dict = None):
        super().__init__(message)
        self.details = details or {}


class UnsupportedRail(MoltsPayError):
    code = "UNSUPPORTED_RAIL"


class WalletError(MoltsPayError):
    """Wallet-related errors."""
    code = "WALLET_ERROR"


class PaymentError(MoltsPayError):
    """Payment failed."""
    code = "PAYMENT_ERROR"
    def __init__(self, message: str, tx_hash: str = None, details: dict = None):
        super().__init__(message, details=details)
        self.tx_hash = tx_hash


class AlipayError(PaymentError):
    """Base class for stable Alipay A402 errors."""

    code = "alipay_error"


class AlipayConfigInvalid(AlipayError):
    code = "alipay_config_invalid"


class AlipayNotConfigured(AlipayError):
    code = "alipay_not_configured"


class AlipayCliNotFound(AlipayError):
    code = "alipay_cli_not_found"


class AlipayCliFailed(AlipayError):
    code = "alipay_cli_failed"


class AlipayWalletNotReady(AlipayError):
    code = "alipay_wallet_not_ready"


class AlipayRequestContextMissing(AlipayError):
    code = "alipay_request_context_missing"


class AlipayRequestContextInvalid(AlipayError):
    code = "alipay_request_context_invalid"


class AlipayProtocolError(AlipayError):
    code = "alipay_challenge_invalid"


class AlipayPaymentRejected(AlipayError):
    code = "alipay_payment_rejected"


class AlipayPaymentTimeout(AlipayError):
    code = "alipay_payment_timeout"


class AlipayPaymentStateUnknown(AlipayError):
    code = "alipay_payment_state_unknown"


class AlipayProofMalformed(AlipayError):
    code = "alipay_proof_malformed"


class AlipayProofInactive(AlipayError):
    code = "alipay_proof_inactive"


class AlipayVerifyUnavailable(AlipayError):
    code = "alipay_verify_unavailable"


class AlipayResponseSignatureInvalid(AlipayError):
    code = "alipay_response_signature_invalid"


class AlipayReplayDetected(AlipayError):
    code = "alipay_replay_detected"


class AlipayExecutionInProgress(AlipayError):
    code = "alipay_execution_in_progress"


class InteractiveRailRequiresLifecycle(PaymentError):
    code = "interactive_rail_requires_lifecycle"


class InsufficientBalance(PaymentError):
    """A provider-side custodial balance cannot cover the service price."""

    code = "INSUFFICIENT_BALANCE"

    def __init__(
        self,
        required: str = None,
        balance: str = None,
        currency: str = "CNY",
        topup_packs: list = None,
        message: str = None,
        details: dict = None,
    ):
        payload = dict(details or {})
        if required is not None:
            payload.setdefault("required", str(required))
        if balance is not None:
            payload.setdefault("balance", str(balance))
        if currency:
            payload.setdefault("currency", currency)
        if topup_packs is not None:
            payload.setdefault("topupPacks", [str(item) for item in topup_packs])
        if message is None:
            message = "Insufficient provider balance"
            if required is not None and balance is not None:
                message += f": need {required} {currency}, have {balance}"
        super().__init__(message, details=payload)


class InsufficientFunds(PaymentError):
    """Not enough USDC balance."""
    def __init__(self, required: float, balance: float):
        super().__init__(f"Insufficient funds: need {required} USDC, have {balance}")
        self.required = required
        self.balance = balance


class LimitExceeded(PaymentError):
    """Transaction exceeds spending limit."""
    def __init__(self, limit_type: str, limit: float, amount: float):
        super().__init__(f"Exceeds {limit_type} limit: {amount} > {limit}")
        self.limit_type = limit_type
        self.limit = limit
        self.amount = amount
