"""Render a .mmd file to PNG via mermaid.ink public renderer.

Usage: python scripts/render_mermaid.py <input.mmd> <output.png>
"""
import base64
import json
import sys
import urllib.request
import zlib


def _pako_encode(src: str) -> str:
    """Mermaid.ink's pako format: zlib-compressed then urlsafe base64."""
    state = {"code": src, "mermaid": {"theme": "dark"}}
    raw = json.dumps(state, ensure_ascii=False).encode("utf-8")
    compressed = zlib.compress(raw, 9)
    return base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: render_mermaid.py <input.mmd> <output.png>")
        return 2

    src = open(sys.argv[1], "r", encoding="utf-8").read()
    encoded = _pako_encode(src)

    url = f"https://mermaid.ink/img/pako:{encoded}?type=png&bgColor=0f172a"
    print(f"url length: {len(url)}")
    print(f"fetching: {url[:140]}...")

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (mermaid-render-script)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.reason}")
        print(f"body: {e.read()[:500]!r}")
        return 1

    with open(sys.argv[2], "wb") as f:
        f.write(data)
    print(f"wrote {len(data):,} bytes to {sys.argv[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
