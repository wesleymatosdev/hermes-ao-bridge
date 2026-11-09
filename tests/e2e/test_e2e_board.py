"""Browser-level E2E regression gate — the rendered board in a real Chrome.

Unit tests run pieces; four shipped bugs (v0.8.0–v0.8.3) stayed green through
all of them because none asserted what a user actually SEES. This lane renders
the REAL frontend (dashboard/dist/index.js + style.css, served like the
dashboard serves them) in a real headless Chrome over CDP, against the REAL
backend router pointed at a fixture daemon — and asserts user-visible
contracts, one per shipped bug:

  1. Theme rendering — for EVERY theme the picker offers, board cards are
     actually visible in Chrome (computed card background not transparent,
     text contrast sane) — including with the fragile color functions made
     unparseable, the exact mechanism that blanked non-default themes on
     engines without oklch()/color-mix() (v0.8.1).
  2. Show-all toggle — switching Active-only off changes column card counts
     (the v0.8.2 dropped-toggle bug made it a no-op).
  3. State honesty — exited sessions never wear the Archived chip, and the
     Archive strip contains only sessions archived by the daemon's real
     semantics (isTerminated / status terminated; v0.8.2/v0.8.3).
  4. Orchestrators — orchestrator sessions appear on the board (the
     orchestrator strip renders with Active-only on; v0.8.3).
  5. Theme picker — every swatch applies its theme (theme class on the board
     root) and persists across a reload (localStorage).

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

PLUGIN_ROOT = Path(os.environ.get("AO_BRIDGE_E2E_PLUGIN_ROOT") or _REPO_ROOT)
ARTIFACT_DIR = Path(os.environ.get("AO_BRIDGE_E2E_ARTIFACT_DIR")
                    or (Path(tempfile.gettempdir()) / "ao-bridge-e2e-screenshots"))

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
            "document.querySelectorAll('.ao-card').length > 0",
            desc="cards rendered")
        self.browser.wait_for(
            "!!document.querySelector('.ao-orch-strip') "
            "|| document.querySelectorAll('.ao-card').length > 0",
            desc="strips/cards settled")

    def card_names(self) -> list[str]:
        return self.browser.eval_fn(f"return {CARD_NAMES_EXPR}")

    def show_all(self) -> None:
        """Turn the Active-only toggle OFF (Show all) and wait for the
        control itself to flip. Does NOT wait for cards to appear — on a
        broken tree the whole point may be that they never do, and each
        contract's own assertion should produce that failure message."""
        self.assertTrue(self.browser.click(".ao-btn-toggle"),
                        "Active-only toggle button not found")
        self.browser.wait_for(
            "!document.querySelector('.ao-btn-toggle').classList.contains('is-on')",
            desc="toggle switched to Show all")

    def wait_card_visible(self, name: str, timeout: float = 8.0) -> None:
        self.browser.wait_for(
            f"({CARD_NAMES_EXPR}).indexOf({name!r}) !== -1",
            timeout=timeout,
            desc=f"card {name!r} visible on the board")

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

    def chip_of_card(self, card_name: str) -> dict:
        """The state chip on the named card: {class, text}."""
        out = self.browser.eval_fn(f"""
            var names = Array.from(document.querySelectorAll('.ao-card-name'));
            var el = names.find(function (e) {{ return e.textContent === {card_name!r}; }});
            if (!el) return null;
            var card = el.closest('.ao-card');
            var chip = card ? card.querySelector('[class*="ao-chip-state-"]') : null;
            if (!chip) return null;
            var m = chip.className.match(/ao-chip-state-([a-z_]+)/);
            return {{ state: m ? m[1] : null, text: chip.textContent }};
        """)
        self.assertIsNotNone(out, f"card {card_name!r} not found or has no state chip")
        return out

    def column_card_names(self, column_class: str) -> list[str]:
        return self.browser.eval_fn(f"""
            var col = document.querySelector('.ao-column-{column_class}');
            if (!col) return [];
            return Array.from(col.querySelectorAll('.ao-card-name'))
                .map(function (e) {{ return e.textContent; }});
        """)

    def screenshot(self, name: str) -> Path:
        return self.browser.screenshot(ARTIFACT_DIR / f"{name}.png")


class ThemeRenderingTest(E2EBoardTestBase):
    """Contract 1 (the v0.8.1 bug): a non-default theme must still PAINT."""

    def test_cards_are_visible_in_every_theme(self):
        self.load_board()
        themes = self.theme_ids_in_picker()
        for theme_id in themes:
            self.apply_theme(theme_id)
            self.assert_cards_visible()
        self.screenshot("contract1-themes-normal")

    def test_cards_survive_broken_color_functions(self):
        """Engine-proof mode: the exact reproduction the v0.8.1 fix used —
        oklch()/color-mix()/oklab() made unparseable. Token values must be
        plain srgb so cards paint even on engines without those functions."""
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


class ShowAllToggleTest(E2EBoardTestBase):
    """Contract 2 (the v0.8.2 bug): Show all must actually show all."""

    def test_toggling_off_active_only_changes_column_counts(self):
        self.load_board()
        # Default view: Active only. The fixture's non-working cards
        # (idle/exited/awaiting) must be hidden, the working card shown.
        self.assertIn("working-worker", self.column_card_names("running"))
        running_before = len(self.column_card_names("running"))
        self.assertEqual(running_before, 1,
                         "Active-only default should keep just the live turn "
                         f"in Running, got {running_before}")
        self.assertNotIn("idle-worker", self.column_card_names("running"))

        self.show_all()
        self.wait_card_visible("idle-worker")

        running_after = self.column_card_names("running")
        self.assertGreater(
            len(running_after), running_before,
            "Show all did not change Running's card count — the toggle is a "
            "no-op (the v0.8.2 dropped-toggle bug)")
        for name in ("idle-worker", "exited-worker"):
            self.assertIn(name, running_after, f"{name} never returns with Show all")
        self.assertIn("awaiting-worker", self.column_card_names("done"))
        self.screenshot("contract2-show-all")


class StateHonestyTest(E2EBoardTestBase):
    """Contract 3 (v0.8.2/v0.8.3): chips and the Archive strip tell the truth."""

    def test_exited_session_never_wears_archived(self):
        self.load_board()
        self.show_all()
        chip = self.chip_of_card("exited-worker")
        self.assertEqual(chip["state"], "exited",
                         f"an exited session wears chip state {chip['state']!r} "
                         "— 'Exited' is its own honest state, 'Archived' is "
                         "reserved for termination (v0.8.2)")
        self.assertEqual(chip["text"], "Exited")
        self.assertIn("exited-worker", self.column_card_names("running"),
                      "an exited session is resumable and stays on its column")
        self.screenshot("contract3-exited-chip")

    def test_archive_strip_holds_only_terminated_sessions(self):
        self.load_board()
        self.show_all()
        # v0.8.3+ renders the strip collapsed; older trees render the rows
        # expanded. Either way the membership contract is the same.
        if self.browser.eval("!!document.querySelector('.ao-arch-toggle')"):
            self.assertTrue(self.browser.click(".ao-arch-toggle"),
                            "archive strip header not found")
            self.browser.wait_for(
                "document.querySelector('.ao-arch-strip').classList.contains('is-expanded')",
                desc="archive strip expanded")
        rows = self.browser.eval_fn(
            "return Array.from(document.querySelectorAll("
            "'.ao-arch-strip .ao-orch-name'))"
            ".map(function (e) { return e.textContent; })")
        self.assertEqual(
            sorted(rows), ["orch-dead", "terminated-worker"],
            "Archive strip membership must be exactly the sessions archived "
            "by the daemon's semantics (isTerminated / status terminated) — "
            "nothing alive, nothing exited")
        self.screenshot("contract3-archive-strip")


class OrchestratorTest(E2EBoardTestBase):
    """Contract 4 (the v0.8.3 bug): orchestrators render on the board."""

    def test_orchestrator_strip_renders_with_active_only_on(self):
        self.load_board()  # default view: Active-only ON, no orchestrator mid-turn
        names = self.browser.eval_fn(
            "return Array.from(document.querySelectorAll("
            "'.ao-orch-strip:not(.ao-arch-strip) .ao-orch-name'))"
            ".map(function (e) { return e.textContent; })")
        self.assertIn(
            "orch-live", names,
            "no live orchestrator renders on the board with Active-only on — "
            "the orchestrator strip must not be gated by the activity toggle "
            "(the v0.8.3 vanishing-orchestrators bug)")
        self.assertNotIn(
            "orch-live", self.card_names(),
            "orchestrators coordinate; they never render as column cards")
        self.screenshot("contract4-orchestrators")


class ThemePickerTest(E2EBoardTestBase):
    """Contract 5: every swatch applies its theme and persists."""

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
