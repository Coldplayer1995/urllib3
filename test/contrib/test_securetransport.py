# -*- coding: utf-8 -*-
import contextlib
import socket
import ssl

import pytest

try:
    from urllib3.contrib.securetransport import WrappedSocket
except ImportError:
    pass

pytestmark = pytest.mark.skip("SecureTransport currently not supported on v2!")


def setup_module():
    try:
        from urllib3.contrib.securetransport import inject_into_urllib3

        inject_into_urllib3()
    except ImportError as e:
        pytest.skip("Could not import SecureTransport: %r" % e)


def teardown_module():
    try:
        from urllib3.contrib.securetransport import extract_from_urllib3

        extract_from_urllib3()
    except ImportError:
        pass


# Currently TLSv1.3 doesn't work with SecureTransport despite
# Apple previously documenting support. See:
# https://github.com/python-trio/trio/issues/1165#issuecomment-526563135
from ..with_dummyserver.test_https import (  # noqa: F401
    TestHTTPS,
    TestHTTPS_TLSv1,
    TestHTTPS_TLSv1_1,
    TestHTTPS_TLSv1_2,
)
from ..with_dummyserver.test_socketlevel import (  # noqa: F401
    TestSNI,
    TestSocketClosing,
    TestClientCerts,
)


def test_no_crash_with_empty_trust_bundle():
    with contextlib.closing(socket.socket()) as s:
        ws = WrappedSocket(s)
        with pytest.raises(ssl.SSLError):
            ws._custom_validate(True, b"")
