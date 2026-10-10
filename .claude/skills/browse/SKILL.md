---
name: browse
description: Operate websites like a person at a browser (open pages, search, click, fill forms, read results) using the browser_* tools. Use for any task that needs a real website.
---
# Browsing

1. `browser_goto` the site, then read the returned TEXT and numbered ELEMENTS.
2. Act by element number with `browser_click` / `browser_type` (set `submit` to press Enter). Always pass a `description` saying what you are clicking or typing.
3. After each action the page is re-read for you. If an element number is missing, call `browser_read`.
4. If you hit a login wall, a CAPTCHA, or 2FA, stop and tell the user to complete it in the open browser window, then continue when they say so.
5. Prefer the site's own search box over guessing URLs. Scroll with `browser_press` (PageDown) when results continue below.
6. Extract concrete facts (titles, companies, links, dates) rather than describing the page.
