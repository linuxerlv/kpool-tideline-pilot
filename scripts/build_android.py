"""Build an unsigned Android app bundle containing Tideline from the current Git commit."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZIP_DEFLATED, ZipFile

from build_windows import bundle_html, git

PACKAGE = "io.github.linuxerlv.tidelinepilot"
MIN_SDK = "24"
TARGET_SDK = "36"
BUILD_TOOLS = "36.0.0"
MAX_VERSION_CODE = 2_100_000_000
MANIFEST = "android/AndroidManifest.xml"
ACTIVITY = "android/MainActivity.java"
ANDROID = "{http://schemas.android.com/apk/res/android}"


def parse_version_code(value):
    if not re.fullmatch(r"[1-9][0-9]{0,9}", value) or int(value) > MAX_VERSION_CODE:
        raise argparse.ArgumentTypeError(f"expected a canonical decimal from 1 to {MAX_VERSION_CODE}")
    return int(value)


def absolute_path(value):
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("expected an absolute path")
    return path.resolve()


def validate_manifest(manifest):
    root = ElementTree.fromstring(manifest)
    if root.tag != "manifest" or root.get("package") != PACKAGE:
        raise ValueError(f"Android manifest must set package={PACKAGE}")
    sdk = root.find("uses-sdk")
    if sdk is None or sdk.get(ANDROID + "minSdkVersion") != MIN_SDK or sdk.get(ANDROID + "targetSdkVersion") != TARGET_SDK:
        raise ValueError(f"Android manifest must set minSdkVersion={MIN_SDK} and targetSdkVersion={TARGET_SDK}")
    for element in root:
        if element.tag.startswith("uses-permission") and element.get(ANDROID + "name") == "android.permission.INTERNET":
            raise ValueError("The offline Android app must not request Internet permission")


def write_base_module(output, linked, dex, html, source_sha, version_code):
    metadata = {
        "source_sha": source_sha,
        "bundle_sha256": hashlib.sha256(html).hexdigest(),
        "version_code": version_code,
        "package": PACKAGE,
    }
    with ZipFile(linked) as resources, ZipFile(output, "w", compression=ZIP_DEFLATED) as module:
        module.writestr("manifest/AndroidManifest.xml", resources.read("AndroidManifest.xml"))
        if "resources.pb" in resources.namelist():
            module.writestr("resources.pb", resources.read("resources.pb"))
        module.write(dex, "dex/classes.dex")
        module.writestr("assets/tideline.html", html)
        module.writestr("assets/tideline-build.json", json.dumps(metadata, sort_keys=True) + "\n")


def build(output, bundletool, version_code):
    sdk_path = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if not sdk_path:
        raise ValueError("Set ANDROID_HOME or ANDROID_SDK_ROOT to the Android SDK directory")
    sdk = Path(sdk_path).resolve()
    tools = sdk / "build-tools" / BUILD_TOOLS
    aapt2 = tools / ("aapt2.exe" if os.name == "nt" else "aapt2")
    d8 = tools / ("d8.bat" if os.name == "nt" else "d8")
    android_jar = sdk / "platforms" / f"android-{TARGET_SDK}" / "android.jar"
    for path in (aapt2, d8, android_jar):
        if not path.is_file():
            raise ValueError(f"Android SDK component not found: {path}")
    javac, java = shutil.which("javac"), shutil.which("java")
    if not javac or not java:
        raise ValueError("Install a JDK and put javac and java on PATH")

    source_sha = git("rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("Expected a 40-character Git commit SHA")
    names = ("index.html", "styles.css", "game.mjs", "game-engine.mjs", MANIFEST, ACTIVITY)
    # Bypass checkout filters and working-tree edits to preserve the committed source bytes.
    index, stylesheet, game, engine, manifest, activity = (
        git("cat-file", "blob", f"{source_sha}:{name}") for name in names
    )
    validate_manifest(manifest)
    html = bundle_html(index, stylesheet, game, engine)

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".tideline-android-", dir=output.parent) as directory:
        staging = Path(directory)
        manifest_file = staging / "AndroidManifest.xml"
        manifest_file.write_bytes(manifest)
        activity_file = staging / "MainActivity.java"
        activity_file.write_bytes(activity)
        linked = staging / "linked.apk"
        classes = staging / "classes"
        classes.mkdir()
        dex = staging / "dex"
        dex.mkdir()
        module = staging / "base.zip"
        unsigned = staging / "tideline-unsigned.aab"

        subprocess.run([
            aapt2, "link", "--proto-format", "--manifest", manifest_file, "-I", android_jar,
            "--version-code", str(version_code), "--version-name", f"1.0.{version_code}",
            "--replace-version", "-o", linked,
        ], check=True)
        subprocess.run([
            javac, "--release", "8", "-encoding", "UTF-8", "-proc:none", "-classpath", android_jar,
            "-d", classes, activity_file,
        ], check=True)
        subprocess.run([
            d8, "--release", "--min-api", MIN_SDK, "--lib", android_jar, "--output", dex,
            *sorted(classes.rglob("*.class")),
        ], check=True)
        write_base_module(module, linked, dex / "classes.dex", html, source_sha, version_code)
        subprocess.run([
            java, "-jar", bundletool, "build-bundle", f"--modules={module}", f"--output={unsigned}",
        ], check=True)
        unsigned.replace(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version-code", required=True, type=parse_version_code)
    parser.add_argument("--bundletool", required=True, type=absolute_path, help="Path to the bundletool JAR")
    parser.add_argument("--out", required=True, type=absolute_path, help="Path for the unsigned .aab")
    args = parser.parse_args()
    if not args.bundletool.is_file() or args.bundletool.suffix.lower() != ".jar":
        parser.error("--bundletool must point to an existing .jar file")
    if args.out.suffix.lower() != ".aab":
        parser.error("--out must end in .aab")
    try:
        build(args.out, args.bundletool, args.version_code)
    except (ValueError, OSError, ElementTree.ParseError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Android build failed: {error}\n")


if __name__ == "__main__":
    main()
