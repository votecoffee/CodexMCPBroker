"""Small OS-owned state lock shared by the broker and desktop manager."""

import errno
import math
import os
import time
from pathlib import Path

from .models import BrokerError


class InstanceLock:
    """An OS-owned lock is released automatically if its process exits or crashes."""

    def __init__(self, path: Path, *, timeout=0):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("lock timeout must be a finite number from 0 to 10 seconds")
        if not math.isfinite(timeout) or not 0 <= timeout <= 10:
            raise ValueError("lock timeout must be a finite number from 0 to 10 seconds")
        self.path = path
        self.timeout = timeout
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                try:
                    self._acquire()
                    break
                except OSError as error:
                    if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        message = (
                            "state lock remained busy; no operation was started"
                            if self.timeout
                            else "another process already owns this state directory"
                        )
                        raise BrokerError(message) from None
                    time.sleep(min(0.02, remaining))
        except BaseException:
            self.stream.close()
            raise
        return self

    def _acquire(self):
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __exit__(self, *args):
        self.stream.close()
