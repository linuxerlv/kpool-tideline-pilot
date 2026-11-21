"""Check the identity of this hosted probe and print only verified coordinates."""

import json
import os
import re
import secrets
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import jwt

ISSUER = "https://token.actions.githubusercontent.com"
JWKS = f"{ISSUER}/.well-known/jwks"
REPOSITORY = "linuxerlv/kpool-tideline-pilot"
BRANCH = "refs/heads/main"
WORKFLOW = f"{REPOSITORY}/.github/workflows/oidc-probe.yml@{BRANCH}"
ENVIRONMENT = "tideline-oidc-probe"
EVENT = "workflow_dispatch"
INTEGER = re.compile(r"[1-9][0-9]{0,19}")
SHA = re.compile(r"[0-9a-f]{40}")
KEY_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


class ProbeFailure(Exception):
    """A fixed error category whose text is safe to publish."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, location):
        return None


def github_id(value):
    if type(value) not in (int, str) or not INTEGER.fullmatch(str(value)):
        raise ValueError
    return str(value)


def expected_identity(env):
    try:
        if (
            env["GITHUB_REPOSITORY"] != REPOSITORY
            or env["GITHUB_REF"] != BRANCH
            or env["GITHUB_WORKFLOW_REF"] != WORKFLOW
            or env["GITHUB_EVENT_NAME"] != EVENT
            or not SHA.fullmatch(env["GITHUB_SHA"])
        ):
            raise ValueError
        return {
            "repository_id": github_id(env["GITHUB_REPOSITORY_ID"]),
            "repository": REPOSITORY,
            "ref": BRANCH,
            "workflow_ref": WORKFLOW,
            "environment": ENVIRONMENT,
            "run_id": github_id(env["GITHUB_RUN_ID"]),
            "run_attempt": int(github_id(env["GITHUB_RUN_ATTEMPT"])),
            "sha": env["GITHUB_SHA"],
            "event_name": EVENT,
        }
    except (KeyError, ValueError, TypeError):
        raise ProbeFailure("WORKFLOW_CONTEXT_INVALID") from None


def token_request(endpoint, credential, audience):
    try:
        url = urlsplit(endpoint)
        if (
            not endpoint.isascii()
            or any(ord(char) < 33 or ord(char) == 127 for char in endpoint)
            or url.scheme != "https"
            or not url.hostname
            or not url.hostname.endswith(".actions.githubusercontent.com")
            or url.port not in (None, 443)
            or url.username is not None
            or url.password is not None
            or not url.path.startswith("/")
            or url.fragment
            or any(
                key.casefold() == "audience"
                for key, _ in parse_qsl(
                    url.query, keep_blank_values=True, max_num_fields=32
                )
            )
            or not credential
            or any(ord(char) < 33 or ord(char) > 126 for char in credential)
        ):
            raise ValueError
        query = f"{url.query}&" if url.query else ""
        url = url._replace(query=query + urlencode({"audience": audience}))
        return Request(
            urlunsplit(url),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {credential}",
            },
        )
    except (TypeError, ValueError):
        raise ProbeFailure("TOKEN_ENDPOINT_INVALID") from None


def get_json(opener, request, limit, failure):
    try:
        with opener.open(request, timeout=10) as response:
            data = response.read(limit + 1)
            if response.status != 200 or len(data) > limit:
                raise ValueError
        payload = json.loads(data)
        if not isinstance(payload, dict):
            raise TypeError
        return payload
    except (OSError, TypeError, ValueError):
        raise ProbeFailure(failure) from None


def verify_identity(token, jwks, audience, expected):
    try:
        if not isinstance(token, str) or not 1 <= len(token) <= 16384:
            raise ValueError
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if (
            header.get("alg") != "RS256"
            or not isinstance(kid, str)
            or not KEY_ID.fullmatch(kid)
            or {"jku", "jwk", "x5u", "crit"} & header.keys()
        ):
            raise ValueError
        keys = jwks.get("keys")
        if not isinstance(keys, list) or not 1 <= len(keys) <= 32:
            raise ValueError
        selected = [
            key for key in keys if isinstance(key, dict) and key.get("kid") == kid
        ]
        if len(selected) != 1:
            raise ValueError
        key = selected[0]
        if (
            key.get("kty") != "RSA"
            or key.get("use", "sig") != "sig"
            or key.get("alg", "RS256") != "RS256"
        ):
            raise ValueError
        public_key = jwt.PyJWK.from_dict(key, algorithm="RS256").key
        if public_key.key_size < 2048:
            raise ValueError
        claims = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=audience,
            issuer=ISSUER,
            options={
                "require": ["iss", "sub", "aud", "iat", "nbf", "exp", *expected],
                "strict_aud": True,
            },
            leeway=15,
        )
        if (
            any(type(claims[name]) is not int for name in ("iat", "nbf", "exp"))
            or not 0 < claims["exp"] - claims["iat"] <= 600
            or claims["nbf"] >= claims["exp"]
            or not claims["sub"]
        ):
            raise ValueError
        identity = {name: claims[name] for name in expected}
        for name in ("repository_id", "run_id"):
            identity[name] = github_id(identity[name])
        identity["run_attempt"] = int(github_id(identity["run_attempt"]))
        if (
            identity != expected
            or claims.get("runner_environment", "github-hosted") != "github-hosted"
        ):
            raise ProbeFailure("CLAIM_BINDING_INVALID")
        for name, value in (
            ("job_workflow_ref", expected["workflow_ref"]),
            ("job_workflow_sha", expected["sha"]),
        ):
            if name in claims:
                if claims[name] != value:
                    raise ProbeFailure("CLAIM_BINDING_INVALID")
                identity[name] = value
        return identity
    except (
        jwt.PyJWTError,
        AttributeError,
        KeyError,
        OverflowError,
        TypeError,
        ValueError,
    ):
        raise ProbeFailure("SIGNATURE_OR_TOKEN_INVALID") from None


def main():
    try:
        expected = expected_identity(os.environ)
        audience = f"urn:tideline:oidc-probe:{expected['run_id']}:{expected['run_attempt']}:{secrets.token_hex(16)}"
        request = token_request(
            os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", ""),
            os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", ""),
            audience,
        )
        opener = build_opener(ProxyHandler({}), NoRedirects())
        token = get_json(opener, request, 65536, "TOKEN_REQUEST_FAILED").get("value")
        keys = get_json(
            opener,
            Request(JWKS, headers={"Accept": "application/json"}),
            65536,
            "JWKS_REQUEST_FAILED",
        )
        identity = verify_identity(token, keys, audience, expected)
    except ProbeFailure as failure:
        print(f"KPOOL_OIDC_PROBE_FAILED {failure}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - An unexpected error must never print request credentials.
        print("KPOOL_OIDC_PROBE_FAILED UNEXPECTED", file=sys.stderr)
        return 1

    print("KPOOL_OIDC_IDENTITY " + json.dumps(identity, separators=(",", ":")))
    print("KPOOL_OIDC_PROBE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
