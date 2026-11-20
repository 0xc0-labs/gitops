import compileall
import os
import pathlib
import subprocess
import sys
import time


def inside_gvisor():
    # gVisor's Sentry answers with a fixed 4.4.0 kernel; a runc pod on the
    # node reports the host's own.
    return os.uname().release == "4.4.0" and "Linux version 4.4.0" in pathlib.Path("/proc/version").read_text()


def timed(label, fn):
    start = time.perf_counter()
    fn()
    seconds = time.perf_counter() - start
    print(f"phase {label}: {seconds:.2f}s", flush=True)
    return seconds


def small_files(root):
    directory = root / "files"
    directory.mkdir()
    for i in range(3000):
        (directory / f"f{i}.txt").write_text(f"{i}\n" * 64)
    assert sum(len(p.read_bytes()) for p in directory.iterdir()) > 0


def processes():
    for _ in range(150):
        subprocess.run(["/usr/local/bin/python", "-S", "-c", "pass"], check=True)


def compile_sources(root):
    sources = root / "src"
    sources.mkdir()
    for i in range(300):
        body = "\n".join(f"def f{j}(x):\n    return x * {j} + {i}\n" for j in range(40))
        (sources / f"m{i}.py").write_text(body)
    assert compileall.compile_dir(sources, quiet=2, force=True)


def main():
    expected = os.environ["EXPECTED_RUNTIME"]
    sandboxed = inside_gvisor()
    print(f"runtime expected: {expected}")
    print(f"kernel: {os.uname().release} ({pathlib.Path('/proc/version').read_text().strip()})")
    print(f"inside gVisor: {sandboxed}")
    if sandboxed != (expected == "gvisor"):
        sys.exit("the pod is not running under the runtime it expected")

    root = pathlib.Path(os.environ["WORKDIR"])
    total = (
        timed("small-files", lambda: small_files(root))
        + timed("processes", processes)
        + timed("compile", lambda: compile_sources(root))
    )
    print(f"total: {total:.2f}s")


main()
