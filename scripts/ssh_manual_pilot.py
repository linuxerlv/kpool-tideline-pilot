"""Witness a single approved release after it routes but before verification."""

import base64
import json
import os
import re
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


REPO = "linuxerlv/kpool-tideline-pilot"
REPOSITORY = "1466251904"
REF = "refs/heads/codex/tideline-after-switch-loss"
WORKFLOW = f"{REPO}/.github/workflows/oidc-probe.yml@{REF}"
TARGET = "tgt_tideline_ssh_manual_20261121"
SOURCE = "8bf75f94e2fe206ad21509f0cbc6dfc50e90a387"
ARTIFACT = "sha256:cee560b387be230c6b5cecafcf5e5e885ef6046e3006674fe441ba00bc064bac"
POLICY = "p1"
REVISION = "cfg_a"
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
    assert (context["config_revision"], context["expected_generation"]) == (REVISION, 0)
    assert type(context["expires_at"]) is int and time.time() < context["expires_at"]
    return {
        **BASE, "kind": "submit", "repository_id": REPOSITORY, "request_id": intent["request_id"],
        "source_sha": SOURCE, "artifact": ARTIFACT, "policy_revision": POLICY,
        "config_revision": REVISION, "expected_generation": 0,
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


def wait_for_enrollment(directory):
    preflight = {**BASE, "kind": "preflight", "query_id": "signed-preflight"}
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        token = oidc()
        try:
            return token, target(directory, token, "query", preflight)
        except (RuntimeError, subprocess.TimeoutExpired):
            time.sleep(5)
    raise TimeoutError("The independently enrolled read-only identity did not respond")


def exercise(directory, command):
    token, prior = wait_for_enrollment(directory)
    assert (prior["status"], prior["policy_revision"], prior["generation"]) == ("applied", POLICY, 0)
    target(directory, token, "query", command, denied=True)
    admitted = target(directory, token, "command", command)
    assert admitted["status"] in {"accepted", "applied"} and admitted["generation"] == 1
    query = {**BASE, "kind": "status", "query_id": "signed-status", "lookup_request_id": command["request_id"]}
    deadline = time.monotonic() + 260
    while time.monotonic() < deadline:
        try:
            measured = target(directory, oidc(), "query", query)
        except (RuntimeError, subprocess.TimeoutExpired):
            time.sleep(5)
            continue
        if measured["status"] == "running" and measured.get("observed_artifact") == ARTIFACT:
            break
        if measured["status"] in {"applied", "not_applied", "rejected"}:
            raise RuntimeError("Signed publication ended with " + measured.get("reason_code", "unknown state"))
        time.sleep(5)
    else:
        raise TimeoutError("Live route was not witnessed during the observation phase")
    assert (measured["generation"], measured["config_revision"]) == (1, REVISION)
    assert "verification" not in measured and not measured.get("ledger_records")
    assert any(item["source"] == "target" and item["artifact"] == ARTIFACT for item in measured["evidence"])
    receipt = Path(os.environ["RUNNER_TEMP"]) / "tideline-after-switch-checkpoint.json"
    receipt.write_text(json.dumps({
        "execution_sha": os.environ["GITHUB_SHA"], "run_id": os.environ["GITHUB_RUN_ID"],
        "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]), "request_id": command["request_id"],
        "signature_verified": True, "tamper_denied": True, "query_write_denied": True,
        "admitted": admitted, "after_switch": measured,
    }, sort_keys=True), encoding="utf-8")
    print("KPOOL_AFTER_SWITCH_CHECKPOINT_READY", flush=True)


def main():
    assert os.environ["GITHUB_REPOSITORY"] == REPO and os.environ["GITHUB_REPOSITORY_ID"] == REPOSITORY
    assert os.environ["GITHUB_REF"] == REF and os.environ["GITHUB_REF_PROTECTED"] == "true"
    assert os.environ["GITHUB_WORKFLOW_REF"] == WORKFLOW and os.environ["GITHUB_EVENT_NAME"] == "workflow_dispatch"
    os.umask(0o077)
    with TemporaryDirectory(dir=os.environ["RUNNER_TEMP"]) as root:
        directory = Path(root)
        command = approved_command(directory)
        (directory / "hosts").write_text(os.environ["PILOT_KNOWN_HOSTS"].strip() + "\n")
        exercise(directory, command)


if __name__ == "__main__":
    main()
