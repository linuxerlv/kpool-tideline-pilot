# Managed by kpool for target tgt_26a833c41ec04a81b1139cbc1287636a
import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath


def main():
    root = Path.cwd()
    project = json.loads(Path(__file__).with_name("project.json").read_text())
    head = os.environ["KPOOL_REVIEW_HEAD"]
    source = os.environ["KPOOL_SOURCE_SHA"]
    context, dockerfile = project["context"], project["dockerfile"]
    if not all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (head, source)):
        raise ValueError("Invalid GitHub revision")
    if not re.fullmatch(r"(?:\.|[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*)", context):
        raise ValueError("Invalid reviewed build context")
    if not re.fullmatch(r"(?:[A-Za-z0-9_-]+/)*Dockerfile", dockerfile):
        raise ValueError("Invalid reviewed Dockerfile")
    if subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() != head:
        raise ValueError("Runner checkout differs from the reviewed head")
    if not (root / context / PurePosixPath(dockerfile)).resolve().is_relative_to(root):
        raise ValueError("Build path outside the reviewed checkout")

    out = Path(os.environ["RUNNER_TEMP"]) / "kpool-bootstrap"
    out.mkdir()
    tag = f"kpool-bootstrap:{head}"
    receipt = {"head_sha": head, "source_sha": source, "context": context, "stage": "build"}
    with (out / "build.log").open("wb") as log:
        result = subprocess.run(["docker", "build", "--network=none", "--pull=false", "--progress=plain",
                                 "--label", f"org.opencontainers.image.revision={source}", "-f", dockerfile,
                                 "-t", tag, "."], cwd=root / context, stdout=log, stderr=subprocess.STDOUT)
    receipt["exit_code"] = result.returncode
    if result.returncode == 0:
        receipt["stage"] = "test"
        with (out / "test.log").open("wb") as log:
            result = subprocess.run(["docker", "run", "--rm", "--network", "none", "--entrypoint",
                                     "python", tag, "app.py", "--self-test"],
                                    stdout=log, stderr=subprocess.STDOUT)
        receipt["exit_code"] = result.returncode
        if result.returncode == 0:
            receipt["artifact"] = subprocess.check_output(
                ["docker", "image", "inspect", tag, "--format", "{{.Id}}"], text=True).strip()
            subprocess.run(["docker", "save", "-o", str(out / "image.tar"), tag], check=True)
    (out / "receipt.json").write_text(json.dumps(receipt) + "\n")
    if result.returncode:
        print((out / ("build.log" if receipt["stage"] == "build" else "test.log")).read_text(errors="replace")[-2000:])
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
