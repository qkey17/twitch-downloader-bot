"""Non-blocking OS leases for temporary files (released on process exit)."""
import os


def try_lock_file(handle) -> bool:
    try:
        if os.name == "nt":
            import msvcrt
            position = os.lseek(handle.fileno(), 0, os.SEEK_CUR)
            os.lseek(handle.fileno(), 0, os.SEEK_SET)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            finally:
                os.lseek(handle.fileno(), position, os.SEEK_SET)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
