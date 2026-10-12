"""Node-local automatic ACME TLS; public configuration contains no key paths."""

async def certificate_control(*, action="status", browser_public_key="", browser_nonce=""):
    # Health probes can import the package without loading HTTP/crypto libraries.
    from .client import certificate_control as request
    return await request(action=action, browser_public_key=browser_public_key, browser_nonce=browser_nonce)

__all__ = ["certificate_control"]
