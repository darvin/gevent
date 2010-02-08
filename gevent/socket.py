# Copyright (c) 2005-2006, Bob Ippolito
# Copyright (c) 2007, Linden Research, Inc.
# Copyright (c) 2009-2010 Denis Bilenko
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

"""Cooperative socket module.

This module provides socket operations and some related functions.
The API of the functions and classes matches the API of the corresponding
items in standard :mod:`socket` module exactly, but the synchronous functions
in this module only block the current greenlet and let the others run.

For convenience, exceptions (like :class:`error <socket.error>` and :class:`timeout <socket.timeout>`)
as well as the constants from :mod:`socket` module are imported into this module.
"""


__all__ = ['create_connection',
           'error',
           'fromfd',
           'gaierror',
           'getaddrinfo',
           'gethostbyname',
           'inet_aton',
           'inet_ntoa',
           'inet_pton',
           'inet_ntop',
           'socket',
           'socketpair',
           'timeout',
           'ssl',
           'sslerror',
           'SocketType']

import sys
import errno
import time
import random
import re
import platform

is_windows = platform.system() == 'Windows'

if is_windows:
    # no such thing as WSAEPERM or error code 10001 according to winsock.h or MSDN
    from errno import WSAEINVAL as EINVAL
    from errno import WSAEWOULDBLOCK as EWOULDBLOCK
    from errno import WSAEINPROGRESS as EINPROGRESS
    from errno import WSAEALREADY as EALREADY
    from errno import WSAEISCONN as EISCONN
    from gevent.win32util import formatError as strerror
    EGAIN = EWOULDBLOCK
else:
    from errno import EINVAL
    from errno import EWOULDBLOCK
    from errno import EINPROGRESS
    from errno import EALREADY
    from errno import EAGAIN
    from errno import EISCONN
    from os import strerror


import _socket
error = _socket.error
timeout = _socket.timeout
_realsocket = _socket.socket
__socket__ = __import__('socket')
_fileobject = __socket__._fileobject
gaierror = _socket.gaierror

# Import public constants from the standard socket (called __socket__ here) into this module.

for name in __socket__.__all__:
    if name[:1].isupper():
        value = getattr(__socket__, name)
        if isinstance(value, (int, basestring)):
            globals()[name] = value
            __all__.append(name)

del name, value

inet_ntoa = _socket.inet_ntoa
inet_aton = _socket.inet_aton
try:
    inet_ntop = _socket.inet_ntop
except AttributeError:
    def inet_ntop(address_family, packed_ip):
        if address_family == AF_INET:
            return inet_ntoa(packed_ip)
        # XXX: ipv6 won't work on windows
        raise NotImplementedError('inet_ntop() is not available on this platform')
try:
    inet_pton = _socket.inet_pton
except AttributeError:
    def inet_pton(address_family, ip_string):
        if address_family == AF_INET:
            return inet_aton(ip_string)
        # XXX: ipv6 won't work on windows
        raise NotImplementedError('inet_ntop() is not available on this platform')

# XXX: import other non-blocking stuff, like ntohl
# XXX: implement blocking functions that are not yet implemented
# XXX: add test that checks that socket.__all__ matches gevent.socket.__all__ on all supported platforms

from gevent.hub import getcurrent, get_hub, spawn_raw
from gevent import core

_ip4_re = re.compile('^[\d\.]+$')


def _wait_helper(ev, evtype):
    current, timeout_exc = ev.arg
    if evtype & core.EV_TIMEOUT:
        current.throw(timeout_exc)
    else:
        current.switch(ev)


def wait_read(fileno, timeout=-1, timeout_exc=_socket.timeout('timed out')):
    evt = core.read_event(fileno, _wait_helper, timeout, (getcurrent(), timeout_exc))
    try:
        switch_result = get_hub().switch()
        assert evt is switch_result, 'Invalid switch into wait_read(): %r' % (switch_result, )
    finally:
        evt.cancel()


def wait_write(fileno, timeout=-1, timeout_exc=_socket.timeout('timed out')):
    evt = core.write_event(fileno, _wait_helper, timeout, (getcurrent(), timeout_exc))
    try:
        switch_result = get_hub().switch()
        assert evt is switch_result, 'Invalid switch into wait_write(): %r' % (switch_result, )
    finally:
        evt.cancel()


def wait_readwrite(fileno, timeout=-1, timeout_exc=_socket.timeout('timed out')):
    evt = core.readwrite_event(fileno, _wait_helper, timeout, (getcurrent(), timeout_exc))
    try:
        switch_result = get_hub().switch()
        assert evt is switch_result, 'Invalid switch into wait_readwrite(): %r' % (switch_result, )
    finally:
        evt.cancel()


if sys.version_info[:2] <= (2, 4):
    # implement close argument to _fileobject that we require

    realfileobject = _fileobject

    class _fileobject(realfileobject):

        __slots__ = realfileobject.__slots__ + ['_close']

        def __init__(self, *args, **kwargs):
            self._close = kwargs.pop('close', False)
            realfileobject.__init__(self, *args, **kwargs)

        def close(self):
            try:
                if self._sock:
                    self.flush()
            finally:
                if self._close:
                    self._sock.close()
                self._sock = None


class _closedsocket(object):
    __slots__ = []
    def _dummy(*args):
        raise error(errno.EBADF, 'Bad file descriptor')
    # All _delegate_methods must also be initialized here.
    send = recv = recv_into = sendto = recvfrom = recvfrom_into = _dummy
    __getattr__ = _dummy


_delegate_methods = ("recv", "recvfrom", "recv_into", "recvfrom_into", "send", "sendto", 'sendall')

timeout_default = object()

class socket(object):

    def __init__(self, family=AF_INET, type=SOCK_STREAM, proto=0, _sock=None):
        if _sock is None:
            self._sock = _realsocket(family, type, proto)
            self.timeout = _socket.getdefaulttimeout()
        else:
            if hasattr(_sock, '_sock'):
                self._sock = _sock._sock
                self.timeout = getattr(_sock, 'timeout', False)
                if self.timeout is False:
                    self.timeout = _socket.getdefaulttimeout()
            else:
                self._sock = _sock
                self.timeout = _socket.getdefaulttimeout()
        self._sock.setblocking(0)

    def __repr__(self):
        return '<%s at %s %s>' % (type(self).__name__, hex(id(self)), self._formatinfo())

    def __str__(self):
        return '<%s %s>' % (type(self).__name__, self._formatinfo())

    def _formatinfo(self):
        try:
            fileno = self.fileno()
        except Exception, ex:
            fileno = str(ex)
        try:
            sockname = self.getsockname()
            sockname = '%s:%s' % sockname
        except Exception:
            sockname = None
        try:
            peername = self.getpeername()
            peername = '%s:%s' % peername
        except Exception:
            peername = None
        result = 'fileno=%s' % fileno
        if sockname is not None:
            result += ' sock=' + str(sockname)
        if peername is not None:
            result += ' peer=' + str(peername)
        if self.timeout is not None:
            result += ' timeout=' + str(self.timeout)
        return result

    @property
    def fd(self):
        import warnings
        warnings.warn("socket.fd is deprecated; use socket._sock", DeprecationWarning, stacklevel=2)
        return self._sock

    def accept(self):
        while True:
            try:
                client, addr = self._sock.accept()
                break
            except error, ex:
                if ex[0] != errno.EWOULDBLOCK or self.timeout == 0.0:
                    raise
            wait_read(self._sock.fileno(), timeout=self.timeout)
        return socket(_sock=client), addr

    def close(self):
        self._sock = _closedsocket()
        dummy = self._sock._dummy
        for method in _delegate_methods:
            setattr(self, method, dummy)

    def connect(self, address):
        if isinstance(address, tuple) and len(address)==2:
            address = gethostbyname(address[0]), address[1]
        if self.timeout == 0.0:
            return self._sock.connect(address)
        sock = self._sock
        if self.timeout is None:
            while True:
                err = sock.getsockopt(SOL_SOCKET, SO_ERROR)
                if err:
                    raise error(err, strerror(err))
                result = sock.connect_ex(address)
                if not result or result == EISCONN:
                    break
                elif (result in (EWOULDBLOCK, EINPROGRESS, EALREADY)) or (result == EINVAL and is_windows):
                    wait_readwrite(sock.fileno())
                else:
                    raise error(result, strerror(result))
        else:
            end = time.time() + self.timeout
            while True:
                err = sock.getsockopt(SOL_SOCKET, SO_ERROR)
                if err:
                    raise error(err, strerror(err))
                result = sock.connect_ex(address)
                if not result or result == EISCONN:
                    break
                elif (result in (EWOULDBLOCK, EINPROGRESS, EALREADY)) or (result == EINVAL and is_windows):
                    timeleft = end - time.time()
                    if timeleft <= 0:
                        raise timeout('timed out')
                    wait_readwrite(sock.fileno(), timeout=timeleft)
                else:
                    raise error(result, strerror(result))

    def connect_ex(self, address):
        try:
            return self.connect(address) or 0
        except timeout:
            return EAGAIN
        except error, ex:
            if type(ex) is error:
                return ex[0]
            else:
                raise # gaierror is not silented by connect_ex

    def dup(self):
        """dup() -> socket object

        Return a new socket object connected to the same system resource."""
        return socket(_sock=self._sock)

    def makefile(self, mode='r', bufsize=-1):
        return _fileobject(self.dup(), mode, bufsize)

    def recv(self, *args):
        while True:
            try:
                return self._sock.recv(*args)
            except error, ex:
                if ex[0] != EWOULDBLOCK or self.timeout == 0.0:
                    raise
                # QQQ without clearing exc_info test__refcount.test_clean_exit fails
                sys.exc_clear()
            wait_read(self.fileno(), timeout=self.timeout)

    def recvfrom(self, *args):
        while True:
            try:
                return self._sock.recvfrom(*args)
            except error, ex:
                sys.exc_clear()
                if ex[0] != EWOULDBLOCK or self.timeout == 0.0:
                    raise ex
            wait_read(self._sock.fileno(), timeout=self.timeout)

    def recvfrom_into(self, *args):
        while True:
            try:
                return self._sock.recvfrom_into(*args)
            except error, ex:
                if ex[0] != EWOULDBLOCK or self.timeout == 0.0:
                    raise
                sys.exc_clear()
            wait_read(self._sock.fileno(), timeout=self.timeout)

    def recv_into(self, *args):
        while True:
            try:
                return self._sock.recv_into(*args)
            except error, ex:
                if ex[0] != EWOULDBLOCK or self.timeout == 0.0:
                    raise
                sys.exc_clear()
            wait_read(self._sock.fileno(), timeout=self.timeout)

    def send(self, data, flags=0, timeout=timeout_default):
        if timeout is timeout_default:
            timeout = self.timeout
        try:
            return self._sock.send(data, flags)
        except error, ex:
            if ex[0] != EWOULDBLOCK or timeout == 0.0:
                raise
            sys.exc_clear()
            wait_write(self._sock.fileno(), timeout=timeout)
            try:
                return self._sock.send(data, flags)
            except error, ex2:
                if ex2[0] == EWOULDBLOCK:
                    return 0
                raise

    def sendall(self, data, flags=0):
        # this sendall is also reused by SSL subclasses (both from ssl and sslold modules),
        # so it should not call self._sock methods directly
        if self.timeout is None:
            data_sent = 0
            while data_sent < len(data):
                data_sent += self.send(data[data_sent:], flags)
        else:
            timeleft = self.timeout
            end = time.time() + timeleft
            data_sent = 0
            while True:
                data_sent += self.send(data[data_sent:], flags, timeout=timeleft)
                if data_sent >= len(data):
                    break
                timeleft = end - time.time()
                if timeleft <= 0:
                    raise timeout('timed out')

    def sendto(self, *args):
        try:
            return self._sock.sendto(*args)
        except error, ex:
            if ex[0] != EWOULDBLOCK or timeout == 0.0:
                raise
            sys.exc_clear()
            wait_write(self.fileno(), timeout=self.timeout)
            try:
                return self._sock.sendto(*args)
            except error, ex2:
                if ex2[0] == EWOULDBLOCK:
                    return 0
                raise

    def setblocking(self, flag):
        if flag:
            self.timeout = None
        else:
            self.timeout = 0.0

    def settimeout(self, howlong):
        if howlong is not None:
            try:
                f = howlong.__float__
            except AttributeError:
                raise TypeError('a float is required')
            howlong = f()
            if howlong < 0.0:
                raise ValueError('Timeout value out of range')
        self.timeout = howlong

    def gettimeout(self):
        return self.timeout

    family = property(lambda self: self._sock.family, doc="the socket family")
    type = property(lambda self: self._sock.type, doc="the socket type")
    proto = property(lambda self: self._sock.proto, doc="the socket protocol")

    # delegate the functions that we haven't implemented to the real socket object

    _s = ("def %s(self, *args): return self._sock.%s(*args)\n\n"
          "%s.__doc__ = _realsocket.%s.__doc__\n")
    for _m in set(__socket__._socketmethods) - set(locals()):
        exec _s % (_m, _m, _m, _m)
    del _m, _s

SocketType = socket


def socketpair(*args):
    one, two = _socket.socketpair(*args)
    return socket(_sock=one), socket(_sock=two)


def fromfd(*args):
    return socket(_sock=_socket.fromfd(*args))


def bind_and_listen(descriptor, addr=('', 0), backlog=50, reuse_addr=True):
    if reuse_addr:
        try:
            descriptor.setsockopt(SOL_SOCKET, SO_REUSEADDR, descriptor.getsockopt(SOL_SOCKET, SO_REUSEADDR) | 1)
        except error:
            pass
    descriptor.bind(addr)
    descriptor.listen(backlog)


def socket_bind_and_listen(*args, **kwargs):
    import warnings
    warnings.warn("gevent.socket.socket_bind_and_listen is renamed to bind_and_listen", DeprecationWarning, stacklevel=2)
    bind_and_listen(*args, **kwargs)
    return args[0]


def set_reuse_addr(descriptor):
    import warnings
    warnings.warn("gevent.socket.set_reuse_addr is deprecated", DeprecationWarning, stacklevel=2)
    try:
        descriptor.setsockopt(SOL_SOCKET, SO_REUSEADDR, descriptor.getsockopt(SOL_SOCKET, SO_REUSEADDR) | 1)
    except error:
        pass


def tcp_listener(address, backlog=50, reuse_addr=True):
    """A shortcut to create a TCP socket, bind it and put it into listening state."""
    sock = socket()
    bind_and_listen(sock, address, backlog=backlog, reuse_addr=reuse_addr)
    return sock


def connect_tcp(address, localaddr=None):
    """
    Create a TCP connection to address (host, port) and return the socket.
    Optionally, bind to localaddr (host, port) first.
    """
    import warnings
    warnings.warn("gevent.socket.connect_tcp is deprecated", DeprecationWarning, stacklevel=2)
    desc = socket()
    if localaddr is not None:
        desc.bind(localaddr)
    desc.connect(address)
    return desc


def tcp_server(listensocket, server, *args, **kw):
    """
    Given a socket, accept connections forever, spawning greenlets
    and executing *server* for each new incoming connection.
    When *listensocket* is closed, the ``tcp_server()`` greenlet will end.

    listensocket
        The socket from which to accept connections.
    server
        The callable to call when a new connection is made.
    \*args
        The positional arguments to pass to *server*.
    \*\*kw
        The keyword arguments to pass to *server*.
    """
    import warnings
    warnings.warn("gevent.socket.tcp_server is deprecated", DeprecationWarning, stacklevel=2)
    try:
        try:
            while True:
                client_socket = listensocket.accept()
                spawn_raw(server, client_socket, *args, **kw)
        except error, e:
            # Broken pipe means it was shutdown
            if e[0] != 32:
                raise
    finally:
        listensocket.close()

_GLOBAL_DEFAULT_TIMEOUT = object()

def create_connection(address, timeout=_GLOBAL_DEFAULT_TIMEOUT):
    """Connect to *address* and return the socket object.

    Convenience function.  Connect to *address* (a 2-tuple ``(host,
    port)``) and return the socket object.  Passing the optional
    *timeout* parameter will set the timeout on the socket instance
    before attempting to connect.  If no *timeout* is supplied, the
    global default timeout setting returned by :func:`getdefaulttimeout`
    is used.
    """

    msg = "getaddrinfo returns an empty list"
    host, port = address
    for res in getaddrinfo(host, port, 0, SOCK_STREAM):
        af, socktype, proto, _canonname, sa = res
        sock = None
        try:
            sock = socket(af, socktype, proto)
            if timeout is not _GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            sock.connect(sa)
            return sock
        except error, msg:
            if sock is not None:
                sock.close()
    raise error, msg


try:
    from gevent.dns import resolve_ipv4, resolve_ipv6
except Exception:
    import traceback
    traceback.print_exc()
    __all__.remove('gethostbyname')
    __all__.remove('getaddrinfo')
else:

    def gethostbyname(hostname):
        """:func:`socket.gethostbyname` implemented using :mod:`gevent.dns`.

        Differs in the following ways:

        * raises :class:`DNSError` (a subclass of :class:`socket.gaierror`) with dns error
          codes instead of standard socket error codes
        * does not support ``/etc/hosts`` but calls the original :func:`socket.gethostbyname`
          if *hostname* has no dots
        * does not iterate through all addresses, instead picks a random one each time
        """
        # TODO: this is supposed to iterate through all the addresses
        # could use a global dict(hostname, iter)
        # - fix these nasty hacks for localhost, ips, etc.
        if not isinstance(hostname, str) or '.' not in hostname:
            return _socket.gethostbyname(hostname)
        if _ip4_re.match(hostname):
            return hostname
        if hostname == _socket.gethostname():
            return _socket.gethostbyname(hostname)
        _ttl, addrs = resolve_ipv4(hostname)
        return inet_ntoa(random.choice(addrs))


    def getaddrinfo(host, port, *args, **kwargs):
        """*Some* approximation of :func:`socket.getaddrinfo` implemented using :mod:`gevent.dns`.

        If *host* is not a string, does not has any dots or is a numeric IP address, then
        the standard :func:`socket.getaddrinfo` is called.

        Otherwise, calls either :func:`resolve_ipv4` or :func:`resolve_ipv6` and
        formats the result the way :func:`socket.getaddrinfo` does it.

        Differs in the following ways:

        * raises :class:`DNSError` (a subclass of :class:`gaierror`) with libevent-dns error
          codes instead of standard socket error codes
        * IPv6 support is untested.
        * AF_UNSPEC only tries IPv4
        * only supports TCP, UDP, IP protocols
        * port must be numeric, does not support string service names. see socket.getservbyname
        * *flags* argument is ignored

        Additionally, supports *evdns_flags* keyword arguments (default ``0``) that is passed
        to :mod:`dns` functions.
        """
        family, socktype, proto, _flags = args + (None, ) * (4 - len(args))
        if not isinstance(host, str) or '.' not in host or _ip4_re.match(host):
            return _socket.getaddrinfo(host, port, *args)

        evdns_flags = kwargs.pop('evdns_flags', 0)
        if kwargs:
            raise TypeError('Unsupported keyword arguments: %s' % (kwargs.keys(), ))

        if family in (None, AF_INET, AF_UNSPEC):
            family = AF_INET
            # TODO: AF_UNSPEC means try both AF_INET and AF_INET6
            _ttl, addrs = resolve_ipv4(host, evdns_flags)
        elif family == AF_INET6:
            _ttl, addrs = resolve_ipv6(host, evdns_flags)
        else:
            raise NotImplementedError('family is not among AF_UNSPEC/AF_INET/AF_INET6: %r' % (family, ))

        socktype_proto = [(SOCK_STREAM, 6), (SOCK_DGRAM, 17), (SOCK_RAW, 0)]
        if socktype is not None:
            socktype_proto = [(x, y) for (x, y) in socktype_proto if socktype == x]
        if proto is not None:
            socktype_proto = [(x, y) for (x, y) in socktype_proto if proto == y]

        result = []
        for addr in addrs:
            for socktype, proto in socktype_proto:
                result.append((family, socktype, proto, '', (inet_ntop(family, addr), port)))
        return result


_have_ssl = False

try:
    from gevent.ssl import sslwrap_simple as ssl, SSLError as sslerror
    _have_ssl = True
except ImportError:
    try:
        from gevent.sslold import ssl, sslerror
        _have_ssl = True
    except ImportError:
        pass

if not _have_ssl:
    __all__.remove('ssl')
    __all__.remove('sslerror')

