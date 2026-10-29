"""Idempotent public Relay supervisor. Runtime state stays in .mira/runtime/."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[2]


def probe(url: str, instance: str | None = None, websocket: bool = False) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url.rstrip('/') + '/api/health', timeout=3) as response:
        data = json.load(response)
    if data.get('service') != 'mira-relay' or not data.get('instanceId'):
        raise ValueError('endpoint is not a Mira Relay')
    if instance and data['instanceId'] != instance:
        raise ValueError('public tunnel points to a different Relay instance')
    if websocket:
        from mira.mcp.server import BrowserWebSocket, RelayHttpClient
        ws = BrowserWebSocket(RelayHttpClient(url))
        try:
            ws.connect()
        finally:
            ws.close()
    return data


class Manager:
    def __init__(self):
        self.port = int(os.environ.get('MIRA_RELAY_PORT', '8765'))
        self.directory = ROOT / '.mira/runtime' / f'web-{self.port}'
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_path = self.directory / 'status.json'
        self.url_path = self.directory / 'public-url'
        self.log_path = self.directory / 'supervisor.log'
        host = os.environ.get('MIRA_RELAY_HOST', '0.0.0.0')
        probe_host = {'0.0.0.0': '127.0.0.1', '::': '::1'}.get(host, host)
        authority = f'[{probe_host}]' if ':' in probe_host else probe_host
        self.local = os.environ.get('MIRA_LOCAL_ADVERTISE_URL', f'http://{authority}:{self.port}')
        self.bind_host = probe_host
        self.stop_event = threading.Event()
        self.run_id = uuid.uuid4().hex
        self.stop_path = self.directory / "stop-request"
        self.child = None

    def read(self):
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def write(self, **fields):
        state = dict(pid=os.getpid(), supervisorId=self.run_id, localUrl=self.local, updatedAt=time.time(), **fields)
        temp = self.state_path.with_suffix('.tmp')
        temp.write_text(json.dumps(state, indent=2) + '\n')
        temp.replace(self.state_path)

    def locked(self):
        handle = (self.directory / 'supervisor.lock').open('a')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
        return handle

    def running(self):
        handle = self.locked()
        if handle is None:
            return True
        handle.close()
        return False

    def status(self):
        state = self.read()
        state['running'] = self.running()
        if not state['running']:
            state['status'] = 'stopped'
        elif state.get('status') == 'ready':
            try:
                local = probe(state.get('localUrl', self.local))
                probe(state['publicUrl'], local['instanceId'], websocket=True)
            except Exception as exc:
                state.update(status='unhealthy', error=str(exc))
        print(json.dumps(state, indent=2))
        return 0 if state.get('status') == 'ready' else 1

    def stop_child(self):
        if self.child is None:
            return
        # The shell and its relay/tunnel share a private process group.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.child.pid, signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.child.wait(timeout=8)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.child.pid, signal.SIGKILL)
        self.child.wait()
        self.child = None

    def watch_stop_request(self):
        # Bind stop to a particular daemon generation, never to a reusable PID.
        while not self.stop_event.wait(0.2):
            with contextlib.suppress(OSError):
                if self.stop_path.read_text().strip() == self.run_id:
                    self.stop_event.set()

    def supervise(self):
        lock = self.locked()
        if lock is None:
            return 0
        old_term = signal.signal(signal.SIGTERM, lambda *_: self.stop_event.set())
        old_int = signal.signal(signal.SIGINT, lambda *_: self.stop_event.set())
        attempts = max(1, int(os.environ.get('MIRA_WEB_RETRIES', '3')))
        startup_timeout = max(5, float(os.environ.get('MIRA_WEB_STARTUP_TIMEOUT', '180')))
        attempt = 0
        has_been_ready = False
        threading.Thread(target=self.watch_stop_request, daemon=True).start()
        try:
            while attempt < attempts and not self.stop_event.is_set():
                attempt += 1
                self.url_path.unlink(missing_ok=True)
                self.write(status='starting', attempt=attempt)
                env = dict(os.environ, MIRA_MANAGED_URL_FILE=str(self.url_path),
                           MIRA_TUNNEL_ATTEMPTS='1', MIRA_TUNNEL_STRICT_CHECK='1',
                           MIRA_RELAY_AUTO_KILL_PORT_PROCESS='0', MIRA_LOCAL_ADVERTISE_URL=self.local, PYTHONUNBUFFERED='1')
                # Build on first boot only. Recovery uses the already-built console.
                if has_been_ready and (ROOT / 'apps/console/out/index.html').is_file():
                    env['MIRA_SKIP_CONSOLE_BUILD'] = '1'
                with (self.directory / f'attempt-{time.time_ns()}.log').open('w') as log:
                    self.child = subprocess.Popen([str(ROOT / 'tools/relay/start-public-relay.sh')],
                        cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log,
                        stderr=subprocess.STDOUT, start_new_session=True)
                started = time.monotonic()
                ready_at = None
                failures = 0
                error = 'startup deadline exceeded'
                while not self.stop_event.is_set():
                    if self.child.poll() is not None:
                        error = f'launcher exited: {self.child.returncode}'
                        break
                    if ready_at is None and time.monotonic() - started > startup_timeout:
                        break
                    try:
                        url = self.url_path.read_text().strip()
                        local = probe(self.local)
                        probe(url, local['instanceId'], websocket=ready_at is None)
                        if ready_at is None:
                            ready_at = time.monotonic()
                        has_been_ready = True
                        failures = 0
                        self.write(status='ready', publicUrl=url, instanceId=local['instanceId'], attempt=attempt)
                    except Exception as exc:
                        error = str(exc)
                        if ready_at is not None:
                            failures += 1
                            self.write(status='recovering', error=error, attempt=attempt)
                            if failures >= 3:
                                break
                    if self.stop_event.wait(5 if ready_at else 1):
                        break
                self.stop_child()
                if self.stop_event.is_set():
                    break
                # A sustained healthy run resets the restart budget.
                if ready_at is not None and time.monotonic() - ready_at >= 60:
                    attempt = 0
                self.write(status='retrying' if attempt < attempts else 'failed', error=error, attempt=attempt)
                if attempt < attempts:
                    self.stop_event.wait(min(2 ** max(attempt, 1), 15))
        finally:
            stopped_requested = self.stop_event.is_set()
            self.stop_child()
            if self.stop_event.is_set():
                self.write(status='stopped')
            self.stop_event.set()
            signal.signal(signal.SIGTERM, old_term)
            signal.signal(signal.SIGINT, old_int)
            lock.close()
        return 0 if stopped_requested else 1

    def start(self):
        # Serialize concurrent callers as well as the daemon itself.
        with (self.directory / 'start.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not self.running():
                # Refuse occupied ports; never terminate an unrelated service.
                import socket
                try:
                    with socket.create_connection((self.bind_host, self.port), timeout=2):
                        print(f'Port {self.port} is occupied by an unmanaged server; stop its launcher first.', file=sys.stderr)
                        return 1
                except OSError:
                    pass
                with self.log_path.open('a') as log:
                    daemon = subprocess.Popen([sys.executable, '-m', 'mira.relay.managed', '_supervise'],
                        cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                        start_new_session=True)
                # Allow daemon to acquire its lock and replace stale status.
                for _ in range(30):
                    if self.running() and self.read().get('pid') == daemon.pid:
                        break
                    time.sleep(0.1)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            state = self.read()
            if state.get('status') == 'ready':
                return self.status()
            if state.get('status') == 'failed' or not self.running():
                return self.status()
            time.sleep(1)
        print(f'Startup continues in background. Run ./mira-web status; logs: {self.directory}')
        return 2

    def stop(self):
        with (self.directory / 'start.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            deadline = time.monotonic() + 30
            while self.running() and time.monotonic() < deadline:
                state = self.read()
                token = state.get('supervisorId')
                if token:
                    self.stop_path.write_text(token)
                else:
                    print('Supervisor has no control token; stop its original launcher before upgrading.', file=sys.stderr)
                    return 1
                time.sleep(0.2)
            if self.running():
                print('Supervisor did not stop before deadline.', file=sys.stderr)
                return 1
        print('Mira Web stopped.')
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog="Configuration and recovery: docs/relay-startup.md")
    parser.add_argument('command', choices=['start', 'status', 'stop', '_supervise'], nargs='?', default='start')
    args = parser.parse_args()
    if not (ROOT / 'tools/relay/start-public-relay.sh').is_file():
        parser.error('Mira Web requires a source checkout with tools/ and apps/; the wheel provides mira-mcp and mira-relay only')
    manager = Manager()
    return manager.supervise() if args.command == '_supervise' else getattr(manager, args.command)()


if __name__ == '__main__':
    raise SystemExit(main())
