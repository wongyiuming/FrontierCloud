"""Single-worker business ASGI or native storage HTTPS with online hot loading."""
from __future__ import annotations

import logging
import ssl
import time

import uvicorn
from cryptography.x509.verification import VerificationError

from app.core.config import settings
from . import assets
from .contract import validate_settings

logger = logging.getLogger("frontiercloud.storage_tls")


class HotTLS:
    """Select complete generations for new handshakes without dropping existing streams."""
    def __init__(self, host, directory=assets.PUBLISHED, *, roots=None):
        self.host, self.directory, self.roots = host.lower(), directory, roots
        self.generation = ""
        self.context = None
        self.load()

    def load(self):
        generation, certificate, key = assets.material(self.directory)
        if generation == self.generation:
            return self.context
        # Controller already validates chain before publication. Web checks key/SAN/time
        # independently while loading, then atomically swaps the context reference.
        assets.validate(self.host, certificate, key, roots=self.roots)
        base = self.directory / generation
        candidate = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        candidate.minimum_version = ssl.TLSVersion.TLSv1_2
        candidate.load_cert_chain(base / "fullchain.pem", base / "privkey.pem")
        candidate.set_servername_callback(self.select)
        self.context, self.generation = candidate, generation
        return candidate

    def select(self, stream, server_name, _initial):
        if not server_name or server_name.lower() != self.host:
            return ssl.ALERT_DESCRIPTION_UNRECOGNIZED_NAME
        try:
            context = self.load()
        except Exception:
            # Failed candidates leave the last complete serving context untouched.
            logger.warning("storage_tls_generation_refused")
            context = self.context
        stream.context = context


def main():
    import os
    validate_settings(settings, os.environ)
    storage = settings.DEPLOYMENT_MODE == "only_stroge"
    if not storage:
        uvicorn.run("main:app", host="0.0.0.0", port=8000, workers=1,
                    proxy_headers=False, access_log=False)
        return
    while True:
        try:
            tls = HotTLS(settings.SERVER_NAME)
            break
        except (OSError, ValueError, VerificationError):
            # The live controller remains diagnostic/retryable. No plaintext listener
            # or crash-restart loop is created while certificate issuance is pending.
            time.sleep(2)
    config = uvicorn.Config("main:app", host="0.0.0.0", port=settings.STORAGE_PORT,
                            workers=1, proxy_headers=False, access_log=False,
                            ssl_certfile=str(assets.PUBLISHED/tls.generation/"fullchain.pem"),
                            ssl_keyfile=str(assets.PUBLISHED/tls.generation/"privkey.pem"))
    config.load()
    config.ssl = tls.context
    uvicorn.Server(config).run()


if __name__ == "__main__":
    main()
