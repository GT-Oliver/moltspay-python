"""Local MoltsPay service handlers."""


def ping(params: dict) -> dict:
    """Return a successful ping response."""
    return {"ok": True}


def pong(params: dict) -> dict:
    """Return a successful pong response."""
    return {"ok": True}
