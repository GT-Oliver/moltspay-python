"""MoltsPay exceptions."""


class MoltsPayError(Exception):
    """Base exception for MoltsPay."""
    pass


class WalletError(MoltsPayError):
    """Wallet-related errors."""
    pass


class PaymentError(MoltsPayError):
    """Payment failed."""
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
