#!/usr/bin/env python3
"""Real Chromium regression checks for the public/Admin UI behavior contract."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:18080").rstrip("/")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
CHROME = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE", "/usr/bin/google-chrome")
ROOT = Path(__file__).resolve().parents[1]
MAINTENANCE_FLAG = ROOT / "data" / ".frontiercloud-maintenance"


def require(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def wait_image(page, selector: str) -> None:
    page.locator(selector).wait_for(state="visible", timeout=15000)
    page.wait_for_function(
        "selector => { const image = document.querySelector(selector); return image && image.complete && image.naturalWidth > 0; }",
        arg=selector,
        timeout=15000,
    )


def home_hit_area_check(browser) -> None:
    context = browser.new_context(viewport={"width": 1792, "height": 860})
    page = context.new_page()
    page.goto(BASE_URL + "/api/v1/media/", wait_until="load")
    wait_image(page, 'img[src*="/brand/logo/entertainment"]')
    geometry = page.evaluate("""
        () => {
            const brand = document.querySelector('.brand-main').getBoundingClientRect();
            const cards = [...document.querySelectorAll('a.card')].map(card => {
                const rect = card.getBoundingClientRect();
                const points = [
                    [rect.left + 18, rect.top + 18],
                    [rect.right - 18, rect.bottom - 18],
                    [rect.left + rect.width / 2, rect.top + rect.height / 2],
                ];
                return {
                    width: rect.width,
                    height: rect.height,
                    hit: points.every(([x, y]) => document.elementFromPoint(x, y)?.closest('a.card') === card),
                };
            });
            return {width: innerWidth, height: innerHeight, brandHeight: brand.height, cards};
        }
    """)
    require(len(geometry["cards"]) == 2, "home must expose exactly two business cards")
    require(geometry["brandHeight"] < geometry["height"] * 0.22, "main entertainment logo must remain a small header")
    for index, card in enumerate(geometry["cards"]):
        require(card["width"] > geometry["width"] * 0.40, f"business card {index} lost horizontal hit area")
        require(card["height"] > geometry["height"] * 0.58, f"business card {index} lost vertical hit area")
        require(card["hit"], f"business card {index} is not clickable across its full visible area")
    ratio = geometry["cards"][0]["width"] / geometry["cards"][1]["width"]
    require(0.94 <= ratio <= 1.06, "music and media cards must split the remaining width evenly")
    context.close()


def logo_cross_page_cache_check(browser) -> None:
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    session = context.new_cdp_session(page)
    session.send("Network.enable")
    versioned = []

    def response_received(event):
        response = event.get("response") or {}
        url = str(response.get("url") or "")
        if "/api/v1/media/brand/logo/music?v=" in url:
            versioned.append({
                "url": url,
                "status": int(response.get("status") or 0),
                "fromDiskCache": bool(response.get("fromDiskCache", False)),
            })

    session.on("Network.responseReceived", response_received)
    page.goto(BASE_URL + "/api/v1/media/", wait_until="load")
    wait_image(page, 'img[src*="/brand/logo/music"]')
    page.wait_for_timeout(300)
    require(versioned, "home did not resolve the music logo to a versioned immutable URL")
    first_url = versioned[-1]["url"]
    require("?v=" in first_url, "logo final URL must contain the content fingerprint")
    first_count = len(versioned)

    page.goto(BASE_URL + "/api/v1/media/music", wait_until="load")
    wait_image(page, 'img[src*="/brand/logo/music"]')
    page.wait_for_timeout(500)
    repeated = versioned[first_count:]
    for item in repeated:
        require(item["url"] == first_url, "the same logo content changed URL between home and secondary page")
        require(
            item["fromDiskCache"] or item["status"] == 304,
            "secondary page transferred the already-loaded versioned logo from the network again",
        )
    context.close()


def tesla_player_layout_check(browser) -> None:
    context = browser.new_context(viewport={"width": 1792, "height": 860})
    page = context.new_page()
    page.goto(
        BASE_URL + "/api/v1/media/music/category?path=music/ui-regression",
        wait_until="domcontentloaded",
    )
    page.wait_for_function("window.FrontierPlayerLayout && document.body.dataset.playerSidebarWidth", timeout=15000)
    initial = page.evaluate("""
        () => ({
            viewport: window.FrontierPlayerLayout.metrics(),
            sidebar: document.querySelector('#playerSidebar').getBoundingClientRect().width,
            player: document.querySelector('#playerSection').getBoundingClientRect().width,
            declared: Number(document.body.dataset.playerSidebarWidth || 0),
        })
    """)
    require(initial["viewport"]["wideLayout"], "Tesla-sized viewport must use the wide player layout")
    require(280 <= initial["sidebar"] <= 330, f"Tesla sidebar is too wide or too narrow: {initial['sidebar']}")
    require(abs(initial["sidebar"] - initial["declared"]) <= 2, "runtime sidebar width and rendered width diverged")
    require(initial["player"] > initial["sidebar"] * 4, "player did not receive the dominant horizontal area")

    page.wait_for_timeout(15750)
    page.wait_for_function("document.body.classList.contains('sidebar-collapsed')", timeout=3000)
    page.wait_for_timeout(300)
    collapsed = page.evaluate("""
        () => ({
            sidebar: document.querySelector('#playerSidebar').getBoundingClientRect().width,
            player: document.querySelector('#playerSection').getBoundingClientRect().width,
            width: innerWidth,
        })
    """)
    require(collapsed["sidebar"] <= 1, "idle sidebar did not return its layout width to the player")
    require(collapsed["player"] >= initial["player"] + initial["sidebar"] * 0.85, "collapsed sidebar space was not reclaimed")

    page.locator("#playerSection").click(position={"x": 200, "y": 200})
    page.wait_for_timeout(350)
    require(page.evaluate("document.body.classList.contains('sidebar-collapsed')"), "player interaction unexpectedly reopened the hidden queue")

    page.locator("#sidebarToggle").click()
    page.wait_for_function("!document.body.classList.contains('sidebar-collapsed')")
    page.wait_for_timeout(300)
    require(page.locator("#playerSidebar").bounding_box()["width"] >= 250, "explicit queue button did not restore the sidebar")

    page.set_viewport_size({"width": 1400, "height": 700})
    page.wait_for_function("document.body.dataset.playerViewport === '1400x700'", timeout=5000)
    resized = page.locator("#playerSidebar").bounding_box()["width"]
    require(250 <= resized <= 285, f"runtime viewport resize did not recompute a compact sidebar: {resized}")

    page.set_viewport_size({"width": 600, "height": 900})
    page.wait_for_function("!window.FrontierPlayerLayout.metrics().wideLayout", timeout=5000)
    require(page.locator("#sidebarToggle").evaluate("element => getComputedStyle(element).display") == "none", "portrait layout must hide the floating queue toggle")
    require(page.locator("#playerSidebar").bounding_box()["width"] >= 560, "portrait queue must return to the full-width stacked layout")
    context.close()


def admin_focus_check(browser) -> None:
    require(bool(ADMIN_KEY), "ADMIN_KEY is required for the Admin browser regression")
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    response = context.request.post(BASE_URL + "/api/v1/media/admin/elevate", form={"token": ADMIN_KEY})
    require(response.ok, f"Admin elevation failed: HTTP {response.status}")
    page = context.new_page()
    page.goto(BASE_URL + "/api/v1/media/admin/", wait_until="domcontentloaded")
    page.locator("#siteAccessPanel .module-heading").wait_for(state="visible", timeout=15000)
    page.locator("#systemVersionPanel").wait_for(state="attached", timeout=15000)
    page.locator("#siteAccessPanel .module-heading").click()
    page.wait_for_function("document.querySelector('#siteAccessPanel').classList.contains('expanded')")
    page.wait_for_function("document.querySelector('#siteAccessState').textContent !== '正在加载'", timeout=10000)
    page.wait_for_timeout(1000)
    layout = page.evaluate("""
        () => {
            const viewport = innerHeight;
            const focused = document.querySelector('#siteAccessPanel').getBoundingClientRect();
            const expanded = [...document.querySelectorAll('.admin-module.expanded')].map(item => item.id || item.dataset.adminModule);
            const others = [...document.querySelectorAll('.admin-module:not(#siteAccessPanel)')].map(item => {
                const rect = item.getBoundingClientRect();
                return {
                    id: item.id || item.dataset.adminModule,
                    visible: Math.max(0, Math.min(viewport, rect.bottom) - Math.max(0, rect.top)),
                };
            });
            return {viewport, focused: {top: focused.top, height: focused.height}, expanded, others};
        }
    """)
    require(layout["expanded"] == ["siteAccessPanel"], f"Admin focus must leave exactly one expanded module: {layout['expanded']}")
    require(layout["focused"]["height"] >= layout["viewport"] - 40, "expanded Admin module does not own the viewport")
    visible_others = [item for item in layout["others"] if item["visible"] > 24]
    require(not visible_others, f"other Admin modules remained in the focused viewport: {visible_others}")
    require(page.locator("#siteEnterMaintenance").count() == 1, "site maintenance enter control is missing")
    require(page.locator("#siteEndMaintenance").count() == 1, "site maintenance exit control is missing")
    context.close()


def maintenance_page_check(browser) -> None:
    subprocess.run(["sudo", "touch", str(MAINTENANCE_FLAG)], check=True)
    try:
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()
        response = page.goto(BASE_URL + "/api/v1/media/", wait_until="domcontentloaded")
        require(response is not None and response.status == 503, "public page did not enter the Nginx maintenance gate")
        require(page.locator("h1").inner_text() == "站点正在维护", "maintenance page headline is missing")
        require("前沿娱乐" in page.locator(".brand").inner_text(), "maintenance page lost the product brand")
        require(page.locator("#retry").is_visible(), "maintenance page retry control is not visible")
        require("Admin → 站点开放状态" in page.locator(".hint").inner_text(), "maintenance page does not point administrators to the control module")

        subprocess.run(["sudo", "rm", "-f", str(MAINTENANCE_FLAG)], check=True)
        with page.expect_navigation(wait_until="domcontentloaded", timeout=10000) as navigation:
            page.locator("#retry").click()
        require(navigation.value.status == 200, "retry did not return to the public site after maintenance ended")
        require(page.locator("a.card").count() == 2, "public home did not recover after maintenance ended")
        context.close()
    finally:
        subprocess.run(["sudo", "rm", "-f", str(MAINTENANCE_FLAG)], check=False)


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=CHROME,
            headless=True,
            args=["--autoplay-policy=no-user-gesture-required"],
        )
        try:
            home_hit_area_check(browser)
            logo_cross_page_cache_check(browser)
            tesla_player_layout_check(browser)
            admin_focus_check(browser)
            maintenance_page_check(browser)
        finally:
            browser.close()
    print("browser-ui-regression-round-1: passed")


if __name__ == "__main__":
    main()
