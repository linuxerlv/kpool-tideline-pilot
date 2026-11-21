import argparse
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_android


class AndroidBuilderTest(unittest.TestCase):
    def test_rejects_noncanonical_and_out_of_range_version_codes(self):
        for value in ("0", "-1", "+1", "01", " 1", "1\n", "１", "2100000001"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                build_android.parse_version_code(value)

        self.assertEqual(build_android.parse_version_code("1"), 1)
        self.assertEqual(build_android.parse_version_code("2100000000"), 2100000000)

    def test_packages_offline_assets_and_provenance_in_the_base_module(self):
        source_sha = "a23f" * 10
        html = b"<!doctype html><title>Tideline</title>"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            linked = root / "linked.apk"
            with ZipFile(linked, "w") as archive:
                archive.writestr("AndroidManifest.xml", b"manifest fixture")
                archive.writestr("resources.pb", b"resources fixture")
            dex = root / "classes.dex"
            dex.write_bytes(b"dex fixture")
            module = root / "base.zip"

            build_android.write_base_module(module, linked, dex, html, source_sha, 123)

            with ZipFile(module) as archive:
                self.assertEqual(archive.read("manifest/AndroidManifest.xml"), b"manifest fixture")
                self.assertEqual(archive.read("resources.pb"), b"resources fixture")
                self.assertEqual(archive.read("dex/classes.dex"), b"dex fixture")
                self.assertEqual(archive.read("assets/tideline.html"), html)
                self.assertEqual(json.loads(archive.read("assets/tideline-build.json")), {
                    "source_sha": source_sha,
                    "bundle_sha256": hashlib.sha256(html).hexdigest(),
                    "version_code": 123,
                    "package": "io.github.linuxerlv.tidelinepilot",
                })


if __name__ == "__main__":
    unittest.main()
