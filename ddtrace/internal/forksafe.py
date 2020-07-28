"""
An API to provide after_in_child fork hooks across all Pythons.
"""
import functools
import logging
import os
import threading


__all__ = [
    "call_nocheck",
    "ddtrace_after_in_child",
    "register",
]


log = logging.getLogger(__name__)

BUILTIN_FORK_HOOKS = hasattr(os, "register_at_fork")

registry = []


def ddtrace_after_in_child():
    global registry

    for hook in registry:
        try:
            hook()
        except Exception:
            # Mimic the behaviour of Python's fork hooks.
            log.exception("Exception ignored in forksafe hook %r", hook)


if BUILTIN_FORK_HOOKS:

    def register(after_in_child):
        registry.append(after_in_child)
        return lambda f: f

    def call_nocheck(f, *args, **kwargs):
        return f(*args, **kwargs)

    os.register_at_fork(after_in_child=ddtrace_after_in_child)
else:
    PID = os.getpid()
    PID_LOCK = threading.Lock()

    def register(after_in_child):
        """Decorator that registers a function `after_in_child` that will be
        called in the child process when a fork occurs.

        Decorator usage::
            def after_fork():
                # update fork-sensitive state
                pass

            @forksafe.register(after_in_child=after_fork)
            def fork_sensitive_fn():
                # after_fork is guaranteed to be called by this point
                # if a fork occurred and we're in the child.
                pass
        """
        registry.append(after_in_child)

        def wrapper(func):
            global PID

            @functools.wraps(func)
            def forksafe_func(*args, **kwargs):
                global PID

                if kwargs.pop("_check_pid", True):
                    # A lock is required here to ensure that the hooks
                    # are only called once.
                    with PID_LOCK:
                        pid = os.getpid()

                        # Check the global pid
                        if pid != PID:
                            # Call ALL the hooks.
                            ddtrace_after_in_child()
                            PID = pid
                return func(*args, **kwargs)

            # Set a flag to use to perform sanity checks.
            forksafe_func._is_forksafe = True
            return forksafe_func

        return wrapper

    def call_nocheck(f, *args, **kwargs):
        if not hasattr(f, "_is_forksafe"):
            raise ValueError("The given function is not forksafe. Was it `registered`?")

        return f(*args, _check_pid=False, **kwargs)
