"""Keep player template integrity fixes centralized and cache-safe."""
from __future__ import annotations

from app.api.v1 import media
from app.core.static_assets import static_asset_url


PLAYER_TEMPLATES = frozenset({"audio-player.html", "video-player.html"})
LEGACY_LABEL = "<span>四大发明</span>"
LEGACY_CONTINUOUS_STREAM_SCRIPT = '<script src="/static/js/audio-continuous-stream.js"></script>'


def install() -> None:
    if getattr(media, "_player_directory_label_integrity_installed", False):
        return

    original_load_html_template = media.load_html_template

    def load_html_template(filename: str) -> str:
        content = original_load_html_template(filename)
        if filename not in PLAYER_TEMPLATES:
            return content
        content = content.replace(
            LEGACY_LABEL,
            '<span id="playerDirectoryLabel">当前目录</span>',
            1,
        )
        if filename == "audio-player.html":
            continuous_stream_url = static_asset_url("js/audio-continuous-stream.js")
            content = content.replace(
                LEGACY_CONTINUOUS_STREAM_SCRIPT,
                f'<script src="{continuous_stream_url}"></script>',
                1,
            )
        script_url = static_asset_url("js/player-directory-label.js")
        script_tag = f'<script src="{script_url}"></script>'
        if script_tag not in content:
            content = content.replace("</body>", f"{script_tag}\n</body>")
        return content

    media.load_html_template = load_html_template
    media._player_directory_label_integrity_installed = True
