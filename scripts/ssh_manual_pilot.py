"""Probe a single signed, ordinary-write deployment against a dedicated target."""

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
REF = "refs/heads/codex/tideline-ssh-manual-pilot"
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
            "/usr/bin/ssh", "-F", "/dev/null", "-T", "-i", str(directory / role),
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


def exercise(directory, command):
    token = oidc()
    prior = target(directory, token, "query", {**BASE, "kind": "preflight", "query_id": "signed-preflight"})
    assert (prior["status"], prior["policy_revision"], prior["generation"]) == ("applied", POLICY, 0)
    target(directory, token, "query", command, denied=True)
    admitted = target(directory, token, "command", command)
    assert admitted["status"] in {"accepted", "applied"} and admitted["generation"] == 1
    query = {**BASE, "kind": "status", "query_id": "signed-status", "lookup_request_id": command["request_id"]}
    deadline = time.monotonic() + 100
    while time.monotonic() < deadline:
        measured = target(directory, token, "query", query)
        if measured["status"] == "applied":
            break
        if measured["status"] in {"not_applied", "rejected"}:
            raise RuntimeError("Signed publication ended with " + measured.get("reason_code", "unknown state"))
        time.sleep(2)
    else:
        raise TimeoutError("No terminal result for signed publication")
    assert (measured["generation"], measured["verification"], measured["observed_artifact"]) == (1, "passed", ARTIFACT)
    record, = measured["ledger_records"]
    run_id, attempt = os.environ["GITHUB_RUN_ID"], int(os.environ["GITHUB_RUN_ATTEMPT"])
    assert (record["action_id"], record["origin_run_id"], record["origin_run_attempt"], record["source_sha"]) == (
        command["request_id"], run_id, attempt, SOURCE,
    )
    replay = target(directory, token, "command", command)
    assert replay["status"] == "applied" and replay["generation"] == 1 and replay["observed_artifact"] == ARTIFACT
    assert target(directory, token, "query", query) == measured
    print("KPOOL_SIGNED_SSH_PROOF=" + json.dumps({
        "execution_sha": os.environ["GITHUB_SHA"], "signature_verified": True, "tamper_denied": True,
        "query_write_denied": True, "command_replay": True, "admitted": admitted, "terminal": measured,
    }, sort_keys=True), flush=True)


def main():
    assert os.environ["GITHUB_REPOSITORY"] == REPO and os.environ["GITHUB_REPOSITORY_ID"] == REPOSITORY
    assert os.environ["GITHUB_REF"] == REF and os.environ["GITHUB_REF_PROTECTED"] == "true"
    assert os.environ["GITHUB_WORKFLOW_REF"] == WORKFLOW and os.environ["GITHUB_EVENT_NAME"] == "workflow_dispatch"
    os.umask(0o077)
    with TemporaryDirectory(dir=os.environ["RUNNER_TEMP"]) as root:
        directory = Path(root)
        command = approved_command(directory)
        (directory / "hosts").write_text(os.environ["PILOT_KNOWN_HOSTS"].strip() + "\n")
        for role in ("query", "command"):
            private = os.environ.pop(f"PILOT_{role.upper()}_KEY").strip().replace("\r\n", "\n")
            (directory / role).write_text(private + "\n")
        exercise(directory, command)


if __name__ == "__main__":
    main()
