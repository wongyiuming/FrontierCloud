"""Per-response nonces for trusted HTML templates and consistent browser limits."""
from __future__ import annotations

import re
import secrets

_SCRIPT = re.compile(r'<script\b([^>]*)>', re.I)
_NONCE = re.compile(r'\snonce\s*=\s*(?:"[^"]*"|\x27[^\x27]*\x27|[^\s>]+)', re.I)
_MAX_HTML = 1024 * 1024


class HtmlSecurityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        # FileResponse must send readable body chunks through this renderer.
        # A pathsend skips them entirely and would precede our buffered start.
        extensions = dict(scope.get('extensions', {}))
        extensions.pop('http.response.pathsend', None)
        scope = {**scope, 'extensions': extensions}
        nonce = secrets.token_urlsafe(24)
        start = None
        chunks = []
        is_html = False

        async def protected(message):
            nonlocal start, is_html
            if message['type'] == 'http.response.start':
                headers = list(message.get('headers', []))
                is_html = any(k.lower() == b'content-type' and v.lower().startswith(b'text/html') for k, v in headers)
                headers = [(k, v) for k, v in headers if k.lower() not in {b'x-content-type-options', b'referrer-policy', b'permissions-policy'}]
                headers += [(b'x-content-type-options', b'nosniff'), (b'referrer-policy', b'no-referrer')]
                microphone = 'self' if scope.get('path', '') == '/karaoke/' else ''
                headers.append((b'permissions-policy', f'camera=(), microphone=({microphone}), geolocation=(), fullscreen=(self)'.encode()))
                message = {**message, 'headers': headers}
                if not is_html:
                    await send(message)
                    return
                start = message
            elif message['type'] == 'http.response.body' and is_html:
                chunks.append(message.get('body', b''))
                if sum(map(len, chunks)) > _MAX_HTML:
                    raise RuntimeError('Trusted HTML response exceeds the bounded renderer size')
                if message.get('more_body', False):
                    return
                body = b''.join(chunks).decode('utf-8')
                # Page data has already been JSON/HTML escaped by the renderer;
                # no user-supplied raw HTML is passed to this template boundary.
                body = _SCRIPT.sub(lambda match: '<script' + _NONCE.sub('', match[1]) + f' nonce="{nonce}">', body).encode()
                policy = (f"default-src 'self'; script-src 'self' 'nonce-{nonce}'; style-src 'self' 'unsafe-inline'; "
                    "connect-src 'self' https:; media-src 'self' blob: https:; img-src 'self' data: blob:; "
                    "worker-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-src 'none'; "
                    "frame-ancestors 'none'; form-action 'self'")
                headers = [(k, v) for k, v in start['headers'] if k.lower() not in {b'content-length', b'content-security-policy', b'etag'}]
                headers += [(b'content-length', str(len(body)).encode()), (b'content-security-policy', policy.encode())]
                await send({**start, 'headers': headers})
                await send({**message, 'body': body})
            else:
                await send(message)
        await self.app(scope, receive, protected)
