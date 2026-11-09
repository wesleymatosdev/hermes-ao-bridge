"""Browser-level E2E regression gate — PARITY against a minimal reference
daemon.

The fixture daemon (tests/e2e/fixture_server.py, built from
tests/e2e/reference_board.py) serves a small canonical set of AO session rows
covering every board edge. The gate computes the board those rows IMPLY using
the pure model in reference_board.py — archive membership via the AO desktop
predicate extracted from the app.asar in v0.8.3 (kanbanColumn "archive" |
isTerminated | status "terminated"; the daemon's list rows carry no explicit
archive marker, so that predicate IS the desktop truth), column placement via
the documented AO mapping, the honest-state enum for chips, and the
Active-only/Show-all toggle model — then renders the REAL frontend in a real
headless Chrome and asserts the DOM matches 1:1: same card names per column,
same chips, same archive membership, same orchestrator strip. Plus the
theme-visibility and picker-persistence contracts from the original brief.

One contract per shipped regression this gate enforces:
  1. Theme rendering paints in every theme, including with the fragile color
     functions made unparseable (the v0.8.1 oklch/color-mix mechanism).
  2. The rendered board equals the reference board — the v0.8.2 dropped
     toggle, the v0.8.2/v0.8.3 state-honesty bugs, the v0.8.3 vanishing
     orchestrators, and any future derivation drift all break this.
  3. Every theme swatch applies its theme and persists across reload.

Run:  python -m unittest tests.e2e.test_e2e_board   (from the repo root)
Needs a local Chrome/Chromium (CHROME_BIN overrides discovery). No network:
Chrome talks only to the loopback fixture server; React is vendored.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.e2e.cdp_client import Browser, CDPError  # noqa: E402
from tests.e2e.fixture_server import FixtureServer  # noqa: E402
from tests.e2e.reference_board import canonical_rows, expected_board  # noqa: E402

PLUGIN_ROOT = Path(os.environ.get("AO_BRIDGE_E2E_PLUGIN_ROOT") or _REPO_ROOT)
ARTIFACT_DIR = Path(os.environ.get("AO_BRIDGE_E2E_ARTIFACT_DIR")
                    or (Path(tempfile.gettempdir()) / "ao-bridge-e2e-screenshots"))

COLUMN_CLASSES = ("queued", "running", "review", "blocked", "done")

# Board probes, as JS expressions evaluated in the page.
BOARD_READY = ("!!document.querySelector('.ao-board') "
               "|| !!document.querySelector('.ao-board-error')")
CARD_NAMES_EXPR = ("Array.from(document.querySelectorAll('.ao-card-name'))"
                   ".map(function (e) { return e.textContent; })")


class E2EBoardTestBase(unittest.TestCase):
    """One fixture server + one headless Chrome for the whole class."""

    server: FixtureServer
    browser: Browser

    @classmethod
    def setUpClass(cls):
        cls.server = FixtureServer(PLUGIN_ROOT)
        cls.browser = Browser()
        try:
            cls.browser.start()
        except CDPError as err:
            cls.server.close()
            raise unittest.SkipTest(str(err)) from err

    @classmethod
    def tearDownClass(cls):
        cls.browser.stop()
        cls.server.close()

    # -- page helpers -------------------------------------------------------

    def load_board(self, themebreak: bool = False) -> None:
        """Fresh page: cleared board settings, default view (Active only on),
        the board rendered with at least one card."""
        url = self.server.url + ("/?themebreak=1" if themebreak else "/")
        self.browser.goto(url)
        self.browser.eval("try { localStorage.clear(); } catch (e) {}")
        self.browser.goto(url)
        self.browser.wait_for(BOARD_READY, desc="board loaded")
        self.browser.wait_for(
            "document.querySelectorAll('.ao-card').length > 0"
            " || !!document.querySelector('.ao-orch-strip')",
            desc="cards/strips rendered")

    def card_names(self) -> list[str]:
        return self.browser.eval_fn(f"return {CARD_NAMES_EXPR}")

    def show_all(self) -> None:
        """Turn the Active-only toggle OFF (Show all) and wait for the
        control itself to flip. Does NOT wait for cards to appear — on a
        broken tree the whole point may be that they never do, and the parity
        assertion should produce that failure message."""
        self.assertTrue(self.browser.click(".ao-btn-toggle"),
                        "Active-only toggle button not found")
        self.browser.wait_for(
            "!document.querySelector('.ao-btn-toggle').classList.contains('is-on')",
            desc="toggle switched to Show all")

    def column_card_names(self, column_class: str) -> list[str]:
        return self.browser.eval_fn(f"""
            var col = document.querySelector('.ao-column-{column_class}');
            if (!col) return [];
            return Array.from(col.querySelectorAll('.ao-card-name'))
                .map(function (e) {{ return e.textContent; }});
        """)

    def orch_strip_names(self) -> list[str]:
        return self.browser.eval_fn(
            "return Array.from(document.querySelectorAll("
            "'.ao-orch-strip:not(.ao-arch-strip) .ao-orch-name'))"
            ".map(function (e) { return e.textContent; })")

    def arch_strip_names(self) -> list[str]:
        """Archive strip membership (expands the strip when collapsed —
        v0.8.3+; older trees render the rows already expanded)."""
        if self.browser.eval("!!document.querySelector('.ao-arch-toggle')"):
            self.assertTrue(self.browser.click(".ao-arch-toggle"),
                            "archive strip header not found")
            self.browser.wait_for(
                "document.querySelector('.ao-arch-strip')"
                ".classList.contains('is-expanded')",
                desc="archive strip expanded")
        return self.browser.eval_fn(
            "return Array.from(document.querySelectorAll("
            "'.ao-arch-strip .ao-orch-name'))"
            ".map(function (e) { return e.textContent; })")

    def chip_state_of_card(self, card_name: str) -> str | None:
        """The state class of the named card's chip (None: not rendered)."""
        return self.browser.eval_fn(f"""
            var names = Array.from(document.querySelectorAll('.ao-card-name'));
            var el = names.find(function (e) {{ return e.textContent === {card_name!r}; }});
            if (!el) return null;
            var card = el.closest('.ao-card');
            var chip = card ? card.querySelector('[class*="ao-chip-state-"]') : null;
            if (!chip) return null;
            var m = chip.className.match(/ao-chip-state-([a-z_]+)/);
            return m ? m[1] : null;
        """)

    def theme_ids_in_picker(self) -> list[str]:
        ids = self.browser.eval_fn("""
            return Array.from(
                document.querySelectorAll('[class*="ao-theme-dot-"]')
            ).map(function (b) {
                var m = b.className.match(/ao-theme-dot-([a-z]+)/);
                return m ? m[1] : null;
            }).filter(Boolean);
        """)
        self.assertTrue(ids, "theme picker rendered no swatches")
        return ids

    def apply_theme(self, theme_id: str) -> None:
        self.assertTrue(self.browser.click(f".ao-theme-dot-{theme_id}"),
                        f"theme swatch .ao-theme-dot-{theme_id} not found")
        self.browser.wait_for(
            "document.querySelector('.ao-bridge').className.indexOf("
            f"'ao-theme-{theme_id}') !== -1",
            desc=f"theme {theme_id} applied to the board root")

    # -- parity -------------------------------------------------------------

    def assert_board_matches(self, active_only: bool, screenshot: str) -> None:
        """1:1 DOM parity with the reference board the daemon rows imply."""
        expected = expected_board(canonical_rows("unused"), active_only=active_only)

        for column in COLUMN_CLASSES:
            actual = set(self.column_card_names(column))
            self.assertEqual(
                actual, expected["columns"][column],
                f"column {column!r} diverges from the reference daemon "
                f"(expected {sorted(expected['columns'][column])}, "
                f"DOM shows {sorted(actual)})")

        actual_archive = set(self.arch_strip_names())
        self.assertEqual(
            actual_archive, expected["archive"],
            "Archive strip membership diverges from the desktop archive "
            f"predicate over the daemon rows (expected "
            f"{sorted(expected['archive'])}, DOM shows {sorted(actual_archive)})")

        actual_orch = set(self.orch_strip_names())
        self.assertEqual(
            actual_orch, expected["orchestrators"],
            "Orchestrator strip diverges from the daemon rows "
            f"(expected {sorted(expected['orchestrators'])}, "
            f"DOM shows {sorted(actual_orch)})")

        for name in self.card_names():
            self.assertEqual(
                self.chip_state_of_card(name), expected["chips"].get(name),
                f"card {name!r} wears the wrong state chip for its daemon row")
        self.screenshot(screenshot)

    def screenshot(self, name: str) -> Path:
        return self.browser.screenshot(ARTIFACT_DIR / f"{name}.png")


class BoardParityTest(E2EBoardTestBase):
    """The rendered board equals the board the daemon data implies."""

    def test_default_view_matches_reference(self):
        # Fresh page: Active-only ON. Only live turns fill the columns; both
        # strips render in full (they opt out of the toggle).
        self.load_board()
        self.assert_board_matches(active_only=True,
                                  screenshot="parity-default-view")

    def test_show_all_matches_reference(self):
        self.load_board()
        self.show_all()
        # Give the (correct) board a beat to re-render; on a broken tree the
        # parity assertion below names exactly what diverged.
        self.browser.wait_for(
            f"({CARD_NAMES_EXPR}).length > 1",
            timeout=8, desc="cards after Show all")
        self.assert_board_matches(active_only=False,
                                  screenshot="parity-show-all")


class ThemeRenderingTest(E2EBoardTestBase):
    """Contract: a theme must PAINT, on every engine class."""

    def test_cards_are_visible_in_every_theme(self):
        self.load_board()
        themes = self.theme_ids_in_picker()
        for theme_id in themes:
            self.apply_theme(theme_id)
            self.assert_cards_visible()
        self.screenshot("contract1-themes-normal")

    def test_cards_survive_broken_color_functions(self):
        """Engine-proof mode: the exact reproduction the v0.8.1 fix used —
        oklch()/color-mix()/oklab() made unparseable (balanced, so the tokens
        still parse and the failure lands at var-substitution time, where the
        bug lived). Token values must be plain srgb so cards paint on engines
        without those functions."""
        self.load_board(themebreak=True)
        blanked = []
        for theme_id in self.theme_ids_in_picker():
            self.apply_theme(theme_id)
            probes = self.browser.eval_fn(self.card_probe_js())
            if not probes or all(not p["painted"] for p in probes):
                blanked.append(theme_id)
            else:
                self.assert_cards_visible()
        self.screenshot("contract1-themes-engine-proof")
        self.assertEqual(
            blanked, [],
            "themes rendering NO visible cards with broken color functions: "
            f"{blanked} — a token block still carries oklch()/color-mix() "
            "(invalid at var-substitution time on engines without them; "
            "the v0.8.1 TOKEN RULE)")

    def card_probe_js(self) -> str:
        """Per-card visibility probe: effective painted surface and text-color
        contrast for every rendered card. A card paints when its background
        color has alpha OR a background-image (gradient tokens — aurora) is
        set; a poisoned token (invalid at var-substitution time) zeroes BOTH
        — the v0.8.1 failure signature. Gradient stops are averaged for the
        contrast math. Returns [{name, painted, textAlpha, contrast}]."""
        return r"""
        var cctx = document.createElement('canvas').getContext('2d');
        function parseC(v) {
          // Sample the color as pixels: the engine parses v (any syntax it
          // supports — hex, rgba(), oklch(), color()) and getImageData hands
          // back exact 8-bit sRGB. An unsupported/unparsable color leaves the
          // canvas untouched (alpha 0) — the same engine split the v0.8.1 fix
          // targeted, one code path for both old and new engines.
          cctx.clearRect(0, 0, 1, 1);
          cctx.fillStyle = v;
          cctx.fillRect(0, 0, 1, 1);
          var d = cctx.getImageData(0, 0, 1, 1).data;
          return {r: d[0], g: d[1], b: d[2], a: d[3] / 255};
        }
        function lum(c) {
          function f(v) { v/=255; return v <= 0.04045 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4); }
          return 0.2126*f(c.r) + 0.7152*f(c.g) + 0.0722*f(c.b);
        }
        function over(top, bottom) {
          var a = top.a + bottom.a*(1-top.a);
          if (a === 0) return {r:0,g:0,b:0,a:0};
          return {r:(top.r*top.a + bottom.r*bottom.a*(1-top.a))/a,
                  g:(top.g*top.a + bottom.g*bottom.a*(1-top.a))/a,
                  b:(top.b*top.a + bottom.b*bottom.a*(1-top.a))/a, a:a};
        }
        function contrast(l1, l2) {
          var hi = Math.max(l1, l2), lo = Math.min(l1, l2);
          return (hi + 0.05) / (lo + 0.05);
        }
        function gradientAvg(bgImage) {
          var stops = bgImage.match(
            /(?:rgba?|hsl|oklch|lab|oklab|color)\([^)]*\)|#[0-9a-f]{3,8}/gi) || [];
          if (!stops.length) return null;
          var acc = {r:0,g:0,b:0,a:0};
          stops.forEach(function (s) {
            var c = parseC(s);
            acc.r += c.r; acc.g += c.g; acc.b += c.b; acc.a += c.a;
          });
          var n = stops.length;
          return {r:acc.r/n, g:acc.g/n, b:acc.b/n, a:acc.a/n};
        }
        function ownSurface(el) {
          var cs = getComputedStyle(el);
          var c = parseC(cs.backgroundColor);
          if (c.a > 0) return c;
          if (cs.backgroundImage && cs.backgroundImage !== 'none') {
            var g = gradientAvg(cs.backgroundImage);
            if (g && g.a > 0) return g;
          }
          return null;
        }
        return Array.from(document.querySelectorAll('.ao-card')).map(function (card) {
          var nameEl = card.querySelector('.ao-card-name');
          var fg = parseC(nameEl ? getComputedStyle(nameEl).color : '');
          var surface = ownSurface(card);
          var acc = {r:255,g:255,b:255,a:1};
          var cur = card.parentElement;
          while (cur && cur !== document.documentElement) {
            var under = ownSurface(cur);
            if (under) acc = over(under, acc);
            cur = cur.parentElement;
          }
          var effective = surface ? over(surface, acc) : acc;
          return {
            name: nameEl ? nameEl.textContent : card.className,
            painted: !!surface,
            textAlpha: fg.a,
            contrast: contrast(lum(fg), lum(effective))
          };
        });
        """

    def assert_cards_visible(self, min_contrast: float = 2.0) -> list[dict]:
        """Every rendered card paints: a real background surface, visible
        text, sane contrast. Returns the probe for screenshot annotation."""
        probes = self.browser.eval_fn(self.card_probe_js())
        self.assertTrue(probes, "no cards rendered to probe")
        for p in probes:
            self.assertTrue(
                p["painted"],
                f"card {p['name']!r} paints nothing — its background color AND "
                "image are unset (a poisoned/unset --ao-bg-card token)")
            self.assertGreater(p["textAlpha"], 0,
                               f"card {p['name']!r} text is transparent")
            self.assertGreaterEqual(
                p["contrast"], min_contrast,
                f"card {p['name']!r} contrast {p['contrast']:.2f} — unreadable")
        return probes


class ThemePickerTest(E2EBoardTestBase):
    """Contract: every swatch applies its theme and persists."""

    def test_every_swatch_applies_its_theme_and_persists(self):
        self.load_board()
        themes = self.theme_ids_in_picker()
        root_class = "document.querySelector('.ao-bridge').className"
        for theme_id in themes:
            self.apply_theme(theme_id)
            stored = self.browser.eval(
                "localStorage.getItem('ao-bridge.theme')")
            self.assertEqual(
                stored, theme_id,
                f"swatch {theme_id!r} did not persist to localStorage")
        # Reload: the last choice must survive the round-trip.
        last = themes[-1]
        self.browser.goto(self.server.url + "/")
        self.browser.wait_for(BOARD_READY, desc="board reloaded")
        self.assertIn(f"ao-theme-{last}", self.browser.eval(root_class),
                      f"theme {last!r} did not survive a reload")
        self.screenshot("contract5-theme-persist")


if __name__ == "__main__":
    unittest.main()
