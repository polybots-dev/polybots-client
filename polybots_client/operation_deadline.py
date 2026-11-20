"""Shared monotonic deadline across nested transport phases, never renewed."""

import math
import time
from contextlib import contextmanager
from contextvars import ContextVar

_deadline = ContextVar("polytopia_operation_deadline", default=math.inf)


def timeout(cap):
    remaining = min(cap, _deadline.get() - time.monotonic())
    if remaining <= 0:
        raise TimeoutError("Operation deadline exceeded")
    return remaining


def check():
    timeout(math.inf)


@contextmanager
def bounded_lock(lock, cap):
    if not lock.acquire(timeout=timeout(cap)):
        raise TimeoutError("Operation lock deadline exceeded")
    try:
        check()
        yield
    finally:
        lock.release()


@contextmanager
def operation_deadline(expires):
    if type(expires) not in (int, float) or not math.isfinite(expires):
        raise ValueError("Invalid operation deadline")
    token = _deadline.set(min(expires, _deadline.get()))
    try:
        check()
        yield
    finally:
        _deadline.reset(token)
