"""This module implements functions for creating stack traces in format
required when sending errors and point of call for slow database queries
etc.

"""

import sys

from .config import global_settings

_global_settings = global_settings()

def _extract_stack(f, skip=0, limit=None):
    if limit is None:
        limit = _global_settings.max_stack_trace_lines

    n = 0
    l = []

    while f is not None and skip > 0:
        f = f.f_back
        skip -= 1

    while f is not None and n < limit:
        l.append(dict(source=f.f_code.co_filename,
                line=f.f_lineno, name=f.f_code.co_name))

        f = f.f_back
        n += 1

    l.reverse()

    return l

def current_stack(skip=0, limit=None):
    try:
        raise ZeroDivisionError
    except ZeroDivisionError:
        f = sys.exc_info()[2].tb_frame.f_back

    result = ['Traceback (most recent call last):']
    result.extend(['File "{source}", line {line}, in {name}'.format(**d)
            for d in _extract_stack(f, skip, limit)])

    return result

def _extract_tb(tb, limit=None):
    if tb is None:
        return []

    if limit is None:
        limit = _global_settings.max_stack_trace_lines

    # The first traceback object is actually connected to the top most
    # frame where the exception was caught. As the limit needs to apply
    # to the bottom most, we need to traverse the complete list of
    # traceback objects and calculate as we go what would be the top
    # traceback object. Only once we have done that can we then work
    # out what frames to return.

    n = 0
    top = tb

    while tb is not None:
        if n >= limit:
            top = top.tb_next

        tb = tb.tb_next
        n += 1

    n = 0
    l = []

    # We have now the top traceback object for the limit of what we are
    # to return. The bottom most will be that where the error occurred.

    tb = top

    while tb is not None and n < limit:
        f = tb.tb_frame
        l.append(dict(source=f.f_code.co_filename,
                line=f.f_lineno, name=f.f_code.co_name))

        tb = tb.tb_next
        n += 1

    return l

def exception_stack(tb, limit=None):
    if tb is None:
        return []

    if limit is None:
        limit = _global_settings.max_stack_trace_lines

    _tb_stack = _extract_tb(tb, limit)

    result = ['Traceback (most recent call last):']
    result.extend(['File "{source}", line {line}, in {name}'.format(**d)
            for d in _tb_stack])

    return result
