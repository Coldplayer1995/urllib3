# -*- coding: utf-8 -*-

from urllib3 import HTTPConnectionPool
from urllib3.exceptions import InvalidBodyError
from dummyserver.testcase import SocketDummyServerTestCase
import pytest


class TestChunkedTransfer(SocketDummyServerTestCase):
    def start_chunked_handler(self):
        self.buffer = b""

        def socket_handler(listener):
            sock = listener.accept()[0]

            while not self.buffer.endswith(b"\r\n0\r\n\r\n"):
                self.buffer += sock.recv(65536)

            sock.send(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-type: text/plain\r\n"
                b"Content-Length: 0\r\n"
                b"\r\n"
            )
            sock.close()

        self._start_server(socket_handler)

    def test_chunks(self):
        self.start_chunked_handler()
        chunks = [b"foo", b"bar", b"", b"bazzzzzzzzzzzzzzzzzzzzzz"]
        with HTTPConnectionPool(self.host, self.port, retries=False) as pool:
            pool.urlopen("GET", "/", chunks, headers=dict(DNT="1"))

            assert b"transfer-encoding" in self.buffer
            body = self.buffer.split(b"\r\n\r\n", 1)[1]
            lines = body.split(b"\r\n")
            # Empty chunks should have been skipped, as this could not be distinguished
            # from terminating the transmission
            for i, chunk in enumerate([c for c in chunks if c]):
                assert lines[i * 2] == hex(len(chunk))[2:].encode("utf-8")
                assert lines[i * 2 + 1] == chunk

    def _test_body(self, data):
        self.start_chunked_handler()
        with HTTPConnectionPool(self.host, self.port, retries=False) as pool:
            pool.urlopen("GET", "/", data)
            header, body = self.buffer.split(b"\r\n\r\n", 1)

            assert b"transfer-encoding: chunked" in header.split(b"\r\n")
            if data:
                bdata = data if isinstance(data, bytes) else data.encode("utf-8")
                assert b"\r\n" + bdata + b"\r\n" in body
                assert body.endswith(b"\r\n0\r\n\r\n")

                len_str = body.split(b"\r\n", 1)[0]
                stated_len = int(len_str, 16)
                assert stated_len == len(bdata)
            else:
                assert body == b"0\r\n\r\n"

    def test_bytestring_body(self):
        self._test_body(b"thisshouldbeonechunk\r\nasdf")

    def test_unicode_body(self):
        # Unicode bodies are not supported.
        chunk = u"thisshouldbeonechunk\r\näöüß"
        with pytest.raises(InvalidBodyError):
            self._test_body(chunk)

    def test_empty_string_body(self):
        self._test_body(b"")

    def test_empty_iterable_body(self):
        self._test_body([])

    def test_removes_duplicate_host_header(self):
        self.start_chunked_handler()
        chunks = [b"foo", b"bar", b"", b"bazzzzzzzzzzzzzzzzzzzzzz"]
        with HTTPConnectionPool(self.host, self.port, retries=False) as pool:
            pool.urlopen("GET", "/", chunks, headers={"Host": "test.org"})

            header_block = self.buffer.split(b"\r\n\r\n", 1)[0].lower()
            header_lines = header_block.split(b"\r\n")[1:]

            host_headers = [x for x in header_lines if x.startswith(b"host")]
            assert len(host_headers) == 1

    def test_provides_default_host_header(self):
        self.start_chunked_handler()
        chunks = [b"foo", b"bar", b"", b"bazzzzzzzzzzzzzzzzzzzzzz"]
        with HTTPConnectionPool(self.host, self.port, retries=False) as pool:
            pool.urlopen("GET", "/", chunks)

            header_block = self.buffer.split(b"\r\n\r\n", 1)[0].lower()
            header_lines = header_block.split(b"\r\n")[1:]

            host_headers = [x for x in header_lines if x.startswith(b"host")]
            assert len(host_headers) == 1
