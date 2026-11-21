"""Proof that the runner's tree works in a sandbox's pod (README.md, The runner).

Plain Python on the standard library, run by the toolchain image's interpreter.
Every check prints `ok <name>` or `FAILED <name>: <how>`; the exit status is
non-zero when any failed. The tree's own programs run with an empty environment
but for PATH, as the sandbox does.
"""

import os
import subprocess
import sys
import time

TREE = "/opt/runner"
VENV_PYTHON = f"{TREE}/venv/bin/python"
BWRAP = f"{TREE}/bin/bwrap"
MARKER = "PROOF_MARKER_5f0c1d"

failed = []


def report(name, passed, detail=""):
    if passed:
        print(f"ok {name}", flush=True)
    else:
        failed.append(name)
        print(f"FAILED {name}: {detail}", flush=True)


def tree_env(**extra):
    return {"PATH": os.environ["PATH"], "HOME": "/tmp", **extra}


def run(argv, timeout=60, **kwargs):
    kwargs.setdefault("env", tree_env())
    # Every argv is a list of this file's own constants and paths of the tree.
    return subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        argv, capture_output=True, text=True, timeout=timeout, **kwargs
    )


def tail(result):
    return f"exit {result.returncode}: {(result.stdout + result.stderr).strip()[-600:]}"


def check_command(name, argv, **kwargs):
    try:
        result = run(argv, **kwargs)
    except (OSError, subprocess.TimeoutExpired) as error:
        report(name, False, repr(error))
        return None
    report(name, result.returncode == 0, tail(result))
    return result


def check_tree_python():
    probe = (
        "import sys, matchmark_runner, claude_agent_sdk;"
        f"assert sys.prefix.startswith('{TREE}/'), sys.prefix;"
        f"bad = [p for p in sys.path if p and not p.startswith('{TREE}/')];"
        "assert not bad, bad"
    )
    check_command(
        "tree runs with its own python (runner and Agent SDK import)",
        [VENV_PYTHON, "-c", probe],
    )


def check_claude_binary():
    # The Agent SDK bundles the Claude Code binary under its package.
    found = run(
        [VENV_PYTHON, "-c",
         "import pathlib, claude_agent_sdk as m;"
         "print(*pathlib.Path(m.__file__).parent.glob('_bundled/claude*'))"]
    )
    paths = found.stdout.split()
    if found.returncode != 0 or not paths:
        report("claude binary starts (--version)", False, f"not found: {tail(found)}")
        return
    check_command("claude binary starts (--version)", [paths[0], "--version"])


def check_bwrap_version():
    check_command("bwrap --version from the tree", [BWRAP, "--version"])


def pod_first_process_environ():
    with open("/proc/1/environ", "rb") as handle:
        return handle.read()


def check_bwrap_namespaces():
    name = "bwrap makes user, pid and mount namespaces with its own /proc"
    try:
        outer = pod_first_process_environ()
        assert b"PROOF_POD_FIRST_PROCESS=1" in outer, "the pod's pid 1 is not the proof's own"
        result = run(
            [
                BWRAP, "--unshare-user", "--unshare-pid", "--ro-bind", "/", "/",
                "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                "--clearenv", "--setenv", "PATH", os.environ["PATH"],
                "--setenv", "PROOF_INSIDE", "1",
                "--", "sh", "-c",
                "readlink /proc/self/ns/pid; cat /proc/1/environ | tr '\\0' '\\n'",
            ],
            env=tree_env(PROOF_BWRAP_PARENT="1"),
        )
    except (OSError, subprocess.TimeoutExpired, AssertionError) as error:
        report(name, False, repr(error))
        return
    if result.returncode != 0:
        report(name, False, tail(result))
        return
    inside = result.stdout
    # Pid 1 of the new namespace is bwrap, started with the proof's scrubbed
    # environment: the pod's first process's variable must not be there.
    if "PROOF_POD_FIRST_PROCESS" in inside:
        report(name, False, "/proc/1/environ inside is the pod's first process's")
    elif "PROOF_BWRAP_PARENT=1" not in inside:
        report(name, False, f"/proc/1/environ inside is not bwrap's own: {inside.strip()[-400:]}")
    else:
        report(name, True)


READER = f"""
import ctypes, os, sys
if sys.argv[1] == "hide":
    PR_SET_DUMPABLE = 4
    if ctypes.CDLL(None, use_errno=True).prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        sys.exit("prctl failed")
print("ready", flush=True)
sys.stdin.read()
"""


def start_holder(mode):
    process = subprocess.Popen(
        [VENV_PYTHON, "-c", READER, mode],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        env=tree_env(**{MARKER: mode}),
    )
    if process.stdout.readline().strip() != "ready":
        process.kill()
        raise RuntimeError(f"holder {mode} did not start")
    return process


def readable_address(pid):
    with open(f"/proc/{pid}/maps") as maps:
        for line in maps:
            span, perms = line.split()[:2]
            if perms.startswith("r"):
                return int(span.split("-")[0], 16)
    raise RuntimeError("no readable mapping")


def try_reads(pid):
    """Return (environ read, mem read) as outcomes: bytes seen or the error."""
    outcomes = []
    try:
        with open(f"/proc/{pid}/environ", "rb") as handle:
            outcomes.append(MARKER.encode() in handle.read())
    except OSError as error:
        outcomes.append(repr(error))
    try:
        address = readable_address(pid)
        with open(f"/proc/{pid}/mem", "rb", buffering=0) as handle:
            handle.seek(address)
            outcomes.append(len(handle.read(16)) == 16)
    except (OSError, RuntimeError, ValueError, OverflowError) as error:
        outcomes.append(repr(error))
    return outcomes


def check_dumpable():
    name = "a non-dumpable process cannot be read by another of the same user"
    visible = hidden = None
    try:
        visible = start_holder("show")
        hidden = start_holder("hide")
        control = try_reads(visible.pid)
        # The control: a process that did not hide itself must be readable, or
        # the refusal below proves nothing.
        if control != [True, True]:
            report(name, False, f"control process not readable (the test sees nothing): {control}")
            return
        refused = try_reads(hidden.pid)
        leaked = [what for what, outcome in zip(("environ", "mem"), refused) if outcome is True]
        if leaked:
            report(name, False, f"read {', '.join(leaked)} of a process with PR_SET_DUMPABLE 0")
        else:
            report(name, True)
    except (OSError, RuntimeError) as error:
        report(name, False, repr(error))
    finally:
        for process in (visible, hidden):
            if process:
                process.kill()
                process.wait()


def main():
    print(f"kernel: {os.uname().release}, uid {os.getuid()}", flush=True)
    check_tree_python()
    check_claude_binary()
    check_bwrap_version()
    check_bwrap_namespaces()
    check_dumpable()
    if failed:
        print(f"{len(failed)} FAILED: {'; '.join(failed)}", flush=True)
        sys.exit(1)
    print("all checks ok", flush=True)


if __name__ == "__main__":
    main()
