#!/usr/bin/env python

# Lantern App Engine Proxy
# ------------------------

# Derivative work of Mirrorrr <http://code.google.com/p/mirrorrr>

# Copyright 2011 the Lantern developers <http://www.getlantern.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 3
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the
# Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor,
# Boston, MA  02110-1301
# USA
#
# Mirrorrr
# --------
#
# Copyright 2008 Brett Slatkin
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#     http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from datetime import datetime
from os import environ
from traceback import format_exc
from urllib import unquote

from google.appengine.api import urlfetch
from google.appengine.ext import webapp
from google.appengine.runtime import DeadlineExceededError
from google.appengine.runtime.apiproxy_errors import OverQuotaError

import logging

fetch = urlfetch.fetch
DownloadError = urlfetch.DownloadError
InvalidURLError = urlfetch.InvalidURLError

now = datetime.utcnow
logger = logging.getLogger('laeproxy')
logger.setLevel(logging.DEBUG)

PROD = environ.get('SERVER_SOFTWARE', '').startswith('Google App Engine')
DEV = not PROD

METHODS = frozenset(('delete', 'get', 'head', 'put', 'post'))
RANGE_METHODS = frozenset(('get',))
PAYLOAD_METHODS = frozenset(('put', 'post'))

# http://code.google.com/appengine/docs/python/urlfetch/overview.html#Quotas_and_Limits
URLFETCH_REQ_MAXBYTES = 1024 * 1024 * 5 # 5MB
URLFETCH_RES_MAXBYTES = 1024 * 1024 * 32
# http://code.google.com/appengine/docs/python/urlfetch/fetchfunction.html
URLFETCH_REQ_MAXSECS = 60
# http://code.google.com/appengine/docs/python/runtime.html#Quotas_and_Limits
GAE_REQ_MAXBYTES = 1024 * 1024 * 32
GAE_RES_MAXBYTES = 1024 * 1024 * 32
GAE_REQ_MAXSECS = 60

RANGE_REQ_SIZE = 2000000 # bytes. matches Lantern's CHUNK_SIZE.

# stamp our responses with this header
H_LAEPROXY = 'X-laeproxy'
H_TRUNCATED = 'X-laeproxy-truncated'
H_UPSTREAM_SERVER = 'X-laeproxy-upstream-server'
H_UPSTREAM_STATUS_CODE = 'X-laeproxy-upstream-status-code'
H_UPSTREAM_CONTENT_RANGE = 'X-laeproxy-upstream-content-range'
# various values corresponding to possible results of proxy requests
RETRIEVED_FROM_NET = 'Retrieved from network %s'
IGNORED_RECURSIVE = 'Ignored recursive request'
REQ_TOO_LARGE = 'Request size exceeds urlfetch limit'
MISSED_DEADLINE_URLFETCH = 'Missed urlfetch deadline'
MISSED_DEADLINE_GAE = ' Missed GAE deadline'
EXCEEDED_URLFETCH_QUOTA = 'Exceeded urlfetch quota'
UNEXPECTED_ERROR = 'Unexpected error: %r'

# remove hop-by-hop headers
# http://www.w3.org/Protocols/rfc2616/rfc2616-sec13.html#sec13.5.1
HOPBYHOP = frozenset((
    'connection',
    'keep-alive',
    'proxy-authenticate',
    'proxy-authorization',
    'te',
    'trailer',
    'transfer-encoding',
    'upgrade',
    ))

def copy_headers(from_, to, ignore=set()):
    ignored = []
    for k, v in from_.iteritems():
        if k.lower() not in ignore:
            to[k] = v
        else:
            ignored.append((k, v))
    return ignored

class LaeproxyHandler(webapp.RequestHandler):

    def _extract_url(self, req):
        # reconstruct original url
        path = req.path_qs.lstrip('/')
        try:
            scheme, rest = path.split('/', 1)
            parts = rest.split('/', 1)
        except ValueError:
            logger.debug('Invalid url: %s' % path)
            self.response.headers[H_LAEPROXY] = 'Invalid url'
            self.abort(404)
        try:
            rest = parts[1]
        except IndexError:
            rest = ''
        host = unquote(parts[0])
        if not host:
            logger.debug('No host specified: %s' % path)
            self.response.headers[H_LAEPROXY] = 'Missing host'
            self.abort(404)
        if req.host.lower() == host.lower():
            logger.info('Ignoring recursive request: %s' % req.url)
            self.response.headers[H_LAEPROXY] = IGNORED_RECURSIVE
            self.abort(404)
        url = scheme + '://' + host + '/' + rest
        logger.debug('Target url: %s' % url)
        return url

    def _send_response(self, fheaders, resheaders, ignoreheaders, content, error=None):
        ignored = copy_headers(fheaders, resheaders, ignoreheaders)
        if ignored:
            logger.debug('Stripped response headers: %r' % ignored)
        logger.debug('final response headers: %r' % resheaders)
        self.response.out.write(content)
        if error:
            self.abort(error)

    def make_handler(method):
        assert method in METHODS, 'unsupported method: %s' % method
        rangemethod = method in RANGE_METHODS # if so, always send Range header
        payloadmethod = method in PAYLOAD_METHODS

        def handler(self, *args, **kw):
            req = self.request
            res = self.response
            reqheaders = req.headers
            resheaders = res.headers

            url = self._extract_url(req)

            # check payload
            payload = req.body if payloadmethod else None
            payloadlen = len(payload) if payload else 0
            if payloadlen >= URLFETCH_REQ_MAXBYTES:
                resheaders[H_LAEPROXY] = REQ_TOO_LARGE
                self.abort(503)

            # strip hop-by-hop headers
            ignoreheaders = set(i.strip() for i in
                reqheaders.get('connection', '').lower().split(',') if i.strip()) \
                | HOPBYHOP 
            ignoreheaders.add('host')
            ignored = []
            for i in ignoreheaders:
                if i in reqheaders:
                    ignored.append(reqheaders.pop(i))
            if ignored:
                logger.debug('Stripped request headers: %r' % ignored)

            if rangemethod:
                if not req.range:
                    logger.debug('No upstream range header')
                    resheaders[H_LAEPROXY] = 'Missing range header'
                    self.abort(400)
                ranges = req.range.ranges # removed in webob 1.2b1 (http://docs.webob.org/en/latest/news.html) but app engine python 2.7 runtime uses webob 1.1.1
                if len(ranges) != 1:
                    logger.debug('Multiple ranges requested')
                    resheaders[H_LAEPROXY] = 'Multiple ranges unsupported'
                    self.abort(400)
                range_start, range_end = ranges[0]
                if range_end is None:
                    logger.debug('Expected range header of the form bytes=x-y')
                    resheaders[H_LAEPROXY] = 'Range must be of the form bytes=x-y'
                    self.abort(400)
                range_end -= 1 # webob uses uninclusive end
                assert range_start is not None, 'Expected range header of the form bytes=x-y'
                if not (0 <= range_start <= range_end):
                    logger.debug('Range must satisfy 0 <= range_start <= range_end')
                    resheaders[H_LAEPROXY] = 'Range must satisfy 0 <= range_start <= range_end'
                    self.abort(416)
                nbytes_requested = range_end - range_start + 1
                if nbytes_requested > RANGE_REQ_SIZE:
                    logger.warn('Range specifies %d bytes, limit is %d' % (nbytes_requested, RANGE_REQ_SIZE))
                    resheaders[H_LAEPROXY] = 'Range specifies %d bytes, limit is %d' % (nbytes_requested, RANGE_REQ_SIZE)
                    self.abort(503)

            try:
                fetched = fetch(url,
                    payload=payload,
                    method=method,
                    headers=reqheaders,
                    allow_truncated=True,
                    follow_redirects=False,
                    deadline=URLFETCH_REQ_MAXSECS,
                    validate_certificate=True,
                    )
                resheaders[H_LAEPROXY] = RETRIEVED_FROM_NET % now()
            except InvalidURLError:
                logger.debug('InvalidURLError: %s' % url)
                resheaders[H_LAEPROXY] = 'Invalid url'
                self.abort(404)
            except DownloadError:
                logger.warn(MISSED_DEADLINE_URLFETCH)
                resheaders[H_LAEPROXY] = MISSED_DEADLINE_URLFETCH
                self.abort(504)
            except OverQuotaError:
                logger.warn(EXCEEDED_URLFETCH_QUOTA)
                res.headers[H_LAEPROXY] = EXCEEDED_URLFETCH_QUOTA
                self.abort(503)
            except Exception, e:
                logger.error('Unexpected error: %r' % e)
                logger.debug(format_exc())
                resheaders[H_LAEPROXY] = UNEXPECTED_ERROR % e
                self.abort(500)

            status = fetched.status_code
            res.set_status(status)
            resheaders[H_UPSTREAM_STATUS_CODE] = str(status)
            logger.debug('urlfetch response status: %d' % status)

            fheaders = fetched.headers
            resheaders[H_UPSTREAM_SERVER] = fheaders.get('server', '')
            logger.debug('urlfetch response headers: %r' % fheaders)

            # strip hop-by-hop headers
            ignoreheaders = set(i.strip() for i in
                fheaders.get('connection', '').lower().split(',') if i.strip()) \
                | HOPBYHOP

            content = fetched.content
            contentlen = len(content)

            trunc = fetched.content_was_truncated
            if trunc:
                logger.warn('urlfetch returned truncated response, returning as-is, originator should verify')
                resheaders[H_TRUNCATED] = 'true'
                return self._send_response(fheaders, resheaders, ignoreheaders, content)

            if not rangemethod:
                logger.debug('Non-range method, returning response as-is')
                return self._send_response(fheaders, resheaders, ignoreheaders, content)

            if status == 200:
                # Last paragraph (re proxies) of
                # http://tools.ietf.org/html/rfc2616#section-14.35.2
                # says we SHOULD send back 206 and cache entire entity in this
                # case. Disregarding for the sake of simplicity in the face of
                # App Engine's peculiar environment.
                logger.debug('Destination server does not support range requests, returning response as-is')
                return self._send_response(fheaders, resheaders, ignoreheaders, content)

            if status == 206:
                crange = fheaders.get('content-range', '')
                resheaders[H_UPSTREAM_CONTENT_RANGE] = crange
                logger.debug('Upstream Content-Range: %s' % crange)
                try:
                    assert crange.startswith('bytes '), 'Content-Range only supported in bytes'
                    sent, total = crange[6:].split('/', 1)
                    start, end = [int(i) for i in sent.split('-', 1)]
                    total = int(total)
                except Exception, e:
                    logger.warn('Error parsing upstream Content-Range %r: %r, returning 206 response as-is' % (crange, e))
                    logger.debug(format_exc())
                    return self._send_response(fheaders, resheaders, ignoreheaders, content)

                logger.debug('Parsed Content-Range: %d-%d/%d' % (start, end, total))
                entire = start == 0 and end == total - 1

                # check if the 206 actually fulfills it
                if start == range_start and end <= range_end: # could have requested more than there is
                    logger.debug('Upstream 206 response fulfills upstream range request, returning as-is')
                else:
                    logger.warn('Upstream Content-Range "%s" does not match range requested upstream "%s"' % (crange, reqheaders.get('range', '(no range?)')))
                    logger.warn('Returning upstream 206 response as-is, originator should verify')
                return self._send_response(fheaders, resheaders, ignoreheaders, content)

            logger.debug('Non-200 or 206 response to range request, returning response as-is')
            return self._send_response(fheaders, resheaders, ignoreheaders, content)

        handler.func_name = method
        return handler

    def catch_deadline_exceeded(handler):
        def wrapper(self, *args, **kw):
            try:
                return handler(self, *args, **kw)
            except DeadlineExceededError:
                res = self.response
                res.headers[H_LAEPROXY] = res.headers.get(H_LAEPROXY, '') + MISSED_DEADLINE_GAE
                self.abort(503)
        wrapper.func_name = handler.func_name
        return wrapper

    for method in METHODS:
        locals()[method] = catch_deadline_exceeded(make_handler(method))

app = webapp.WSGIApplication((
    (r'/http(s)?/.*', LaeproxyHandler),
    ), debug=DEV)

def main():
    from google.appengine.ext.webapp.util import run_wsgi_app
    run_wsgi_app(app)

if __name__ == "__main__":
    main()
