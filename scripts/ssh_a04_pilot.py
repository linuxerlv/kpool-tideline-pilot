"""Exercise the isolated Tideline queue from its protected GitHub workflow."""

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


REPO = 'linuxerlv/kpool-tideline-pilot'
REPOSITORY_ID = '1466251904'
REF = 'refs/heads/codex/tideline-a04-queue-v2'
WORKFLOW = f'{REPO}/.github/workflows/oidc-probe.yml@{REF}'
OPENSSL = '/usr/bin/openssl'
SIGNED_FIELDS = ('KPOOL_INTENT', 'KPOOL_EXECUTION_SHA', 'KPOOL_CORRELATION', 'KPOOL_TARGET_CONTEXT')
REPLY_LIMIT = 1024 * 1024
WRITER = {'C': 'native', 'H': 'command'}
ID = re.compile(r'[1-9][0-9]{0,19}')
REQUEST = re.compile(r'[a-zA-Z0-9_-]{1,200}')
REVISION = re.compile(r'[a-zA-Z0-9_.-]{1,100}')
SHA = re.compile(r'[a-f0-9]{40}')
DIGEST = re.compile(r'sha256:[a-f0-9]{64}')


class PilotError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise PilotError(message)


def check_pins(entries):
    require(isinstance(entries, list) and len(entries) <= 20, 'Invalid check pins')
    pins = set()
    for entry in entries:
        require(isinstance(entry, dict) and set(entry) == {'name', 'app_id'}
                and isinstance(entry['name'], str) and 0 < len(entry['name']) <= 200
                and isinstance(entry['app_id'], str) and ID.fullmatch(entry['app_id']), 'Invalid app-pinned check')
        pins.add((entry['name'], entry['app_id']))
    require(len({name for name, _ in pins}) == len(entries), 'Duplicate check pins')
    return pins


def context():
    scope = json.loads(os.environ['PILOT_SCOPE'])
    role = os.environ['PILOT_ROLE']
    require(role in WRITER and type(scope['base_generation']) is int and scope['base_generation'] == 1
            and REQUEST.fullmatch(scope['target_id']) and REVISION.fullmatch(scope['policy_revision'])
            and re.fullmatch(r'[a-zA-Z0-9_./-]{1,160}', scope['physical_key']), 'Invalid pilot target scope')
    releases = scope['releases']
    require(set(releases) == {'A', 'H', 'C'} and REQUEST.fullmatch(releases['A']['request_id'])
            and all(SHA.fullmatch(row['source_sha']) and row['source_sha'] != '0' * 40
                    and DIGEST.fullmatch(row['artifact']) and REVISION.fullmatch(row['config_revision'])
                    for row in releases.values())
            and all(len({row[field] for row in releases.values()}) == 3 for field in ('source_sha', 'artifact')),
            'Invalid immutable release scope')
    required = check_pins(scope['required_checks'])
    optional = check_pins(scope['optional_checks'])
    require(required and len(required | optional) <= 20 and len({name for name, _ in required | optional})
            == len(required) + len(optional) and check_pins(scope['skip_optional_checks']) <= optional,
            'Invalid release check policy')
    expected = {
        'GITHUB_REPOSITORY': REPO, 'GITHUB_REPOSITORY_ID': REPOSITORY_ID, 'GITHUB_REF': REF,
        'GITHUB_WORKFLOW_REF': WORKFLOW, 'GITHUB_REF_PROTECTED': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
        'GITHUB_SHA': releases['C']['source_sha'],
    }
    require(all(os.environ.get(key) == value for key, value in expected.items()), 'Unexpected provider context')
    run_id, run_attempt = os.environ['GITHUB_RUN_ID'], os.environ['GITHUB_RUN_ATTEMPT']
    require(ID.fullmatch(run_id) and re.fullmatch(r'[1-9][0-9]{0,8}', run_attempt), 'Invalid provider run identity')
    return scope, role, {'run_id': run_id, 'run_attempt': int(run_attempt)}


def native_request(scope):
    fields = [REPOSITORY_ID, scope['physical_key'], scope['releases']['C']['source_sha'], 'submit']
    return 'native_' + hashlib.sha256(json.dumps(fields, separators=(',', ':')).encode()).hexdigest()


def approved_request(scope, directory):
    fields = [os.environ.get(name, '') for name in SIGNED_FIELDS]
    signature = base64.b64decode(os.environ['KPOOL_AUTHORIZATION'], validate=True)
    require(len(signature) == 64 and all(0 < len(value) <= 12000 and '\0' not in value for value in fields),
            'Invalid signed hotfix input')
    public_key = os.environ['KPOOL_DISPATCH_PUBLIC_KEY'].strip()
    require(public_key.startswith('-----BEGIN PUBLIC KEY-----') and len(public_key) <= 1000,
            'Missing scoped dispatch public key')
    (directory / 'dispatch.pub').write_text(public_key + '\n', encoding='ascii')
    (directory / 'dispatch.raw').write_bytes(b'kpool.workflow-dispatch.v1\n' + b'\0'.join(value.encode() for value in fields))
    (directory / 'dispatch.sig').write_bytes(signature)
    result = subprocess.run((OPENSSL, 'pkeyutl', '-verify', '-pubin', '-inkey', 'dispatch.pub', '-rawin',
                             '-in', 'dispatch.raw', '-sigfile', 'dispatch.sig'),
                            cwd=directory, capture_output=True, timeout=10, check=False)
    require(result.returncode == 0, 'Hotfix signature verification failed')
    intent, pin, correlation, target_context = json.loads(fields[0]), fields[1], fields[2], json.loads(fields[3])
    release = scope['releases']['H']
    expected = {'schema_version': 1, 'target_id': scope['target_id'], 'source_sha': release['source_sha'],
                'repository_id': REPOSITORY_ID, 'policy_revision': scope['policy_revision'],
                'workflow_ref': WORKFLOW, 'ref': REF, 'operation_kind': 'hotfix'}
    require(pin == scope['releases']['C']['source_sha'] and REQUEST.fullmatch(correlation)
            and all(intent.get(key) == value for key, value in expected.items())
            and re.fullmatch(r'op_[a-f0-9]{32}', intent['request_id'])
            and not ({'priority', 'target_command'} & intent.keys()), 'Dispatch differs from the isolated hotfix scope')
    arguments = intent['arguments']
    approval_id = arguments.get('approval_id')
    require(isinstance(approval_id, str) and re.fullmatch(r'ap_[a-f0-9]{32}', approval_id), 'Invalid owner approval ID')
    require(arguments.get('kind') == 'hotfix' and arguments.get('source_sha') == release['source_sha']
            and arguments.get('skip_optional_checks', []) == scope['skip_optional_checks']
            and set(arguments) <= {'kind', 'source_sha', 'reason', 'skip_optional_checks', 'approval_id'},
            'Unexpected approved hotfix arguments')
    require(set(target_context) == {'config_revision', 'expected_generation', 'expires_at'}
            and target_context['config_revision'] == release['config_revision']
            and type(target_context['expected_generation']) is int
            and target_context['expected_generation'] == scope['base_generation']
            and type(target_context['expires_at']) is int and target_context['expires_at'] > time.time(),
            'Dispatch approval is expired or does not bind the expected target revision')
    return intent['request_id'], target_context['expires_at']


def command_for(scope, role, approval=None):
    release = scope['releases'][role]
    command = {'schema_version': 1, 'target_id': scope['target_id'], 'kind': 'submit', 'repository_id': REPOSITORY_ID,
               'request_id': approval[0] if role == 'H' else native_request(scope), 'artifact': release['artifact'],
               'source_sha': release['source_sha'], 'config_revision': release['config_revision'],
               'policy_revision': scope['policy_revision'], 'expected_generation': scope['base_generation']}
    return {**command, 'priority': 'hotfix'} if role == 'H' else command


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http_json(url, token):
    request = Request(url, headers={'Authorization': 'Bearer ' + token,
                                    'Accept': 'application/vnd.github+json', 'User-Agent': 'tideline-a04-pilot'})
    with build_opener(ProxyHandler({}), NoRedirects()).open(request, timeout=10) as response:
        payload = response.read(REPLY_LIMIT + 1)
        require(len(payload) <= REPLY_LIMIT, 'Provider reply exceeded the pilot limit')
        return json.loads(payload), 'rel="next"' in response.headers.get('Link', '')


def verify_checks(scope, role):
    token = os.environ['KPOOL_CHECK_TOKEN']
    require(token and len(token) <= 4096 and not any(char.isspace() for char in token), 'Missing scoped check token')
    base = f'https://api.github.com/repos/{REPO}'
    repo, _ = http_json(base, token)
    require(str(repo.get('id')) == REPOSITORY_ID and repo.get('full_name') == REPO, 'Check repository is outside the pilot')
    source = scope['releases'][role]['source_sha']
    endpoint = f'{base}/commits/{source}'
    runs, more = http_json(f'{endpoint}/check-runs?filter=all&per_page=100', token)
    statuses, status_page = http_json(f'{endpoint}/statuses?per_page=100', token)
    require(not more and not status_page and len(runs['check_runs']) == runs['total_count'], 'GitHub check list is incomplete')
    legacy = {row['context'] for row in statuses}
    pins = check_pins(scope['required_checks']) | check_pins(scope['optional_checks'])
    if role == 'H':
        pins -= check_pins(scope['skip_optional_checks'])
    for name, app_id in sorted(pins):
        matches = [row for row in runs['check_runs'] if row.get('name') == name and str((row.get('app') or {}).get('id')) == app_id]
        require(name not in legacy and len(matches) == 1, 'An app-pinned required check is missing or ambiguous')
        row = matches[0]
        require(ID.fullmatch(str(row['id'])) and row.get('url') == f'{base}/check-runs/{row["id"]}'
                and (row.get('head_sha'), row.get('status'), row.get('conclusion')) == (source, 'completed', 'success'),
                'An app-pinned required check did not pass at the exact source')


def oidc(target_id):
    address = urlsplit(os.environ['ACTIONS_ID_TOKEN_REQUEST_URL'])
    hostname = address.hostname or ''
    require(address.scheme == 'https' and hostname.endswith('.actions.githubusercontent.com')
            and address.netloc == hostname and not address.fragment, 'Unexpected GitHub token endpoint')
    parameters = parse_qsl(address.query, keep_blank_values=True)
    require(not any(name == 'audience' for name, _ in parameters), 'Unexpected token endpoint audience')
    audience = urlencode({'audience': f'kpool-target:{target_id}'})
    query = f'{address.query}&{audience}' if address.query else audience
    payload, _ = http_json(urlunsplit(address._replace(query=query)), os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN'])
    return payload['value']


def request(directory, role, command):
    host, port = os.environ['KPOOL_SSH_HOST'], os.environ.get('KPOOL_SSH_PORT') or '22'
    username = os.environ[f'KPOOL_SSH_{role.upper()}_USER']
    require(re.fullmatch(r'[a-zA-Z0-9.-]{1,253}', host) and re.fullmatch(r'[1-9][0-9]{0,4}', port)
            and int(port) <= 65535 and re.fullmatch(r'[a-z_][a-z0-9_-]{1,62}', username), 'Invalid scoped SSH address')
    token = oidc(command['target_id'])
    identity = Path(os.environ['RUNNER_TEMP']) / 'a04-keys' / role
    args = ('/usr/bin/ssh', '-F', '/dev/null', '-T', '-i', str(identity), '-p', port, '-l', username,
            '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none', '-o', 'IdentityFile=none',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'GlobalKnownHostsFile=/dev/null',
            '-o', f'UserKnownHostsFile={directory / "hosts"}', '-o', 'ClearAllForwardings=yes', '-o', 'UpdateHostKeys=no',
            '-o', 'PreferredAuthentications=publickey', '-o', 'ConnectTimeout=10', '--', host, 'kpool-target-v1')
    envelope = {'schema_version': 1, 'command': command, 'oidc_token': token}
    process = subprocess.run(args, input=json.dumps(envelope).encode(), capture_output=True, timeout=40, check=False, env={})
    if process.returncode:
        raise RuntimeError('The scoped SSH identity did not return a target reply')
    require(len(process.stdout) <= REPLY_LIMIT and token.encode() not in process.stdout, 'Unsafe target reply')
    reply = json.loads(process.stdout)
    require(reply.get('target_id') == command['target_id'] and reply.get('correlation_id')
            == command.get('request_id', command.get('query_id')), 'Wrong target or request in reply')
    return reply


def query(scope, kind, query_id, action=None):
    command = {'schema_version': 1, 'target_id': scope['target_id'], 'kind': kind, 'query_id': query_id}
    if action is not None:
        command['lookup_request_id'] = action
    return command


def wait_for(exchange, command, predicate, deadline):
    while time.monotonic() < deadline:
        try:
            reply = exchange('query', command)
        except (RuntimeError, OSError, subprocess.TimeoutExpired):
            time.sleep(4)
            continue
        if predicate(reply):
            return reply
        require(reply['status'] not in {'not_applied', 'rejected'}, 'Target rejected or ended the request')
        time.sleep(3)
    raise TimeoutError('Timed out waiting for the scoped target')


def queued(reply, scope, release):
    require((reply['status'], reply.get('reason_code'), reply['generation'])
            == ('accepted', 'QUEUED', scope['base_generation']) and not reply.get('ledger_records'),
            f'{release} was outside the expected queue window')


def terminal(scope, role, reply, request_id, origin=None):
    version = scope['releases'][role]
    generation = scope['base_generation'] + {'A': 0, 'H': 1, 'C': 2}[role]
    require((reply['status'], reply.get('verification'), reply['generation'], reply.get('observed_artifact'),
             reply.get('config_revision')) == ('applied', 'passed', generation, version['artifact'], version['config_revision']),
            f'The {role} route did not verify its expected artifact and revision')
    evidence = {item['evidence_id']: item for item in reply['evidence'] if item['generation'] == generation
                and item.get('artifact') == version['artifact'] and item['evidence_id'] in reply['evidence_ids']}
    require({item['source'] for item in evidence.values()} >= {'target', 'probe'}, f'{role} lacks route or probe evidence')
    records = reply.get('ledger_records', [])
    if role == 'A':
        require(not records, 'The isolated A seed unexpectedly has a provider origin')
        return max(datetime.fromisoformat(item['observed_at']) for item in evidence.values())
    require(len(records) == 1, f'{role} lacks a unique target ledger record')
    record = records[0]
    previous = scope['releases']['A' if role == 'H' else 'H']['artifact']
    expected = {'action_id': request_id, 'kind': 'deploy', 'outcome': 'applied', 'generation': generation,
                'observed_artifact': version['artifact'], 'previous_artifact': previous, 'source_sha': version['source_sha'],
                'policy_revision': scope['policy_revision']}
    run_id, attempt = record.get('origin_run_id'), record.get('origin_run_attempt')
    require(all(record.get(key) == value for key, value in expected.items()) and isinstance(run_id, str)
            and ID.fullmatch(run_id) and type(attempt) is int and attempt > 0
            and (origin is None or (run_id, attempt) == (origin['run_id'], origin['run_attempt']))
            and (record.get('priority'), record.get('config_revision'))
            == (('hotfix', version['config_revision']) if role == 'H' else (None, None))
            and set(record['evidence_ids']) <= evidence.keys()
            and {evidence[key]['source'] for key in record['evidence_ids']} >= {'target', 'probe'},
            f'{role} has a mismatched target ledger record')
    return datetime.fromisoformat(record['observed_at'])


def exercise(scope, role, origin, command, exchange, *, expiry=None):
    releases = scope['releases']
    a_id, c_id, action = releases['A']['request_id'], native_request(scope), command['request_id']
    a_route = query(scope, 'inspect', 'a04-live-a')
    before = wait_for(exchange, a_route, lambda reply: reply['status'] == 'applied', time.monotonic() + 360)
    require((before['generation'], before.get('active_request_id'), before.get('observed_artifact'), before.get('config_revision'),
             before.get('policy_revision'), before.get('mode'), before.get('frozen'))
            == (scope['base_generation'], a_id, releases['A']['artifact'], releases['A']['config_revision'],
                scope['policy_revision'], 'enabled', False), 'A is outside its expected observation window')
    ordinary_queued = None
    if role == 'H':
        ordinary_queued = exchange('query', query(scope, 'status', 'a04-ordinary-before-h', c_id))
        queued(ordinary_queued, scope, 'C')
        require(expiry > time.time(), 'Signed hotfix approval expired while enrolling')
    admission = exchange(WRITER[role], command)
    queued(admission, scope, role)
    print('A04_QUEUED=' + json.dumps({**origin, 'role': role, 'target_id': scope['target_id'],
                                     'request_id': action, 'generation': admission['generation']}), flush=True)

    deadline = time.monotonic() + 850
    status_request = query(scope, 'status', 'poll_' + action, action)
    reply = wait_for(exchange, status_request, lambda value: value['status'] == 'applied', deadline)
    observed = terminal(scope, role, reply, action, origin)
    prior = successor = None
    if role == 'H':
        prior = exchange('query', query(scope, 'status', 'poll_' + a_id, a_id))
        successor = wait_for(exchange, query(scope, 'status', 'poll_' + c_id, c_id),
                             lambda value: value['status'] == 'applied', deadline)
        require(terminal(scope, 'A', prior, a_id) < observed < terminal(scope, 'C', successor, c_id),
                'The target did not verify A, H and C in order')

    replay = exchange(WRITER[role], command)
    original = {key: value for key, value in reply.items() if key != 'ledger_records'}
    require(replay == {**original, 'correlation_id': action} and exchange('query', status_request) == reply,
            'Same-run replay changed the original reply or ledger')
    return {**origin, 'role': role, 'execution_sha': releases['C']['source_sha'], 'workflow_ref': WORKFLOW,
            'command': command, 'before': before, 'ordinary_queued': ordinary_queued, 'queued': admission,
            'terminal': reply, 'prior': prior, 'successor': successor, 'replay': replay}


def main():
    scope, role, origin = context()
    os.umask(0o077)
    with TemporaryDirectory(dir=os.environ['RUNNER_TEMP']) as root:
        directory = Path(root)
        (directory / 'hosts').write_text(os.environ['KPOOL_SSH_KNOWN_HOSTS'].strip() + '\n', encoding='ascii')
        if role == 'C':
            require(not any(os.environ.get(name) for name in (*SIGNED_FIELDS, 'KPOOL_AUTHORIZATION')),
                    'The ordinary run received signed operation inputs')
        approval = approved_request(scope, directory) if role == 'H' else None
        verify_checks(scope, role)
        proof = exercise(scope, role, origin, command_for(scope, role, approval),
                         lambda identity, command: request(directory, identity, command), expiry=approval[1] if approval else None)
    with (Path(os.environ['RUNNER_TEMP']) / 'a04-evidence.json').open('x', encoding='utf-8') as output:
        json.dump(proof, output, indent=2)
        output.write('\n')


if __name__ == '__main__':
    try:
        main()
    except PilotError as error:
        sys.exit(str(error))
    except (KeyError, OSError, RuntimeError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        sys.exit('A04 pilot could not verify its inputs or complete the scoped target exchange')
