import unittest
from pathlib import Path


class AnonymousWallUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path("static/wall/index.html").read_text(encoding="utf-8")
        cls.javascript = Path("static/js/wall.js").read_text(encoding="utf-8")

    def test_message_must_be_selected_before_two_finger_reveal(self):
        self.assertIn("function selectMessage(messageId)", self.javascript)
        self.assertIn("!state.selectedId", self.javascript)
        self.assertIn("event.touches.length!==2", self.javascript)
        self.assertIn("setTimeout(revealSelected,350)", self.javascript)
        self.assertIn("只能在支持双指触控的移动设备揭示", self.javascript)

    def test_plaintext_is_removed_on_every_leave_signal(self):
        self.assertIn("state.revealedBytes.fill(0)", self.javascript)
        self.assertIn("URL.revokeObjectURL", self.javascript)
        self.assertIn("addEventListener('touchcancel',endHold", self.javascript)
        self.assertIn("addEventListener('pagehide',endHold", self.javascript)
        self.assertIn("addEventListener('blur',endHold", self.javascript)
        self.assertIn("visibilitychange", self.javascript)

    def test_client_encrypts_payload_and_sanitizes_images(self):
        self.assertIn("AES-GCM", self.javascript)
        self.assertIn("crypto.subtle.encrypt", self.javascript)
        self.assertIn("createImageBitmap", self.javascript)
        self.assertIn("canvas.toBlob", self.javascript)
        self.assertIn("20*1024*1024", self.javascript)

    def test_wall_has_no_inline_script_or_unsafe_html_rendering(self):
        self.assertIn('src="/static/js/wall.js"', self.html)
        self.assertNotIn("<script>", self.html)
        self.assertNotIn("innerHTML", self.javascript)
        self.assertIn("textContent", self.javascript)

    def test_proxy_rejects_oversized_wall_uploads_before_multipart_parsing(self):
        nginx = Path("nginx/nginx.conf").read_text(encoding="utf-8")
        self.assertIn("zone=wall_write_per_ip", nginx)
        self.assertIn("location = /api/v1/wall/messages", nginx)
        self.assertIn("client_max_body_size 21M", nginx)
        self.assertIn("proxy_request_buffering on", nginx)


if __name__ == "__main__":
    unittest.main()
