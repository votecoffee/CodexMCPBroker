"""Windows process identity and atomic, suspended job-contained creation.

No native engineering software is discovered, attached or launched on import.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import subprocess
import time
import uuid
from ctypes import wintypes as w
from pathlib import Path


class StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", w.DWORD),
        ("lpReserved", w.LPWSTR),
        ("lpDesktop", w.LPWSTR),
        ("lpTitle", w.LPWSTR),
        *[
            (name, w.DWORD)
            for name in (
                "dwX",
                "dwY",
                "dwXSize",
                "dwYSize",
                "dwXCountChars",
                "dwYCountChars",
                "dwFillAttribute",
                "dwFlags",
            )
        ],
        ("wShowWindow", w.WORD),
        ("cbReserved2", w.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", w.HANDLE),
        ("hStdOutput", w.HANDLE),
        ("hStdError", w.HANDLE),
    ]


class StartupInfoEx(ctypes.Structure):
    _fields_ = [("startup", StartupInfo), ("attributes", ctypes.c_void_p)]


class ProcessInfo(ctypes.Structure):
    _fields_ = [("process", w.HANDLE), ("thread", w.HANDLE), ("pid", w.DWORD), ("tid", w.DWORD)]


def checked(result):
    if not result:
        raise ctypes.WinError(ctypes.get_last_error())
    return result


class ProcessDriver:
    def __init__(self):
        self.kernel = None
        self.host_id = None
        if os.name != "nt":
            return
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                0,
                winreg.KEY_QUERY_VALUE | winreg.KEY_WOW64_64KEY,
            ) as key:
                machine_guid = str(uuid.UUID(winreg.QueryValueEx(key, "MachineGuid")[0]))
            self.host_id = hashlib.sha256(machine_guid.encode("utf-8")).hexdigest()
        except (OSError, ValueError, TypeError, AttributeError):
            pass  # Status can still be read; process control requires a known host.
        k = self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        definitions = {
            "OpenProcess": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "CloseHandle": ([w.HANDLE], w.BOOL),
            "WaitForSingleObject": ([w.HANDLE, w.DWORD], w.DWORD),
            "TerminateProcess": ([w.HANDLE, w.UINT], w.BOOL),
            "GetProcessTimes": ([w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4)], w.BOOL),
            "QueryFullProcessImageNameW": (
                [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)],
                w.BOOL,
            ),
            "InitializeProcThreadAttributeList": (
                [ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.c_size_t)],
                w.BOOL,
            ),
            "UpdateProcThreadAttribute": (
                [
                    ctypes.c_void_p,
                    w.DWORD,
                    ctypes.c_size_t,
                    ctypes.c_void_p,
                    ctypes.c_size_t,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                ],
                w.BOOL,
            ),
            "DeleteProcThreadAttributeList": ([ctypes.c_void_p], None),
            "CreateProcessW": (
                [
                    w.LPCWSTR,
                    w.LPWSTR,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                    w.BOOL,
                    w.DWORD,
                    ctypes.c_void_p,
                    w.LPCWSTR,
                    ctypes.c_void_p,
                    ctypes.POINTER(ProcessInfo),
                ],
                w.BOOL,
            ),
            "ResumeThread": ([w.HANDLE], w.DWORD),
            "QueryInformationJobObject": (
                [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)],
                w.BOOL,
            ),
        }
        for name, (arguments, returns) in definitions.items():
            function = getattr(k, name)
            function.argtypes, function.restype = arguments, returns

    def _require_windows(self):
        if self.kernel is None:
            raise OSError("native resource process control currently requires Windows")

    def capture_handle(self, handle, pid):
        if self.host_id is None:
            raise OSError("cannot verify the local resource host identity")
        created, exited, kernel, user = (w.FILETIME() for _ in range(4))
        checked(
            self.kernel.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            )
        )
        size = w.DWORD(32768)
        image = ctypes.create_unicode_buffer(size.value)
        checked(self.kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)))
        return {
            "host_id": self.host_id,
            "pid": pid,
            "created": str((created.dwHighDateTime << 32) | created.dwLowDateTime),
            "executable": os.path.normcase(os.path.normpath(image.value)),
        }

    def _open(self, pid, terminate=False):
        self._require_windows()
        if type(pid) is not int or not 5 <= pid <= 2**32 - 1:
            raise ValueError("invalid resource process ID")
        handle = self.kernel.OpenProcess(0x100000 | 0x1000 | int(terminate), False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:
                raise ProcessLookupError("recorded process no longer exists")
            raise ctypes.WinError(ctypes.get_last_error())
        return ProcessHandle(self, handle, pid)

    def capture(self, pid):
        with self._open(pid) as handle:
            if not handle.alive():
                raise ProcessLookupError("recorded process already exited")
            return handle.identity

    def state(self, identity):
        if self.kernel is None or self.host_id is None or identity.get("host_id") != self.host_id:
            return "unknown"
        try:
            with self._open(identity["pid"]) as handle:
                if not handle.alive():
                    return "absent"
                try:
                    observed = handle.identity
                except OSError:
                    return "unknown" if handle.alive() else "absent"
                if observed["created"] != identity["created"]:
                    return "different"
                if observed["executable"] != identity["executable"]:
                    return "unknown"
                return "alive" if handle.alive() else "absent"
        except ProcessLookupError:
            return "absent"
        except (OSError, ValueError):
            return "unknown"

    def open(self, identity):
        if self.host_id is None or identity.get("host_id") != self.host_id:
            raise ValueError("resource belongs to a different or unverifiable host")
        handle = self._open(identity["pid"], terminate=True)
        try:
            if not handle.alive() or handle.identity != identity:
                raise ProcessLookupError("process identity changed; refusing termination")
            return handle
        except BaseException:
            handle.close()
            raise

    def create_suspended(self, argv, *, cwd=None, env=None, max_processes=32, show_window=False):
        """Assign a non-inherited kill-on-close job DURING CreateProcess, before execution.

        This intentionally has no uncontained fallback. The caller must durably bind
        the returned process identity before calling resume(). Standard streams are
        not inherited; use an app-owned IPC/argument contract for a background worker.
        """
        self._require_windows()
        import win32job

        if not isinstance(argv, (list, tuple)) or not 1 <= len(argv) <= 256:
            raise ValueError("use a bounded executable argument list")
        if any(not isinstance(v, str) or "\0" in v for v in argv):
            raise ValueError("invalid process arguments")
        if not Path(argv[0]).is_absolute() or not Path(argv[0]).is_file():
            raise ValueError("use an existing absolute executable")
        if type(max_processes) is not int or not 1 <= max_processes <= 256:
            raise ValueError("invalid process tree limit")
        command = subprocess.list2cmdline(argv)
        if len(command.encode("utf-16-le")) > 65532:
            raise ValueError("process command line exceeds Windows limit")
        environment = None
        if env is not None:
            if not isinstance(env, dict) or any(
                not isinstance(k, str)
                or not isinstance(v, str)
                or not k
                or "=" in k
                or "\0" in k
                or "\0" in v
                for k, v in env.items()
            ):
                raise ValueError("invalid process environment")
            block = "\0".join(
                f"{k}={v}" for k, v in sorted(env.items(), key=lambda p: p[0].upper())
            )
            if len(block.encode("utf-16-le")) > 2 * 1024 * 1024:
                raise ValueError("process environment exceeds its budget")
            environment = ctypes.create_unicode_buffer(block + "\0\0")
        job = win32job.CreateJobObject(None, "")
        attributes_ready = False
        info = None
        try:
            limits = win32job.QueryInformationJobObject(
                job, win32job.JobObjectExtendedLimitInformation
            )
            limits["BasicLimitInformation"]["LimitFlags"] = (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                | win32job.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            )
            limits["BasicLimitInformation"]["ActiveProcessLimit"] = max_processes
            win32job.SetInformationJobObject(
                job, win32job.JobObjectExtendedLimitInformation, limits
            )
            size = ctypes.c_size_t()
            self.kernel.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
            if not size.value:
                raise ctypes.WinError(ctypes.get_last_error())
            attributes = ctypes.create_string_buffer(size.value)
            checked(
                self.kernel.InitializeProcThreadAttributeList(attributes, 1, 0, ctypes.byref(size))
            )
            attributes_ready = True
            jobs = (w.HANDLE * 1)(int(job))
            # PROC_THREAD_ATTRIBUTE_JOB_LIST, supported on Windows 10/Server 2016+.
            checked(
                self.kernel.UpdateProcThreadAttribute(
                    attributes, 0, 0x2000D, jobs, ctypes.sizeof(jobs), None, None
                )
            )
            startup = StartupInfoEx()
            startup.startup.cb = ctypes.sizeof(startup)
            startup.startup.dwFlags = 1  # STARTF_USESHOWWINDOW
            startup.startup.wShowWindow = 1 if show_window else 0
            startup.attributes = ctypes.cast(attributes, ctypes.c_void_p)
            info = ProcessInfo()
            flags = 0x4 | 0x80000 | 0x400  # suspended, extended startup, Unicode environment
            if not show_window:
                flags |= 0x8000000  # CREATE_NO_WINDOW
            checked(
                self.kernel.CreateProcessW(
                    argv[0],
                    ctypes.create_unicode_buffer(command),
                    None,
                    None,
                    False,
                    flags,
                    environment,
                    str(cwd) if cwd else None,
                    ctypes.byref(startup),
                    ctypes.byref(info),
                )
            )
            return ContainedProcess(self, job, info)
        except BaseException:
            job.Close()
            if info is not None:
                for handle in (info.thread, info.process):
                    if handle:
                        self.kernel.CloseHandle(handle)
            raise
        finally:
            if attributes_ready:
                self.kernel.DeleteProcThreadAttributeList(attributes)


class ProcessHandle:
    def __init__(self, driver, handle, pid):
        self.driver, self.handle, self.pid = driver, handle, pid
        self._identity = None

    @property
    def identity(self):
        if self._identity is None:
            self._identity = self.driver.capture_handle(self.handle, self.pid)
        return self._identity

    def alive(self):
        result = self.driver.kernel.WaitForSingleObject(self.handle, 0)
        if result not in (0, 258):
            raise OSError("process wait failed")
        return result == 258

    def terminate(self):
        checked(self.driver.kernel.TerminateProcess(self.handle, 1))

    def wait(self, milliseconds):
        return self.driver.kernel.WaitForSingleObject(self.handle, milliseconds) == 0

    def close(self):
        if self.handle:
            self.driver.kernel.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class ContainedProcess:
    def __init__(self, driver, job, info):
        self.driver, self.job, self.info = driver, job, info
        self.identity = driver.capture_handle(info.process, info.pid)

    @property
    def pid(self):
        return self.info.pid

    def resume(self):
        if not self.info.thread:
            raise ValueError("process was already resumed")
        if self.driver.kernel.ResumeThread(self.info.thread) == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        self.driver.kernel.CloseHandle(self.info.thread)
        self.info.thread = None

    def stop(self):
        import win32job

        if self.job is None:
            return
        win32job.TerminateJobObject(self.job, 1)
        deadline = time.monotonic() + 5
        while win32job.QueryInformationJobObject(
            self.job, win32job.JobObjectBasicAccountingInformation
        )["ActiveProcesses"]:
            if time.monotonic() >= deadline:
                raise TimeoutError("owned process tree did not stop")
            time.sleep(0.01)

    def identities(self):
        class ProcessList(ctypes.Structure):
            _fields_ = [("assigned", w.DWORD), ("count", w.DWORD), ("pids", ctypes.c_size_t * 256)]

        info = ProcessList()
        checked(
            self.driver.kernel.QueryInformationJobObject(
                int(self.job), 3, ctypes.byref(info), ctypes.sizeof(info), None
            )
        )
        if info.count > 256:
            raise ValueError("process tree membership exceeds its budget")
        identities = []
        for pid in sorted(info.pids[: info.count]):
            try:
                identities.append(self.driver.capture(pid))
            except ProcessLookupError:
                continue
        return identities

    def close(self):
        if self.job is not None:
            self.job.Close()
            self.job = None
        for name in ("thread", "process"):
            handle = getattr(self.info, name)
            if handle:
                self.driver.kernel.CloseHandle(handle)
                setattr(self.info, name, None)
