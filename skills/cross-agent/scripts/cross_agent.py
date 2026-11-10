"""Trusted-host cross-agent dispatch. Child text is data, never authorization."""
import argparse
from dataclasses import asdict, dataclass, field, replace
import json
import math
import os
import pwd
import re
from pathlib import Path
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid

import cross_agent_state as state_store
import cli_transport as transport
from cross_agent_protocol import (ProtocolError, validate_binding, load_skill_context, adapter_call, prepare_stop)
from cross_agent_request import AgentGroup, FAMILIES, FORM_HINT, load_config, parse_request, resolve_agents, UnavailableError
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
    pass_env: tuple = ()
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
    required_roots: tuple = ()


@dataclass
class DispatchResult:
    schema_version: int = 1
    task_id: object = None
    target_identity: dict = field(default_factory=dict)
    results: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def _temp_roots():
    return tuple(Path(p) for p in ('/tmp', '/private/tmp', '/var/tmp')) + (Path(tempfile.gettempdir()).resolve(),)


def _under_temp(path, roots):
    """True when the normalized or the symlink-resolved path is under a root as given or resolved."""
    path = Path(os.path.normpath(path))
    resolved = path.resolve()
    candidates = tuple(roots) + tuple(root.resolve() for root in roots)
    return any(path.is_relative_to(root) or resolved.is_relative_to(root) for root in candidates)


def _temp_warnings(paths, roots):
    """Warn for each state path under a temporary directory, which a reboot may clear."""
    warnings = []
    for key in ('artifact_root', 'registry_path'):
        path = paths.get(key)
        if path is None:
            continue
        path = Path(path)
        if _under_temp(path, roots):
            warnings.append('%s under a temporary directory: %s' % (key, path))
    return warnings


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


def _codex_turn_context(thread_id, env=None):
    """Model and effort of the last turn in the child's Codex rollout for thread_id.

    codex exec --json prints no model; Codex records it in turn_context events of
    <CODEX_HOME or HOME/.codex>/sessions/YYYY/MM/DD/rollout-*-<thread_id>.jsonl."""
    env = os.environ if env is None else env
    home = env.get('HOME') or pwd.getpwuid(os.getuid()).pw_dir
    root = Path(env.get('CODEX_HOME') or Path(home) / '.codex') / 'sessions'
    context = {}
    for path in sorted(root.glob('*/*/*/rollout-*-' + thread_id + '.jsonl')):
        for line in path.read_text('utf-8', errors='replace').splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if (isinstance(event, dict) and event.get('type') == 'turn_context'
                    and isinstance(event.get('payload'), dict)):
                context = {k: event['payload'].get(k) for k in ('model', 'effort')}
    return context


def _provider(family, run, requested_model=None, env=None):
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
        observed['model_basis'] = 'provider' if observed.get('actual_model') is not None else None
        if observed.get('actual_model') is None and parsed['session']:
            context = _codex_turn_context(parsed['session'], env)
            if context.get('model') is not None:
                observed['actual_model'] = context['model']
                observed['actual_effort'] = context.get('effort')
                observed['model_basis'] = 'provider-rollout'
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
        observed['model_basis'] = 'provider' if observed['actual_model'] is not None else None
        # claude -p reports no main-thread model, and modelUsage also lists subagent
        # models. The requested model's presence is only a necessary condition; the
        # identity rests on the launcher's own --model flag, recorded as model_basis.
        model_usage = payload.get('modelUsage')
        if (family == 'claude-code' and observed['actual_model'] is None
                and isinstance(model_usage, dict) and requested_model in model_usage):
            observed['actual_model'] = requested_model
            observed['model_basis'] = 'launch-flag+modelUsage'
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


def _usage(agent, reported, observed, decision, previous_run, run, round_no, adopted=False):
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
        rounds_previous=previous, profile=decision.profile)
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
    if adopted and previous_run is None and agent.family != 'codex':
        # Cumulative totals of an adopted session include usage from before adoption:
        # keep them as the session total and baseline, but this round is unmeasured.
        usage.update(tokens=None, cost_usd=None, cost_basis='unknown', prices_as_of=None,
                     target_total=transport._target_total(None, None, round_no),
                     delta_reason='adopted session: no baseline')
    if adopted and agent.family == 'codex':
        # Per-round usage is measured, but usage before adoption is unknown.
        usage['session_total']['complete'] = False
    _write(run / 'usage.json', usage)
    return usage


FENCE = re.compile(r'```[ \t]*(?:json)?[ \t]*\n(.*?)\n?```', re.DOTALL | re.IGNORECASE)


def _loads_result(text):
    """Parse raw JSON first; fall back to the JSON inside a single fenced block."""
    try:
        return json.loads(text)
    except ValueError:
        blocks = FENCE.findall(text.strip())
        if len(blocks) != 1:
            raise
        return json.loads(blocks[0].strip())


def _answer(text):
    try:
        value = _loads_result(text)
    except ValueError:
        raise ValueError('child result is not JSON v1: ' + text.strip()[:80]) from None
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
            # Keep the child's question resumable: the host answers from output.
            return {'status': value['status'], 'output': value['output'], 'handoff_invalid': True}
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
    orchestrator = (family == 'claude-code' and host.policy is not None
                    and 'orchestrate' in getattr(host.policy, 'local_actions', ()))
    delegation = ('You are a delegated orchestrator. Never dispatch cross-agent; your own '
                  'subagents and the review commands the assignment names are allowed.'
                  if orchestrator else
                  'You are a delegated agent. Never dispatch cross-agent or another agent.')
    lines = [delegation]
    if orchestrator:
        lines.append('No user is attached. A decision for the owner is status USER_INPUT_REQUIRED '
                     'with handoff question, options, evidence and consultants (lists, may be '
                     'empty); put your recommendation first in options. BLOCKED is only for a '
                     'blocker no answer can lift; start its output with "Blocker: <one line>".')
    lines += [
             'Return only raw JSON, no code fence, no text before or after: '
             'schema_version=1, status, output (text).',
             'Statuses: ' + ', '.join(sorted(STATUSES)) + '.',
             'For consultation/user input include handoff with all four fields, for example: '
             + json.dumps({'handoff': {'question': 'Which option?', 'options': ['A', 'B'],
                                       'evidence': [], 'consultants': []}}) + '.',
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
        cancelled = adopted = False
        round_no = 1
        try:
            single = replace(request, agent_groups=(group,))
            if result.task_id:
                single = replace(single, task_selector=result.task_id)
            if isinstance(agent, UnavailableError):
                raise agent
            if agent.family == 'claude-code' and agent.profile == 'read-only':
                # The restricted Claude child reads only its working directory.
                workspace = Path(host.workspace).resolve()
                for root in host.required_roots:
                    if not Path(root).resolve().is_relative_to(workspace):
                        raise UnavailableError('read-only profile cannot read: ' + root)
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
                    adopted = registry['sessions'][decision.session_id].get('adopted') is True
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
            resume = decision.resume and decision.cli_session_id is not None
            cli_id = decision.cli_session_id if resume else str(uuid.uuid4())
            # Only Claude Code takes the launcher's ID; recording it lets --status
            # read the transcript of a fresh run before the provider confirms it.
            launch_id = cli_id if not resume and agent.family == 'claude-code' else None
            run_id = reserve_session(host.task_context, decision, str(run), launch_id)
            entry['run_id'] = run_id
            context = transport.ProfileContext(decision.task_id, host.task_context.conversation_id,
                                               decision.role, decision.profile, host.policy)
            builder = transport.build_resume_command if resume else transport.build_run_command
            argv = builder(agent.family, Path(host.workspace), run, agent.model, agent.effort,
                           cli_id, profile=decision.profile, context=context)
            if agent.family == 'antigravity':
                transport.check_agy_isolation(env)
            child_env = transport.reviewer_environment(env, agent.family, pass_env=host.pass_env,
                                                       context=context)
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
                text, observed, reported = _provider(agent.family, run, agent.model, child_env)
                entry['observed_identity'] = observed
                entry['usage'] = _usage(agent, reported, observed, decision, previous_run, run,
                                        round_no, adopted)
                entry['artifacts']['usage'] = str(run / 'usage.json')
            except (OSError, ValueError, TypeError) as exc:
                if entry['status'] == 'COMPLETED':
                    raise
                entry.setdefault('metadata_errors', []).append(str(exc))
            if entry['status'] == 'COMPLETED':
                identity = transport.validate_actual_identity(agent.model, agent.effort,
                                      decision.cli_session_id if resume else None, observed,
                                      effort_reported=agent.family != 'claude-code')
                entry.update(identity)
                # claude -p never reports effort; as for resume, session and model attest.
                entry['usage']['identity_attested'] = all(
                    value != 'unknown' for key, value in identity.items()
                    if not (key == 'actual_effort' and agent.family == 'claude-code'))
                _write(run / 'usage.json', entry['usage'])
                fields = {k: v for k, v in identity.items() if v != 'unknown'}
                # A known provider ID owns its continuity boundary even when
                # incomplete model/effort evidence cannot authorize a resume.
                if identity['cli_session_id'] != 'unknown':
                    fields['observed_cli_session_id'] = identity['cli_session_id']
                # A session can resume only when provider identity is complete.
                # claude -p never reports effort; its resume rests on session and model.
                if identity['actual_model'] == 'unknown' or (
                        identity['actual_effort'] == 'unknown' and agent.family != 'claude-code'):
                    fields.pop('cli_session_id', None)
                if fields:
                    update_session(host.task_context, decision.session_id, run_id, **fields)
                entry.update(_answer(text))
                if entry['status'] == 'BLOCKED' and not entry.get('reason'):
                    entry['reason'] = entry.get('output', '').strip().split('\n', 1)[0]
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


def claude_transcript(workspace, cli_session_id, config_dir=None, env=None):
    """Where Claude Code keeps a session started in workspace; resume finds only this one.

    env is the child environment; an explicit config_dir wins over it."""
    env = os.environ if env is None else env
    home = env.get('HOME') or pwd.getpwuid(os.getuid()).pw_dir
    root = Path(config_dir) if config_dir else Path(
        env.get('CLAUDE_CONFIG_DIR') or Path(home) / '.claude')
    slug = re.sub(r'[^A-Za-z0-9]', '-', str(Path(workspace)))
    return root / 'projects' / slug / (cli_session_id + '.jsonl')


def _claude_child_env(host):
    """The environment dispatch gives a Claude Code child, without per-run markers."""
    env = dict(os.environ if host.environment is None else host.environment)
    return transport.reviewer_environment(env, 'claude-code', pass_env=host.pass_env)


def adopt(host, request, cli_session_id, config_dir=None):
    if len(request.agent_groups) != 1:
        raise ValueError('adopt requires exactly one agent')
    agent = resolve_agents(request, host.config, host.host_family)[0]
    if agent.family == 'claude-code':
        transcript = claude_transcript(host.workspace, str(cli_session_id), config_dir,
                                       _claude_child_env(host))
        if not transcript.is_file():
            raise ValueError('ADOPT_TRANSCRIPT_MISSING: no session %s in the workspace project %s'
                             % (cli_session_id, transcript.parent))
    return state_store.adopt_session(host.task_context, agent, cli_session_id)


def _transcript_tail(path, limit=262144):
    """Last main-thread assistant text and tool call; subagent (sidechain) rows are skipped."""
    text = tool = at = None
    try:
        with open(path, 'rb') as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - limit))
            rows = handle.read().decode('utf-8', errors='replace').splitlines()
    except OSError:
        return None, None, None
    for line in reversed(rows):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get('type') != 'assistant' or row.get('isSidechain'):
            continue
        message = row.get('message')
        content = message.get('content') if isinstance(message, dict) else None
        for item in reversed(content if isinstance(content, list) else []):
            if not isinstance(item, dict):
                continue
            if tool is None and item.get('type') == 'tool_use':
                given = item.get('input') if isinstance(item.get('input'), dict) else {}
                tool = '%s: %s' % (item.get('name'), given.get('description') or given.get('command')
                                   or given.get('file_path') or '')
                tool = tool.rstrip(': ')[:200]
            if text is None and item.get('type') == 'text' and item.get('text', '').strip():
                text = item['text'].strip()[:500]
        at = at or row.get('timestamp')
        if text is not None and tool is not None:
            break
    return text, tool, at


_ESTIMATE_KEYS = ('input_tokens', 'output_tokens', 'cache_creation_input_tokens',
                  'cache_read_input_tokens')


def _count_or_none(value):
    return value if type(value) is int and value >= 0 else None


def _estimate_usage(row):
    """(key, model, usage) of an assistant row; usage None when its required counts
    are missing or invalid; None for any row that carries no assistant usage."""
    if not isinstance(row, dict) or row.get('type') != 'assistant':
        return None
    message = row.get('message')
    if not isinstance(message, dict) or not isinstance(message.get('usage'), dict):
        return None
    raw = message['usage']
    usage = {name: _count_or_none(raw.get(name)) for name in ('input_tokens', 'output_tokens')}
    for name in ('cache_creation_input_tokens', 'cache_read_input_tokens'):
        # Cache counters are optional: absent or null is 0, anything else must be a count.
        usage[name] = 0 if raw.get(name) is None else _count_or_none(raw[name])
    if any(value is None for value in usage.values()):
        usage = None
    key = next((value for value in (message.get('id'), row.get('requestId'))
                if isinstance(value, str) and value), None)
    model = message.get('model') if isinstance(message.get('model'), str) else 'unknown'
    return key, model, usage


def transcript_estimate(path):
    """Estimated tokens and cost of a Claude Code session from its transcripts.

    Reads the main transcript and its subagents/agent-*.jsonl files line by line and
    sums message.usage of assistant rows per message.model. One API message spans
    several rows, so each message.id (else requestId, else the row) counts once across
    all files, as its copy with the largest output_tokens; this also drops subagent
    rows a main file repeats. A row without valid input/output counts, or an
    unreadable subagent file, makes the estimate incomplete; an unreadable main
    transcript gives None. Prices come from cli_transport; a model missing there
    makes the cost null. Not billing data.
    """
    path = Path(path)
    if not path.is_file():
        return None
    subagents = path.with_suffix('') / 'subagents'
    extra = sorted(subagents.glob('agent-*.jsonl')) if subagents.is_dir() else []
    best, complete, unread = {}, True, []
    for index, source in enumerate([path] + extra):
        try:
            with open(source, 'rb') as handle:
                for number, line in enumerate(handle):
                    if b'"usage"' not in line:
                        continue
                    try:
                        parsed = _estimate_usage(json.loads(line))
                    except ValueError:
                        continue
                    if parsed is None:
                        continue
                    key, model, usage = parsed
                    if usage is None:
                        complete = False
                        continue
                    key = key or (index, number)
                    if key not in best or usage['output_tokens'] > best[key][1]['output_tokens']:
                        best[key] = (model, usage)
        except OSError:
            if source == path:
                return None
            complete = False
            unread.append(str(source))
    by_model = {}
    for model, usage in best.values():
        totals = by_model.setdefault(model, dict.fromkeys(_ESTIMATE_KEYS, 0))
        for name in _ESTIMATE_KEYS:
            totals[name] += usage[name]
    tokens, cost, unpriced, models = dict.fromkeys(_ESTIMATE_KEYS, 0), 0.0, [], {}
    for model, totals in sorted(by_model.items()):
        # Zero-usage rows (for example <synthetic> notices) cost nothing and name no model.
        if not any(totals.values()):
            continue
        written, read = totals['cache_creation_input_tokens'], totals['cache_read_input_tokens']
        model_cost = transport.claude_cost(model, {
            'input': totals['input_tokens'] + written + read, 'cached_input': read,
            'cache_write': written, 'output': totals['output_tokens'], 'reasoning': 0})
        models[model] = {'tokens': totals, 'cost_usd': model_cost}
        for name in _ESTIMATE_KEYS:
            tokens[name] += totals[name]
        if model_cost is None:
            unpriced.append(model)
        else:
            cost += model_cost
    return {'tokens': tokens, 'cost_usd': None if unpriced else round(cost, 4),
            'by_model': models, 'basis': 'transcript-estimate',
            'prices_as_of': transport.PRICES_AS_OF, 'subagent_files': len(extra),
            'unpriced_models': unpriced, 'complete': complete, 'unread_files': unread}


def _elapsed(run):
    """Run duration: until now while running, until finished_at once terminal."""
    if not run.get('started_at'):
        return None
    end = time.time() if run.get('status') == 'running' else run.get('finished_at')
    return round(end - run['started_at']) if end is not None else None


def status(host, config_dir=None):
    """Read-only view of this repository's sessions, newest run first."""
    rows = []
    data = state_store.read_registry(host.task_context)
    sessions = [s for s in data['sessions'].values() if s['repo'] == host.task_context.repo]
    tasks = data['tasks']
    child_env = _claude_child_env(host)
    for session in sessions:
        run = session['runs'][-1] if session['runs'] else {}
        alive = None
        if run.get('status') == 'running':
            try:
                os.kill(run['pid'], 0)
                alive = True
            except ProcessLookupError:
                alive = False
            except PermissionError:
                alive = True
        cli = session.get('cli_session_id') or session.get('observed_cli_session_id')
        launch = run.get('launch_cli_session_id') if run.get('status') == 'running' else None
        row = {'session_id': session['session_id'], 'task': tasks[session['task_id']]['canonical_issue']
               or session['task_id'], 'family': session['family'], 'model': session['model'],
               'effort': session['effort'], 'cli_session_id': cli,
               'launch_cli_session_id': launch, 'state': session['status'],
               'pid': run.get('pid'), 'alive': alive, 'started_at': run.get('started_at'),
               'elapsed_seconds': _elapsed(run),
               'last_text': None, 'last_tool': None, 'last_at': None, 'estimate': None}
        if session['family'] == 'claude-code' and (cli or launch):
            transcript = claude_transcript(host.workspace, cli or launch, config_dir, child_env)
            row['last_text'], row['last_tool'], row['last_at'] = _transcript_tail(transcript)
            row['estimate'] = transcript_estimate(transcript)
        rows.append(row)
    rows.sort(key=lambda r: r['started_at'] or 0, reverse=True)
    return {'schema_version': 1, 'sessions': rows}


DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / 'config' / 'default.json'
MAX_ASSIGNMENT_BYTES = 1024 * 1024


def read_assignment_file(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError('assignment file must be a regular file')
            data = source.read(MAX_ASSIGNMENT_BYTES + 1)
        if len(data) > MAX_ASSIGNMENT_BYTES:
            raise ValueError('assignment file exceeds 1 MiB limit')
        return data.decode('utf-8')
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError('cannot read assignment file as UTF-8: ' + str(exc)) from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host-context', required=True, help='Explicit trusted host JSON, never child-provided')
    parser.add_argument('--config', default=str(DEFAULT_CONFIG),
                        help='Trusted agent configuration JSON; defaults to the bundled config/default.json')
    parser.add_argument('--adopt-cli-session', metavar='UUID',
                        help='Bind this existing provider session to a registry session, print its ID and exit')
    parser.add_argument('--assignment-file', metavar='PATH', help='Read assignment as strict UTF-8')
    parser.add_argument('--status', action='store_true',
                        help='Print the sessions of this repository with their last activity and exit')
    parser.add_argument('request', nargs='?', help='One literal bracket request argument; no shell evaluation')
    args = parser.parse_args(argv)
    raw = json.loads(Path(args.host_context).read_text('utf-8'))
    task = TaskContext(**raw.pop('task_context'))
    role = raw.pop('role')
    profile = raw.pop('profile')
    if 'policy' in raw:
        policy = raw['policy']
        policy['local_actions'] = tuple(policy['local_actions'])
        raw['policy'] = transport.VerifiedProfilePolicy(**policy)
    if 'pass_env' in raw:
        raw['pass_env'] = tuple(transport.validate_pass_env(raw['pass_env']))
    roots = raw.get('required_roots', [])
    if (not isinstance(roots, list)
            or any(not isinstance(p, str) or not os.path.isabs(p) for p in roots)):
        print(json.dumps({'schema_version': 1, 'status': 'BLOCKED',
                          'reason': 'required_roots must be a list of absolute path strings'}))
        return 2
    raw['required_roots'] = tuple(roots)
    host = HostContext(config=load_config(host_config=args.config, role=role, profile=profile),
                       task_context=task, **raw)
    if args.status:
        print(json.dumps(status(host), ensure_ascii=False, indent=1))
        return 0
    if args.request is None:
        print(json.dumps({'schema_version': 1, 'status': 'BLOCKED',
                          'reason': 'request is required: ' + FORM_HINT}))
        return 2
    try:
        assignment = None
        if args.assignment_file is not None:
            assignment = read_assignment_file(args.assignment_file)
        request = parse_request(args.request, assignment)
    except ValueError as exc:
        print(json.dumps({'schema_version': 1, 'status': 'BLOCKED', 'reason': str(exc)}))
        return 2
    if args.adopt_cli_session:
        try:
            session_id = adopt(host, request, args.adopt_cli_session)
        except ValueError as exc:
            print(json.dumps({'schema_version': 1, 'status': 'BLOCKED', 'reason': str(exc)}))
            return 2
        print(json.dumps({'schema_version': 1, 'status': 'COMPLETED', 'session_id': session_id}))
        return 0
    warnings = _temp_warnings({'artifact_root': host.artifact_root,
                               'registry_path': task.registry_path}, _temp_roots())
    try:
        result = dispatch(request, host)
    except (ValueError, transport.CrossReviewError) as exc:
        print(json.dumps({'schema_version': 1, 'status': 'BLOCKED', 'reason': str(exc),
                          'warnings': warnings}))
        return 2
    result.warnings = warnings
    print(json.dumps(result.to_dict()))
    return 0 if all(r['status'] == 'COMPLETED' for r in result.results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
