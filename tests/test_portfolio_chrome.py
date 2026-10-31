"""Every page loads the portfolio top bar, which the shared Caddy serves at /_portfolio/."""

from pathlib import Path

TAG = '<script src="/_portfolio/chrome.js" defer></script>'
ROOT = Path(__file__).resolve().parents[1]
FILES = ["src/tachometer/server.py", "src/tachometer/thermal.py"]


def test_every_page_loads_the_portfolio_top_bar():
    pages = [p for pattern in FILES for p in sorted(ROOT.glob(pattern))]
    assert pages
    for path in pages:
        text = path.read_text(encoding="utf-8")
        assert text.count("</head>") >= 1, path
        assert text.count(TAG) == text.count("</head>"), f"{path} has a page without the top bar"


def test_pages_take_their_colours_from_the_shared_theme():
    import re

    for name, pages in (("server.py", 2), ("thermal.py", 1)):
        text = (ROOT / "src/tachometer" / name).read_text(encoding="utf-8")
        assert text.count('<link rel="stylesheet" href="/_portfolio/theme.css">') == pages, name
        assert text.count('<script src="/_portfolio/theme.js"></script>') == pages, name
        code = re.sub(r"var\([^)]*\)", "", text)
        lines = [ln for ln in code.splitlines() if "shadow" not in ln]
        assert re.findall(r"(?<![&\w])#[0-9a-fA-F]{3,8}\b|rgba?\(", "\n".join(lines)) == [], name
        assert "prefers-color-scheme" not in text, name
