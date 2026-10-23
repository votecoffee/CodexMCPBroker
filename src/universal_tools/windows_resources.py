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
            "GetExitCodeProcess": ([w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL),
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
        return ProcessHandle(self, handle, pid, allow_termination=terminate)

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

    def _verified_open(self, identity, *, terminate):
        if self.host_id is None or identity.get("host_id") != self.host_id:
            raise ValueError("resource belongs to a different or unverifiable host")
        handle = self._open(identity["pid"], terminate=terminate)
        try:
            if not handle.alive() or handle.identity != identity:
                raise ProcessLookupError("process identity changed; refusing access")
            return handle
        except BaseException:
            handle.close()
            raise

    def open(self, identity):
        """Open a creation-checked handle for an explicitly authorized cleanup."""
        return self._verified_open(identity, terminate=True)

    def observe(self, identity):
        """Pin one verified kernel object with query/wait rights and no termination.

        Identity/image acquisition happens once. Later identity reads and waits do
        not reopen the PID or repeat image queries during process shutdown.
        """
        return self._verified_open(identity, terminate=False)

    def create_suspended(
        self, argv, *, cwd=None, env=None, max_processes=32, show_window=False, stdio_handles=None
    ):
        """Assign a non-inherited kill-on-close job DURING CreateProcess, before execution.

        This intentionally has no uncontained fallback. The caller must durably bind
        the returned process identity before calling resume(). By default no handles
        are inherited. A trusted stdio provider may select exactly three pipe/file
        handles; the job and all unrelated inheritable handles remain excluded.
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
        if stdio_handles is not None and (
            not isinstance(stdio_handles, (tuple, list))
            or len(stdio_handles) != 3
            or any(type(h) is not int or h <= 0 for h in stdio_handles)
            or len(set(stdio_handles)) != 3
        ):
            raise ValueError("stdio requires three distinct explicitly selected handles")
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
            count = 2 if stdio_handles is not None else 1
            self.kernel.InitializeProcThreadAttributeList(None, count, 0, ctypes.byref(size))
            if not size.value:
                raise ctypes.WinError(ctypes.get_last_error())
            attributes = ctypes.create_string_buffer(size.value)
            checked(
                self.kernel.InitializeProcThreadAttributeList(
                    attributes, count, 0, ctypes.byref(size)
                )
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
            if stdio_handles is not None:
                handles = (w.HANDLE * 3)(*stdio_handles)
                # PROC_THREAD_ATTRIBUTE_HANDLE_LIST: no other inheritable handle leaks.
                checked(
                    self.kernel.UpdateProcThreadAttribute(
                        attributes, 0, 0x20002, handles, ctypes.sizeof(handles), None, None
                    )
                )
                startup.startup.dwFlags |= 0x100  # STARTF_USESTDHANDLES
                (
                    startup.startup.hStdInput,
                    startup.startup.hStdOutput,
                    startup.startup.hStdError,
                ) = stdio_handles
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
                    stdio_handles is not None,
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
    def __init__(self, driver, handle, pid, *, allow_termination=True):
        self.driver, self.handle, self.pid = driver, handle, pid
        self._identity = None
        self._allow_termination = allow_termination

    @property
    def identity(self):
        if self._identity is None:
            self._identity = self.driver.capture_handle(self.handle, self.pid)
        return dict(self._identity)

    def alive(self):
        if self.handle is None:
            raise ValueError("process handle is closed")
        result = self.driver.kernel.WaitForSingleObject(self.handle, 0)
        if result not in (0, 258):
            raise OSError("process wait failed")
        return result == 258

    def terminate(self):
        if not self._allow_termination:
            raise PermissionError("observation handles cannot terminate a process")
        if self.handle is None:
            raise ValueError("process handle is closed")
        checked(self.driver.kernel.TerminateProcess(self.handle, 1))

    def wait(self, milliseconds):
        if self.handle is None:
            raise ValueError("process handle is closed")
        if type(milliseconds) is not int or not 0 <= milliseconds < 2**32 - 1:
            raise ValueError("process wait requires a bounded nonnegative millisecond count")
        result = self.driver.kernel.WaitForSingleObject(self.handle, milliseconds)
        if result not in (0, 258):
            raise OSError("process wait failed")
        return result == 0

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
        self._returncode = None

    @property
    def pid(self):
        return self.info.pid

    @property
    def returncode(self):
        if self._returncode is None and self.info.process:
            result = self.driver.kernel.WaitForSingleObject(self.info.process, 0)
            if result == 0:
                code = w.DWORD()
                checked(
                    self.driver.kernel.GetExitCodeProcess(self.info.process, ctypes.byref(code))
                )
                self._returncode = code.value
            elif result != 258:
                raise OSError("owned process wait failed")
        return self._returncode

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
        _ = self.returncode  # Retain the exit code before closing the creation handle.
        if self.job is not None:
            self.job.Close()
            self.job = None
        for name in ("thread", "process"):
            handle = getattr(self.info, name)
            if handle:
                self.driver.kernel.CloseHandle(handle)
                setattr(self.info, name, None)
