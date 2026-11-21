"""Build the Windows x64 launcher and embed Tideline from the current Git commit."""

import argparse
import base64
import hashlib
import os
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = "scripts/TidelineLauncher.cs"


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def replace_once(source, original, replacement):
    if source.count(original) != 1:
        raise ValueError(f"Expected one {original!r} in the game source")
    return source.replace(original, replacement, 1)


def bundle_html(index, stylesheet, game, engine):
    if b"</style" in stylesheet.lower() or b"</script" in game.lower():
        raise ValueError("The game source contains a closing tag that cannot be inlined")

    engine_url = b"data:text/javascript;charset=utf-8;base64," + base64.b64encode(engine)
    game = replace_once(game, b"from './game-engine.mjs';", b"from '" + engine_url + b"';")
    policy = (
        b"<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; "
        b"style-src 'unsafe-inline'; script-src 'unsafe-inline' data:; img-src data:\">"
    )
    index = replace_once(
        index,
        b'<link rel="stylesheet" href="./styles.css">',
        policy + b"\n    <style>\n" + stylesheet + b"</style>",
    )
    return replace_once(
        index,
        b'<script src="./game.mjs" type="module"></script>',
        b'<script type="module">\n' + game + b"</script>",
    )


def build(output, html_output):
    source_sha = git("rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
    # Read blobs to keep the committed bytes even when Git converted line endings in the checkout.
    names = ("index.html", "styles.css", "game.mjs", "game-engine.mjs", LAUNCHER)
    index, stylesheet, game, engine, launcher = (git("cat-file", "blob", f"{source_sha}:{name}") for name in names)
    bundle = bundle_html(index, stylesheet, game, engine)
    bundle_sha = hashlib.sha256(bundle).hexdigest()

    compiler = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".tideline-build-", dir=output.parent) as directory:
        staging = Path(directory)
        html = staging / "Tideline.html"
        html.write_bytes(bundle)
        source = staging / "TidelineLauncher.cs"
        source.write_bytes(launcher)
        metadata = staging / "BuildInfo.cs"
        metadata.write_text(
            "internal static class BuildInfo\n"
            "{\n"
            f'    internal const string SourceSha = "{source_sha}";\n'
            f'    internal const string BundleSha = "{bundle_sha}";\n'
            "}\n",
            encoding="ascii",
        )
        executable = staging / "Tideline.exe"
        subprocess.run(
            [
                str(compiler), "/nologo", "/noconfig", "/target:winexe", "/platform:x64",
                "/optimize+", "/warnaserror+", "/reference:System.dll", "/reference:System.Windows.Forms.dll",
                f"/out:{executable.name}", f"/resource:{html.name}", source.name, metadata.name,
            ],
            cwd=staging,
            check=True,
        )
        if html_output is not None:
            html_output.parent.mkdir(parents=True, exist_ok=True)
            html_output.write_bytes(bundle)
        executable.replace(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="Path for the Windows executable")
    parser.add_argument("--html-out", type=Path, help="Optional path for a copy of the standalone HTML")
    args = parser.parse_args()
    output = args.out.resolve()
    html_output = args.html_out.resolve() if args.html_out else None
    if output == html_output:
        parser.error("--out and --html-out must be different files")
    build(output, html_output)


if __name__ == "__main__":
    main()
