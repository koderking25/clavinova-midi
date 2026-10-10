"""Anything the page hides with the hidden attribute has to actually leave the screen.

Convertify shipped for four versions with an update banner that could not be hidden. The code
said banner.hidden = true, the property read back true, and the banner sat there anyway, because
.banner set display:flex and any rule of ours beats the browser's own [hidden] { display: none }.
Midify never had the bug only because its stylesheet carries display:none !important for [hidden].

So this checks the thing that matters: for every element the script toggles, hiding it has to win.
"""

import re
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
PAGES = [HERE / "convertify" / "page.html", HERE / "app" / "static" / "index.html"]


def styles(src):
    return "\n".join(re.findall(r"<style>(.*?)</style>", src, re.S)) or src.split("</style>")[0]


def guards_hidden(style):
    """True when the stylesheet itself forces hidden elements off the screen."""
    for m in re.finditer(r"\[hidden\][^{]*\{([^}]*)\}", style):
        body = m.group(1)
        if re.search(r"display:\s*none\s*!important", body):
            return True
    return False


def classes_that_set_display(style):
    out = {}
    for m in re.finditer(r"\.([A-Za-z0-9_-]+)[^{]*\{([^}]*)\}", style, re.S):
        if "display:" in m.group(2):
            out.setdefault(m.group(1), re.search(r"display:\s*([a-z-]+)", m.group(2)).group(1))
    return out


def toggled_ids(src):
    return set(re.findall(r'#([A-Za-z0-9_-]+)"\)\.hidden', src)) | set(
        re.findall(r'id="([A-Za-z0-9_-]+)"[^>]*\shidden[\s>]', src)
    )


def unhideable(src):
    """Ids the script tries to hide that a display rule would keep on screen anyway."""
    style = styles(src)
    if guards_hidden(style):
        return []
    display = classes_that_set_display(style)
    wanted = toggled_ids(src)
    stuck = []
    for tag in re.finditer(r"<(\w+)([^>]*?)>", src):
        attrs = tag.group(2)
        ident = re.search(r'id="([A-Za-z0-9_-]+)"', attrs)
        klass = re.search(r'class="([^"]*)"', attrs)
        if not ident or ident.group(1) not in wanted:
            continue
        for c in klass.group(1).split() if klass else []:
            if c in display:
                stuck.append((ident.group(1), c, display[c]))
                break
    return stuck


class HidingWorks(unittest.TestCase):
    def test_every_page_can_hide_what_it_hides(self):
        for page in PAGES:
            with self.subTest(page=page.name):
                stuck = unhideable(page.read_text())
                self.assertEqual(
                    stuck,
                    [],
                    "\n".join(
                        f"#{i} is hidden in code but .{c} forces display:{d}, so it stays visible. "
                        f"Add [hidden] {{ display: none !important; }} to {page.name}."
                        for i, c, d in stuck
                    ),
                )

    def test_both_pages_carry_the_guard(self):
        # Cheaper to keep the one line than to re-audit every class that sets display.
        for page in PAGES:
            with self.subTest(page=page.name):
                self.assertTrue(
                    guards_hidden(styles(page.read_text())),
                    f"{page.name} needs [hidden] {{ display: none !important; }}",
                )

    def test_the_checker_notices_a_real_break(self):
        # Validate the test on an input with a known answer before trusting it.
        broken = """<style>.banner { display: flex; }</style>
                    <div class="banner" id="b" hidden></div>
                    <script>$("#b").hidden = true;</script>"""
        self.assertEqual(unhideable(broken), [("b", "banner", "flex")])

        fixed = """<style>.banner { display: flex; } [hidden] { display: none !important; }</style>
                   <div class="banner" id="b" hidden></div>
                   <script>$("#b").hidden = true;</script>"""
        self.assertEqual(unhideable(fixed), [])


class PageIsNeverStale(unittest.TestCase):
    """Both apps must tell the browser not to hold on to the page.

    An app that updates itself and then serves a cached page has not really updated. Convertify
    served its page with no cache headers at all, and a browser duly showed the previous version
    after an update had been installed.
    """

    SERVERS = [HERE / "convertify" / "engine.py", HERE / "app" / "server.py"]

    def test_the_page_is_served_with_no_store(self):
        for server in self.SERVERS:
            with self.subTest(server=server.name):
                src = server.read_text()
                # the handler for "/" and the FileResponse that answers it
                m = re.search(r'@app\.get\("/"\)\s*\ndef \w+\(\):(?:.|\n)*?return FileResponse\([^)]*\)',
                              src)
                self.assertIsNotNone(m, f"{server.name}: no handler for the page found")
                self.assertIn("no-store", m.group(0),
                              f"{server.name} serves its page without Cache-Control: no-store, "
                              f"so a browser may show the version from before an update")


if __name__ == "__main__":
    unittest.main(verbosity=2)
