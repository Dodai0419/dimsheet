import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import dimsheet as ds  # noqa: E402

FIX = ROOT / "tests" / "fixtures"


def load(name):
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def values(rows, title):
    return {v: (c, n) for c, n, v in rows[title]}


class Variables(unittest.TestCase):
    """The codes on the drawing carry the values from the CSS of examples/*.html, nothing invented."""

    def test_card(self):
        tabs, rows, _ = ds.variables(load("card"))
        sp = values(rows, "Spacing")
        self.assertEqual(sp[24], ("S1", "container pad-top"))      # .card padding: 24px
        self.assertEqual(sp[16][1], "container stack gap")          # .card gap: 16px
        self.assertIn(12, sp)                                       # .head gap: 12px
        self.assertIn(8, sp)                                        # .foot gap: 8px
        self.assertEqual(tabs["R"][20], "R1")                       # border-radius: 20px
        self.assertEqual(values(rows, "Size")[40], ("Z1", "lead size"))  # 40 x 40 avatar
        self.assertEqual(set(values(rows, "Radius")), {20, 12, 8, 18, 10})

    def test_nested_corner_formula(self):
        _, _, formulas = ds.variables(load("player"))  # radius 28, border 1, padding 8, art radius 19
        self.assertEqual(formulas, ["R2 = R1 − B − S1 = 19"])

    def test_space_between_gaps_are_coded(self):
        tabs, _, _ = ds.variables(load("player"))
        self.assertIn(77, tabs["S"])  # three buttons spread across 316 px

    def test_shadow(self):
        self.assertEqual(ds.shadow_parts("rgba(0, 0, 0, 0.08) 0px 8px 24px 0px"), (8.0, 24.0, 0.0))
        self.assertIsNone(ds.shadow_parts(""))

    def test_slot_words(self):
        card = load("card")
        head, body, foot = card["kids"]
        self.assertEqual(ds.slot(body, card), ("CONTENT SLOT", "FILL"))
        self.assertEqual(ds.slot(head["kids"][0], head), ("LEAD", "HUG"))
        self.assertEqual(ds.slot(head["kids"][1], head), ("TITLE SLOT", "FLEX"))
        self.assertEqual(ds.slot(foot["kids"][1], foot), ("ACTION", "HUG"))


class Drawing(unittest.TestCase):
    def test_static_and_motion_svgs_are_valid_and_deterministic(self):
        for name in ("card", "player", "dialog"):
            a = ds.draw(load(name), name.title(), f"{name}.html", "2026-10-28")
            b = ds.draw(load(name), name.title(), f"{name}.html", "2026-10-28")
            self.assertEqual(a.svg(), b.svg())
            static, motion = ET.fromstring(a.svg()), a.svg(motion=True)
            ET.fromstring(motion)
            self.assertNotIn("@keyframes", a.svg())
            self.assertEqual(len(re.findall(r"@keyframes d\d+", motion)), a.phases)
            self.assertIn("prefers-reduced-motion", motion)
            texts = " ".join(t.text or "" for t in static.iter("{http://www.w3.org/2000/svg}text"))
            for word in ("PLAN VIEW", "SPACING", "RADIUS", "EXPLODED VIEW", "NOTES:", name.upper()):
                self.assertIn(word, texts)

    def test_unknown_limits_are_tbd(self):
        svg = ds.draw(load("player"), "Player", "player.html", "2026-10-28").svg()
        self.assertEqual(svg.count("[TBD]"), 3)  # no min-width / max-width in player.html, plus note 6

    def test_cli_redraws_from_json_without_a_browser(self):
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run([sys.executable, str(ROOT / "dimsheet.py"), "--json", str(FIX / "card.json"), "-o", d,
                                "--date", "2026-10-28"], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("17 variables", r.stdout)
            self.assertTrue(Path(d, "sheet.svg").exists() and Path(d, "sheet-motion.svg").exists())

    def test_port_file_read_never_raises(self):
        self.assertEqual(ds.Chrome._port_text(Path(tempfile.gettempdir(), "no-such-dir-dimsheet", "DevToolsActivePort")), "")


@unittest.skipUnless(ds.find_chrome(), "no Chrome/Edge on this machine")
class Browser(unittest.TestCase):
    def test_measured_values_match_the_css(self):
        t = ds.measure(str(ROOT / "examples" / "card.html"), ".card", 1280, ds.find_chrome())
        self.assertEqual((t["w"], t["pad"], t["border"], t["radius"], t["gap"]), (420, [24, 24, 24, 24], 1, 20, 16))
        self.assertEqual((t["minW"], t["maxW"]), ("320px", "680px"))
        self.assertEqual([k["tag"] for k in t["kids"]], ["header", "p", "footer"])
        avatar = t["kids"][0]["kids"][0]
        self.assertEqual((avatar["x"], avatar["y"], avatar["w"], avatar["h"], avatar["radius"]), (25, 27, 40, 40, 12))  # centred in the 44 px header row

    def test_missing_selector_is_a_clear_error(self):
        with self.assertRaises(SystemExit) as e:
            ds.measure(str(ROOT / "examples" / "card.html"), ".nope", 1280, ds.find_chrome())
        self.assertIn("nothing matches", str(e.exception))


if __name__ == "__main__":
    unittest.main()
