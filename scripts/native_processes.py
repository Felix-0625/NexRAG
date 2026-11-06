"""只监管某一次原生启动所拥有的进程树。

调用方提供状态目录和进度报告器。这些辅助函数不会发现或终止已有的服务。
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
    name: str
    process: subprocess.Popen
    log: IO


class WindowsJob:
    """只终止本次启动的进程树，包括 Vite/Ollama 工作进程。"""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
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
        """把子进程加入该 job，之后随 job 关闭而被终止；失败时抛出 WinError。"""
        import ctypes

        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        """关闭 job 句柄；因设置了 KILL_ON_JOB_CLOSE，仍在 job 内的进程会被终止。可重复调用。"""
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class Supervisor:
    def __init__(self, state: Path, report) -> None:
        self.state = state
        self.report = report
        self.children: list[Child] = []
        self.job = WindowsJob() if os.name == "nt" else None

    def start(self, name: str, args: list, cwd: Path, env: dict) -> None:
        """启动子进程，输出写入 logs/<name>.log，并在 Windows 上加入 job；启动期退出或隔离失败时终止进程并抛出 RuntimeError，不绕过隔离。"""
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
                    # 进程在 CreateProcess 与加入 job 之间退出时，Windows 同样会返回
                    # ERROR_ACCESS_DENIED。这属于服务启动失败，不是要求提权或关闭
                    # 隔离的请求；需保留真实的退出码/日志上下文。
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
        """任一受管子进程已退出则抛出 RuntimeError，附带退出码和日志路径。"""
        for child in self.children:
            if child.process.poll() is not None:
                raise RuntimeError(
                    f"{child.name} stopped (exit {child.process.returncode}). "
                    f"See {self.state / 'logs' / (child.name + '.log')}"
                )

    def wait_http(self, endpoints: dict[str, str], timeout: float = 1200) -> None:
        """轮询各端点（绕过系统代理）直到全部返回 200；期间子进程退出或超过 timeout 秒则抛出 RuntimeError，并定期输出等待提示。"""
        remaining = dict(endpoints)
        deadline = time.monotonic() + timeout
        next_report = time.monotonic() + 20
        # 回环健康检查请求必须绕过系统代理。
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
        """按启动的逆序终止子进程（10 秒内未退出则强制结束）、关闭日志文件并释放 job。"""
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
