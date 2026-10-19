#!/usr/bin/env python3
"""
glue.py — cross-platform entry point (Windows / macOS / Linux).

The bash `tools/glue` wrapper is convenient on macOS/Linux but doesn't run on
Windows. This Python dispatcher works everywhere and reads credentials via the
cross-platform store (keyring -> file -> env), so no shell is required.

  python3 tools/glue.py setup                 # onboarding wizard
  python3 tools/glue.py auth token|keys|command-list
  python3 tools/glue.py ble  scan|unlock|lock|bolt|info
  python3 tools/glue.py test                  # offline protocol self-audit
  python3 tools/glue.py where                 # show which secret store is active
"""
import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _run(module, argv):
    sys.argv = [module] + argv
    runpy.run_path(os.path.join(HERE, module), run_name="__main__")


def main():
    tool = sys.argv[1] if len(sys.argv) > 1 else "auth"
    rest = sys.argv[2:]
    if tool == "setup":
        _run("glue_setup.py", rest)
    elif tool == "auth":
        _run("glue_auth.py", rest or ["token"])
    elif tool == "ble":
        _run("glue_ble.py", rest)
    elif tool == "test":
        _run("test_protocol.py", rest)
    elif tool == "sync":
        _run("glue_sync.py", rest)
    elif tool == "where":
        import glue_store
        print(glue_store.backend_name())
    else:
        sys.exit("usage: glue.py {setup|auth|ble|test|sync|where} ...")


if __name__ == "__main__":
    main()
