import contextlib
import http.server
import itertools
import json
import os
from pathlib import Path
import re
import subprocess
import signal
import socket
import time
import tempfile
import threading
import unittest
from unittest.mock import patch

from mira.relay import managed


class HealthTests(unittest.TestCase):
    def test_rejects_html_and_wrong_relay(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            payload = b'<html>tunnel error</html>'
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(self.payload)
            def log_message(self, *_): pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}'
        try:
            with self.assertRaises(ValueError): managed.probe(url)
            Handler.payload = json.dumps(dict(service='mira-relay', instanceId='other')).encode()
            with self.assertRaisesRegex(ValueError, 'different Relay'): managed.probe(url, 'mine')
            self.assertEqual(managed.probe(url, 'other')['instanceId'], 'other')
        finally:
            server.shutdown()
            server.server_close()

    def test_cpolar_log_variants(self):
        source = Path('tools/relay/start-public-relay.sh').read_text()
        pattern = re.search(r"https_url=.*?grep -Eo '([^']+)'", source).group(1)
        for line in ('Forwarding https://sample.test -> http://localhost',
                     '[INFO] Tunnel established at https://sample.test',
                     'msg="Tunnel established at https://sample.test"'):
            result = subprocess.run(['grep', '-Eo', pattern], input=line, text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout.split()[-1], 'https://sample.test')

    def test_failed_launch_retries_and_exhausts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'tools/relay/start-public-relay.sh'
            script.parent.mkdir(parents=True)
            script.write_text('#!/bin/sh\nexit 7\n')
            script.chmod(0o755)
            with patch.object(managed, 'ROOT', root), patch.dict(os.environ, MIRA_WEB_RETRIES='2'):
                manager = managed.Manager()
                self.assertEqual(manager.supervise(), 1)
                state = manager.read()
                self.assertEqual(state['status'], 'failed')
                self.assertEqual(state['attempt'], 2)
                self.assertEqual(len(list(manager.directory.glob('attempt-*.log'))), 2)
                self.assertFalse(manager.running())

    def test_bind_address_is_used_for_probes(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(managed, 'ROOT', Path(directory)):
            with patch.dict(os.environ, {'MIRA_RELAY_HOST': '::1', 'MIRA_RELAY_PORT': '12345'}, clear=True):
                self.assertEqual(managed.Manager().local, 'http://[::1]:12345')
            with patch.dict(os.environ, {'MIRA_LOCAL_ADVERTISE_URL': 'http://custom.test:12345'}, clear=True):
                self.assertEqual(managed.Manager().local, 'http://custom.test:12345')

    def test_stop_never_signals_a_stale_pid(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(managed, 'ROOT', Path(directory)):
            manager = managed.Manager()
            manager.state_path.write_text(json.dumps({'pid': 987654, 'status': 'stopped'}))
            with patch.object(manager, 'running', return_value=True), patch.object(os, 'kill') as kill:
                self.assertEqual(manager.stop(), 1)
                kill.assert_not_called()

    def test_stop_request_is_bound_to_daemon_generation(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(managed, 'ROOT', Path(directory)):
            manager = managed.Manager()
            manager.stop_path.write_text('previous-generation')
            watcher = threading.Thread(target=manager.watch_stop_request)
            watcher.start()
            try:
                self.assertFalse(manager.stop_event.wait(0.3))
                manager.stop_path.write_text(manager.run_id)
                self.assertTrue(manager.stop_event.wait(1))
            finally:
                manager.stop_event.set(); watcher.join(timeout=1)

    def test_http_public_url_and_frozen_relay_recovery(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
            listener.close()
            local = f'http://127.0.0.1:{port}'
            public_file = Path(directory) / 'public-url'
            env = dict(os.environ, MIRA_RELAY_PORT=str(port), MIRA_RELAY_HOST='127.0.0.1',
                       MIRA_LOCAL_ADVERTISE_URL=local, MIRA_PUBLIC_URL=local,
                       MIRA_SKIP_CONSOLE_BUILD='1', MIRA_MANAGED_URL_FILE=str(public_file))
            with (Path(directory) / 'launcher.log').open('w') as log:
                child = subprocess.Popen(['bash', 'tools/relay/start-public-relay.sh'], env=env,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                def wait_ready(previous=None):
                    deadline = time.monotonic() + 25
                    while time.monotonic() < deadline:
                        self.assertIsNone(child.poll(), (Path(directory) / 'launcher.log').read_text())
                        with contextlib.suppress(Exception):
                            state = managed.probe(local)
                            if public_file.exists() and state['instanceId'] != previous:
                                return state['instanceId']
                        time.sleep(0.2)
                    return self.fail('Relay recovery deadline exceeded: ' +
                                     (Path(directory) / 'launcher.log').read_text())
                first = wait_ready()
                self.assertEqual(public_file.read_text().strip(), local)
                entries = subprocess.check_output(['ps', '-eo', 'pid,ppid,args'], text=True).splitlines()[1:]
                relays = [int(parts[0]) for line in entries if len(parts := line.split(None, 2)) == 3
                          and int(parts[1]) == child.pid and '-m mira.relay.server' in parts[2]]
                self.assertEqual(len(relays), 1)
                os.kill(relays[0], signal.SIGSTOP)
                self.assertNotEqual(first, wait_ready(first))
                self.assertEqual(public_file.read_text().strip(), local)
            finally:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGTERM)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    child.wait(timeout=5)
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait()

    def test_recovery_reuses_console_after_retry_budget_reset(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(managed, 'ROOT', Path(directory)):
            root = Path(directory)
            output = root / 'apps/console/out/index.html'
            output.parent.mkdir(parents=True); output.write_text('built console')
            manager = managed.Manager()
            manager.local = 'http://192.0.2.1:8765'
            launches = []
            clock = itertools.count(0, 61)
            class Child:
                def __init__(self): self.polls = 0; self.returncode = 7
                def poll(self):
                    self.polls += 1
                    return None if self.polls == 1 else 7
            def launch(*_, **kwargs):
                launches.append(kwargs['env'])
                manager.url_path.write_text('http://public.example')
                return Child()
            def probe(*_, **kwargs):
                if len(launches) == 2: manager.stop_event.set()
                return {'instanceId': 'test-instance'}
            with patch.dict(os.environ, {'MIRA_SKIP_CONSOLE_BUILD': '0'}), \
                 patch.object(managed.subprocess, 'Popen', side_effect=launch), \
                 patch.object(managed, 'probe', side_effect=probe), \
                 patch.object(managed.time, 'monotonic', side_effect=lambda: next(clock)), \
                 patch.object(manager, 'watch_stop_request'), \
                 patch.object(manager, 'stop_child', side_effect=lambda: setattr(manager, 'child', None)), \
                 patch.object(manager.stop_event, 'wait', return_value=False):
                self.assertEqual(manager.supervise(), 0)
            self.assertEqual(len(launches), 2)
            self.assertEqual(launches[0]['MIRA_SKIP_CONSOLE_BUILD'], '0')
            self.assertEqual(launches[1]['MIRA_SKIP_CONSOLE_BUILD'], '1')
            self.assertEqual(launches[0]['MIRA_LOCAL_ADVERTISE_URL'], manager.local)

    def test_status_uses_saved_startup_address(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(managed, 'ROOT', Path(directory)):
            manager = managed.Manager()
            manager.state_path.write_text(json.dumps({'status': 'ready', 'localUrl': 'http://192.0.2.1:8765',
                                                    'publicUrl': 'http://public.example'}))
            with patch.object(manager, 'running', return_value=True), \
                 patch.object(managed, 'probe', return_value={'instanceId': 'test-instance'}) as probe:
                self.assertEqual(manager.status(), 0)
                self.assertEqual(probe.call_args_list[0].args[0], 'http://192.0.2.1:8765')


if __name__ == '__main__': unittest.main()
