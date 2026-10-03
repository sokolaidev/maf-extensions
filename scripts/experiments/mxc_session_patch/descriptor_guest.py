"""Fixed guest programs for descriptor qualification; guest assertions are not host authority."""

import ctypes
import hashlib
import json
import os
import sys
import threading

LIMIT = 1024 * 1024


def payload(stream):
    """Mix non-text bytes, multibyte text and a long single write."""
    return bytes(range(256)) * 32 + ("\u20ac\U0001f642\n" * 1000).encode() + bytes([stream]) * 4097


def pipe_case(case):
    """Collect fixed workloads through two guest pipe readers."""
    sys.stdout.flush()
    sys.stderr.flush()
    saved = [os.dup(1), os.dup(2)]
    reads = []
    collectors = []
    threads = []

    def drain(fd, result):
        digest = hashlib.sha256()
        while True:
            data = os.read(fd, 4096)
            if not data:
                break
            digest.update(data)
            result["total"] += len(data)
            room = LIMIT - len(result["retained"])
            result["retained"].extend(data[:room])
        result["sha256"] = digest.hexdigest()
        os.close(fd)

    for fd in (1, 2):
        read, write = os.pipe()
        reads.append(read)
        os.dup2(write, fd)
        os.close(write)
        result = {"total": 0, "retained": bytearray()}
        collectors.append(result)
        thread = threading.Thread(target=drain, args=(read, result))
        thread.start()
        threads.append(thread)

    def write_all(fd, data):
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            assert written > 0
            view = view[written:]

    try:
        if case == "fidelity":
            libc = ctypes.CDLL(None)
            libc.write.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
            libc.write.restype = ctypes.c_ssize_t

            def produce(fd):
                data = payload(fd)
                write_all(fd, data)
                duplicate = os.dup(fd)
                write_all(duplicate, b"DUP\x00\xff")
                os.close(duplicate)
                if sys.platform == "linux":
                    written = os.writev(fd, [b"VEC\x00", b"\xff"])
                    if written != 5:
                        raise RuntimeError(f"writev returned {written}, expected 5")
                else:
                    raise RuntimeError("this probe requires the Linux guest")
                written = libc.write(fd, b"NATIVE\x00\xff", 8)
                if written != 8:
                    raise RuntimeError(f"native write returned {written}, expected 8")

            writers = [threading.Thread(target=produce, args=(fd,)) for fd in (1, 2)]
            for thread in writers:
                thread.start()
            for thread in writers:
                thread.join()
            print("PYTHON", flush=True)
            print("PYTHON", file=sys.stderr, flush=True)
        elif case in ("exact", "overflow"):
            write_all(1, b"Q" * (LIMIT + (case == "overflow")))
        elif case == "bypass":
            fd = os.open("/dev/stdout", os.O_WRONLY)
            write_all(fd, b"MXC_BYPASS\n")
            os.close(fd)
        elif case in ("timeout", "cancel"):
            write_all(1, b"Q" * 4096)
            os.write(saved[0], b"MXC_OUTPUT_READY\n")
            while True:
                write_all(1, b"Q" * 4096)
        else:
            raise ValueError(case)
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        for fd, original in zip((1, 2), saved):
            os.dup2(original, fd)
            os.close(original)
        for thread in threads:
            thread.join(timeout=5)
        assert not any(thread.is_alive() for thread in threads)
    return [
        {
            "total": item["total"],
            "sha256": item["sha256"],
            "retained_bytes": len(item["retained"]),
            "retained_sha256": hashlib.sha256(item["retained"]).hexdigest(),
            "overflow": item["total"] > LIMIT,
        }
        for item in collectors
    ]


def file_case():
    """Measure whether the guest enforces its file-size resource limit."""
    if sys.platform == "linux":
        import resource

        before = resource.getrlimit(resource.RLIMIT_FSIZE)
        resource.setrlimit(resource.RLIMIT_FSIZE, (4096, before[1]))
        with open("/tmp/mxc-output-limit", "wb", buffering=0) as stream:
            try:
                written = stream.write(b"F" * 4097)
                error = None
            except OSError as exc:
                written = None
                error = exc.errno
        return {
            "written": written,
            "errno": error,
            "size": os.stat("/tmp/mxc-output-limit").st_size,
        }
    raise RuntimeError("this probe requires the Linux guest")


case = globals()["MXC_CASE"]
if case.startswith("console_"):
    os.write(1, b"X" * int(case.split("_")[1]))
elif case == "file_limit":
    print("MXC_OBSERVATION:" + json.dumps(file_case()), flush=True)
else:
    print("MXC_OBSERVATION:" + json.dumps(pipe_case(case)), flush=True)
