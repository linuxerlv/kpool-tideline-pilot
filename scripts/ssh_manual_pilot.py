"""Verify recovery from a single signed candidate on a dedicated target."""

import base64
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


REPO = "linuxerlv/kpool-tideline-pilot"
REPOSITORY = "1466251904"
REF = "refs/heads/codex/tideline-hosted-auto-recovery"
WORKFLOW = f"{REPO}/.github/workflows/oidc-probe.yml@{REF}"
TARGET = "tgt_tideline_ssh_manual_20261121"
SOURCE = "0644d4686776f68c41f27e4d825b0cb056e99cf2"
ARTIFACT = "sha256:5ecf3c0785dfcae69795040fd3d34374277100164507a981009e61e80c7228cf"
PREVIOUS = "sha256:cee560b387be230c6b5cecafcf5e5e885ef6046e3006674fe441ba00bc064bac"
SEED = "op_tideline_recovery_seed"
POLICY = "p1"
REVISION = "cfg_b"
CORRELATION = "7a24f48d35a2da4be11f3a66c2ea09d4"
BASE = {"schema_version": 1, "target_id": TARGET}


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def oidc():
    address = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
    separator = "&" if "?" in address else "?"
    request = Request(
        address + separator + urlencode({"audience": f"kpool-target:{TARGET}"}),
        headers={"Authorization": "Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]},
    )
    with build_opener(NoRedirects()).open(request, timeout=10) as response:
        return json.load(response)["value"]


def verify_signature(directory, values):
    payload = b"kpool.workflow-dispatch.v1\n" + b"\0".join(item.encode() for item in values)
    (directory / "approved-input").write_bytes(payload)
    process = subprocess.run(
        ("/usr/bin/openssl", "pkeyutl", "-verify", "-pubin", "-inkey", "signer.pem",
         "-rawin", "-in", "approved-input", "-sigfile", "signature"),
        cwd=directory, capture_output=True, timeout=10, check=False,
    )
    return process.returncode == 0


def approved_command(directory):
    names = ("INTENT", "EXECUTION_SHA", "CORRELATION", "TARGET_CONTEXT")
    values = tuple(os.environ[f"KPOOL_{name}"] for name in names)
    assert all("\0" not in value for value in values)
    signature = base64.b64decode(os.environ["KPOOL_AUTHORIZATION"], validate=True)
    assert len(signature) == 64
    (directory / "signature").write_bytes(signature)
    (directory / "signer.pem").write_text(os.environ["KPOOL_DISPATCH_PUBLIC_KEY"].strip() + "\n")
    assert verify_signature(directory, values)
    raw_intent, execution, correlation, raw_context = values
    intent, context = json.loads(raw_intent), json.loads(raw_context)
    tampered = json.dumps({**intent, "source_sha": "0" * 40})
    assert not verify_signature(directory, (tampered, execution, correlation, raw_context))
    assert execution == os.environ["GITHUB_SHA"] and correlation == CORRELATION
    expected = {
        **BASE, "repository_id": REPOSITORY, "workflow_ref": WORKFLOW, "ref": REF,
        "source_sha": SOURCE, "policy_revision": POLICY, "operation_kind": "deploy",
    }
    assert all(intent.get(key) == value for key, value in expected.items())
    assert intent["arguments"]["kind"] == "deploy" and "target_command" not in intent
    assert re.fullmatch(r"op_[0-9a-f]{32}", intent["request_id"])
    assert set(context) == {"config_revision", "expected_generation", "expires_at"}
    assert (context["config_revision"], context["expected_generation"]) == (REVISION, 1)
    assert type(context["expires_at"]) is int and time.time() < context["expires_at"]
    return {
        **BASE, "kind": "submit", "repository_id": REPOSITORY, "request_id": intent["request_id"],
        "source_sha": SOURCE, "artifact": ARTIFACT, "policy_revision": POLICY,
        "config_revision": REVISION, "expected_generation": 1,
    }


def target(directory, token, role, command, *, denied=False):
    username = os.environ[f"PILOT_{role.upper()}_USER"]
    assert re.fullmatch(r"[a-z_][a-z0-9_]+", username)
    host = os.environ["PILOT_HOST"]
    assert re.fullmatch(r"[A-Za-z0-9.-]+", host)
    envelope = {"schema_version": 1, "command": command, "oidc_token": token}
    process = subprocess.run(
        (
            "/usr/bin/ssh", "-F", "/dev/null", "-T", "-i",
            str(Path(os.environ["RUNNER_TEMP"]) / "tideline-keys" / role),
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", "IdentityFile=none", "-o", "StrictHostKeyChecking=yes", "-o",
            "GlobalKnownHostsFile=/dev/null", "-o", f"UserKnownHostsFile={directory / 'hosts'}",
            "-o", "ClearAllForwardings=yes", "-o", "ConnectTimeout=12", "-o",
            "PreferredAuthentications=publickey", "--", f"{username}@{host}", "kpool-target-v1",
        ),
        input=json.dumps(envelope).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=42, check=False, env={},
    )
    if denied:
        assert process.returncode == 1 and not process.stdout
        assert b"SSH target command denied" in process.stderr
        return None
    if process.returncode or len(process.stdout) > 1024 * 1024:
        raise RuntimeError(f"The {role} identity did not receive a bound reply")
    result = json.loads(process.stdout)
    assert result["target_id"] == TARGET
    assert result["correlation_id"] == command.get("query_id", command.get("request_id"))
    return result


def status(directory, operation):
    return target(directory, oidc(), "query", {**BASE, "kind": "status", "query_id": "signed-status",
                                                "lookup_request_id": operation})


def wait_for_status(directory, operation, predicate, allowed, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            result = status(directory, operation)
        except (RuntimeError, subprocess.TimeoutExpired):
            time.sleep(5)
            continue
        if predicate(result):
            return result
        if result["status"] not in allowed:
            raise ValueError("The trial entered an unexpected state: " + result.get("reason_code", result["status"]))
        time.sleep(3)
    raise TimeoutError("The expected target phase did not arrive")


def exercise(directory, command):
    prior = wait_for_status(directory, SEED, lambda result: result["status"] == "applied",
                            {"accepted", "running", "unknown", "not_applied"}, 420)
    assert (prior["generation"], prior["verification"], prior["observed_artifact"]) == (1, "passed", PREVIOUS)
    token = oidc()
    target(directory, token, "query", command, denied=True)
    admitted = target(directory, token, "command", command)
    assert admitted["status"] == "accepted" and admitted["generation"] == 2
    measured = wait_for_status(directory, command["request_id"],
                               lambda result: result["status"] == "running" and result.get("observed_artifact") == ARTIFACT,
                               {"accepted", "running", "unknown"}, 265)
    assert measured["generation"] == 2 and measured["config_revision"] == REVISION and not measured.get("verification")
    return {"execution_sha": os.environ["GITHUB_SHA"], "run_id": os.environ["GITHUB_RUN_ID"],
            "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]), "command": command,
            "prior": prior, "admitted": admitted, "before_fault": measured}


def verify_recovery(directory, checkpoint):
    run_id, attempt = os.environ["GITHUB_RUN_ID"], int(os.environ["GITHUB_RUN_ATTEMPT"])
    assert (checkpoint["run_id"], checkpoint["run_attempt"], checkpoint["execution_sha"]) == (
        run_id, attempt, os.environ["GITHUB_SHA"],
    )
    command = checkpoint["command"]
    parent = wait_for_status(directory, command["request_id"],
                             lambda result: result.get("reason_code") == "AUTO_RESTORED",
                             {"accepted", "running", "unknown"}, 295)
    assert (parent["status"], parent["generation"], parent["verification"]) == ("not_applied", 2, "failed")
    first, second = parent["ledger_records"]
    assert (first["action_id"], first["kind"], first["outcome"], first["observed_artifact"]) == (
        command["request_id"], "deploy", "failed", ARTIFACT,
    )
    assert (second["parent_action_id"], second["kind"], second["outcome"], second["observed_artifact"]) == (
        command["request_id"], "restore", "applied", PREVIOUS,
    )
    for entry in (first, second):
        assert (entry["generation"], entry["origin_run_id"], entry["origin_run_attempt"], entry["source_sha"]) == (
            2, run_id, attempt, SOURCE,
        )
    child = status(directory, second["action_id"])
    assert (child["status"], child["verification"], child["generation"], child["observed_artifact"]) == (
        "applied", "passed", 2, PREVIOUS,
    )
    replay = target(directory, oidc(), "command", command)
    assert (replay["status"], replay["generation"], replay["reason_code"]) == ("not_applied", 2, "AUTO_RESTORED")
    assert status(directory, command["request_id"]) == parent
    print("KPOOL_HOSTED_RECOVERY_PROOF=" + json.dumps({
        "execution_sha": checkpoint["execution_sha"], "command": command, "run_id": run_id,
        "run_attempt": attempt, "parent": parent, "child": child, "replay": replay,
    }, sort_keys=True), flush=True)


def main():
    assert os.environ["GITHUB_REPOSITORY"] == REPO and os.environ["GITHUB_REPOSITORY_ID"] == REPOSITORY
    assert os.environ["GITHUB_REF"] == REF and os.environ["GITHUB_REF_PROTECTED"] == "true"
    assert os.environ["GITHUB_WORKFLOW_REF"] == WORKFLOW and os.environ["GITHUB_EVENT_NAME"] == "workflow_dispatch"
    os.umask(0o077)
    path = Path(os.environ["RUNNER_TEMP"]) / "tideline-candidate-route.json"
    with TemporaryDirectory(dir=os.environ["RUNNER_TEMP"]) as root:
        directory = Path(root)
        (directory / "hosts").write_text(os.environ["PILOT_KNOWN_HOSTS"].strip() + "\n")
        stage, = sys.argv[1:]
        if stage == "candidate":
            path.write_text(json.dumps(exercise(directory, approved_command(directory)), sort_keys=True), encoding="utf-8")
        elif stage == "recovered":
            verify_recovery(directory, json.loads(path.read_text(encoding="utf-8")))
        else:
            raise ValueError("Expected candidate or recovered stage")


if __name__ == "__main__":
    main()
