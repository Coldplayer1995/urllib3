# -*- coding: utf-8 -*-
"""
This module implements the connection management logic.

Unlike in http.client, the connection here is an object that is responsible
for a very small number of tasks:

    1. Serializing/deserializing data to/from the network.
    2. Being able to do basic parsing of HTTP and maintaining the framing.
    3. Understanding connection state.

This object knows very little about the semantics of HTTP in terms of how to
construct HTTP requests and responses. It mostly manages the socket itself.
"""
from __future__ import absolute_import

import collections
import datetime
import itertools
import socket
import warnings

import h11

from ..base import Request, Response
from ..exceptions import (
    ConnectTimeoutError,
    NewConnectionError,
    SubjectAltNameWarning,
    SystemTimeWarning,
    BadVersionError,
    FailedTunnelError,
    InvalidBodyError,
    ProtocolError,
)
from urllib3.packages import six
from ..util import ssl_ as ssl_util
from .._backends._common import LoopAbort
from .._backends._loader import load_backend, normalize_backend

try:
    import ssl
except ImportError:
    ssl = None


def is_async_mode():
    """Tests if we're in the async part of the code or not"""

    async def f():
        """Unasync transforms async functions in sync functions"""
        return None

    obj = f()
    if obj is None:
        return False
    else:
        obj.close()  # prevent unawaited coroutine warning
        return True


_ASYNC_MODE = is_async_mode()


# When it comes time to update this value as a part of regular maintenance
# (ie test_recent_date is failing) update it to ~6 months before the current date.
RECENT_DATE = datetime.date(2019, 1, 1)

_SUPPORTED_VERSIONS = frozenset([b"1.0", b"1.1"])

# A sentinel object returned when some syscalls return EAGAIN.
_EAGAIN = object()


def _headers_to_native_string(headers):
    """
    A temporary shim to convert received headers to native strings, to match
    the behaviour of httplib. We will reconsider this later in the process.
    """
    # TODO: revisit.
    # This works because fundamentally we know that all headers coming from
    # h11 are bytes, so if they aren't of type `str` then we must be on Python
    # 3 and need to decode the headers using Latin1.
    for n, v in headers:
        if not isinstance(n, str):
            n = n.decode("latin1")
        if not isinstance(v, str):
            v = v.decode("latin1")
        yield (n, v)


def _stringify_headers(headers):
    """
    A generator that transforms headers so they're suitable for sending by h11.
    """
    # TODO: revisit
    for name, value in headers:
        if isinstance(name, six.text_type):
            name = name.encode("ascii")

        if isinstance(value, six.text_type):
            value = value.encode("latin-1")
        elif isinstance(value, int):
            value = str(value).encode("ascii")

        yield (name, value)


def _read_readable(readable):
    # TODO: reconsider this block size
    blocksize = 8192
    while True:
        datablock = readable.read(blocksize)
        if not datablock:
            break
        yield datablock


# XX this should return an async iterator
def _make_body_iterable(body):
    """
    This function turns all possible body types that urllib3 supports into an
    iterable of bytes. The goal is to expose a uniform structure to request
    bodies so that they all appear to be identical to the low-level code.

    The basic logic here is:
        - byte strings are turned into single-element lists
        - readables are wrapped in an iterable that repeatedly calls read until
          nothing is returned anymore
        - other iterables are used directly
        - anything else is not acceptable

    In particular, note that we do not support *text* data of any kind. This
    is deliberate: users must make choices about the encoding of the data they
    use.
    """
    if body is None:
        return []
    elif isinstance(body, bytes):
        return [body]
    elif hasattr(body, "read"):
        return _read_readable(body)
    elif isinstance(body, collections.Iterable) and not isinstance(body, six.text_type):
        return body
    else:
        raise InvalidBodyError("Unacceptable body type: %s" % type(body))


# XX this should return an async iterator
def _request_bytes_iterable(request, state_machine):
    """
    An iterable that serialises a set of bytes for the body.
    """

    def all_pieces_iter():
        h11_request = h11.Request(
            method=request.method,
            target=request.target,
            headers=_stringify_headers(request.headers.items()),
        )
        yield state_machine.send(h11_request)

        for chunk in _make_body_iterable(request.body):
            yield state_machine.send(h11.Data(data=chunk))

        yield state_machine.send(h11.EndOfMessage())

    # Try to combine the header bytes + (first set of body bytes or end of
    # message bytes) into one packet.
    # As long as all_pieces_iter() yields at least two messages, this should
    # never raise StopIteration.
    remaining_pieces = all_pieces_iter()
    first_packet_bytes = next(remaining_pieces) + next(remaining_pieces)
    all_pieces_combined_iter = itertools.chain([first_packet_bytes], remaining_pieces)

    # We filter out any empty strings, because we don't want to call
    # send(b""). You might think this is a no-op, so it shouldn't matter
    # either way. But this isn't true. For example, if we're sending a request
    # with Content-Length framing, we could have this sequence:
    #
    # - We send the last Data event.
    # - The peer immediately sends its response and closes the socket.
    # - We attempt to send the EndOfMessage event, which (b/c this request has
    #   Content-Length framing) is encoded as b"".
    # - We call send(b"").
    # - This triggers the kernel / SSL layer to discover that the socket is
    #   closed, so it raises an exception.
    #
    # It's easier to fix this once here instead of worrying about it in all
    # the different backends.
    for piece in all_pieces_combined_iter:
        if piece:
            yield piece


def _response_from_h11(h11_response, body_object):
    """
    Given a h11 Response object, build a urllib3 response object and return it.
    """
    if bytes(h11_response.http_version) not in _SUPPORTED_VERSIONS:
        raise BadVersionError(h11_response.http_version)

    version = b"HTTP/" + h11_response.http_version
    our_response = Response(
        status_code=h11_response.status_code,
        headers=_headers_to_native_string(h11_response.headers),
        body=body_object,
        version=version,
    )
    return our_response


def _build_tunnel_request(host, port, headers):
    """
    Builds a urllib3 Request object that is set up correctly to request a proxy
    to establish a TCP tunnel to the remote host.
    """

    try:
        socket.inet_pton(socket.AF_INET6, host)
    except (socket.error, ValueError, OSError):
        # Not a raw IPv6 address
        target = "%s:%d" % (host, port)
    else:
        # raw IPv6 address
        target = "[%s]:%d" % (host, port)

    if not isinstance(target, bytes):
        target = target.encode("latin1")

    tunnel_request = Request(method=b"CONNECT", target=target, headers=headers)
    tunnel_request.add_host(host=host, port=port, scheme="http")
    return tunnel_request


async def _start_http_request(request, state_machine, sock, read_timeout=None):
    """
    Send the request using the given state machine and connection, wait
    for the response headers, and return them.

    If we get response headers early, then we stop sending and return
    immediately, poisoning the state machine along the way so that we know
    it can't be re-used.

    This is a standalone function because we use it both to set up both
    CONNECT requests and real requests.
    """
    # Before we begin, confirm that the state machine is ok.
    if (
        state_machine.our_state is not h11.IDLE
        or state_machine.their_state is not h11.IDLE
    ):
        raise ProtocolError("Invalid internal state transition")

    request_bytes_iterable = _request_bytes_iterable(request, state_machine)

    # Hack around Python 2 lack of nonlocal
    context = {"send_aborted": True, "h11_response": None}

    async def produce_bytes():
        try:
            return next(request_bytes_iterable)
        except StopIteration:
            # We successfully sent the whole body!
            context["send_aborted"] = False
            return None

    def consume_bytes(data):
        state_machine.receive_data(data)
        while True:
            event = state_machine.next_event()
            if event is h11.NEED_DATA:
                break
            elif isinstance(event, h11.InformationalResponse):
                # Ignore 1xx responses
                continue
            elif isinstance(event, h11.Response):
                # We have our response! Save it and get out of here.
                context["h11_response"] = event
                raise LoopAbort
            else:
                # Can't happen
                raise RuntimeError("Unexpected h11 event {}".format(event))

    await sock.send_and_receive_for_a_while(produce_bytes, consume_bytes, read_timeout)
    assert context["h11_response"] is not None

    if context["send_aborted"]:
        # Our state machine thinks we sent a bunch of data... but maybe we
        # didn't! Maybe our send got cancelled while we were only half-way
        # through sending the last chunk, and then h11 thinks we sent a
        # complete request and we actually didn't. Then h11 might think we can
        # re-use this connection, even though we can't. So record this in
        # h11's state machine.
        state_machine.send_failed()

    return context["h11_response"]


async def _read_until_event(state_machine, sock, read_timeout):
    """
    A loop that keeps issuing reads and feeding the data into h11 and
    checking whether h11 has an event for us. The moment there is an event
    other than h11.NEED_DATA, this function returns that event.
    """
    while True:
        event = state_machine.next_event()
        if event is not h11.NEED_DATA:
            return event
        state_machine.receive_data(await sock.receive_some(read_timeout))


_DEFAULT_SOCKET_OPTIONS = object()


class HTTP1Connection(object):
    """
    A wrapper around a single HTTP/1.1 connection.

    This wrapper manages connection state, ensuring that connections are
    appropriately managed throughout the lifetime of a HTTP transaction. In
    particular, this object understands the conditions in which connections
    should be torn down, and also manages sending data and handling early
    responses.

    This object can be iterated over to return the response body. When iterated
    over it will return all of the data that is currently buffered, and if no
    data is buffered it will issue one read syscall and return all of that
    data. Buffering of response data must happen at a higher layer.
    """

    #: Disable Nagle's algorithm by default.
    #: ``[(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)]``
    default_socket_options = [(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)]

    def __init__(
        self,
        host,
        port,
        backend=None,
        socket_options=_DEFAULT_SOCKET_OPTIONS,
        source_address=None,
        tunnel_host=None,
        tunnel_port=None,
        tunnel_headers=None,
    ):
        self.is_verified = False
        self.read_timeout = None
        self._backend = load_backend(normalize_backend(backend, _ASYNC_MODE))
        self._host = host
        self._port = port
        self._socket_options = (
            socket_options
            if socket_options is not _DEFAULT_SOCKET_OPTIONS
            else self.default_socket_options
        )
        self._source_address = source_address
        self._tunnel_host = tunnel_host
        self._tunnel_port = tunnel_port
        self._tunnel_headers = tunnel_headers
        self._sock = None
        self._state_machine = None

    async def _wrap_socket(self, sock, ssl_context, fingerprint, assert_hostname):
        """
        Handles extra logic to wrap the socket in TLS magic.
        """
        is_time_off = datetime.date.today() < RECENT_DATE
        if is_time_off:
            warnings.warn(
                (
                    "System time is way off (before {0}). This will probably "
                    "lead to SSL verification errors"
                ).format(RECENT_DATE),
                SystemTimeWarning,
            )

        # XX need to know whether this is the proxy or the final host that
        # we just did a handshake with!
        # Google App Engine's httplib does not define _tunnel_host
        check_host = (
            assert_hostname or getattr(self, "_tunnel_host", None) or self._host
        )

        # Stripping trailing dots from the hostname is important because
        # they indicate that this host is an absolute name (for DNS
        # lookup), but are irrelevant to SSL hostname matching and in fact
        # will break it.
        check_host = check_host.rstrip(".")

        sock = await sock.start_tls(check_host, ssl_context)

        if fingerprint:
            ssl_util.assert_fingerprint(sock.getpeercert(binary_form=True), fingerprint)

        elif ssl_context.verify_mode != ssl.CERT_NONE and assert_hostname is not False:
            cert = sock.getpeercert()
            if not cert.get("subjectAltName", ()):
                warnings.warn(
                    (
                        "Certificate for {0} has no `subjectAltName`, falling "
                        "back to check for a `commonName` for now. This "
                        "feature is being removed by major browsers and "
                        "deprecated by RFC 2818. (See "
                        "https://github.com/shazow/urllib3/issues/497 for "
                        "details.)".format(self._host)
                    ),
                    SubjectAltNameWarning,
                )
            ssl_util.match_hostname(cert, check_host)

        self.is_verified = ssl_context.verify_mode == ssl.CERT_REQUIRED and (
            assert_hostname is not False or fingerprint
        )

        return sock

    async def send_request(self, request, read_timeout):
        """
        Given a Request object, performs the logic required to get a response.
        """
        h11_response = await _start_http_request(
            request, self._state_machine, self._sock, read_timeout
        )
        return _response_from_h11(h11_response, self)

    async def _tunnel(self, sock):
        """
        This method establishes a CONNECT tunnel shortly after connection.
        """
        # Basic sanity check that _tunnel is only called at appropriate times.
        assert self._state_machine.our_state is h11.IDLE

        tunnel_request = _build_tunnel_request(
            self._tunnel_host, self._tunnel_port, self._tunnel_headers
        )

        tunnel_state_machine = h11.Connection(our_role=h11.CLIENT)

        h11_response = await _start_http_request(
            tunnel_request, tunnel_state_machine, sock
        )
        # XX this is wrong -- 'self' here will try to iterate using
        # self._state_machine, not tunnel_state_machine. Also, we need to
        # think about how this failure case interacts with the pool's
        # connection lifecycle management.
        tunnel_response = _response_from_h11(h11_response, self)

        if h11_response.status_code != 200:
            sock.forceful_close()
            raise FailedTunnelError(
                "Unable to establish CONNECT tunnel", tunnel_response
            )

    async def connect(
        self,
        ssl_context=None,
        fingerprint=None,
        assert_hostname=None,
        connect_timeout=None,
    ):
        """
        Connect this socket to the server, applying the source address, any
        relevant socket options, and the relevant connection timeout.
        """
        if self._sock is not None:
            # We're already connected, move on.
            self._sock.set_readable_watch_state(False)
            return

        extra_kw = {}
        if self._source_address:
            extra_kw["source_address"] = self._source_address

        if self._socket_options:
            extra_kw["socket_options"] = self._socket_options

        # This was factored out into a separate function to allow overriding
        # by subclasses, but in the backend approach the way to to this is to
        # provide a custom backend. (Composition >> inheritance.)
        try:
            self._sock = await self._backend.connect(
                self._host, self._port, connect_timeout, **extra_kw
            )
            self._state_machine = h11.Connection(our_role=h11.CLIENT)

        # XX these two error handling blocks needs to be re-done in a
        # backend-agnostic way
        except socket.timeout:
            raise ConnectTimeoutError(
                self,
                "Connection to %s timed out. (connect timeout=%s)"
                % (self._host, connect_timeout),
            )

        except socket.error as e:
            raise NewConnectionError(
                self, "Failed to establish a new connection: %s" % e
            )

        if ssl_context is not None:
            # Google App Engine's httplib does not define _tunnel_host
            if getattr(self, "_tunnel_host", None) is not None:
                self._tunnel(self._sock)

            self._sock = await self._wrap_socket(
                self._sock, ssl_context, fingerprint, assert_hostname
            )

    def close(self):
        """
        Close this connection.
        """
        if self._sock is not None:
            # Make sure self._sock is None even if closing raises an exception
            # Also keep self._state_machine in sync with self._sock: it should only be
            # defined when self._sock is defined
            self._state_machine = None
            sock, self._sock = self._sock, None
            sock.forceful_close()

    def _reset(self):
        """
        Called once we hit EndOfMessage, and checks whether we can re-use this
        state machine and connection or not, and if not, closes the socket and
        state machine.
        """
        try:
            self._state_machine.start_next_cycle()
        except h11.LocalProtocolError:
            # Not re-usable
            self.close()
        else:
            # This connection can be returned to the connection pool, and
            # eventually we'll take it out again and want to know if it's been
            # dropped.
            self._sock.set_readable_watch_state(True)

    @property
    def complete(self):
        if not self._state_machine:
            return True

        our_state = self._state_machine.our_state
        their_state = self._state_machine.their_state
        return our_state is h11.IDLE and their_state is h11.IDLE

    def __aiter__(self):
        return self

    def next(self):  # Platform-specific: Python 2.7
        return self.__next__()

    async def __anext__(self):
        """
        Iterate over the body bytes of the response until end of message.
        """
        event = await _read_until_event(
            self._state_machine, self._sock, self.read_timeout
        )
        if isinstance(event, h11.Data):
            return bytes(event.data)
        elif isinstance(event, h11.EndOfMessage):
            self._reset()
            raise StopAsyncIteration
        else:
            # can't happen
            raise RuntimeError("Unexpected h11 event {}".format(event))
