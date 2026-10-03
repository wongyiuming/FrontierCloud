"""Read-only live Direct MP3 probe; optional script override stays in this browser.

Example:
    python tests/direct_audio_browser_probe.py --base-url https://520mall.cc \
        --path music/example --title example --source static/js/audio-continuous-stream.js

The probe uses real catalog/redirect/CORS/media responses. It never uploads or
changes server configuration. No capability URLs or media bytes are saved.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from playwright.sync_api import sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--duration", type=float, default=75)
    parser.add_argument("--bandwidth", type=int, default=30000)
    parser.add_argument("--expect-smooth", action="store_true")
    args = parser.parse_args()
    url = args.base_url.rstrip("/") + "/api/v1/media/music/category?" + urlencode({
        "path": args.path, "include_hidden": "true",
    })
    responses = []
    with sync_playwright() as playwright:
        options = {"headless": True, "args": ["--autoplay-policy=no-user-gesture-required"]}
        if os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"):
            options["executable_path"] = os.environ["PLAYWRIGHT_CHROMIUM_EXECUTABLE"]
        browser = playwright.chromium.launch(**options)
        try:
            page = browser.new_page()

            def catalog(route):
                response = route.fetch()
                data = response.json()
                assert any(item["title"] == args.title for item in data["entries"]), "Track not found"
                data["entries"].sort(key=lambda item: item["title"] != args.title)
                route.fulfill(response=response, json=data)

            def response_seen(response):
                parsed = urlsplit(response.url)
                if parsed.path.startswith("/internal/v1/media/"):
                    responses.append({"host": parsed.hostname, "status": response.status,
                                      "range": response.headers.get("content-range", "")})

            page.route("**/api/v1/media/catalog/media?**", catalog)
            page.on("response", response_seen)
            if args.source:
                source = args.source.read_text(encoding="utf-8")
                page.route("**/static/js/audio-continuous-stream.js*", lambda route: route.fulfill(
                    status=200, content_type="application/javascript", body=source,
                ))
            cdp = page.context.new_cdp_session(page)
            cdp.send("Network.enable")
            if args.bandwidth:
                cdp.send("Network.emulateNetworkConditions", {
                    "offline": False, "latency": 80,
                    "downloadThroughput": args.bandwidth, "uploadThroughput": 1000000,
                })
            page.add_init_script("""
                document.addEventListener('DOMContentLoaded', () => {
                    window.__directProbe = [];
                    const bind = () => {
                        const video = document.querySelector('video');
                        if (!video) return setTimeout(bind, 50);
                        for (const event of ['waiting', 'playing', 'error']) {
                            video.addEventListener(event, () => window.__directProbe.push({
                                event, time: video.currentTime, at: performance.now(),
                            }));
                        }
                    };
                    bind();
                });
            """)
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_function("typeof art !== 'undefined' && art && art.video.currentTime > 0.1", timeout=60000)
            samples = []
            for _ in range(int(args.duration * 4)):
                page.wait_for_timeout(250)
                samples.append(page.evaluate("""() => ({
                    time: art.video.currentTime, ready: art.video.readyState,
                    ahead: window.frontierCloudContinuousAudio.status().buffered_ahead_seconds,
                    quota: window.frontierCloudContinuousAudio.status().quota_wait_count,
                })"""))
            events = page.evaluate("window.__directProbe")
            stalls = [event for event in events if event["event"] == "waiting" and event["time"] > 0.1]
            result = {
                "override": bool(args.source), "title": args.title,
                "bandwidth_bytes_per_second": args.bandwidth,
                "sample_seconds": args.duration, "played_seconds": samples[-1]["time"] - samples[0]["time"],
                "stall_count": len(stalls), "events": events,
                "min_ahead_seconds": min(sample["ahead"] for sample in samples),
                "max_ahead_seconds": max(sample["ahead"] for sample in samples),
                "quota_wait_count": max(sample["quota"] for sample in samples),
                "retry": page.evaluate("window.frontierCloudContinuousFetchRetry.status()"),
                "direct_responses": responses,
            }
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if args.expect_smooth:
                assert not stalls, "Direct playback starved after startup"
                assert result["played_seconds"] >= args.duration * 0.95, "Playback clock did not advance continuously"
                assert not any(event["event"] == "error" for event in events), "Media decode failed"
        finally:
            browser.close()


if __name__ == "__main__":
    main()
