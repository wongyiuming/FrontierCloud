# Generated native playback fixtures

These generated tones are test inputs, not downloaded media. `tone.mp3` is a
two-second 440-Hz, 44.1-kHz, 128-kbit/s MP3 with no ID3/Xing header or bit reservoir.
Repeating its complete frames creates the large bounded-streaming fixture without
requiring an encoder in CI or in the product. `tone.mp4` is four seconds of a
160-by-90 blue picture at 10 frames/s, H.264 yuv420p and a 660-Hz AAC tone.

Both were generated on the isolated development host with FFmpeg 8.0.1. No
encoder, decoder executable or transformation library is added to product images.
The real input invocation is retained in the development evidence directory.

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| `tone.mp3` | 32600 | `0d53c5e76925690cb3011f83d4b793966e521b9b276247b91437f97d7cca2188` |
| `tone.mp4` | 28310 | `48db325690e59ba3fdeaea715ae5df820fe1c128398e75bb430e3b5dd3e39ca2` |
