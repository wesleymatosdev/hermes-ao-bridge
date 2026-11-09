"""Theme-system contracts (v0.6.0): five switchable board themes plus the
Nebula Classic default, a header swatch picker, live switching (the theme
class on the root div re-points CSS custom properties — no reload), and
localStorage persistence keyed ``ao-bridge.theme``.

The selection/persistence logic ships as a pure block in dist/index.js
(demarcated by "theme layer: begin/end"), so this suite EXECUTES the real
shipped code under node with a stubbed localStorage instead of re-reading
it as text. The CSS side is contract-checked: every theme id the picker
offers must have a token block and a swatch in dist/style.css, and the
unthemed .ao-bridge base (what a fresh page renders before any choice)
must carry the Nebula tokens — the default look is today's live look.
"""

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard"

INDEX_JS = (DASHBOARD / "dist" / "index.js").read_text()
STYLE_CSS = (DASHBOARD / "dist" / "style.css").read_text()

# Harness: stub window.localStorage, load the shipped theme layer verbatim,
# run the behavior probes, print JSON. `bad_store` throws on every access to
# prove the try/except paths keep the board alive (private-mode Safari etc.).
HARNESS_TEMPLATE = r"""
const store = new Map();
const bad_store = {
  getItem() { throw new Error("denied"); },
  setItem() { throw new Error("denied"); },
};
const window = {
  localStorage: MODE === "ok" ? {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, v),
  } : bad_store,
};
__THEME_BLOCK__
const out = {};
out.normalize_unknown = normalizeTheme("does-not-exist");
out.normalize_null = normalizeTheme(null);
out.normalize_valid = normalizeTheme("aurora");
// v0.8.4 renames: removed ids fall back to the default; renamed ids alias.
out.normalize_removed_waves = normalizeTheme("waves");
out.normalize_removed_full = normalizeTheme("waves-discovery");
out.normalize_legacy_air = normalizeTheme("air");
out.themes_offered = THEMES.map((t) => t.id);
writeStoredTheme("editorial");
out.read_back = readStoredTheme();
writeStoredTheme("garbage");           // normalized before persisting
out.read_back_after_bad_write = readStoredTheme();
if (MODE === "ok") window.localStorage.setItem(THEME_STORAGE_KEY, "nope"); // corrupt storage
out.read_corrupt = readStoredTheme();
out.storage_key = THEME_STORAGE_KEY;
if (MODE === "ok") out.stored_after_bad_write = window.localStorage.getItem(THEME_STORAGE_KEY);
console.log(JSON.stringify(out));
"""


def theme_block():
    m = re.search(r"// === theme layer: begin\b.*?===\n(.*?)// === theme layer: end ===",
                  INDEX_JS, re.S)
    if not m:
        raise AssertionError("theme layer block not found in dist/index.js")
    return m.group(1)


def run_harness(mode):
    with tempfile.TemporaryDirectory(prefix="ao-bridge-theme-test-") as td:
        script = Path(td) / "harness.js"
        script.write_text(HARNESS_TEMPLATE.replace("__THEME_BLOCK__", theme_block())
                          .replace("MODE", json.dumps(mode)))
        proc = subprocess.run(["node", str(script)], capture_output=True, text=True,
                              timeout=30)
    if proc.returncode != 0:
        raise AssertionError(f"theme harness failed: {proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


class ThemeSelectionTest(unittest.TestCase):
    """Executed theme layer (node + stubbed localStorage), not a re-read."""

    def test_unknown_ids_fall_back_to_nebula(self):
        out = run_harness("ok")
        self.assertEqual(out["normalize_unknown"], "nebula")
        self.assertEqual(out["normalize_null"], "nebula")
        self.assertEqual(out["normalize_valid"], "aurora")

    def test_selection_persists_and_reads_back(self):
        out = run_harness("ok")
        self.assertEqual(out["read_back"], "editorial")
        # A garbage write is normalized BEFORE persisting: storage never
        # holds an invalid id from our code (it lands on the default).
        self.assertEqual(out["read_back_after_bad_write"], "nebula")

    def test_corrupt_storage_falls_back_without_crashing(self):
        out = run_harness("ok")
        self.assertEqual(out["read_corrupt"], "nebula")
        self.assertEqual(out["storage_key"], "ao-bridge.theme")

    def test_denied_localstorage_degrades_to_default(self):
        # Private mode / storage-disabled browsers: the board must still work.
        out = run_harness("denied")
        self.assertEqual(out["normalize_unknown"], "nebula")
        self.assertEqual(out["read_back"], "nebula")
        self.assertEqual(out["read_corrupt"], "nebula")

    def test_picker_offers_all_five_themes(self):
        # v0.8.4 rename/removal: nebula, editorial, aurora, glass, sky.
        out = run_harness("ok")
        self.assertEqual(out["themes_offered"],
                         ["nebula", "editorial", "aurora", "glass", "sky"])

    def test_removed_waves_falls_back_to_nebula(self):
        # waves-discovery was REMOVED in v0.8.4. Both persisted spellings
        # (the old id "waves" and the artifact name "waves-discovery") must
        # land cleanly on the default, never break the board.
        out = run_harness("ok")
        self.assertEqual(out["normalize_removed_waves"], "nebula")
        self.assertEqual(out["normalize_removed_full"], "nebula")

    def test_legacy_air_id_aliases_to_sky(self):
        # air was RENAMED to sky in v0.8.4: a persisted "air" keeps the
        # Sky theme via the legacy alias instead of dropping to nebula.
        out = run_harness("ok")
        self.assertEqual(out["normalize_legacy_air"], "sky")


class ThemeCssContractTest(unittest.TestCase):
    """Every offered theme resolves in the stylesheet; the picker can render."""

    def test_every_theme_has_a_token_block(self):
        offered = set(run_harness("ok")["themes_offered"])
        defined = set(re.findall(r"\.ao-theme-([a-z]+)\s*\{", STYLE_CSS))
        self.assertTrue(offered <= defined, f"themes without CSS: {offered - defined}")

    def test_every_theme_has_a_picker_swatch(self):
        offered = set(run_harness("ok")["themes_offered"])
        swatches = set(re.findall(r"\.ao-theme-dot-([a-z]+)\s*\{", STYLE_CSS))
        self.assertTrue(offered <= swatches, f"themes without swatch: {offered - swatches}")

    def test_base_tokens_are_the_nebula_default(self):
        # The root class list is "ao-bridge ao-theme-<id>"; before any choice
        # (and for nebula) the base block on .ao-bridge IS the live look.
        base = re.search(r"\.ao-bridge\s*\{(.*?)\n\}", STYLE_CSS, re.S)
        self.assertIsNotNone(base)
        self.assertIn("--ao-accent: #7c6cff", base.group(1))
        self.assertIn("--ao-page-bg", base.group(1))
        self.assertIn("--ao-bg-panel", base.group(1))

    def test_root_div_carries_the_theme_class(self):
        self.assertIn('className: "ao-bridge ao-theme-" + normalizeTheme(theme)', INDEX_JS)

    def test_token_values_are_engine_proof(self):
        # v0.8.0 regression: waves shipped oklch()/color-mix() inside custom
        # properties. An engine without those functions treats the value as
        # invalid at var-substitution time, so every surface consuming the
        # token went unset — transparent cards, inherited text: the theme
        # rendered "no cards" while nebula (plain hex/rgba) kept working.
        # Tokens must stay plain srgb; component rules may use color-mix
        # (a failed mix there costs one cosmetic declaration, never a token).
        for m in re.finditer(r"\.(ao-bridge|ao-theme-[a-z]+)\s*\{([^}]*)\}", STYLE_CSS):
            block, body = m.group(1), m.group(2)
            for fn in ("oklch(", "color-mix(", "oklab", "lab("):
                self.assertNotIn(fn, body,
                                 f"{block} uses {fn!r} in a token value — "
                                 "precompute it to plain srgb (see the TOKEN RULE "
                                 "in style.css)")


if __name__ == "__main__":
    unittest.main()
