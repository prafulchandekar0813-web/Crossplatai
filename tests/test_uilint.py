from llm_agent import buildcheck, uilint

PLAIN = {"templates/index.html": "<!doctype html><html><body><h1>Todo</h1><ul id=l></ul><script src=a.js></script></body></html>",
         "static/style.css": "body{font-family:sans-serif}"}

RICH_CSS = ":root{--accent:#0f766e;--bg:#fff;--text:#111;--radius:14px}\nbody{font-family:Inter,system-ui,sans-serif}\n" + "\n".join(
    f".c{i}{{padding:{i}px;color:var(--text)}}" for i in range(70)) + """
.btn{min-height:48px}.btn:hover{opacity:.9}
.tabbar{position:fixed;bottom:0;left:0;right:0}
.sidebar{display:none}
@media (min-width: 900px){.tabbar{display:none}.sidebar{display:block}}
@media (prefers-color-scheme: dark){:root{--bg:#111;--text:#eee}}"""
RICH_HTML = ("<!doctype html><html><head><meta name=viewport content='width=device-width'></head><body><nav class='tabbar'><svg></svg></nav>"
             "<aside class='sidebar'></aside><p>Nothing here yet</p></body></html>")


def test_plain_page_gets_specific_design_work():
    notes = uilint.lint(PLAIN)
    text = " ".join(notes)
    assert "very small" in text and ":root" in text and "@media" in text and "dark theme" in text and "icons" in text and "empty state" in text


def test_a_rich_page_for_both_platforms_passes():
    files = {"templates/index.html": RICH_HTML, "static/style.css": RICH_CSS}
    assert uilint.lint(files, ["android", "windows"]) == []


def test_platform_rules_only_apply_when_asked():
    files = {"templates/index.html": RICH_HTML.replace("tabbar", "x").replace("sidebar", "y"), "static/style.css": RICH_CSS.replace("tabbar", "x").replace("sidebar", "y").replace("bottom:0", "top:0")}
    assert uilint.lint(files) == []
    android = " ".join(uilint.lint(files, ["android"]))
    assert "bottom navigation" in android
    assert "side navigation" in " ".join(uilint.lint(files, ["windows"]))


def test_api_only_projects_are_not_judged_and_check_build_reports_design_separately():
    assert uilint.lint({"app.py": "x=1"}) == []
    r = buildcheck.check(dict(PLAIN, **{"app.py": "import os\nprint(os.environ['PORT'])\n", "requirements.txt": "flask\n"}), ["android"])
    assert r["design"] and "design" in r and not any("CSS lines" in w for w in r["warnings"])
