# coding: utf-8
#
# Copyright 2010 Alexandre Fiori
# based on the original Tornado by Facebook
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from __future__ import absolute_import, division, with_statement

import Cookie
import socket
import time
import urlparse

from twisted.python import log
from twisted.protocols import basic
from twisted.internet import defer
from twisted.internet import interfaces

from cyclone.escape import utf8, native_str, parse_qs_bytes
from cyclone import httputil
from cyclone.util import b, bytes_type


class _BadRequestException(Exception):
    """Exception class for malformed HTTP requests."""
    pass


class HTTPConnection(basic.LineReceiver):
    """Handles a connection to an HTTP client, executing HTTP requests.

    We parse HTTP headers and bodies, and execute the request callback
    until the HTTP conection is closed.

    If ``xheaders`` is ``True``, we support the ``X-Real-Ip`` and ``X-Scheme``
    headers, which override the remote IP and HTTP scheme for all requests.
    These headers are useful when running Tornado behind a reverse proxy or
    load balancer.
    """
    delimiter = b("\r\n")

    def connectionMade(self):
        self._headersbuffer = []
        self._contentbuffer = []
        self._finish_callback = None
        self.no_keep_alive = False
        self.content_length = None
        self.request_callback = self.factory
        self.xheaders = self.factory.settings.get('xheaders', False)
        self._request = None
        self._request_finished = False

    def connectionLost(self, reason):
        if self._finish_callback:
            self._finish_callback.callback(reason.getErrorMessage())
            self._finish_callback = None

    def notifyFinish(self):
        if self._finish_callback is None:
            self._finish_callback = defer.Deferred()
        return self._finish_callback

    def lineReceived(self, line):
        if line:
            self._headersbuffer.append(line + self.delimiter)
        else:
            buff = "".join(self._headersbuffer)
            self._headersbuffer = []
            self._on_headers(buff)

    def rawDataReceived(self, data):
        if self.content_length is not None:
            data, rest = data[:self.content_length], data[self.content_length:]
            self.content_length -= len(data)
        else:
            rest = ''

        self._contentbuffer.append(data)
        if self.content_length == 0:
            buff = "".join(self._contentbuffer)
            self._contentbuffer = []
            self.content_length = None
            self._on_request_body(buff)
            self.setLineMode(rest)

    def write(self, chunk):
        assert self._request, "Request closed"
        self.transport.write(chunk)

    def finish(self):
        assert self._request, "Request closed"
        self._request_finished = True
        self._finish_request()

    def _on_write_complete(self):
        if self._request_finished:
            self._finish_request()

    def _finish_request(self):
        if self.no_keep_alive:
            disconnect = True
        else:
            connection_header = self._request.headers.get("Connection")
            if self._request.supports_http_1_1():
                disconnect = connection_header == "close"
            elif ("Content-Length" in self._request.headers
                    or self._request.method in ("HEAD", "GET")):
                disconnect = connection_header != "Keep-Alive"
            else:
                disconnect = True

        if self._finish_callback:
            self._finish_callback.callback(None)
            self._finish_callback = None
        self._request = None
        self._request_finished = False
        if disconnect is True:
            self.transport.loseConnection()

    def _on_headers(self, data):
        try:
            data = native_str(data.decode("latin1"))
            eol = data.find("\r\n")
            start_line = data[:eol]
            try:
                method, uri, version = start_line.split(" ")
            except ValueError:
                raise _BadRequestException("Malformed HTTP request line")
            if not version.startswith("HTTP/"):
                raise _BadRequestException(
                        "Malformed HTTP version in HTTP Request-Line")
            headers = httputil.HTTPHeaders.parse(data[eol:])
            self._request = HTTPRequest(
                connection=self, method=method, uri=uri, version=version,
                headers=headers, remote_ip=self.transport.getPeer().host)

            content_length = int(headers.get("Content-Length", 0))
            if content_length:
                if headers.get("Expect") == "100-continue":
                    self.transport.write("HTTP/1.1 100 (Continue)\r\n\r\n")
                self.content_length = content_length
                self.setRawMode()
                return

            self.request_callback(self._request)
        except _BadRequestException, e:
            log.msg("Malformed HTTP request from %s: %s",
                    self.transport.getPeer().host, e)
            self.transport.loseConnection()

    def _on_request_body(self, data):
        self._request.body = data
        content_type = self._request.headers.get("Content-Type", "")
        if self._request.method in ("POST", "PATCH", "PUT"):
            if content_type.startswith("application/x-www-form-urlencoded"):
                arguments = parse_qs_bytes(native_str(self._request.body))
                for name, values in arguments.iteritems():
                    values = [v for v in values if v]
                    if values:
                        self._request.arguments.setdefault(name,
                                                           []).extend(values)
            elif content_type.startswith("multipart/form-data"):
                fields = content_type.split(";")
                for field in fields:
                    k, sep, v, = field.strip().partition("=")
                    if k == "boundary" and v:
                        httputil.parse_multipart_form_data(
                            utf8(v), data,
                            self._request.arguments,
                            self._request.files)
                        break
                else:
                    log.msg("Invalid multipart/form-data")
        self.request_callback(self._request)


class HTTPRequest(object):
    """A single HTTP request.

    All attributes are type `str` unless otherwise noted.

    .. attribute:: method

       HTTP request method, e.g. "GET" or "POST"

    .. attribute:: uri

       The requested uri.

    .. attribute:: path

       The path portion of `uri`

    .. attribute:: query

       The query portion of `uri`

    .. attribute:: version

       HTTP version specified in request, e.g. "HTTP/1.1"

    .. attribute:: headers

       `HTTPHeader` dictionary-like object for request headers.  Acts like
       a case-insensitive dictionary with additional methods for repeated
       headers.

    .. attribute:: body

       Request body, if present, as a byte string.

    .. attribute:: remote_ip

       Client's IP address as a string.  If `HTTPServer.xheaders` is set,
       will pass along the real IP address provided by a load balancer
       in the ``X-Real-Ip`` header

    .. attribute:: protocol

       The protocol used, either "http" or "https".  If `HTTPServer.xheaders`
       is set, will pass along the protocol used by a load balancer if
       reported via an ``X-Scheme`` header.

    .. attribute:: host

       The requested hostname, usually taken from the ``Host`` header.

    .. attribute:: arguments

       GET/POST arguments are available in the arguments property, which
       maps arguments names to lists of values (to support multiple values
       for individual names). Names are of type `str`, while arguments
       are byte strings.  Note that this is different from
       `RequestHandler.get_argument`, which returns argument values as
       unicode strings.

    .. attribute:: files

       File uploads are available in the files property, which maps file
       names to lists of :class:`HTTPFile`.

    .. attribute:: connection

       An HTTP request is attached to a single HTTP connection, which can
       be accessed through the "connection" attribute. Since connections
       are typically kept open in HTTP/1.1, multiple requests can be handled
       sequentially on a single connection.
    """
    def __init__(self, method, uri, version="HTTP/1.0", headers=None,
                 body=None, remote_ip=None, protocol=None, host=None,
                 files=None, connection=None):
        self.method = method
        self.uri = uri
        self.version = version
        self.headers = headers or httputil.HTTPHeaders()
        self.body = body or ""
        if connection and connection.xheaders:
            # Squid uses X-Forwarded-For, others use X-Real-Ip
            self.remote_ip = self.headers.get(
                "X-Real-Ip", self.headers.get("X-Forwarded-For", remote_ip))
            if not self._valid_ip(self.remote_ip):
                self.remote_ip = remote_ip
            # AWS uses X-Forwarded-Proto
            self.protocol = self.headers.get("X-Scheme",
                            self.headers.get("X-Forwarded-Proto", protocol))
            if self.protocol not in ("http", "https"):
                self.protocol = "http"
        else:
            self.remote_ip = remote_ip
            if connection and interfaces.ISSLTransport.providedBy(
                                                        connection.transport):
                self.protocol = "https"
            else:
                self.protocol = "http"
        self.host = host or self.headers.get("Host") or "127.0.0.1"
        self.files = files or {}
        self.connection = connection
        self._start_time = time.time()
        self._finish_time = None

        scheme, netloc, path, query, fragment = \
            urlparse.urlsplit(native_str(uri))
        self.path = path
        self.query = query
        arguments = parse_qs_bytes(query)
        self.arguments = {}
        for name, values in arguments.iteritems():
            values = [v for v in values if v]
            if values:
                self.arguments[name] = values

    def supports_http_1_1(self):
        """Returns True if this request supports HTTP/1.1 semantics"""
        return self.version == "HTTP/1.1"

    @property
    def cookies(self):
        """A dictionary of Cookie.Morsel objects."""
        if not hasattr(self, "_cookies"):
            self._cookies = Cookie.SimpleCookie()
            if "Cookie" in self.headers:
                try:
                    self._cookies.load(
                        native_str(self.headers["Cookie"]))
                except Exception:
                    self._cookies = {}
        return self._cookies

    def write(self, chunk):
        """Writes the given chunk to the response stream."""
        assert isinstance(chunk, bytes_type)
        self.connection.write(chunk)

    def finish(self):
        """Finishes this HTTP request on the open connection."""
        self.connection.finish()
        self._finish_time = time.time()

    def full_url(self):
        """Reconstructs the full URL for this request."""
        return self.protocol + "://" + self.host + self.uri

    def request_time(self):
        """Returns the amount of time it took for this request to execute."""
        if self._finish_time is None:
            return time.time() - self._start_time
        else:
            return self._finish_time - self._start_time

    def notifyFinish(self):
        return self.connection.notifyFinish()

    def __repr__(self):
        attrs = ("protocol", "host", "method", "uri", "version", "remote_ip",
                 "body")
        args = ", ".join(["%s=%r" % (n, getattr(self, n)) for n in attrs])
        return "%s(%s, headers=%s)" % (
            self.__class__.__name__, args, dict(self.headers))

    def _valid_ip(self, ip):
        try:
            res = socket.getaddrinfo(ip, 0, socket.AF_UNSPEC,
                                     socket.SOCK_STREAM,
                                     0, socket.AI_NUMERICHOST)
            return bool(res)
        except socket.gaierror, e:
            if e.args[0] == socket.EAI_NONAME:
                return False
            raise
        return True
