"""The JavaScript the Chrome backend injects, run for real in a headless Chromium against a tricky local page."""
import json

import pytest

from voicectl.agents.browser import READ_JS, click_js, format_page, media_js, press_js, scroll_js, type_js

pw = pytest.importorskip("playwright.async_api")

PAGE = """
<html><head><title>Test page</title></head><body style="height:4000px">
<h1>Hello</h1>
<form id="f" action="#" onsubmit="document.getElementById('out').textContent='SUBMITTED:'+document.getElementById('q').value; return false;">
  <input id="q" placeholder="Search" aria-label="Search box">
</form>
<!-- a "React-style" controlled input: it ignores plain .value assignment unless an input event follows -->
<input id="ctl" placeholder="Controlled"><div id="mirror"></div>
<input id="enter" placeholder="Enter handler">
<a id="lnk" href="#clicked" target="_blank">Open thing</a>
<button id="btn" onclick="document.getElementById('out').textContent='CLICKED'">Press me</button>
<div id="out"></div>
<div style="display:none"><button>Hidden</button></div>
<video id="v" muted></video>
<script>
  const ctl = document.getElementById('ctl'); let state = '';
  ctl.addEventListener('input', e => { state = e.target.value; document.getElementById('mirror').textContent = state; });
  document.getElementById('enter').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); document.getElementById('out').textContent = 'ENTER:' + e.target.value; }});
</script></body></html>
"""


@pytest.fixture
async def page():
    async with pw.async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
        except Exception as e:
            pytest.skip(f"no chromium: {e}")
        pg = await browser.new_page()
        await pg.set_content(PAGE)
        yield pg
        await browser.close()


async def elements(pg):
    return json.loads(await pg.evaluate(READ_JS))["elements"]


def find(els, label):
    hits = [e["n"] for e in els if label.lower() in e["label"].lower()]
    assert hits, f"no element labelled {label!r} in {[e['label'] for e in els]}"
    return hits[0]


async def test_read_numbers_visible_elements_only_and_formats(page):
    raw = await page.evaluate(READ_JS)
    d = json.loads(raw)
    labels = [e["label"] for e in d["elements"]]
    assert d["title"] == "Test page" and "Hello" in d["text"]
    assert "Press me" in labels and "Open thing" in labels and "Hidden" not in labels      # hidden things are not offered
    assert "[1] input" in format_page(raw) and "Search" in format_page(raw)


async def test_click_works_and_never_opens_a_new_tab(page):
    els = await elements(page)
    assert await page.evaluate(click_js(find(els, "Press me"))) == "OK"
    assert await page.inner_text("#out") == "CLICKED"
    n_pages = len(page.context.pages)
    assert await page.evaluate(click_js(find(els, "Open thing"))) == "OK"
    assert len(page.context.pages) == n_pages                                               # target=_blank was neutralised
    assert await page.evaluate(click_js(999)) == "MISSING"


async def test_typing_reaches_controlled_inputs_and_enter_submits(page):
    els = await elements(page)
    await page.evaluate(type_js(find(els, "Controlled"), "lofi hip hop", False))
    assert await page.inner_text("#mirror") == "lofi hip hop"                               # the framework saw the change
    await page.evaluate(type_js(find(els, "Search"), "piano music", True))
    await page.wait_for_timeout(300)
    assert await page.inner_text("#out") == "SUBMITTED:piano music"                         # form submitted via Enter fallback
    await page.evaluate(type_js(find(els, "Enter handler"), "hello", True))
    assert await page.inner_text("#out") == "ENTER:hello"                                   # a page's own Enter handler wins


async def test_typing_text_with_quotes_and_unicode_is_safe(page):
    els = await elements(page)
    nasty = 'say "hi" \\ it\'s <b>naïve</b> 日本語'
    await page.evaluate(type_js(find(els, "Controlled"), nasty, False))
    assert await page.inner_text("#mirror") == nasty


async def test_scroll_and_press(page):
    await page.evaluate(scroll_js("down", 1)); y1 = await page.evaluate("scrollY")
    await page.evaluate(scroll_js("bottom", 1)); y2 = await page.evaluate("scrollY")
    await page.evaluate(scroll_js("top", 1)); y3 = await page.evaluate("scrollY")
    assert y1 > 100 and y2 > y1 and y3 == 0
    assert await page.evaluate(scroll_js("sideways", 1)) == "BAD_DIRECTION"
    await page.evaluate(press_js("PageDown")); assert await page.evaluate("scrollY") > 100


async def test_media_without_a_video_reports_it(page):
    await page.set_content("<html><body>nothing here</body></html>")
    assert await page.evaluate(media_js("play")) == "NO_MEDIA"
