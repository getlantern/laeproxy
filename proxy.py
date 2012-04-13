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
RES_MAXBYTES = 1024 * 1024 * 7 # max urlfetch bandwidth of 22MB/sec
GAE_REQ_MAXSECS = 60

RANGE_REQ_SIZE = 2000000 # bytes. matches Lantern's CHUNK_SIZE.

# stamp our responses with this header
EIGEN_HEADER_KEY = 'X-laeproxy'
# indicates whether upstream server gave back 206
UPSTREAM_206 = 'X-laeproxy-upstream-206'
# indicates truncated response
TRUNC_HEADER_KEY = 'X-laeproxy-truncated'
# specifies full length of an entity that has been truncated
UNTRUNC_LEN = 'X-laeproxy-untruncated-content-length'
# upstream Content-Range moved aside to this when we have to respond with 200
UPSTREAM_CONTENT_RANGE = 'X-laeproxy-upstream-content-range'
# various values corresponding to possible results of proxy requests
RETRIEVED_FROM_NET = 'Retrieved from network %s'
IGNORED_RECURSIVE = 'Ignored recursive request'
REQ_TOO_LARGE = 'Request size exceeds urlfetch limit'
MISSED_DEADLINE_URLFETCH = 'Missed urlfetch deadline'
MISSED_DEADLINE_GAE = 'Missed GAE deadline'
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

    def _send_response(self, fheaders, resheaders, ignoreheaders, content, error=None):
        ignored = copy_headers(fheaders, resheaders, ignoreheaders)
        if ignored:
            logger.debug('Stripped response headers: %r' % ignored)
        logger.debug('final response headers: %r' % resheaders)
        self.response.out.write(content)
        if error:
            return self.error(error)

    def make_handler(method):
        assert method in METHODS, 'unsupported method: %s' % method
        rangemethod = method in RANGE_METHODS # if so, always send Range header
        payloadmethod = method in PAYLOAD_METHODS

        def handler(self, *args, **kw):
            req = self.request
            res = self.response
            reqheaders = req.headers
            resheaders = res.headers

            # reconstruct original url
            path = req.path_qs.lstrip('/')
            try:
                scheme, rest = path.split('/', 1)
                parts = rest.split('/', 1)
            except ValueError:
                return self.error(404)
            try:
                rest = parts[1]
            except IndexError:
                rest = ''
            host = unquote(parts[0])
            if not host:
                return self.error(404)
            if req.host.lower() == host.lower():
                logger.info('Ignoring recursive request %s' % req.url)
                resheaders[EIGEN_HEADER_KEY] = IGNORED_RECURSIVE
                return self.error(404)
            url = scheme + '://' + host + '/' + rest
            logger.debug('Target url: %s' % url)

            # check payload
            payload = req.body if payloadmethod else None
            payloadlen = len(payload) if payload else 0
            if payloadlen >= URLFETCH_REQ_MAXBYTES:
                resheaders[EIGEN_HEADER_KEY] = REQ_TOO_LARGE
                return self.error(503)

            # strip hop-by-hop headers
            ignoreheaders = HOPBYHOP | set(i.strip() for i in
                reqheaders.get('connection', '').lower().split(',') if i.strip())
            ignored = []
            for i in ignoreheaders:
                if i in reqheaders:
                    ignored.append(reqheaders.pop(i))
            if ignored:
                logger.debug('Stripped request headers: %r' % ignored)

            if rangemethod:
                urange = reqheaders.get('range') # upstream range
                logger.debug('Range specified upstream: %s' % urange)
                urange_kept = None
                urange_nbytesrequested = None
                urange_openended = False # open-ended range request (of the form bytes=x-)
                urange_start = urange_end = None # start and end bytes of upstream range header
                srange_start = srange_end = None # start and end bytes of sent range header
                srange_nbytesrequested = None
                if req.range:
                    ranges = req.range.ranges # removed in webob 1.2b1 (http://docs.webob.org/en/latest/news.html) but app engine python 2.7 runtime uses webob 1.1.1
                    singlerange = len(ranges) == 1 # only a single range given
                    if singlerange:
                        urange_start, urange_end = ranges[0]
                        if urange_end is not None: # range request of the form bytes=x-y
                            urange_end -= 1 # webob uses uninclusive end
                            urange_nbytesrequested = urange_end - urange_start + 1
                        elif urange_start < 0: # range request of the form bytes=-x
                            urange_nbytesrequested = -urange_start
                        else: # open-ended range request of the form bytes=x-
                            urange_openended = True

                        if urange_openended:
                            srange_start = urange_start
                            srange_end = urange_start + RES_MAXBYTES - 1
                            srange_nbytesrequested = RES_MAXBYTES
                            rangestr = 'bytes=%d-%d' % (srange_start, srange_end)
                            reqheaders['range'] = rangestr
                            logger.warn('Optimistically capping open-ended upstream range, sending range: %s' % rangestr)
                            urange_kept = False
                        elif urange_nbytesrequested > RES_MAXBYTES:
                            logger.warn('Upstream request requested %d bytes, limit is %d, returning 503' % (urange_nbytesrequested, RES_MAXBYTES))
                            return self.error(503)
                        else:
                            srange_start = urange_start
                            srange_end = urange_end
                            srange_nbytesrequested = urange_nbytesrequested
                            urange_kept = True
                    else:
                        logger.warn('Cannot fulfill request for multiple ranges, returning 503')
                        return self.error(503)

                if not req.range:
                    if urange:
                        logger.warn('Could not parse range specified upstream, overwriting')
                        urange = None
                    srange_start = 0
                    srange_end = RANGE_REQ_SIZE - 1
                    srange_nbytesrequested = RANGE_REQ_SIZE
                    rangestr = 'bytes=%d-%d' % (srange_start, srange_end)
                    reqheaders['range'] = rangestr
                    logger.debug('Sending range: %s' % rangestr)
                    urange_kept = False


            # XXX http://code.google.com/p/googleappengine/issues/detail?id=739
            # reqheaders.update(cache_control='no-cache,max-age=0', pragma='no-cache')

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
                resheaders[EIGEN_HEADER_KEY] = RETRIEVED_FROM_NET % now()
            except InvalidURLError:
                logger.debug('InvalidURLError: %s' % url)
                return self.error(404)
            except DownloadError:
                logger.warn(MISSED_DEADLINE_URLFETCH)
                resheaders[EIGEN_HEADER_KEY] = MISSED_DEADLINE_URLFETCH
                return self.error(504)
            except OverQuotaError:
                logger.warn(EXCEEDED_URLFETCH_QUOTA)
                res.headers[EIGEN_HEADER_KEY] = EXCEEDED_URLFETCH_QUOTA
                return self.error(503)
            except Exception, e:
                logger.error('Unexpected error: %r' % e)
                logger.debug(format_exc())
                resheaders[EIGEN_HEADER_KEY] = UNEXPECTED_ERROR % e
                return self.error(500)

            fheaders = fetched.headers
            logger.debug('urlfetch response headers: %r' % fheaders)

            # strip hop-by-hop headers
            ignoreheaders = set(i.strip() for i in
                fheaders.get('connection', '').lower().split(',') if i.strip()) \
                | HOPBYHOP

            status = fetched.status_code
            res.set_status(status)
            content = fetched.content
            contentlen = len(content)

            trunc = fetched.content_was_truncated
            if trunc:
                logger.warn('urlfetch returned truncated response, returning as-is, originator should verify')
                resheaders[TRUNC_HEADER_KEY] = 'True'
                return self._send_response(fheaders, resheaders, ignoreheaders, content)

            if not rangemethod:
                logger.debug('Non-range method, returning response as-is')
                return self._send_response(fheaders, resheaders, ignoreheaders, content)

            if status == 200:
                resheaders[UPSTREAM_206] = 'False'
                # If we set the Range header and got back a 200, just
                # send back the response as-is.
                if not urange_kept:
                    logger.warn('Upstream range (if any) not kept and got 200 response, returning as-is')
                    return self._send_response(fheaders, resheaders, ignoreheaders, content)

                # If we kept the upstream Range header and we got back
                # a 200, change to 206 and slice content accordingly (see
                # http://tools.ietf.org/html/rfc2616#section-14.35.2
                # last paragraph (re proxies))
                logger.debug('Upstream range kept and got 200 response, slicing and converting to 206')
                res.set_status(206)
                content = content[urange_start:urange_end+1] # XXX cache discarded content for subsequent requests before throwing away if cache policy allows
                crangestr = 'bytes %d-%d/%d' % (urange_start, urange_end, contentlen)
                resheaders['content-range'] = crangestr
                logger.debug('Sending Content-Range: %s' % crangestr)
                return self._send_response(fheaders, resheaders, ignoreheaders, content)

            elif status == 206:
                resheaders[UPSTREAM_206] = 'True'
                crange = fheaders.get('content-range', '')
                resheaders[UPSTREAM_CONTENT_RANGE] = crange
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

                # If we *added* the range header and got back 206...
                if not urange:
                    # it's against the spec to send 206 downstream, so convert to 200 response
                    if entire: # can only send 200 if we have the entire entity
                        logger.debug('Got entire entity, converting to 200 response to fulfill non-range request')
                        res.set_status(200)
                        ignoreheaders.add('content-range')
                        return self._send_response(fheaders, resheaders, ignoreheaders, content)
                    logger.warn('Did not get entire entity so cannot fulfill non-range request, returning 503')
                    return self._send_response(fheaders, resheaders, ignoreheaders, content, error=503)

                # If we kept the upstream range header and got back 206...
                if urange_kept:
                    # check if the 206 actually fulfills it
                    if start == urange_start and total <= urange_nbytesrequested: # could have requested more than there is
                        logger.debug('Upstream 206 response fulfills upstream range request, returning as-is')
                        return self._send_response(fheaders, resheaders, ignoreheaders, content)
                    logger.warn('Upstream Content-Range "%s" does not fulfill range requested upstream "%s"' % (crange, urange))
                    logger.warn('Returning upstream 206 response as-is, originator should verify')
                    return self._send_response(fheaders, resheaders, ignoreheaders, content)

                # If the upstream range request was open-ended (e.g. bytes=x-),
                # we capped it (e.g. bytes=x-y) hoping to still retrieve the
                # whole rest of the entity
                if urange_openended:
                    assert not urange_kept, 'Expected to have modified range header from upstream to not be open-ended'
                    if end != total - 1:
                        logger.warn('Could only request last %d bytes of entity but %d bytes are required to fulfill open-ended upstream range request, returning 503' % (srange_nbytesrequested, total - end))
                        return self._send_response(fheaders, resheaders, ignoreheaders, content, error=503)
                    logger.debug('Upstream 206 response fulfills open-ended upstream range request, returning as-is')
                    return self._send_response(fheaders, resheaders, ignoreheaders, content)

                # should never get here
                logger.error('Reached unexpected code path')
                return self._send_response(fheaders, resheaders, ignoreheaders, content, error=500)


        handler.func_name = method
        return handler

    def catch_deadline_exceeded(handler):
        def wrapper(self, *args, **kw):
            try:
                return handler(self, *args, **kw)
            except DeadlineExceededError:
                res = self.response
                res.set_status(503)
                res.headers[EIGEN_HEADER_KEY] = MISSED_DEADLINE_GAE
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
