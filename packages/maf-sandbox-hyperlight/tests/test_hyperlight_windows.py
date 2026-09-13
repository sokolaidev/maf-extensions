"""Windows kernel enforcement of worker memory and lifetime, independent of WHP availability."""

from __future__ import annotations

import ctypes
import subprocess
import sys
from ctypes import wintypes

import pytest

from maf_sandbox_hyperlight._windows import Job

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows kernel jobs")


def test_job_memory_limit_prevents_native_buffer_growth():
    job = Job(64 * 1024 * 1024)
    script = """import sys
sys.stdin.readline()
try:
    buffer = bytearray(128 * 1024 * 1024)
except MemoryError:
    print('bounded')
else:
    raise AssertionError('allocation exceeded the job budget')
"""
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        job.assign(process.pid)
        stdout, stderr = process.communicate(b"start\n", timeout=5)
        assert process.returncode == 0 and stdout.strip() == b"bounded", stderr
    finally:
        job.close()
        process.wait(timeout=5)


def test_abrupt_owner_exit_kills_the_worker():
    if sys.platform != "win32":
        pytest.skip("Windows kernel job API")
    script = """import os, subprocess, sys
from maf_sandbox_hyperlight._windows import Job
job = Job(128 * 1024 * 1024)
worker = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(60)'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
job.assign(worker.pid)
print(worker.pid, flush=True)
sys.stdin.readline()
os._exit(0)
"""
    owner = subprocess.Popen(
        [sys.executable, "-I", "-u", "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = None
    try:
        assert owner.stdout is not None
        pid = int(owner.stdout.readline())
        handle = api.OpenProcess(0x00100000 | 0x0001, False, pid)
        assert handle
        owner.communicate(b"exit\n", timeout=5)
        assert owner.returncode == 0
        assert api.WaitForSingleObject(handle, 3000) == 0, "worker survived abrupt owner exit"
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=5)
        if handle:
            api.TerminateProcess(handle, 1)
            api.CloseHandle(handle)
