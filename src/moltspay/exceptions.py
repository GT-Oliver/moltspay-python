"""MoltsPay exceptions."""


class MoltsPayError(Exception):
    """Base exception for MoltsPay."""
    code = "MOLTSPAY_ERROR"

    def __init__(self, message: str = ""):
        super().__init__(message)


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
    def __init__(self, message: str, tx_hash: str = None):
        super().__init__(message)
        self.tx_hash = tx_hash


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
