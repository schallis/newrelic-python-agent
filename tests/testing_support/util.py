import re
import socket

def _to_int(version_str):
    m = re.match(r'\d+', version_str)
    return int(m.group(0)) if m else 0

def version2tuple(version_str):
    """Convert version, even if it contains non-numeric chars.

    >>> version2tuple('9.4rc1.1')
    (9, 4)

    """

    parts = version_str.split('.')[:2]
    return tuple(map(_to_int, parts))

def instance_hostname(hostname):
    if hostname == 'localhost':
        hostname = socket.gethostname()
    return hostname
