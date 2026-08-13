"""MoltsPay exceptions."""


class MoltsPayError(Exception):
    """Base exception for MoltsPay."""
    code = "MOLTSPAY_ERROR"

    def __init__(self, message: str = "", details: dict = None):
        super().__init__(message)
        self.details = details or {}


class UnsupportedRail(MoltsPayError):
    code = "UNSUPPORTED_RAIL"


class AlipayCliNotFound(MoltsPayError):
    code = "ALIPAY_CLI_NOT_FOUND"


class AlipayPaymentTimeout(MoltsPayError):
    code = "ALIPAY_PAYMENT_TIMEOUT"


class AlipayPaymentRejected(MoltsPayError):
    code = "ALIPAY_PAYMENT_REJECTED"


class AlipayProtocolError(MoltsPayError):
    code = "ALIPAY_PROTOCOL"


class WalletError(MoltsPayError):
    """Wallet-related errors."""
    code = "WALLET_ERROR"


class PaymentError(MoltsPayError):
    """Payment failed."""
    code = "PAYMENT_ERROR"
    def __init__(self, message: str, tx_hash: str = None, details: dict = None):
        super().__init__(message, details=details)
        self.tx_hash = tx_hash


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
