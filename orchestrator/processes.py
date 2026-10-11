"""Process-tree cleanup and an exclusive lock for a single local worker."""
from __future__ import annotations

import ctypes
import os
import signal
import subprocess
from pathlib import Path


class InstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self.file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        try:
            if self.path.stat().st_size == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            self.file = None
            raise RuntimeError("Local Orchestrator już korzysta z tego katalogu danych.") from exc

    def release(self):
        if self.file:
            self.file.close()
            self.file = None


class ProcessTree:
    """Windows Job Objects also stop descendants after the parent CLI exits.

    Closing the server process closes its job handles, so children do not survive
    a crash. POSIX uses a new process group and explicit group cleanup.
    """
    def __init__(self, process: subprocess.Popen):
        self.process = process
        self.handle = None
        if os.name == "nt":
            from ctypes import wintypes as w

            class BasicLimits(ctypes.Structure):
                _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                            ("flags", w.DWORD), ("minimum", ctypes.c_size_t),
                            ("maximum", ctypes.c_size_t), ("active", w.DWORD),
                            ("affinity", ctypes.c_size_t), ("priority", w.DWORD),
                            ("scheduling", w.DWORD)]

            class IoCounters(ctypes.Structure):
                _fields_ = [(name, ctypes.c_uint64) for name in
                            ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

            class ExtendedLimits(ctypes.Structure):
                _fields_ = [("basic", BasicLimits), ("io", IoCounters),
                            ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                            ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
            self.kernel.CreateJobObjectW.restype = w.HANDLE
            self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
            self.kernel.SetInformationJobObject.restype = w.BOOL
            self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
            self.kernel.AssignProcessToJobObject.restype = w.BOOL
            self.kernel.CloseHandle.argtypes = [w.HANDLE]
            self.kernel.CloseHandle.restype = w.BOOL
            handle = self.kernel.CreateJobObjectW(None, None)
            if not handle:
                process.kill()
                process.wait()
                raise ctypes.WinError(ctypes.get_last_error())
            limits = ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            configured = self.kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
            assigned = configured and self.kernel.AssignProcessToJobObject(handle, int(process._handle))
            if not assigned:
                error = ctypes.WinError(ctypes.get_last_error())
                self.kernel.CloseHandle(handle)
                if process.poll() is None:
                    process.kill()
                    process.wait()
                raise error
            self.handle = handle

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
        elif os.name != "nt":
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def process_options():
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}
