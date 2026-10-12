import unittest

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app.middleware.html_security import HtmlSecurityMiddleware


class HtmlPolicy(unittest.IsolatedAsyncioTestCase):
    async def test_nonce_varies_and_matches_all_trusted_scripts(self):
        app = FastAPI()
        app.add_middleware(HtmlSecurityMiddleware)
        @app.get('/page')
        async def page():
            return HTMLResponse('<script>window.fixture=true;</script><script src="/static/app.js"></script>')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://4399.com') as client:
            first = await client.get('/page')
            second = await client.get('/page')
        import re
        nonce = re.search("'nonce-([^']+)'", first.headers['content-security-policy'])[1]
        self.assertEqual(first.text.count('nonce="' + nonce + '"'), 2)
        self.assertNotEqual(first.headers['content-security-policy'], second.headers['content-security-policy'])
        self.assertNotIn("script-src 'self' 'unsafe-inline'", first.headers['content-security-policy'])
        self.assertIn("worker-src 'self'", first.headers['content-security-policy'])
        self.assertEqual(int(first.headers['content-length']), len(first.content))
        self.assertIn('microphone=()', first.headers['permissions-policy'])

    async def test_karaoke_microphone_scope_and_json_stream_unchanged(self):
        app = FastAPI()
        app.add_middleware(HtmlSecurityMiddleware)
        @app.get('/karaoke/')
        async def page():
            return HTMLResponse('<script src="/static/karaoke.js"></script>')
        @app.get('/data')
        async def data():
            return {'opaque': True}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://4399.com') as client:
            page = await client.get('/karaoke/')
            data = await client.get('/data')
        self.assertIn('microphone=(self)', page.headers['permissions-policy'])
        self.assertEqual(data.json(), {'opaque': True})
        self.assertNotIn('content-security-policy', data.headers)
