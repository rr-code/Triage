"""Scratch verification script -- not part of the project, deleted after use.

Renders dashboard.html headlessly at 1400x1000 in both color schemes,
clicks through all six views, screenshots each, checks for horizontal
overflow, and records console/page errors.
"""
import pathlib
import sys

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "dashboard.html"
SHOTS = pathlib.Path(__file__).resolve().parent
URL = DASHBOARD.as_uri()

VIEWS = ["overview", "comparison", "funnel", "decline-codes", "decision-log", "how-it-works"]

results = []

with sync_playwright() as p:
    browser = p.chromium.launch()
    for scheme in ["dark", "light"]:
        context = browser.new_context(viewport={"width": 1400, "height": 1000}, color_scheme=scheme)
        page = context.new_page()

        console_errors = []
        page_errors = []
        page.on("console", lambda msg: console_errors.append(f"[{msg.type}] {msg.text}") if msg.type == "error" else None)
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        page.goto(URL)
        page.wait_for_timeout(200)

        for view in VIEWS:
            if view != "overview":
                page.click(f'.nav-btn[data-view="{view}"]')
                page.wait_for_timeout(150)

            overflow = page.evaluate(
                "document.documentElement.scrollWidth > document.documentElement.clientWidth"
            )
            scroll_w = page.evaluate("document.documentElement.scrollWidth")
            client_w = page.evaluate("document.documentElement.clientWidth")

            shot_path = SHOTS / f"{scheme}_{view}.png"
            page.screenshot(path=str(shot_path))
            full_shot_path = SHOTS / f"{scheme}_{view}_full.png"
            page.screenshot(path=str(full_shot_path), full_page=True)

            results.append(
                {
                    "scheme": scheme,
                    "view": view,
                    "overflow": overflow,
                    "scroll_w": scroll_w,
                    "client_w": client_w,
                    "shot": str(shot_path),
                }
            )

        if console_errors or page_errors:
            results.append({"scheme": scheme, "console_errors": console_errors, "page_errors": page_errors})

        context.close()
    browser.close()

print("=" * 70)
for r in results:
    if "view" in r:
        flag = "OVERFLOW!" if r["overflow"] else "ok"
        print(f"{r['scheme']:6} {r['view']:16} scrollW={r['scroll_w']:5} clientW={r['client_w']:5}  {flag}")
    else:
        print(f"{r['scheme']:6} console_errors={r['console_errors']} page_errors={r['page_errors']}")
print("=" * 70)

any_overflow = any(r.get("overflow") for r in results if "view" in r)
any_errors = any(r.get("console_errors") or r.get("page_errors") for r in results if "view" not in r)
if any_overflow or any_errors:
    sys.exit(1)
