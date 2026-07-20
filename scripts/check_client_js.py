#!/usr/bin/env python3
"""Extract client JS and check for obvious syntax issues."""
import re
import ssl
import sys
import urllib.request


def main():
    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen("https://127.0.0.1:8081/client", context=ctx, timeout=20) as r:
        html = r.read().decode("utf-8", errors="replace")

    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL | re.IGNORECASE)
    print("scripts:", len(scripts))
    for i, s in enumerate(scripts):
        print(f"script[{i}] len={len(s)}")
        # check unbalanced braces in rough way
        opens = s.count("{")
        closes = s.count("}")
        print(f"  braces {{ {opens} }} {closes} diff={opens-closes}")
        # common break: </script> inside string
        if "</script>" in s.lower():
            print("  WARNING: </script> inside script block")
        # check for invalid apostrophe breaks in strings - hard

    big = scripts[-1] if scripts else ""
    # try node if available
    import subprocess
    import tempfile
    import os

    path = os.path.join(tempfile.gettempdir(), "g1_client_check.js")
    with open(path, "w", encoding="utf-8") as f:
        f.write(big)
    for cmd in [["node", "--check", path], ["nodejs", "--check", path]]:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            print("node check:", " ".join(cmd), "exit", r.returncode)
            if r.stdout:
                print(r.stdout[:500])
            if r.stderr:
                print(r.stderr[:800])
            break
        except FileNotFoundError:
            continue
    else:
        print("node not available for syntax check")


if __name__ == "__main__":
    main()
