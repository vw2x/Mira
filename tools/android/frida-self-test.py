#!/usr/bin/env python3
"""Exercise Mira's embedded Frida Gadget through its Android Python binding."""

import argparse
import json
import sys

import frida


SCRIPT_SOURCE = r"""
var nativeHits = 0;
var javaHits = 0;
var getpidAddress = Module.getGlobalExportByName !== undefined
  ? Module.getGlobalExportByName('getpid')
  : Module.getExportByName(null, 'getpid');
var callGetpid = new NativeFunction(getpidAddress, 'int', []);
var nativeListener = Interceptor.attach(getpidAddress, {
  onEnter: function () { nativeHits++; }
});
var HookTarget = null;
var echoMethod = null;
var targetObject = null;
var javaInitError = null;

try {
  Java.performNow(function () {
    Java.openClassFile('/data/user/0/com.vwww.mira/files/home/frida-hook-target.dex').load();
    HookTarget = Java.use('com.vwww.mira.test.FridaHookTarget');
    targetObject = HookTarget.$new();
    echoMethod = HookTarget.echo.overload('java.lang.String');
    echoMethod.implementation = function (value) {
      javaHits++;
      return echoMethod.call(this, value);
    };
  });
} catch (error) {
  javaInitError = error.stack || String(error);
}

rpc.exports = {
  probe: function (tag) {
    var pid = -1;
    var javaValue = null;
    var javaRunError = null;
    var nativeBefore = nativeHits;
    var javaBefore = javaHits;
    try {
      pid = callGetpid();
      if (targetObject !== null) {
        try {
          Java.performNow(function () {
            javaValue = targetObject.echo(tag);
          });
        } catch (error) {
          javaRunError = error.stack || String(error);
        }
      }
      return {
        tag: tag,
        pid: pid,
        processPid: Process.id,
        nativeDelta: nativeHits - nativeBefore,
        nativeOk: pid === Process.id && nativeHits - nativeBefore >= 1,
        javaInitError: javaInitError,
        javaRunError: javaRunError,
        javaDelta: javaHits - javaBefore,
        javaValue: javaValue,
        javaOk: javaInitError === null && javaRunError === null &&
          javaHits - javaBefore >= 1 && javaValue === 'echo:' + tag
      };
    } finally {
      try { if (echoMethod !== null) echoMethod.implementation = null; } catch (_) {}
      try { if (targetObject !== null) targetObject.$dispose(); } catch (_) {}
      try { nativeListener.detach(); } catch (_) {}
    }
  }
};
"""


def run(tag, rounds, remote):
    device = frida.get_device_manager().add_remote_device(remote)
    output = {
        "tag": tag,
        "python": sys.version.split()[0],
        "fridaPython": frida.__version__,
        "scriptRuntime": "default (create_script runtime omitted)",
        "roundsRequested": rounds,
        "rounds": [],
    }
    for number in range(1, rounds + 1):
        session = None
        script = None
        messages = []
        record = {"round": number, "messages": messages}
        try:
            session = device.attach("Gadget")
            script = session.create_script(SCRIPT_SOURCE)
            script.on("message", lambda message, data: messages.append(message))
            script.load()
            record["result"] = script.exports.probe(tag)
        except Exception as error:
            record["exception"] = repr(error)
        finally:
            if script is not None:
                try:
                    script.unload()
                    record["unloaded"] = True
                except Exception as error:
                    record["unloadError"] = repr(error)
            if session is not None:
                try:
                    session.detach()
                    record["detached"] = True
                except Exception as error:
                    record["detachError"] = repr(error)
        output["rounds"].append(record)
    output["ok"] = all(
        item.get("result", {}).get("nativeOk") is True
        and item.get("result", {}).get("javaOk") is True
        and item.get("unloaded") is True
        and item.get("detached") is True
        and not item.get("messages")
        for item in output["rounds"]
    )
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("tag")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--output", default="frida-self-test-result.json")
    parser.add_argument("--remote", default="127.0.0.1:27042")
    args = parser.parse_args()
    result = run(args.tag, args.rounds, args.remote)
    serialized = json.dumps(result, sort_keys=True)
    with open(args.output, "w", encoding="utf-8") as stream:
        stream.write(serialized + "\n")
    print(serialized)
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
