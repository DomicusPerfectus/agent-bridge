"""Private, dependency-free Windows process-tree and synchronous-I/O ownership.

Git starts suspended, joins a kill-on-close job, then resumes. Assigning an
already running process would race its children; taskkill loses the tree after
the parent exits. These handles never refer to unrelated processes.
https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
https://learn.microsoft.com/en-us/windows/win32/api/processsnapshot/nf-processsnapshot-psscapturesnapshot
"""

import ctypes
from ctypes import wintypes as w
import time


class _Limits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                ("flags", w.DWORD), ("minimum", ctypes.c_size_t),
                ("maximum", ctypes.c_size_t), ("active", w.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", w.DWORD),
                ("scheduling", w.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", _Limits), ("io", ctypes.c_ulonglong * 6),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]


class _Accounting(ctypes.Structure):
    _fields_ = [("times", ctypes.c_longlong * 4), ("faults", w.DWORD),
                ("total", w.DWORD), ("active", w.DWORD), ("terminated", w.DWORD)]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [("status", w.DWORD), ("teb", w.LPVOID), ("process", w.DWORD),
                ("thread", w.DWORD), ("affinity", ctypes.c_size_t),
                ("priority", ctypes.c_int), ("base", ctypes.c_int),
                ("argument", w.LPVOID), ("syscall", w.WORD),
                ("create", w.FILETIME), ("exit", w.FILETIME),
                ("kernel", w.FILETIME), ("user", w.FILETIME),
                ("start", w.LPVOID), ("capture", w.FILETIME),
                ("flags", w.DWORD), ("suspend", w.WORD),
                ("context_size", w.WORD), ("context", w.LPVOID)]


_api = ctypes.WinDLL("kernel32", use_last_error=True)
for _name, _result, _arguments in (
    ("CreateJobObjectW", w.HANDLE, [ctypes.c_void_p, w.LPCWSTR]),
    ("SetInformationJobObject", w.BOOL, [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]),
    ("AssignProcessToJobObject", w.BOOL, [w.HANDLE, w.HANDLE]),
    ("TerminateJobObject", w.BOOL, [w.HANDLE, w.UINT]),
    ("QueryInformationJobObject", w.BOOL, [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p]),
    ("OpenProcess", w.HANDLE, [w.DWORD, w.BOOL, w.DWORD]),
    ("IsProcessInJob", w.BOOL, [w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)]),
    ("WaitForSingleObject", w.DWORD, [w.HANDLE, w.DWORD]),
    ("OpenThread", w.HANDLE, [w.DWORD, w.BOOL, w.DWORD]),
    ("ResumeThread", w.DWORD, [w.HANDLE]),
    ("CancelSynchronousIo", w.BOOL, [w.HANDLE]),
    ("PssCaptureSnapshot", w.DWORD, [w.HANDLE, w.DWORD, w.DWORD, ctypes.POINTER(w.HANDLE)]),
    ("PssWalkMarkerCreate", w.DWORD, [w.LPVOID, ctypes.POINTER(w.HANDLE)]),
    ("PssWalkSnapshot", w.DWORD, [w.HANDLE, ctypes.c_int, w.HANDLE, w.LPVOID, w.DWORD]),
    ("PssWalkMarkerFree", w.DWORD, [w.HANDLE]),
    ("PssFreeSnapshot", w.DWORD, [w.HANDLE, w.HANDLE]),
    ("GetCurrentProcess", w.HANDLE, []),
    ("CloseHandle", w.BOOL, [w.HANDLE]),
):
    _function = getattr(_api, _name)
    _function.restype, _function.argtypes = _result, _arguments


def _check(value):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


def _status(error):
    if error:
        raise ctypes.WinError(error)


class WindowsJob:
    """Own Git and its descendants, including after the Git parent is reaped."""

    def __init__(self):
        self.process_handles = []
        self.handle = _check(_api.CreateJobObjectW(None, None))
        try:
            limits = _ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            _check(_api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        except BaseException:
            self.close()
            raise

    def attach_and_resume(self, process):
        # SET_QUOTA | TERMINATE | QUERY_INFORMATION | VM_READ, for our child only.
        handle = _check(_api.OpenProcess(0x0511, False, process.pid))
        snapshot, marker = w.HANDLE(), w.HANDLE()
        try:
            _check(_api.AssignProcessToJobObject(self.handle, handle))
            # Popen closes its thread handle. Capture only our suspended process;
            # a system-wide thread snapshot would add cost to every Git operation.
            _status(_api.PssCaptureSnapshot(handle, 0x80, 0, ctypes.byref(snapshot)))  # PSS_CAPTURE_THREADS
            _status(_api.PssWalkMarkerCreate(None, ctypes.byref(marker)))
            entry = _ThreadEntry()
            _status(_api.PssWalkSnapshot(snapshot, 3, marker, ctypes.byref(entry), ctypes.sizeof(entry)))  # PSS_WALK_THREADS
            if entry.process != process.pid or entry.suspend != 1:
                raise OSError("Git suspended primary thread was not found")
            thread = _check(_api.OpenThread(0x0002, False, entry.thread))
            try:
                previous = _api.ResumeThread(thread)
                if previous == 0xFFFFFFFF:
                    raise ctypes.WinError(ctypes.get_last_error())
                if previous != 1:
                    raise OSError("Git primary thread was not suspended exactly once")
            finally:
                _check(_api.CloseHandle(thread))
        finally:
            try:
                if marker.value:
                    _status(_api.PssWalkMarkerFree(marker))
            finally:
                try:
                    if snapshot.value:
                        _status(_api.PssFreeSnapshot(_api.GetCurrentProcess(), snapshot))
                finally:
                    _check(_api.CloseHandle(handle))

    def terminate(self):
        try:
            # Job accounting can reach zero before process handles are signalled.
            # Keep handles to current members so cleanup can wait for actual exit.
            capacity = self.active() + 16
            for _ in range(3):
                class ProcessIds(ctypes.Structure):
                    _fields_ = [("assigned", w.DWORD), ("listed", w.DWORD),
                                ("ids", ctypes.c_size_t * capacity)]
                members = ProcessIds()
                if _api.QueryInformationJobObject(self.handle, 3, ctypes.byref(members), ctypes.sizeof(members), None):
                    break
                if ctypes.get_last_error() != 234:  # ERROR_MORE_DATA
                    raise ctypes.WinError(ctypes.get_last_error())
                capacity = max(capacity * 2, members.assigned + 16)
            else:
                raise OSError("Git process tree changed too quickly to enumerate")
            for pid in members.ids[:members.listed]:
                handle = _api.OpenProcess(0x100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
                if not handle:
                    if ctypes.get_last_error() == 87:  # Already exited.
                        continue
                    raise ctypes.WinError(ctypes.get_last_error())
                inside = w.BOOL()
                try:
                    _check(_api.IsProcessInJob(handle, self.handle, ctypes.byref(inside)))
                    if inside.value:  # Never wait on a reused PID outside our job.
                        self.process_handles.append(handle)
                        handle = None
                finally:
                    if handle is not None:
                        _check(_api.CloseHandle(handle))
        finally:
            _check(_api.TerminateJobObject(self.handle, 1))

    def wait(self, deadline):
        for handle in self.process_handles:
            result = _api.WaitForSingleObject(handle, max(0, int((deadline - time.monotonic()) * 1000)))
            if result == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
            if result != 0:
                raise OSError("Git descendant exit exceeded the cleanup bound")

    def active(self):
        info = _Accounting()
        _check(_api.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None))
        return info.active

    @staticmethod
    def cancel_io(thread):
        # Never close a buffered stream while its reader holds the stream lock.
        # Cancellation completes in the worker, which then closes its own stream.
        handle = _api.OpenThread(0x0001, False, thread.native_id)  # THREAD_TERMINATE
        if not handle:
            if not thread.is_alive():
                return
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not _api.CancelSynchronousIo(handle):
                error = ctypes.get_last_error()
                if error != 1168:  # ERROR_NOT_FOUND: no pending I/O to cancel.
                    raise ctypes.WinError(error)
        finally:
            _check(_api.CloseHandle(handle))

    def close(self):
        try:
            if self.handle is not None:
                _check(_api.CloseHandle(self.handle))
                self.handle = None
        finally:
            for handle in self.process_handles:
                _check(_api.CloseHandle(handle))
            self.process_handles.clear()
