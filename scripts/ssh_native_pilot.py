"""Exercise the isolated native SSH writer using actual Actions identity."""

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


SCOPE_REPO = "linuxerlv/kpool-tideline-pilot"
SCOPE_REF = "refs/heads/codex/tideline-ssh-native-pilot"
SCOPE_WORKFLOW = f"{SCOPE_REPO}/.github/workflows/oidc-probe.yml@{SCOPE_REF}"
PHYSICAL_KEY = "tideline/ssh-native-pilot"


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def oidc(target):
    url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
    separator = "&" if "?" in url else "?"
    request = Request(
        url + separator + urlencode({"audience": f"kpool-target:{target}"}),
        headers={"Authorization": "Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]},
    )
    with build_opener(NoRedirects()).open(request, timeout=10) as response:
        return json.load(response)["value"]


def request_id(repository, sha):
    identity = json.dumps([repository, PHYSICAL_KEY, sha, "submit"], separators=(",", ":"))
    return "native_" + hashlib.sha256(identity.encode()).hexdigest()


def request(directory, token, role, command, *, denied=False):
    host = os.environ["PILOT_HOST"]
    username = os.environ[f"PILOT_{role.upper()}_USER"]
    assert re.fullmatch(r"[A-Za-z0-9.-]+", host) and re.fullmatch(r"[a-z_][a-z0-9_]+", username)
    envelope = {"schema_version": 1, "command": command, "oidc_token": token}
    process = subprocess.run(
        (
            "/usr/bin/ssh", "-F", "/dev/null", "-T", "-i", str(directory / role),
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", "IdentityFile=none", "-o", "StrictHostKeyChecking=yes",
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", f"UserKnownHostsFile={directory / 'hosts'}",
            "-o", "ClearAllForwardings=yes", "-o", "ConnectTimeout=12",
            "-o", "PreferredAuthentications=publickey", "--", f"{username}@{host}", "kpool-target-v1",
        ),
        input=json.dumps(envelope).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=42, check=False, env={},
    )
    if denied:
        assert process.returncode == 1 and not process.stdout
        assert b"SSH target " in process.stderr and b"denied" in process.stderr
        return None
    if process.returncode or len(process.stdout) > 1024 * 1024:
        raise RuntimeError(f"The {role} identity did not receive a valid bound target reply")
    reply = json.loads(process.stdout)
    assert reply["target_id"] == command["target_id"]
    assert reply["correlation_id"] == command.get("query_id", command.get("request_id"))
    return reply


def exercise(directory, token, base, source, repository, artifact, run_id, attempt):
    preflight = request(directory, token, "query", {**base, "kind": "preflight", "query_id": "native-preflight"})
    assert (preflight["status"], preflight["policy_revision"], preflight["generation"]) == ("applied", "p1", 0)
    action = request_id(repository, source)
    command = {
        **base, "kind": "submit", "request_id": action, "source_sha": source,
        "repository_id": repository, "policy_revision": "p1", "config_revision": "cfg_a",
        "artifact": artifact, "expected_generation": 0,
    }
    request(directory, token, "query", command, denied=True)
    wrong_sha = "0" * 40
    request(directory, token, "native", {
        **command, "source_sha": wrong_sha, "request_id": request_id(repository, wrong_sha),
    }, denied=True)
    accepted = request(directory, token, "native", command)
    assert (accepted["status"], accepted["reason_code"], accepted["generation"]) == ("accepted", "QUEUED", 0)
    query = {**base, "kind": "status", "query_id": "native-status", "lookup_request_id": action}
    deadline = time.monotonic() + 100
    while time.monotonic() < deadline:
        applied = request(directory, token, "query", query)
        if applied["status"] == "applied":
            break
        if applied["status"] in {"not_applied", "rejected"}:
            raise RuntimeError("Native publication ended with " + applied.get("reason_code", "unknown state"))
        time.sleep(2)
    else:
        raise TimeoutError("Native publication had no terminal reply")
    assert (applied["generation"], applied["verification"], applied["observed_artifact"]) == (1, "passed", artifact)
    record, = [record for record in applied["ledger_records"] if record["action_id"] == action]
    assert (record["origin_run_id"], record["origin_run_attempt"], record["source_sha"]) == (run_id, attempt, source)
    replay = request(directory, token, "native", command)
    assert (replay["status"], replay["generation"], replay["observed_artifact"]) == ("applied", 1, artifact)
    assert request(directory, token, "query", query) == applied
    print("KPOOL_SSH_NATIVE_PROOF=" + json.dumps({
        "target_id": base["target_id"], "action_id": action, "artifact": artifact,
        "generation": applied["generation"], "run_id": run_id, "run_attempt": attempt,
        "source_sha": source, "query_role_write_rejected": True, "mismatched_source_rejected": True,
        "same_command_replayed": True, "ledger_record": record,
    }, sort_keys=True), flush=True)


def main():
    assert os.environ["GITHUB_REPOSITORY"] == SCOPE_REPO
    assert os.environ["GITHUB_REF"] == SCOPE_REF and os.environ["GITHUB_REF_PROTECTED"] == "true"
    assert os.environ["GITHUB_WORKFLOW_REF"] == SCOPE_WORKFLOW
    assert os.environ["GITHUB_EVENT_NAME"] == "workflow_dispatch"
    target = os.environ["PILOT_TARGET"]
    artifact, source = os.environ["PILOT_ARTIFACT"], os.environ["GITHUB_SHA"]
    repository = os.environ["GITHUB_REPOSITORY_ID"]
    run_id, attempt = os.environ["GITHUB_RUN_ID"], int(os.environ["GITHUB_RUN_ATTEMPT"])
    assert repository == "1466251904" and re.fullmatch(r"[0-9a-f]{40}", source)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", artifact) and attempt > 0 and run_id.isdecimal()
    base = {"schema_version": 1, "target_id": target}
    token = oidc(target)
    os.umask(0o077)
    with TemporaryDirectory(dir=os.environ["RUNNER_TEMP"]) as root:
        directory = Path(root)
        (directory / "hosts").write_text(os.environ["PILOT_KNOWN_HOSTS"].strip() + "\n")
        for role in ("query", "native"):
            key = os.environ.pop(f"PILOT_{role.upper()}_KEY").strip().replace("\r\n", "\n")
            (directory / role).write_text(key + "\n")
        exercise(directory, token, base, source, repository, artifact, run_id, attempt)


if __name__ == "__main__":
    main()
