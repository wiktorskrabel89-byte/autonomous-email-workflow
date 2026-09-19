"""Make the run's shared files safe when several emails are worked on at once.

With a pool of API keys the app processes one email per key at the same time,
and every one of those workers writes to the same audit log, the same thread
store and the same idempotency file. Each of those does a read-modify-write:
two workers overlapping there silently lose one of the updates, which would
show up as an email processed twice or a missing audit entry - exactly the
things this app is supposed to be able to prove.

One lock per store is enough, and costs nothing worth measuring: the wait is
microseconds next to the API call that surrounds it.
"""

import functools
import threading

_creation = threading.Lock()


def _lock_for(obj) -> threading.RLock:
    """The object's own lock, made on first use.

    Created lazily rather than in every __init__ so that a store constructed
    anywhere - including in a test, or by a subclass that does not call up -
    is still guarded.
    """
    lock = getattr(obj, "_store_lock", None)
    if lock is not None:
        return lock
    with _creation:
        lock = getattr(obj, "_store_lock", None)
        if lock is None:
            lock = threading.RLock()
            obj._store_lock = lock
        return lock


def synchronized(method):
    """Run this method under its object's lock.

    Re-entrant: a guarded method may call another guarded method on the same
    object (update_stage saves the file, for instance) without deadlocking.
    """

    @functools.wraps(method)
    def guarded(self, *args, **kwargs):
        with _lock_for(self):
            return method(self, *args, **kwargs)

    return guarded
