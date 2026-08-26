import pathlib
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
URL = (ROOT / "dashboard.html").as_uri()

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1400, "height": 1000})
    page.goto(URL)
    page.click('.nav-btn[data-view="decision-log"]')
    page.wait_for_timeout(150)
    info = page.evaluate("""
        () => {
            const wrap = document.querySelector('#view-decision-log .table-scroll');
            const table = wrap.querySelector('table');
            const ths = Array.from(table.querySelectorAll('th')).map(th => th.textContent);
            return {
                wrapClientWidth: wrap.clientWidth,
                tableScrollWidth: table.scrollWidth,
                headers: ths,
                needsScroll: table.scrollWidth > wrap.clientWidth
            };
        }
    """)
    print(info)
    browser.close()
