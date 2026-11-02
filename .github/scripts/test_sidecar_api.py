"""Owned HTTP/process behavior tests; no engine, server socket or renderer is executed."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml


ROOT = Path.cwd().resolve()
EXPECTED = {
    'appolon1908/Blender': ('blender', 'codestra-blender-api', 'blender', 18101),
    'appolon1908/Natron': ('natron', 'codestra-natron-api', 'NatronRenderer', 18103),
}


class SidecarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        repository = os.environ.get('GITHUB_REPOSITORY', 'appolon1908/' + Path.cwd().name)
        self.expected = EXPECTED[repository]
        spec = importlib.util.spec_from_file_location('_codestra_sidecar_under_test', ROOT / 'codestra_api/server.py')
        self.api = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {
                'CODESTRA_VIDEO_ROOT': str(self.root), 'CODESTRA_VIDEO_API_TOKEN': 'fixture',
                'CODESTRA_VIDEO_RENDER_ENABLED': '0', 'CODESTRA_VIDEO_MAX_JOBS': '1',
                'CODESTRA_VIDEO_BIN': 'never-execute-a-native-engine-in-source-tests'}), \
                patch('subprocess.Popen', side_effect=AssertionError('Import must not execute any process')):
            spec.loader.exec_module(self.api)
        self.threads = []
        captured = self.threads

        class CapturedThread:
            def __init__(self, target, **kwargs):
                self.target = target
                self.kwargs = kwargs

            def start(self):
                captured.append(self)

        def forbidden(*args, **kwargs):
            raise AssertionError('Real renderer/process execution is forbidden')

        # Replace module references, never the process-wide stdlib shared with the test runner.
        self.api.subprocess = SimpleNamespace(Popen=forbidden, DEVNULL=subprocess.DEVNULL,
                                              STDOUT=subprocess.STDOUT, TimeoutExpired=subprocess.TimeoutExpired)
        self.api.threading = SimpleNamespace(Thread=CapturedThread, RLock=threading.RLock)
        self.api.shutil = SimpleNamespace(which=lambda _: None)
        self.project = self.root / 'projects' / 'scene.project'
        self.project.parent.mkdir()
        self.project.write_text('Synthetic fixture; never passed to a real engine')
        self.script = self.root / 'scripts' / 'fixture.py'
        self.script.parent.mkdir()
        self.script.write_text('raise AssertionError("This script must never execute")\n')

    def enabled(self):
        self.api.RENDER_ENABLED = True
        self.api.shutil.which = lambda _: '/nonexistent/test-only-engine'

    def payload(self):
        return {'project': str(self.project)}

    def request(self, method, path, payload=None, raw=None, headers=None):
        handler = self.api.Handler.__new__(self.api.Handler)
        handler.path = path
        data = json.dumps(payload).encode() if raw is None and payload is not None else raw or b''
        handler.headers = {'Authorization': 'Bearer fixture', 'Content-Length': str(len(data))}
        handler.headers.update(headers or {})
        handler.rfile = io.BytesIO(data)
        handler.wfile = io.BytesIO()
        statuses, response_headers = [], {}
        handler.send_response = statuses.append
        handler.send_header = lambda key, value: response_headers.update({key: value})
        handler.end_headers = lambda: None
        getattr(handler, 'do_' + method)()
        result = handler.wfile.getvalue()
        self.assertEqual(str(len(result)), response_headers['Content-Length'])
        self.assertEqual('application/json', response_headers['Content-Type'])
        return statuses[-1], json.loads(result)

    def test_service_identity_and_frozen_openapi_routes_match_the_actual_repository(self):
        self.assertEqual(self.expected, (self.api.ENGINE, self.api.SERVICE,
                                        self.api.DEFAULT_BINARY, self.api.DEFAULT_PORT))
        contract = yaml.safe_load((ROOT / 'codestra_api/openapi.yaml').read_text())
        self.assertEqual(self.api.SERVICE, contract['info']['title'])
        self.assertEqual('http://127.0.0.1:' + str(self.api.DEFAULT_PORT), contract['servers'][0]['url'])
        self.assertEqual({'/health', '/status', '/status/{job_id}', '/render', '/stop/{job_id}'},
                         set(contract['paths']))
        self.assertEqual({'202', '400', '409', '503'}, set(contract['paths']['/render']['post']['responses']))

    def test_import_and_disabled_render_never_start_processes_or_workers(self):
        self.assertFalse(self.api.RENDER_ENABLED)
        with self.assertRaisesRegex(PermissionError, 'render_disabled'):
            self.api.launch(self.payload())
        self.assertEqual([], self.threads)
        self.assertEqual({}, self.api._jobs)

    def test_path_guard_rejects_absolute_relative_and_symlink_escape(self):
        outside = self.root.parent / 'outside-sidecar-source-test'
        (self.root / 'escape').symlink_to(self.root.parent, target_is_directory=True)
        for value in ('../outside', str(outside), 'escape/outside'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'outside_video_root'):
                self.api.safe_path(value)
        for value in ('', None, 42):
            with self.assertRaisesRegex(ValueError, 'path_required'):
                self.api.safe_path(value)
        with self.assertRaisesRegex(ValueError, 'path_not_found'):
            self.api.safe_path('absent', must_exist=True)

    def test_relative_existing_path_is_resolved_under_the_fixture_root(self):
        self.assertEqual(self.project, self.api.safe_path('projects/scene.project', must_exist=True))

    def test_command_uses_literal_argument_list_and_workspace_paths(self):
        unusual = self.root / 'projects' / 'a ; $(do-not-execute).project'
        unusual.write_text('fixture')
        command = self.api.command_for({'project': str(unusual)})
        self.assertIsInstance(command, list)
        self.assertEqual(self.api.BINARY, command[0])
        self.assertIn(str(unusual), command)

    def test_default_engine_command_and_script_mode(self):
        command = self.api.command_for(self.payload())
        if self.api.ENGINE == 'blender':
            self.assertEqual([self.api.BINARY, '-b', str(self.project), '-a'], command)
            self.assertEqual([self.api.BINARY, '-b', '--python', str(self.script), '-a'],
                             self.api.command_for({'script': str(self.script)}))
        else:
            self.assertEqual([self.api.BINARY, str(self.project)], command)
            self.assertEqual([self.api.BINARY, '--opengl', 'disabled', '-t', str(self.script)],
                             self.api.command_for({'script': str(self.script)}))
        with self.assertRaisesRegex(ValueError, 'project_or_script_required'):
            self.api.command_for({})

    def test_frame_output_and_input_script_constraints(self):
        output = self.root / 'outputs' / 'image'
        if self.api.ENGINE == 'blender':
            command = self.api.command_for({**self.payload(), 'mode': 'frame', 'frame': '7',
                                            'output': str(output), 'script': str(self.script)})
            self.assertEqual(['-f', '7'], command[-2:])
            self.assertIn('--python', command)
            self.assertIn(str(output), command)
            animation = self.api.command_for({**self.payload(), 'frame_start': 2, 'frame_end': 8})
            self.assertEqual(['-s', '2', '-e', '8', '-a'], animation[-5:])
            for payload in ({'mode': 'invalid'}, {'mode': 'frame', 'frame': 'bad'}):
                with self.assertRaises(ValueError):
                    self.api.command_for({**self.payload(), **payload})
        else:
            command = self.api.command_for({**self.payload(), 'writer': 'Write1', 'output': str(output),
                                            'frame_start': 2, 'frame_end': 8, 'onload': str(self.script)})
            self.assertEqual([self.api.BINARY, '-w', 'Write1', str(output), '2-8',
                              '-l', str(self.script), str(self.project)], command)
            for payload in ({'output': str(output)}, {'writer': 'x;bad'},
                            {'writer': 'Write1', 'frame_start': 2}):
                with self.assertRaises(ValueError):
                    self.api.command_for({**self.payload(), **payload})
        self.assertTrue(output.parent.is_dir())

    def test_names_reject_shell_metacharacters_and_empty_values(self):
        self.assertEqual('Write1', self.api.safe_name('Write1', 'writer'))
        for value in ('', 'name;command', 'with space', '$(anything)', None):
            with self.assertRaises(ValueError):
                self.api.safe_name(value, 'writer')

    def test_payload_cannot_change_the_fixed_engine_binary_or_execute_shell_text(self):
        command = self.api.command_for({**self.payload(), 'engine': 'other', 'binary': 'sh',
                                        'command': 'touch /outside'})
        self.assertEqual(self.api.BINARY, command[0])
        self.assertNotIn('sh', command)
        self.assertNotIn('touch /outside', command)

    def test_http_authentication_precedes_render_stop_and_health(self):
        for method, path in (('POST', '/render'), ('POST', '/stop/id'), ('GET', '/health')):
            status, body = self.request(method, path, self.payload(), headers={'Authorization': 'Bearer wrong'})
            self.assertEqual((401, {'error': 'unauthorized'}), (status, body))
        self.assertEqual([], self.threads)
        self.assertEqual({}, self.api._jobs)

    def test_health_truthfully_reports_unavailable_engine_and_disabled_render(self):
        status, body = self.request('GET', '/health?probe=source')
        self.assertEqual(200, status)
        self.assertFalse(body['engine_available'])
        self.assertFalse(body['render_enabled'])
        self.assertEqual(1, body['max_jobs'])
        self.assertNotIn('fixture', json.dumps(body))

    def test_http_payload_boundaries_and_json_object_are_enforced(self):
        self.enabled()
        for raw, headers in ((b'{', {}), (b'[]', {}), (b'{}', {'Content-Length': '-1'}),
                             (b'{}', {'Content-Length': '1048577'}), (b'{}', {'Content-Length': 'bad'})):
            self.assertEqual(400, self.request('POST', '/render', raw=raw, headers=headers)[0])
        self.assertEqual([], self.threads)

    def test_http_disabled_and_missing_binary_error_contract(self):
        self.assertEqual(403, self.request('POST', '/render', self.payload())[0])
        self.api.RENDER_ENABLED = True
        self.assertEqual(503, self.request('POST', '/render', self.payload())[0])
        self.assertEqual([], self.threads)

    def test_http_unknown_routes_and_jobs(self):
        for method, path in (('GET', '/missing'), ('POST', '/missing'),
                             ('GET', '/status/missing'), ('POST', '/stop/missing')):
            self.assertEqual(404, self.request(method, path, {})[0])

    def test_queue_capacity_and_public_job_copy(self):
        self.enabled()
        accepted = self.api.launch(self.payload())
        self.assertEqual('queued', accepted['state'])
        self.assertEqual(1, self.api.running_count())
        copy = self.api.public_job(accepted['job_id'])
        copy['state'] = 'forged'
        self.assertEqual('queued', self.api.public_job(accepted['job_id'])['state'])
        with self.assertRaisesRegex(RuntimeError, 'job_capacity_reached'):
            self.api.launch(self.payload())
        self.assertEqual(409, self.request('POST', '/render', self.payload())[0])
        self.assertEqual(1, len(self.threads))

    def test_capacity_reservation_is_atomic_for_simultaneous_requests(self):
        self.enabled()
        barrier = threading.Barrier(2)
        original = self.api.command_for
        def synchronized(payload):
            barrier.wait(timeout=5)
            return original(payload)
        self.api.command_for = synchronized
        accepted, failed = [], []
        def request():
            try:
                accepted.append(self.api.launch(self.payload()))
            except Exception as error:
                failed.append(error)
        clients = [threading.Thread(target=request) for _ in range(2)]
        for client in clients:
            client.start()
        for client in clients:
            client.join(timeout=7)
            self.assertFalse(client.is_alive())
        self.assertEqual(1, len(accepted), 'Configured one-job capacity must hold across simultaneous callers')
        self.assertEqual(1, len(failed))
        self.assertIsInstance(failed[0], RuntimeError)
        self.assertEqual('job_capacity_reached', str(failed[0]))
        self.assertEqual(1, self.api.running_count())

    def test_worker_success_failure_and_process_arguments_without_native_execution(self):
        self.enabled()
        calls = []
        for rc, expected in ((0, 'succeeded'), (7, 'failed')):
            proc = SimpleNamespace(pid=12345, wait=lambda: rc)
            def popen(command, **kwargs):
                calls.append((command, kwargs))
                return proc
            self.api.subprocess.Popen = popen
            job = self.api.launch(self.payload())
            self.threads[-1].target()
            result = self.api.public_job(job['job_id'])
            self.assertEqual(expected, result['state'])
            self.assertEqual(rc, result['exit_code'])
            self.assertIsNotNone(result['finished_at'])
            self.assertNotIn(job['job_id'], self.api._processes)
        command, kwargs = calls[0]
        self.assertIsInstance(command, list)
        self.assertNotIn('shell', kwargs)
        self.assertEqual(str(self.root), kwargs['cwd'])
        self.assertIs(kwargs['stdin'], subprocess.DEVNULL)
        self.assertIs(kwargs['stderr'], subprocess.STDOUT)
        self.assertTrue(kwargs['start_new_session'])

    def test_worker_process_creation_error_is_recorded_without_leaking_a_process(self):
        self.enabled()
        self.api.subprocess.Popen = lambda *args, **kwargs: (_ for _ in ()).throw(OSError('synthetic failure'))
        job = self.api.launch(self.payload())
        self.threads[-1].target()
        result = self.api.public_job(job['job_id'])
        self.assertEqual('failed', result['state'])
        self.assertEqual('synthetic failure', result['error'])
        self.assertEqual({}, self.api._processes)

    def test_stop_and_timeout_kill_only_the_recorded_mock_process(self):
        calls = []
        def wait(timeout=None):
            self.assertEqual(5, timeout)
            raise subprocess.TimeoutExpired('fixture', 5)
        proc = SimpleNamespace(poll=lambda: None, terminate=lambda: calls.append('terminate'),
                               wait=wait, kill=lambda: calls.append('kill'))
        self.api._jobs['job'] = {'job_id': 'job', 'state': 'running'}
        self.api._processes['job'] = proc
        self.assertEqual('stopping', self.api.stop('job')['state'])
        self.assertEqual(['terminate'], calls)
        self.threads[-1].target()
        self.assertEqual(['terminate', 'kill'], calls)
        with self.assertRaises(KeyError):
            self.api.stop('absent')

    def test_completed_stop_is_idempotent_and_does_not_signal_any_process(self):
        self.api._jobs['done'] = {'job_id': 'done', 'state': 'succeeded'}
        self.assertEqual('succeeded', self.api.stop('done')['state'])
        self.assertEqual([], self.threads)

    def test_status_bounds_history_and_preserves_exact_job_readback(self):
        self.api._jobs.update({str(i): {'job_id': str(i), 'state': 'failed'} for i in range(60)})
        status, body = self.request('GET', '/status')
        self.assertEqual(200, status)
        self.assertEqual(50, len(body['jobs']))
        self.assertEqual('10', body['jobs'][0]['job_id'])
        self.assertEqual(0, body['running'])
        self.assertEqual((200, {'job_id': '59', 'state': 'failed'}), self.request('GET', '/status/59'))

    def test_main_rejects_nonloopback_and_missing_token_without_binding_a_server(self):
        self.api.ThreadingHTTPServer = lambda *args: self.fail('A real server must never bind during source tests')
        for argv, token, error in ((['server.py', '--host', '0.0.0.0'], 'fixture', 'non_loopback'),
                                   (['server.py'], '', 'API_TOKEN_required')):
            self.api.TOKEN = token
            with patch.object(sys, 'argv', argv), self.assertRaisesRegex(SystemExit, error):
                self.api.main()

    def test_builtin_self_test_is_path_only_and_does_not_bind_or_render(self):
        self.api.ThreadingHTTPServer = lambda *args: self.fail('Self-test must never bind a server')
        with patch.object(sys, 'argv', ['server.py', '--self-test']), contextlib.redirect_stdout(io.StringIO()) as output:
            self.api.main()
        self.assertEqual({'ok': True, 'engine': self.api.ENGINE, 'service': self.api.SERVICE},
                         json.loads(output.getvalue()))
        self.assertEqual([], self.threads)

    def test_token_protected_loopback_startup_uses_only_an_explicit_server_double(self):
        calls = []
        class ServerDouble:
            def __init__(self, address, handler):
                calls.append((address, handler))
            def serve_forever(self):
                calls.append('serve-double')
        self.api.ThreadingHTTPServer = ServerDouble
        with patch.object(sys, 'argv', ['server.py']):
            self.api.main()
        self.assertEqual((('127.0.0.1', self.api.DEFAULT_PORT), self.api.Handler), calls[0])
        self.assertEqual('serve-double', calls[1])


if __name__ == '__main__':
    unittest.main()
