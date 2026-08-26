import pathlib
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
URL = (ROOT / "dashboard.html").as_uri()
SHOTS = pathlib.Path(__file__).resolve().parent

with sync_playwright() as p:
    browser = p.chromium.launch()
    for scheme in ["dark", "light"]:
        page = browser.new_page(viewport={"width": 1400, "height": 1000}, color_scheme=scheme)
        page.goto(URL)
        page.click('.nav-btn[data-view="decision-log"]')
        page.wait_for_timeout(150)
        page.click("#blocked-only-toggle")
        page.wait_for_timeout(150)
        page.screenshot(path=str(SHOTS / f"{scheme}_decision-log_blocked.png"))
        page.close()
    browser.close()
