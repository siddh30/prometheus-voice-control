"""Check that Prometheus can drive your real Chrome:  PYTHONPATH=src .venv/bin/python scripts/check_chrome.py
Opens one tab of its own on example.com, reads it, and closes that tab. (macOS may ask whether Terminal may control Chrome.)"""
import asyncio

from voicectl.agents.chrome import ChromeBrowser, ChromeError


async def main() -> None:
    chrome = ChromeBrowser()
    try:
        page = await chrome.goto("https://example.com")
        print("OK: controlling your Chrome.\n  title:", chrome.last_title, "\n  url:  ", chrome.last_url)
        print("  elements seen:", page.count("\n[") )
        await chrome.close_tab()
        print("  (closed Prometheus's tab)")
    except ChromeError as e:
        print("NOT YET:", e)

asyncio.run(main())
