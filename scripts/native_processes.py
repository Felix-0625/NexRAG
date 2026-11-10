"""Supervise only process trees owned by one native launch.

Callers provide state directories and progress reporting. Never discover or stop existing
services owned by someone else.
"""

import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import IO


@dataclass
class Child:
    """Owned child process and its associated log handle."""

    name: str
    process: subprocess.Popen
    log: IO


class WindowsJob:
    """Stop only this launch's process tree, including Vite and Ollama workers."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            """Windows job basic-limit structure matching the native API layout."""

            _fields_ = [
                ("process_time", ctypes.c_int64),
                ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD),
                ("minimum_ws", ctypes.c_size_t),
                ("maximum_ws", ctypes.c_size_t),
                ("active_processes", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            """Windows job extended-limit structure matching the native API layout."""

            _fields_ = [
                ("basic", BasicLimits),
                ("io", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t),
                ("peak_job_memory", ctypes.c_size_t),
            ]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = ExtendedLimits()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(
            self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)
        ):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, process: subprocess.Popen) -> None:
        """Assign a child to the job for termination on close; raise WinError on failure."""
        import ctypes

        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        """Idempotently close the job, terminating members through KILL_ON_JOB_CLOSE."""
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class Supervisor:
    """Manage only child processes created by this launch."""

    def __init__(self, state: Path, report) -> None:
        self.state = state
        self.report = report
        self.children: list[Child] = []
        self.job = WindowsJob() if os.name == "nt" else None

    def start(self, name: str, args: list, cwd: Path, env: dict) -> None:
        """Start a logged child and require Windows job isolation; stop and raise on early exit or
        isolation failure.
        """
        log_path = self.state / "logs" / f"{name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = log_path.open("w", encoding="utf-8")
        kwargs = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        try:
            process = subprocess.Popen(
                [str(arg) for arg in args],
                cwd=cwd,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                **kwargs,
            )
            try:
                if self.job:
                    # Windows may return ERROR_ACCESS_DENIED if a child exits between CreateProcess
                    # and job assignment; treat this as startup failure, not a request for elevation
                    # or weaker isolation, and preserve exit-code and log context.
                    code = process.poll()
                    if code is not None:
                        raise RuntimeError(
                            f"{name} stopped during startup (exit {code}). See {log_path}"
                        )
                    try:
                        self.job.assign(process)
                    except OSError as exc:
                        code = process.poll()
                        if code is not None:
                            raise RuntimeError(
                                f"{name} stopped during startup (exit {code}). See {log_path}"
                            ) from exc
                        raise RuntimeError(
                            f"{name} process isolation failed: {exc}. See {log_path}. "
                            "The service was stopped; job isolation was not bypassed."
                        ) from exc
            except BaseException:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
                raise
        except OSError as exc:
            log.close()
            raise RuntimeError(f"{name} could not start: {exc}. See {log_path}") from exc
        except BaseException:
            log.close()
            raise
        self.children.append(Child(name, process, log))

    def check(self) -> None:
        """Raise with exit code and log path if any owned child exits."""
        for child in self.children:
            if child.process.poll() is not None:
                raise RuntimeError(
                    f"{child.name} stopped (exit {child.process.returncode}). "
                    f"See {self.state / 'logs' / (child.name + '.log')}"
                )

    def wait_http(self, endpoints: dict[str, str], timeout: float = 1200) -> None:
        """Poll loopback endpoints without proxies until healthy, failing on child exit or timeout."""
        remaining = dict(endpoints)
        deadline = time.monotonic() + timeout
        next_report = time.monotonic() + 20
        # Loopback health checks must bypass system proxies.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while remaining:
            self.check()
            for name, endpoint in list(remaining.items()):
                try:
                    with opener.open(endpoint, timeout=2) as response:
                        if response.status == 200:
                            del remaining[name]
                except (OSError, urllib.error.URLError):
                    pass
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"Timed out waiting for {', '.join(remaining)}. See {self.state / 'logs'}."
                )
            if remaining and time.monotonic() > next_report:
                self.report(
                    f"Waiting for {', '.join(remaining)}; first run may download model files. Logs: {self.state / 'logs'}"
                )
                next_report = time.monotonic() + 20
            if remaining:
                time.sleep(0.5)

    def close(self) -> None:
        """Stop children in reverse order, force after ten seconds, close logs, and release the
        job.
        """
        for child in reversed(self.children):
            if child.process.poll() is None:
                child.process.terminate()
                try:
                    child.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.process.kill()
                    child.process.wait()
            child.log.close()
        if self.job:
            self.job.close()
