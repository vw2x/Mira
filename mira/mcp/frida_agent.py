"""Device-side persistent Frida session used by Mira MCP on Android.

This module only uses Python's standard library plus the Frida binding packaged in
Mira. The MCP server copies this exact source into the app sandbox and starts it
as a private Unix-socket daemon.
"""

from __future__ import annotations

import base64
import contextlib
import json
import math
import os
import socket
import subprocess
import sys
import time
import traceback
import zlib
from typing import Any

import frida


def jsonable(value: Any, *, _path: str = "$", _active: set[int] | None = None) -> Any:
    """Convert a value without silently changing or dropping unsupported data."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            "type": "bytes",
            "encoding": "base64",
            "dataBase64": base64.b64encode(bytes(value)).decode("ascii"),
        }
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError(f"value at {_path} is not JSON-compatible: non-finite number")
        return value
    if isinstance(value, (dict, list, tuple)):
        active = _active if _active is not None else set()
        identity = id(value)
        if identity in active:
            raise TypeError(f"value at {_path} is not JSON-compatible: circular reference")
        active.add(identity)
        try:
            if isinstance(value, dict):
                converted = {}
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise TypeError(
                            f"value at {_path} is not JSON-compatible: object key is not a string"
                        )
                    converted[key] = jsonable(item, _path=f"{_path}.{key}", _active=active)
                return converted
            return [
                jsonable(item, _path=f"{_path}[{index}]", _active=active)
                for index, item in enumerate(value)
            ]
        finally:
            active.remove(identity)
    raise TypeError(f"value at {_path} is not JSON-compatible: {type(value).__name__}")


class PersistentAgent:
    MAX_BUFFERED_MESSAGES = 256

    def __init__(self) -> None:
        device = frida.get_device_manager().add_remote_device("127.0.0.1:27042")
        self.session = device.attach("Gadget")
        self.runner: dict[str, Any] | None = None

    @staticmethod
    def exports(script: Any) -> Any:
        exports = getattr(script, "exports_sync", None)
        return exports if exports is not None else script.exports

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        source = str(request.get("script") or "")
        rpc_method = str(request.get("rpcMethod") or "")
        cleanup_method = str(request.get("cleanupMethod") or "")
        if not source or not rpc_method or not cleanup_method:
            raise ValueError("script, rpcMethod and cleanupMethod are required")

        reused = self.runner is not None
        if self.runner is None:
            messages: list[dict[str, Any]] = []
            runner_source = r"""
function miraBytesToBase64(bytes) {
  var alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
  var output = '';
  for (var index = 0; index < bytes.length; index += 3) {
    var first = bytes[index];
    var secondPresent = index + 1 < bytes.length;
    var thirdPresent = index + 2 < bytes.length;
    var second = secondPresent ? bytes[index + 1] : 0;
    var third = thirdPresent ? bytes[index + 2] : 0;
    var bits = (first << 16) | (second << 8) | third;
    output += alphabet[(bits >>> 18) & 63];
    output += alphabet[(bits >>> 12) & 63];
    output += secondPresent ? alphabet[(bits >>> 6) & 63] : '=';
    output += thirdPresent ? alphabet[bits & 63] : '=';
  }
  return output;
}

function miraNormalizeRpcValue(value, active, path) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean')
    return value;
  if (typeof value === 'number') {
    if (!Number.isFinite(value))
      throw new Error('RPC result at ' + path + ' is not JSON-compatible: non-finite number');
    return value;
  }
  if (typeof value === 'undefined' || typeof value === 'function' ||
      typeof value === 'symbol' || typeof value === 'bigint')
    throw new Error('RPC result at ' + path + ' is not JSON-compatible: ' + typeof value);

  var bytes = null;
  if (Object.prototype.toString.call(value) === '[object ArrayBuffer]')
    bytes = new Uint8Array(value);
  else if (typeof ArrayBuffer.isView === 'function' && ArrayBuffer.isView(value))
    bytes = new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  if (bytes !== null)
    return { type: 'bytes', encoding: 'base64', dataBase64: miraBytesToBase64(bytes) };

  if (active.has(value))
    throw new Error('RPC result at ' + path + ' is not JSON-compatible: circular reference');
  active.add(value);
  try {
    if (Array.isArray(value)) {
      var arrayResult = [];
      for (var index = 0; index < value.length; index++)
        arrayResult.push(miraNormalizeRpcValue(value[index], active, path + '[' + index + ']'));
      return arrayResult;
    }
    var prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null)
      throw new Error('RPC result at ' + path + ' is not JSON-compatible: unsupported object');
    var objectResult = {};
    Object.keys(value).forEach(function (key) {
      Object.defineProperty(objectResult, key, {
        value: miraNormalizeRpcValue(value[key], active, path + '.' + key),
        enumerable: true,
        writable: true,
        configurable: true
      });
    });
    return objectResult;
  } finally {
    active.delete(value);
  }
}

rpc.exports = {
  execute: async function (source, method, args, cleanupMethod) {
    var userRpc = { exports: {} };
    var rpc = userRpc;
    var rpcResult = null;
    var operationError = null;
    var cleanupError = null;
    try {
      new Function('rpc', source)(userRpc);
      var operation = userRpc.exports[method];
      if (typeof operation !== 'function')
        throw new Error('rpc.exports method not found: ' + method);
      rpcResult = miraNormalizeRpcValue(
        await operation.apply(null, args || []), new WeakSet(), '$');
    } catch (e) {
      operationError = String(e && e.stack ? e.stack : e);
    } finally {
      try {
        var cleanup = userRpc.exports[cleanupMethod];
        if (typeof cleanup !== 'function')
          throw new Error('cleanup export not found: ' + cleanupMethod);
        await cleanup();
      } catch (e) {
        cleanupError = String(e && e.stack ? e.stack : e);
      }
      userRpc.exports = {};
      rpc = null;
      userRpc = null;
    }
    return { ok: operationError === null && cleanupError === null,
      rpcResult: rpcResult, error: operationError || cleanupError,
      cleanupError: cleanupError };
  }
};
"""
            bridge_path = os.path.join(os.path.dirname(frida.__file__), "_mira_java_bridge_runtime.js")
            if os.path.isfile(bridge_path):
                with open(bridge_path, "r", encoding="utf-8") as bridge_file:
                    bridge_source = bridge_file.read()
                marker = "✄\n"
                marker_end = bridge_source.find(marker) + len(marker)
                header_lines = bridge_source[:marker_end].splitlines()
                if marker_end < len(marker) or len(header_lines) != 3 or header_lines[0] != "📦":
                    raise RuntimeError("invalid compiled Frida Java bridge bundle header")
                entry_name = header_lines[1].split(" ", 1)[1]
                combined_body = bridge_source[marker_end:] + "\n" + runner_source
                runner_source = (
                    "📦\n"
                    + str(len(combined_body.encode("utf-8")))
                    + " "
                    + entry_name
                    + "\n✄\n"
                    + combined_body
                )
            script = self.session.create_script(runner_source)

            def on_message(message: dict[str, Any], data: bytes | None) -> None:
                entry = jsonable(message)
                if data is not None:
                    entry["dataBase64"] = base64.b64encode(data).decode("ascii")
                messages.append(entry)
                if len(messages) > self.MAX_BUFFERED_MESSAGES:
                    del messages[: len(messages) - self.MAX_BUFFERED_MESSAGES]

            script.on("message", on_message)
            script.load()
            load_errors = [item for item in messages if item.get("type") == "error"]
            if load_errors:
                raise RuntimeError(
                    "Frida runner failed during load: "
                    + json.dumps(load_errors, ensure_ascii=False)
                )
            self.runner = {"script": script, "messages": messages}

        script = self.runner["script"]
        messages = self.runner["messages"]
        result = self.exports(script).execute(
            source, rpc_method, request.get("rpcArgs") or [], cleanup_method
        )
        current_messages = list(messages)
        messages.clear()
        script_errors = [item for item in current_messages if item.get("type") == "error"]
        ok = bool(result.get("ok")) and not script_errors
        return {
            "ok": ok,
            "fridaVersion": getattr(frida, "__version__", ""),
            "persistentId": str(request.get("persistentId") or "runner"),
            "reusedScript": reused,
            "rpcMethod": rpc_method,
            "rpcResult": jsonable(result.get("rpcResult")),
            "messageCount": len(current_messages),
            "messages": current_messages,
            "error": result.get("error") or ("Frida JavaScript error" if script_errors else None),
            "cleanupError": result.get("cleanupError"),
            "executionMode": "runner",
        }

def receive_request(connection: socket.socket) -> dict[str, Any]:
    chunks = []
    while True:
        chunk = connection.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        if sum(map(len, chunks)) > 8 * 1024 * 1024:
            raise ValueError("request exceeds 8 MiB")
    value = json.loads(b"".join(chunks).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("request must be an object")
    return value


def serve(socket_path: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(socket_path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(socket_path)
    os.chmod(socket_path, 0o600)
    server.listen(4)
    agent = PersistentAgent()
    while True:
        connection, _ = server.accept()
        with connection:
            serve_connection(agent, connection)


def serve_connection(agent: PersistentAgent, connection: socket.socket) -> None:
    try:
        response = agent.handle(receive_request(connection))
    except Exception as exc:
        response = {"ok": False, "error": str(exc), "traceback": traceback.format_exc().splitlines()}
    try:
        connection.sendall(json.dumps(response, ensure_ascii=False).encode("utf-8"))
    except (BrokenPipeError, ConnectionResetError):
        # The request may already have run. Keep every loaded script and the daemon alive;
        # the client must report an unknown outcome instead of replaying the request.
        pass


class AgentUnavailableBeforeSend(Exception):
    """The private daemon could not be reached and no request bytes were sent."""


def exchange(socket_path: str, request_data: bytes, timeout: float) -> dict[str, Any]:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        connection.connect(socket_path)
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        connection.close()
        raise AgentUnavailableBeforeSend(str(exc)) from exc
    try:
        connection.sendall(request_data)
        connection.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        connection.close()
    return json.loads(b"".join(chunks).decode("utf-8"))


def run_client(socket_path: str, encoded_request: str, timeout: float) -> None:
    request_data = zlib.decompress(base64.urlsafe_b64decode(encoded_request.encode("ascii")))
    try:
        response = exchange(socket_path, request_data, timeout)
    except AgentUnavailableBeforeSend:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(socket_path)
        log_path = socket_path + ".log"
        with open(log_path, "ab", buffering=0) as log:
            subprocess.Popen([sys.executable, os.path.abspath(__file__), "--daemon", socket_path],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                             close_fds=True, start_new_session=True)
        last_error = None
        for _ in range(50):
            try:
                response = exchange(socket_path, request_data, timeout)
                break
            except AgentUnavailableBeforeSend as exc:
                last_error = exc
                time.sleep(0.1)
            except OSError as exc:
                response = {"ok": False, "error": "persistent Frida request outcome unknown; not retried: " + str(exc)}
                break
        else:
            response = {"ok": False, "error": "persistent Frida agent did not start: " + str(last_error),
                        "agentLog": log_path}
    except OSError as exc:
        response = {"ok": False, "error": "persistent Frida request outcome unknown; not retried: " + str(exc)}
    print(json.dumps(response, ensure_ascii=False))
    raise SystemExit(0 if response.get("ok") else 1)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--daemon":
        serve(sys.argv[2])
    elif len(sys.argv) == 5 and sys.argv[1] == "--client":
        run_client(sys.argv[2], sys.argv[3], float(sys.argv[4]))
    else:
        raise SystemExit("usage: frida_agent.py --daemon SOCKET | --client SOCKET REQUEST TIMEOUT")
