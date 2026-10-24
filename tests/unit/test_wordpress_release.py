"""Offline archive allowlist and PHP entrypoint tests; never connect to WordPress."""
import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from scripts.package_wordpress import create_package, FILES

ROOT = Path(__file__).resolve().parents[2]


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.plugin = self.root / "plugin"
        (self.plugin / "assets").mkdir(parents=True)
        (self.plugin / "package.json").write_text(json.dumps({"version": "1.4.0"}), encoding="utf-8")
        (self.plugin / "tayune_assistant.php").write_text("<?php\n/**\n * Version: 1.4.0\n */\n", encoding="utf-8")
        (self.plugin / "assets/tea_companion.js").write_text("/* fixture bundle */", encoding="utf-8")
        (self.plugin / "assets/tea_companion.js.LEGAL.txt").write_text("fixture license", encoding="utf-8")
        self.output = self.root / "release.zip"

    def tearDown(self):
        self.directory.cleanup()

    def test_only_allowlisted_files_and_license_are_packaged(self):
        for name in (".env", "reference_1.py", "private.sqlite3", "old_asset.js"):
            (self.plugin / name).write_text("excluded fixture", encoding="utf-8")
        report = create_package(self.plugin, self.output)
        with ZipFile(self.output) as archive:
            self.assertEqual(set(archive.namelist()), {"tayune-tea-companion/" + name for name in FILES})
            self.assertIsNone(archive.testzip())
        self.assertEqual(report["sha256"], hashlib.sha256(self.output.read_bytes()).hexdigest())
        self.assertEqual(report["version"], "1.4.0")

    def test_mismatched_versions_are_rejected(self):
        (self.plugin / "package.json").write_text('{"version":"1.5.0"}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "versions differ"):
            create_package(self.plugin, self.output)
        self.assertFalse(self.output.exists())

    def test_invalid_version_is_rejected(self):
        (self.plugin / "package.json").write_text('{"version":"../private"}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Invalid plugin version"):
            create_package(self.plugin, self.output)

    def test_missing_license_is_rejected(self):
        (self.plugin / "assets/tea_companion.js.LEGAL.txt").unlink()
        with self.assertRaisesRegex(ValueError, "Missing or empty"):
            create_package(self.plugin, self.output)

    def test_empty_bundle_is_rejected(self):
        (self.plugin / "assets/tea_companion.js").write_text("", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Missing or empty"):
            create_package(self.plugin, self.output)

    def test_existing_release_is_not_overwritten(self):
        create_package(self.plugin, self.output)
        previous = self.output.read_bytes()
        with self.assertRaises(FileExistsError):
            create_package(self.plugin, self.output)
        self.assertEqual(self.output.read_bytes(), previous)


@unittest.skipUnless(shutil.which("php"), "PHP CLI is required for entrypoint verification")
class PHPEntrypointTests(unittest.TestCase):
    def entrypoint(self, mode):
        source = r"""
define('ABSPATH', '/fixture/');
$GLOBALS['mode'] = $argv[2];
$GLOBALS['captured'] = array();
function add_action($name, $callback) { $GLOBALS['callback'] = $callback; }
function is_checkout() { return $GLOBALS['mode'] === 'checkout'; }
function is_account_page() { return $GLOBALS['mode'] === 'account'; }
function is_front_page() { return $GLOBALS['mode'] === 'home'; }
function plugin_dir_url($file) { return 'https://tayunetea.com/wp-content/plugins/tayune-tea-companion/'; }
function wp_enqueue_script($handle, $url, $dependencies, $version, $footer) {
    $GLOBALS['captured']['script'] = array('url'=>$url, 'version'=>$version, 'footer'=>$footer);
}
function wp_add_inline_script($handle, $script, $position) {
    $GLOBALS['captured']['inline'] = $script;
    $GLOBALS['captured']['position'] = $position;
}
function wp_json_encode($value) { return json_encode($value); }
function untrailingslashit($value) { return rtrim($value, '/'); }
function home_url($path) { return 'https://tayunetea.com' . $path; }
require $argv[1];
($GLOBALS['callback'])();
echo json_encode($GLOBALS['captured']);
"""
        result = subprocess.run(["php", "-r", source, str(ROOT / "deploy/wordpress/tayune_assistant.php"), mode],
                                check=True, capture_output=True, text=True)
        return json.loads(result.stdout)

    def test_public_page_uses_matching_version_and_only_public_config(self):
        result = self.entrypoint("public")
        version = json.loads((ROOT / "deploy/wordpress/package.json").read_text())["version"]
        self.assertEqual(result["script"]["version"], version)
        self.assertTrue(result["script"]["footer"])
        self.assertEqual(result["position"], "before")
        config = json.loads(result["inline"].removeprefix("window.TAYUNE_TEA_COMPANION = ").removesuffix(";"))
        self.assertEqual(set(config), {"apiBase", "chatEnabled", "homePage", "contactUrl", "privacyUrl", "shopUrl"})
        self.assertEqual(config["apiBase"], "https://agent.tayunetea.com")
        self.assertTrue(config["chatEnabled"])
        self.assertFalse(config["homePage"])

    def test_checkout_page_is_excluded(self):
        self.assertEqual(self.entrypoint("checkout"), [])

    def test_account_page_is_excluded(self):
        self.assertEqual(self.entrypoint("account"), [])

    def test_home_page_passes_layout_flag(self):
        result = self.entrypoint("home")
        config = json.loads(result["inline"].removeprefix("window.TAYUNE_TEA_COMPANION = ").removesuffix(";"))
        self.assertTrue(config["homePage"])


if __name__ == "__main__":
    unittest.main()
