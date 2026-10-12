import unittest
import re
import tempfile
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from starlette.staticfiles import StaticFiles

from app.middleware.html_security import HtmlSecurityMiddleware


class HtmlPolicy(unittest.IsolatedAsyncioTestCase):
    async def file_response(self, path, *, method='GET', headers=()):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'page.html').write_text('<script>window.fixture=true;</script>',encoding='utf-8')
            (root/'clip.bin').write_bytes(b'0123456789')
            app = FastAPI()
            app.add_middleware(HtmlSecurityMiddleware)
            app.mount('/static/media',StaticFiles(directory=root))
            extensions = {'http.response.pathsend':{},'unrelated.extension':{}}
            scope = {'type':'http','asgi':{'version':'3.0'},'http_version':'1.1',
                'method':method,'scheme':'https','path':'/static/media/'+path,
                'raw_path':('/static/media/'+path).encode(),'query_string':b'',
                'root_path':'','headers':list(headers),'extensions':extensions,
                'server':('4399.com',443),'client':('127.0.0.1',1234)}
            messages = []
            async def receive():
                return {'type':'http.request','body':b'','more_body':False}
            async def send(message):
                messages.append(message)
            await app(scope,receive,send)
            self.assertEqual(extensions,{'http.response.pathsend':{},'unrelated.extension':{}})
            self.assertEqual(messages[0]['type'],'http.response.start')
            self.assertTrue(all(m['type']=='http.response.body' for m in messages[1:]))
            return messages[0],b''.join(m.get('body',b'') for m in messages[1:])

    async def test_real_html_file_response_with_pathsend_extension_is_nonce_rendered(self):
        start, body = await self.file_response('page.html')
        self.assertEqual(start['status'],200)
        headers = dict(start['headers'])
        nonce = re.search(b"'nonce-([^']+)'",headers[b'content-security-policy'])[1]
        self.assertIn(b'nonce="'+nonce+b'"',body)
        self.assertEqual(int(headers[b'content-length']),len(body))
        self.assertEqual(headers[b'x-content-type-options'],b'nosniff')
        self.assertNotIn(b'etag',headers)

    async def test_head_file_response_still_sends_headers_before_empty_body(self):
        start, body = await self.file_response('page.html',method='HEAD')
        self.assertEqual(start['status'],200)
        self.assertEqual(body,b'')
        self.assertIn(b'content-security-policy',dict(start['headers']))

    async def test_non_html_file_range_preserves_bytes_and_range_headers(self):
        start, body = await self.file_response('clip.bin',headers=[(b'range',b'bytes=2-5')])
        self.assertEqual(start['status'],206)
        self.assertEqual(body,b'2345')
        headers = dict(start['headers'])
        self.assertEqual(headers[b'content-range'],b'bytes 2-5/10')
        self.assertEqual(headers[b'content-length'],b'4')
        self.assertNotIn(b'content-security-policy',headers)

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
