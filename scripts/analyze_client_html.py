#!/usr/bin/env python3
"""Analyze /client HTML served locally or from file."""
import re
import ssl
import sys
import urllib.request

def analyze(html: str) -> None:
    print("bytes:", len(html.encode("utf-8")))
    print("soundboardGrid:", "soundboardGrid" in html)
    print("section-soundboard:", "section-soundboard" in html)
    print("section active count:", len(re.findall(r'class="section active"', html)))
    print("script open:", html.count("<script"))
    print("script close:", html.count("</script>"))
    # main structure
    main_open = html.count("<main")
    main_close = html.count("</main>")
    print("main tags:", main_open, main_close)
    # first 500 chars after body
    i = html.find("<body")
    print("body snippet:", repr(html[i : i + 200]))
    # section-soundboard snippet
    j = html.find("section-soundboard")
    print("soundboard section snippet:", repr(html[j - 50 : j + 120]) if j >= 0 else "MISSING")
    # check for broken script - unclosed template in early part
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL | re.IGNORECASE)
    print("script blocks:", len(scripts))
    for idx, s in enumerate(scripts[:3]):
        print(f"  script[{idx}] len={len(s)} starts:", repr(s[:80]))
    # hash handler
    if "g1ApplyClientHash" in html:
        print("has g1ApplyClientHash: yes")
    if "g1ActivateClientSection" in html:
        print("g1ActivateClientSection count:", html.count("g1ActivateClientSection"))

if __name__ == "__main__":
    if len(sys.argv) > 1:
        analyze(open(sys.argv[1], encoding="utf-8").read())
    else:
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen("https://127.0.0.1:8081/client", context=ctx, timeout=15) as r:
            analyze(r.read().decode("utf-8", errors="replace"))
