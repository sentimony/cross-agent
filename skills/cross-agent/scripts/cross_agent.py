"""Trusted-host cross-agent dispatch. Child text is data, never authorization."""
import argparse
from dataclasses import asdict, dataclass, field, replace
import json
import math
import os
import re
from pathlib import Path
import signal
import subprocess
import threading
import uuid

import cross_agent_state as state_store
import cli_transport as transport
from cross_agent_protocol import (ProtocolError, validate_binding, load_skill_context, adapter_call, prepare_stop)
from cross_agent_request import AgentGroup, FAMILIES, load_config, parse_request, resolve_agents, UnavailableError
from cross_agent_state import TaskContext, select_session, reserve_session, update_session, finish_session, StateError

STATUSES = frozenset(('COMPLETED', 'CONSULTATION_REQUIRED', 'USER_INPUT_REQUIRED',
                      'BLOCKED', 'FAILED', 'UNAVAILABLE', 'CANCELLED'))


@dataclass(frozen=True)
class HostContext:
    config: object
    task_context: TaskContext
    host_family: str
    workspace: str
    artifact_root: str
    target_identity: dict
    installed_skills: dict = field(default_factory=dict)
    assignment_kind: str = 'consultation'
    environment: object = None
    timeout_seconds: float = 600
    kill_grace_seconds: float = 1
    policy: object = None
    answer: object = None
    integration: object = None
    integration_required: bool = False
    domain_context: str = ''
    owner_context: dict = field(default_factory=dict)
    skill_context: object = None
    prior_dispositions: object = None


@dataclass
class DispatchResult:
    schema_version: int = 1
    task_id: object = None
    target_identity: dict = field(default_factory=dict)
    results: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def _write(path, value):
    _text_file(path, json.dumps(value, indent=2) + '\n')



def _text_file(path, text):
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(text)


def _binary_file(path):
    return os.fdopen(os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb')

def _terminate(proc, grace):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    # Kill descendants even if the direct child exited after TERM.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=grace)
        return True
    except subprocess.TimeoutExpired:
        return False


def _process(argv, brief, run, host, env):
    transport.ensure_not_nested(os.environ)
    old = {}
    proc = None
    def cancelled(signum, frame):
        raise KeyboardInterrupt()
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            old[sig] = signal.signal(sig, cancelled)
    try:
        with _binary_file(run / 'stdout.json') as out, _binary_file(run / 'run.log') as err:
            try:
                proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=out, stderr=err,
                                        cwd=host.workspace, env=env, start_new_session=True)
            except (FileNotFoundError, PermissionError) as exc:
                raise UnavailableError('configured CLI is unavailable: ' + str(exc)) from exc
            try:
                proc.communicate(brief.encode('utf-8'), timeout=host.timeout_seconds)
            except subprocess.TimeoutExpired:
                cleaned = _terminate(proc, host.kill_grace_seconds)
                proc = None
                return ('FAILED', 'TIMEOUT') if cleaned else ('FAILED', 'PROCESS_CLEANUP_UNCONFIRMED')
            except KeyboardInterrupt:
                cleaned = _terminate(proc, host.kill_grace_seconds)
                proc = None
                return ('CANCELLED', 'SIGNAL') if cleaned else ('CANCELLED', 'PROCESS_CLEANUP_UNCONFIRMED')
            return ('COMPLETED', '') if proc.returncode == 0 else ('FAILED', 'CLI_EXIT_%s' % proc.returncode)
    finally:
        if proc is not None and proc.poll() is None:
            _terminate(proc, host.kill_grace_seconds)
        for sig, handler in old.items():
            signal.signal(sig, handler)


def _provider(family, run):
    """Read only provider envelope fields, never identity in assistant content."""
    raw = (run / 'stdout.json').read_text('utf-8', errors='replace')
    observed = {}
    usage = {'tokens': None, 'cost_usd': None}
    if family == 'codex':
        events = []
        for line in raw.splitlines():
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    events.append(event)
            except ValueError:
                pass
        for event in events:
            if (event.get('type') == 'thread.started'
                    and event.get('thread_id') not in (None, 'unknown')
                    and not transport._is_uuid(event.get('thread_id'))):
                raise ValueError('invalid provider session ID')
        parsed = transport.parse_codex_events(raw)
        observed['cli_session_id'] = parsed['session']
        usage['tokens'] = parsed['tokens']
        # Provider session metadata only. Arbitrary item/assistant fields are ignored.
        for event in events:
            if event.get('type') == 'session_meta' and isinstance(event.get('payload'), dict):
                for dest, source in (('actual_model', 'model'), ('actual_effort', 'reasoning_effort')):
                    if source in event['payload']:
                        observed[dest] = event['payload'][source]
        answer = (run / 'review.md').read_text('utf-8') if (run / 'review.md').is_file() else ''
    else:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError('invalid provider envelope')
        observed['cli_session_id'] = payload.get('session_id' if family == 'claude-code' else 'conversation_id')
        if (observed['cli_session_id'] not in (None, 'unknown')
                and not transport._is_uuid(observed['cli_session_id'])):
            raise ValueError('invalid provider session ID')
        for dest, source in (('actual_model', 'model'), ('actual_effort', 'effort')):
            observed[dest] = payload.get(source)
        if family == 'claude-code':
            answer, parsed = transport.parse_claude_output(raw)
            if payload.get('is_error'):
                raise ValueError('provider error')
        else:
            answer, parsed = transport.parse_antigravity_output(raw)
            if payload.get('status') != 'SUCCESS':
                raise ValueError('provider error')
        if parsed:
            observed['cli_session_id'] = parsed.get('session')
            usage.update({k: parsed.get(k) for k in ('tokens', 'cost_usd')})
        _text_file(run / 'review.md', answer or '')
    return answer, observed, usage


def _provider_ids(family, run):
    """Extract only UUIDs in provider envelope identity fields, even on failure."""
    raw = (run / 'stdout.json').read_text('utf-8', errors='replace')
    ids = []
    for line in raw.splitlines() if family == 'codex' else [raw]:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        if family == 'codex':
            provider_id = value.get('thread_id') if value.get('type') == 'thread.started' else None
        else:
            provider_id = value.get('session_id' if family == 'claude-code' else 'conversation_id')
        if transport._is_uuid(provider_id) and provider_id not in ids:
            ids.append(provider_id)
    return ids


def _usage(agent, reported, observed, decision, previous_run, run, round_no):
    previous = transport._read_usage(previous_run) if previous_run is not None else None
    # Failed or mismatched identity evidence is not a cumulative accounting base,
    # even when its raw provider totals look complete.
    if (previous is not None and (previous.get('identity_attested') is not True
            or previous.get('provider_session_id') != decision.cli_session_id)):
        previous = None
    usage = transport.build_usage(agent.family, agent.model, agent.effort,
        reported['tokens'], reported['cost_usd'], [], previous_run=previous_run,
        previous=previous, session=observed.get('cli_session_id'),
        expected_session=decision.cli_session_id,
        round_no=round_no,
        rounds_previous=previous)
    usage['scope'] = 'round'
    usage['provider_session_id'] = observed.get('cli_session_id')
    usage['identity_attested'] = False
    usage['session_total']['scope'] = 'session'
    usage['provider_reported'] = dict(reported,
        scope='session' if agent.family != 'codex' else 'round')
    usage['delta_reason'] = None
    if decision.resume and agent.family != 'codex':
        _, _, usage['delta_reason'] = transport.resume_usage(
            reported['tokens'], reported['cost_usd'], previous,
            observed.get('cli_session_id'), decision.cli_session_id, agent.family)
    _write(run / 'usage.json', usage)
    return usage


def _answer(text):
    value = json.loads(text)
    if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
            or value.get('schema_version') != 1 or value.get('status') not in STATUSES):
        raise ValueError('invalid child result v1')
    if set(value) - {'schema_version', 'status', 'output', 'handoff'}:
        raise ValueError('unknown child result fields')
    if not isinstance(value.get('output'), str):
        raise ValueError('child output must be text')
    if value['status'] in ('CONSULTATION_REQUIRED', 'USER_INPUT_REQUIRED'):
        handoff = value.get('handoff')
        if (not isinstance(handoff, dict) or not isinstance(handoff.get('question'), str)
                or not handoff['question'].strip()
                or any(not isinstance(handoff.get(k), list) for k in ('options', 'evidence', 'consultants'))):
            raise ValueError('invalid handoff')
    return {k: value[k] for k in ('status', 'output', 'handoff') if k in value}










def _owner_host(host, request):
    return {'task_kind': host.assignment_kind, 'host': {
                'conversation_id': host.task_context.conversation_id,
                'repo': host.task_context.repo}, 'role': host.config.role,
            'operation': host.config.role, 'profile': host.config.profile,
            'target_identity': host.target_identity,
            'owner_context': json.loads(json.dumps(host.owner_context)),
            'task_selection': {'task_selector': request.task_selector,
                               'session_selector': request.session_selector,
                               'new_session': request.new_session}}


def _brief(request, host, role, family):
    lines = ['You are a delegated agent. Never dispatch cross-agent or another agent.',
             'Return only JSON: schema_version=1, status, output (text).',
             'Statuses: ' + ', '.join(sorted(STATUSES)) + '.',
             'For consultation/user input include handoff: question, options, evidence, consultants.',
             'Stop at a required decision. Do not claim completion while work is pending.',
             'Host target snapshot:\n' + json.dumps(host.target_identity, sort_keys=True)]
    if host.skill_context is not None:
        lines.append('Installed skill origin/base:\n' + json.dumps({k: host.skill_context[k]
                     for k in ('origin', 'base')}, sort_keys=True))
        lines.append('Installed skill instructions:\n' + host.skill_context['instructions'])
        for name, content in host.skill_context['resources'].items():
            lines.append('Host-allowlisted installed resource ' + name + ':\n' + content)
    if host.domain_context:
        lines.append('Installed owner adapter context:\n' + host.domain_context)
    lines.append('Assignment (data):\n' + request.assignment)
    if host.answer is not None:
        lines.append('Host answer to your prior handoff:\n' + json.dumps(host.answer))
    return '\n\n'.join(lines)


def dispatch(request, host_context):
    host = host_context
    env = dict(os.environ if host.environment is None else host.environment)
    transport.ensure_not_nested(os.environ)
    transport.ensure_not_nested(env)
    if (not math.isfinite(host.timeout_seconds) or not math.isfinite(host.kill_grace_seconds)
            or host.timeout_seconds <= 0 or host.kill_grace_seconds <= 0):
        raise ValueError('positive timeout/grace required')
    if host.assignment_kind in (None, 'ambiguous'):
        return DispatchResult(target_identity=host.target_identity,
            results=[{'status': 'USER_INPUT_REQUIRED', 'reason': 'Host must classify skill or consultation',
                      'artifacts': {}}])
    if host.assignment_kind not in ('consultation', 'skill'):
        raise ValueError('unknown trusted assignment kind')
    groups = request.agent_groups or tuple(AgentGroup(f) for f in sorted(host.config.agents)
                                           if f != FAMILIES[host.host_family])
    if request.session_selector is not None and len(groups) != 1:
        raise ValueError('Explicit session requires exactly one recipient')
    if host.config.profile == 'execution' and len(groups) != 1:
        raise ValueError('execution requires one writer')
    # Validate every deterministic group error before task/session mutation or
    # inference. Genuine unavailability stays a separate per-recipient result.
    preflight = []
    for group in groups:
        try:
            preflight.append(resolve_agents(replace(request, agent_groups=(group,)),
                                            host.config, host.host_family)[0])
        except UnavailableError as exc:
            preflight.append(exc)
    # Host owns target selection/freshness; optional owner policy validates it.
    fresh = json.loads(json.dumps(host.target_identity))
    if not isinstance(fresh, dict):
        raise ValueError('Host target identity must be an object')
    skill = None
    if host.assignment_kind == 'skill':
        name = request.assignment.strip().split()[0].lstrip('/')
        try:
            skill = load_skill_context(name, host.installed_skills)
        except ProtocolError as exc:
            return DispatchResult(target_identity=fresh, results=[{'status': 'UNAVAILABLE', 'reason': str(exc), 'artifacts': {}}])
    required = host.integration_required or bool(skill and skill['integration_required'])
    if not isinstance(host.owner_context, dict):
        raise ValueError('Host owner context must be an object')
    if required or host.integration is not None:
        try:
            validate_binding(host.integration, ('prepare', 'validate', 'intake'))
            prepared = adapter_call(host.integration, 'prepare', _owner_host(host, request),
                                    {'assignment': request.assignment}, timeout=host.timeout_seconds)
            stop = prepare_stop(prepared)
            if stop is not None:
                return DispatchResult(target_identity=fresh, results=[dict(stop, artifacts={})])
            host = replace(host, domain_context=prepared['context'])
        except ProtocolError as exc:
            return DispatchResult(target_identity=fresh, results=[{'status': 'UNAVAILABLE', 'reason': str(exc), 'artifacts': {}}])
    host = replace(host, skill_context=skill)
    host = replace(host, target_identity=fresh,
                   task_context=replace(host.task_context, canonical_issue=None)
                   if request.task_selector == 'new' else host.task_context)
    result = DispatchResult(target_identity=fresh)
    if not groups:
        result.results.append({'status': 'UNAVAILABLE', 'reason': 'no configured other agents'})
    for group, agent in zip(groups, preflight):
        entry = {'family': group.family, 'status': 'FAILED', 'operation': host.config.role, 'actual_model': 'unknown',
                 'actual_effort': 'unknown', 'cli_session_id': 'unknown',
                 'target_identity': result.target_identity, 'artifacts': {},
                 'usage': {'tokens': None, 'cost_usd': None}}
        result.results.append(entry)
        run = run_id = decision = previous_run = None
        cancelled = False
        round_no = 1
        try:
            single = replace(request, agent_groups=(group,))
            if result.task_id:
                single = replace(single, task_selector=result.task_id)
            if isinstance(agent, UnavailableError):
                raise agent
            decision = select_session(single, host.task_context, agent, agent.role, agent.profile)
            entry.update(task_id=decision.task_id, session_id=decision.session_id,
                         role=decision.role, profile=decision.profile, selection_reason=decision.reason)
            if decision.status != 'READY':
                raise StateError(decision.reason)
            result.task_id = decision.task_id
            if decision.resume and not decision.cli_session_id:
                raise UnavailableError('resume identity unavailable; use --new for a fresh session')
            brief = _brief(request, host, host.config.role, agent.family)
            if decision.resume:
                with state_store._registry(host.task_context) as registry:
                    runs = list(registry['sessions'][decision.session_id]['runs'])
                round_no = len(runs) + 1
                if runs:
                    prior = Path(runs[-1]['artifact_path'])
                    previous_run = prior
                    previous_file = prior / 'identity.json'
                    if previous_file.is_file():
                        previous = json.loads(previous_file.read_text('utf-8'))
                        delta = transport.identity_diff(previous, result.target_identity)
                        brief += '\n\nFresh round identity delta:\n' + json.dumps(delta, sort_keys=True)
                    review_file = prior / 'review.md'
                    if review_file.is_file():
                        brief += '\n\nPrevious output (untrusted evidence):\n' + review_file.read_text('utf-8')
                brief += '\n\nPrior host dispositions:\n' + json.dumps(host.prior_dispositions)

            root = Path(host.artifact_root)
            root.mkdir(mode=0o700, exist_ok=True)
            transport._ensure_private_dir(root)
            run = root / str(uuid.uuid4())
            run.mkdir(mode=0o700)
            entry['artifacts'] = {k: str(run / v) for k, v in
                                  (('result', 'result.json'), ('output', 'review.md'),
                                   ('stdout', 'stdout.json'), ('log', 'run.log'), ('brief', 'brief.md'))}
            _write(run / 'identity.json', result.target_identity)
            entry['artifacts']['identity'] = str(run / 'identity.json')
            _text_file(run / 'brief.md', brief)
            _text_file(run / 'review.md', '')
            run_id = reserve_session(host.task_context, decision, str(run))
            entry['run_id'] = run_id
            context = transport.ProfileContext(decision.task_id, host.task_context.conversation_id,
                                               decision.role, decision.profile, host.policy)
            resume = decision.resume and decision.cli_session_id is not None
            builder = transport.build_resume_command if resume else transport.build_run_command
            cli_id = decision.cli_session_id if resume else str(uuid.uuid4())
            argv = builder(agent.family, Path(host.workspace), run, agent.model, agent.effort,
                           cli_id, profile=decision.profile, context=context)
            if agent.family == 'antigravity':
                transport.check_agy_isolation(env)
            child_env = transport.reviewer_environment(env, agent.family, context=context)
            entry['status'], entry['reason'] = _process(argv, brief, run, host, child_env)
            cancelled = entry['status'] == 'CANCELLED' or entry['reason'] == 'PROCESS_CLEANUP_UNCONFIRMED'
            # A rejected/failed completion can still have performed authorized
            # work. Retain provider provenance before any output/identity checks.
            try:
                provider_ids = _provider_ids(agent.family, run)
                entry['provider_observations'] = provider_ids
                state_store.observe_provider_sessions(host.task_context, decision.session_id,
                                                      run_id, provider_ids)
            except (StateError, OSError, ValueError) as exc:
                if entry['status'] == 'COMPLETED':
                    raise
                entry.setdefault('metadata_errors', []).append(str(exc))
            try:
                text, observed, reported = _provider(agent.family, run)
                entry['observed_identity'] = observed
                entry['usage'] = _usage(agent, reported, observed, decision, previous_run, run, round_no)
                entry['artifacts']['usage'] = str(run / 'usage.json')
            except (OSError, ValueError, TypeError) as exc:
                if entry['status'] == 'COMPLETED':
                    raise
                entry.setdefault('metadata_errors', []).append(str(exc))
            if entry['status'] == 'COMPLETED':
                identity = transport.validate_actual_identity(agent.model, agent.effort,
                                      decision.cli_session_id if resume else None, observed)
                entry.update(identity)
                entry['usage']['identity_attested'] = all(value != 'unknown' for value in identity.values())
                _write(run / 'usage.json', entry['usage'])
                fields = {k: v for k, v in identity.items() if v != 'unknown'}
                # A known provider ID owns its continuity boundary even when
                # incomplete model/effort evidence cannot authorize a resume.
                if identity['cli_session_id'] != 'unknown':
                    fields['observed_cli_session_id'] = identity['cli_session_id']
                # A session can resume only when provider identity is complete.
                if identity['actual_model'] == 'unknown' or identity['actual_effort'] == 'unknown':
                    fields.pop('cli_session_id', None)
                if fields:
                    update_session(host.task_context, decision.session_id, run_id, **fields)
                entry.update(_answer(text))
                if host.integration is not None and entry['status'] == 'COMPLETED':
                    owner_host = dict(_owner_host(host, request), task_id=decision.task_id,
                                      session_id=decision.session_id, run_id=run_id,
                                      provider_id=entry['cli_session_id'])
                    validated = adapter_call(host.integration, 'validate', json.loads(json.dumps(owner_host)),
                        {'result': dict(entry)}, timeout=host.timeout_seconds)
                    if validated.get('accepted') is not True:
                        raise ProtocolError('Owner adapter rejected domain result')
                    intake = adapter_call(host.integration, 'intake', json.loads(json.dumps(owner_host)),
                        {'result': dict(entry)}, timeout=host.timeout_seconds)
                    if intake.get('accepted') is not True:
                        raise ProtocolError('Owner adapter rejected domain intake')
                    entry['integration'] = {k: host.integration[k] for k in
                        ('owner_id', 'adapter_id', 'contract_version', 'protocol_version')}
                    entry['domain_result'] = validated
                    entry['domain_intake'] = intake
        except UnavailableError as exc:
            entry.update(status='UNAVAILABLE', reason=str(exc))
        except StateError as exc:
            entry.update(status='BLOCKED', reason=str(exc))
        except transport.CrossReviewError as exc:
            entry.update(status='UNAVAILABLE' if 'UNAVAILABLE' in str(exc) else 'FAILED', reason=str(exc))
        except (OSError, ValueError, TypeError) as exc:
            entry.update(status='FAILED', reason=str(exc))
        finally:
            if run_id is not None:
                state = 'failed' if entry['status'] in ('FAILED', 'UNAVAILABLE', 'CANCELLED') else 'blocked' if entry['status'] == 'BLOCKED' else 'completed'
                try:
                    finish_session(host.task_context, decision.session_id, run_id, state,
                                   result_path=str(run / 'result.json'))
                except (StateError, OSError) as exc:
                    entry.setdefault('cleanup_errors', []).append('session release failed: ' + str(exc))
                    if entry['status'] not in ('FAILED', 'UNAVAILABLE', 'CANCELLED', 'BLOCKED'):
                        entry.update(status='FAILED', reason='session release failed: ' + str(exc))
            if run is not None:
                try:
                    for path in run.iterdir():
                        if path.is_file():
                            path.chmod(0o600)
                    _write(run / 'result.json', entry)
                except OSError as exc:
                    entry.setdefault('cleanup_errors', []).append('artifact persistence failed: ' + str(exc))
                    if entry['status'] not in ('FAILED', 'UNAVAILABLE', 'CANCELLED', 'BLOCKED'):
                        entry.update(status='FAILED', reason='artifact persistence failed: ' + str(exc))
        if cancelled:
            break
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host-context', required=True, help='Explicit trusted host JSON, never child-provided')
    parser.add_argument('--config', required=True, help='Explicit trusted agent configuration JSON')
    parser.add_argument('request', help='One literal bracket request argument; no shell evaluation')
    args = parser.parse_args(argv)
    raw = json.loads(Path(args.host_context).read_text('utf-8'))
    task = TaskContext(**raw.pop('task_context'))
    role = raw.pop('role')
    profile = raw.pop('profile')
    if 'policy' in raw:
        policy = raw['policy']
        policy['local_actions'] = tuple(policy['local_actions'])
        raw['policy'] = transport.VerifiedProfilePolicy(**policy)
    host = HostContext(config=load_config(host_config=args.config, role=role, profile=profile),
                       task_context=task, **raw)
    try:
        result = dispatch(parse_request(args.request), host)
    except (ValueError, transport.CrossReviewError) as exc:
        print(json.dumps({'schema_version': 1, 'status': 'BLOCKED', 'reason': str(exc)}))
        return 2
    print(json.dumps(result.to_dict()))
    return 0 if all(r['status'] == 'COMPLETED' for r in result.results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
