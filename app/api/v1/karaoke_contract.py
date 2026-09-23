from typing import Literal

from pydantic import BaseModel


class KaraokeContext(BaseModel):
    id: str
    title: str
    type: Literal["audio", "video"]
    stream_url: str
    has_lyrics: bool
    lyrics_url: str | None
    cover_url: str | None
