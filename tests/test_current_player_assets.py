"""Executable template and browser-crypto asset consistency for Python pages."""
import hashlib
import json
from pathlib import Path
import unittest

from app.api.v1 import media
from app.core.static_assets import static_asset_url
from app.services import player_directory_label_integrity


ROOT = Path(__file__).resolve().parents[1]


class CurrentPlayerAssetsTests(unittest.TestCase):
    def test_shared_lyric_assets_are_versioned_for_players_and_karaoke(self):
        player_directory_label_integrity.install()
        for name in ('audio-player.html', 'karaoke.html'):
            with self.subTest(template=name):
                content = media.load_html_template(name)
                for asset in ('css/lyrics-window.css', 'js/lyrics-window.js'):
                    self.assertIn(f'"{static_asset_url(asset)}"', content)
                    self.assertNotIn(f'"/static/{asset}"', content)

    def test_player_scripts_are_versioned_ordered_and_not_duplicated(self):
        player_directory_label_integrity.install()
        player_directory_label_integrity.install()
        navigation = f'<script src="{static_asset_url("js/business-navigation.js")}"></script>'
        metadata = f'<script src="{static_asset_url("js/music-metadata.js")}"></script>'
        for name in ('audio-player.html', 'video-player.html'):
            with self.subTest(template=name):
                content = media.load_html_template(name)
                self.assertEqual(content.count(navigation), 1)
                self.assertLess(content.index(navigation), content.index('{{PLAYER_JS_URL}}'))
                if name == 'audio-player.html':
                    self.assertEqual(content.count(metadata), 1)
                    self.assertLess(content.index(metadata), content.index('{{PLAYER_JS_URL}}'))
                else:
                    self.assertNotIn(metadata, content)

    def test_compiled_crypto_bytes_match_the_current_source_manifest(self):
        directory = ROOT / 'static/js'
        manifest = json.loads((directory / 'compiled/media-crypto-build.json').read_text())
        self.assertEqual({entry['file'] for entry in manifest['files']},
                         {'media-crypto-common.js', 'media-crypto.js', 'media-crypto-sw.js'})
        for entry in manifest['files']:
            with self.subTest(asset=entry['file']):
                source = (directory / entry['file']).read_bytes().replace(b'\r\n', b'\n')
                compiled = (directory / 'compiled' / entry['file']).read_bytes().replace(b'\r\n', b'\n')
                self.assertEqual(hashlib.sha256(source).hexdigest(), entry['source_sha256'])
                self.assertEqual(hashlib.sha256(compiled).hexdigest(), entry['compiled_sha256'])
                self.assertEqual(len(source), entry['source_bytes'])
                self.assertEqual(len(compiled), entry['compiled_bytes'])
