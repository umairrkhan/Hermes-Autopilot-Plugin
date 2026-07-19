from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))

try:
    import hermes_cli
except ImportError as exc:
    raise SystemExit(
        "Hermes Agent must be installed in the active Python environment to run this E2E."
    ) from exc

HERMES_SOURCE = Path(hermes_cli.__file__).resolve().parents[1]
sys.path.insert(0, str(HERMES_SOURCE))


def checked(argv, *, cwd=None, env=None):
    result = subprocess.run(
        [str(item) for item in argv], cwd=cwd, env=env,
        text=True, capture_output=True, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {argv}\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


with tempfile.TemporaryDirectory(prefix='project-autopilot-e2e-') as temp:
    root = Path(temp)
    home = root / 'hermes-home'
    repo = root / 'repo'
    home.mkdir()
    repo.mkdir()
    home = home.resolve()
    repo = repo.resolve()
    os.environ['HERMES_HOME'] = str(home)
    existing_pythonpath = os.environ.get('PYTHONPATH', '')
    os.environ['PYTHONPATH'] = os.pathsep.join(
        part for part in (str(HERMES_SOURCE), existing_pythonpath) if part
    )
    env = dict(os.environ)

    from autopilot.adapters.autonomous_loop import AutonomousLoopSupervisor
    from autopilot.adapters.development_executor import CommandResult
    from autopilot.commands import handle_autopilot_command

    class Runtime:
        def run(self, argv, *, cwd=None, timeout_seconds=30):
            argv = tuple(str(value) for value in argv)
            if argv == ('hermes', 'gateway', 'status'):
                return CommandResult(0, 'Gateway is running\n', '')
            command = list(argv)
            if command and command[0] == 'hermes':
                command = [sys.executable, '-m', 'hermes_cli.main', *command[1:]]
            result = subprocess.run(
                command,
                cwd=cwd,
                env=env,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
            )
            return CommandResult(result.returncode, result.stdout, result.stderr)

    runtime = Runtime()

    checked(['git', 'init', '-b', 'main'], cwd=repo, env=env)
    checked(['git', 'config', 'user.name', 'Autopilot E2E'], cwd=repo, env=env)
    checked(['git', 'config', 'user.email', 'autopilot-e2e@example.invalid'], cwd=repo, env=env)
    (repo / '.gitignore').write_text('.worktrees/\n', encoding='utf-8')
    (repo / 'app.txt').write_text('initial\n', encoding='utf-8')
    (repo / 'local.txt').write_text('tracked baseline\n', encoding='utf-8')
    checked(['git', 'add', '.gitignore', 'app.txt', 'local.txt'], cwd=repo, env=env)
    checked(['git', 'commit', '-m', 'initial'], cwd=repo, env=env)
    starting_revision = checked(['git', 'rev-parse', 'HEAD'], cwd=repo, env=env).stdout.strip()

    # Existing user work that must remain untouched by the Autopilot worktree.
    (repo / 'local.txt').write_text('tracked baseline\nuser local change\n', encoding='utf-8')
    source_status_before = checked(
        ['git', 'status', '--porcelain=v1', '-z'], cwd=repo, env=env
    ).stdout

    checked([
        sys.executable, '-m', 'hermes_cli.main', 'project', 'create',
        'E2E Project', '--slug', 'e2e-project', '--primary', str(repo), '--use',
    ], env=env)

    registration = {
        'project_id': 'e2e-project',
        'workspace_root': str(repo),
        'discussion_session_id': 'sess-disc-e2e0001',
        'development_session_id': 'sess-dev-e2e0002',
        'display_title': 'E2E Project',
        'discussion_title': 'E2E Project Discussion',
        'development_title': 'E2E Project Development',
    }
    registered = str(handle_autopilot_command(
        'register ' + json.dumps(registration), runtime=runtime
    ))
    if 'Registered' not in registered:
        raise RuntimeError(registered)

    phase2 = str(handle_autopilot_command('lease approve phase2-readonly', runtime=runtime))
    if 'Lease approved' not in phase2:
        raise RuntimeError(phase2)
    brief_output = str(handle_autopilot_command(
        'brief ' + json.dumps({
            'scope': 'Append one verified line to app.txt.',
            'tasks': [{
                'task_id': 'e2e-change',
                'title': 'Update app marker',
                'description': 'Append e2e verified to app.txt.',
                'priority': 'low',
                'risk_level': 'low',
                'acceptance_criteria': ['app.txt contains the verified marker'],
                'estimated_files': ['app.txt'],
                'dependencies': [],
            }],
        }), runtime=runtime
    ))
    brief_line = next(line for line in brief_output.splitlines() if line.startswith('Brief ID:'))
    brief_id = brief_line.split(':', 1)[1].strip()
    approved = str(handle_autopilot_command(f'approve {brief_id}', runtime=runtime))
    if 'approved' not in approved.lower():
        raise RuntimeError(approved)

    profile = {
        'schema_version': 1,
        'project_id': 'e2e-project',
        'workspace_root': str(repo),
        'prerequisites': ['python3'],
        'max_remediation_cycles': 1,
        'checks': [{
            'check_id': 'python-version',
            'argv': ['python3', '--version'],
            'cwd': '.',
            'timeout_seconds': 30,
            'required_evidence': [
                'exit_code', 'duration_seconds', 'stdout_excerpt', 'stderr_excerpt'
            ],
        }],
    }
    configured = str(handle_autopilot_command(
        'verify configure ' + json.dumps(profile), runtime=runtime
    ))
    if 'configured' not in configured.lower():
        raise RuntimeError(configured)
    autonomous = str(handle_autopilot_command(
        'lease approve autonomous-development', runtime=runtime
    ))
    if 'Lease approved' not in autonomous:
        raise RuntimeError(autonomous)

    launched = str(handle_autopilot_command(f'loop start {brief_id}', runtime=runtime))
    if 'Development task:' not in launched or 'Verifier task:' not in launched:
        raise RuntimeError(launched)
    supervisor = AutonomousLoopSupervisor(hermes_home=home)
    loop = supervisor.list_loops('e2e-project')[-1]
    loop_id = loop['loop_id']
    dev_task = loop['development_task_id']
    verifier_task = loop['verifier_task_id']
    if loop['starting_revision'] != starting_revision:
        raise RuntimeError('dispatch starting revision mismatch')
    if not loop['dirty_workspace']:
        raise RuntimeError('dirty source workspace was not recorded')

    dev_show = json.loads(checked([
        sys.executable, '-m', 'hermes_cli.main', 'kanban', '--board',
        'e2e-project', 'show', dev_task, '--json'
    ], env=env).stdout)
    verifier_show = json.loads(checked([
        sys.executable, '-m', 'hermes_cli.main', 'kanban', '--board',
        'e2e-project', 'show', verifier_task, '--json'
    ], env=env).stdout)
    if dev_show['task']['status'] not in {'ready', 'running'}:
        raise RuntimeError(f"unexpected Development status: {dev_show['task']['status']}")
    if verifier_show['task']['status'] not in {'ready', 'todo', 'pending', 'blocked'}:
        raise RuntimeError(f"unexpected verifier status: {verifier_show['task']['status']}")

    worktree = repo / '.worktrees' / dev_task
    worktree.parent.mkdir(parents=True, exist_ok=True)
    checked(['git', 'worktree', 'add', '--detach', str(worktree), starting_revision], cwd=repo, env=env)
    (worktree / 'app.txt').write_text('initial\ne2e verified\n', encoding='utf-8')

    checked([
        sys.executable, '-m', 'hermes_cli.main', 'kanban', '--board',
        'e2e-project', 'claim', dev_task,
    ], env=env)
    checked([
        sys.executable, '-m', 'hermes_cli.main', 'kanban', '--board',
        'e2e-project', 'complete', dev_task,
        '--result', 'Implemented scoped marker.',
        '--metadata', json.dumps({
            'autopilot_contract_version': 1,
            'role': 'development',
            'brief_id': brief_id,
            'changed_files': ['app.txt'],
            'commands_run': ['python3 --version'],
            'verification_attempts': 1,
            'decisions': [],
            'blocked_reason': '',
            'residual_risk': 'none',
            'starting_revision': starting_revision,
        }),
    ], env=env)

    checked([
        sys.executable, '-m', 'hermes_cli.main', 'kanban', '--board',
        'e2e-project', 'claim', verifier_task,
    ], env=env)
    version_check = checked(['python3', '--version'], cwd=worktree, env=env)
    evidence = {
        'autopilot_contract_version': 1,
        'role': 'verifier',
        'brief_id': brief_id,
        'verification_status': 'passed',
        'review_status': 'approved',
        'changed_files': ['app.txt'],
        'checks': [{
            'check_id': 'python-version',
            'argv': ['python3', '--version'],
            'cwd': '.',
            'exit_code': 0,
            'duration_seconds': 0.01,
            'stdout_excerpt': version_check.stdout.strip(),
            'stderr_excerpt': version_check.stderr.strip(),
        }],
        'findings': [],
        'residual_risk': 'none',
        'starting_revision': starting_revision,
    }
    checked([
        sys.executable, '-m', 'hermes_cli.main', 'kanban', '--board',
        'e2e-project', 'complete', verifier_task,
        '--result', 'Independent verification passed.',
        '--metadata', json.dumps(evidence),
    ], env=env)

    synced = str(handle_autopilot_command(f'loop sync {loop_id}', runtime=runtime))
    if 'AWAITING_HUMAN_ACCEPTANCE' not in synced:
        raise RuntimeError(synced)
    accepted = str(handle_autopilot_command(f'loop accept {loop_id}', runtime=runtime))
    if 'accepted' not in accepted.lower():
        raise RuntimeError(accepted)
    checkpoint = str(handle_autopilot_command(f'loop checkpoint {loop_id}', runtime=runtime))
    if '=== Immutable checkpoint created ===' not in checkpoint:
        raise RuntimeError(checkpoint)
    authorization = str(handle_autopilot_command(
        f'loop authorize-commit {loop_id}', runtime=runtime
    ))
    if '=== One-time commit authorization created ===' not in authorization:
        raise RuntimeError(authorization)
    committed = str(handle_autopilot_command(f'loop commit {loop_id}', runtime=runtime))
    if '=== Isolated worktree commit created ===' not in committed:
        raise RuntimeError(committed)

    final_loop = next(
        item for item in supervisor.list_loops('e2e-project')
        if item.get('loop_id') == loop_id
    )
    if not final_loop.get('commit_revision'):
        raise RuntimeError('commit revision not recorded')
    source_status_after = checked(
        ['git', 'status', '--porcelain=v1', '-z'], cwd=repo, env=env
    ).stdout
    if source_status_after != source_status_before:
        raise RuntimeError('original source Git status changed')
    if (repo / 'app.txt').read_text(encoding='utf-8') != 'initial\n':
        raise RuntimeError('original source file was modified')
    committed_content = checked(
        ['git', 'show', f"{final_loop['commit_revision']}:app.txt"], cwd=repo, env=env
    ).stdout
    if committed_content != 'initial\ne2e verified\n':
        raise RuntimeError('local worktree commit content mismatch')

    print(json.dumps({
        'status': 'passed',
        'project_id': 'e2e-project',
        'loop_status': final_loop['status'],
        'development_status': 'done',
        'verifier_status': 'done',
        'evidence_recorded': bool(final_loop.get('result_artifact_path')),
        'acceptance_recorded': bool(final_loop.get('acceptance_artifact_path')),
        'checkpoint_recorded': bool(final_loop.get('checkpoint_artifact_path')),
        'commit_authorization_recorded': bool(final_loop.get('commit_authorization_path')),
        'commit_revision_recorded': bool(final_loop.get('commit_revision')),
        'source_status_preserved': source_status_after == source_status_before,
        'source_file_preserved': True,
        'temp_state_cleaned_on_exit': True,
    }, indent=2))
