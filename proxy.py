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

RANGE_REQ_SIZE = 2000000

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

class LaeproxyHandler(webapp.RequestHandler):

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
                return self.error(413)

            # XXX http://code.google.com/p/googleappengine/issues/detail?id=4879
            if 'content-length' not in reqheaders:
                logger.debug('Adding Content-Length: %d' % payloadlen)
                reqheaders['content-length'] = str(payloadlen)
            # even this triggers KeyError('Content-Length') without the above:
            logger.debug('Request headers: %r' % reqheaders)

            # strip hop-by-hop headers
            ignoreheaders = HOPBYHOP | set(i.strip() for i in
                reqheaders.get('connection', '').lower().split(',') if i.strip())
            ignored = []
            for i in ignoreheaders:
                if i in reqheaders:
                    ignored.append(reqheaders.pop(i))
            if ignored:
                logger.debug('Ignored request headers: %r' % ignored)

            if rangemethod:
                # add Range header to avoid hitting app engine limits
                if not req.range:
                    unparsed_range = reqheaders.get('range')
                    if unparsed_range:
                        logger.debug('Could not parse Range specified upstream ("%s"), overwriting' % unparsed_range)
                    rangeadded = True # we are adding the range header
                    start = 0
                    end = RANGE_REQ_SIZE - 1
                    nbytesrequested = end - start + 1 # endpoints are inclusive
                    rangestr = 'bytes=%d-%d' % (start, end)
                    reqheaders['range'] = rangestr
                    logger.debug('Added Range: %s' % rangestr)
                else:
                    rangeadded = False # Range header already present
                    singlerange = False # only a single range given
                    nbytesrequested = None # will be calculated if possible
                    ranges = req.range.ranges
                    if len(ranges) == 1:
                        singlerange = True
                        start, end = ranges[0]
                        if end is not None:
                            end -= 1 # webob uses uninclusive end
                            nbytesrequested = end - start + 1
                        elif start < 0:
                            nbytesrequested = -start
                    rangestr = reqheaders['range']
                    if nbytesrequested:
                        logger.debug('Range specified upstream: %s (%d bytes)' % (rangestr, nbytesrequested))
                        if nbytesrequested > GAE_RES_MAXBYTES:
                            logger.warn('Upstream range request size exceeds App Engine response size limit')
                            # XXX handle this better?
                    else:
                        logger.debug('Range specified upstream: %s (could not determine length)' % rangestr)
                        # XXX handle this better?

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
                logger.warn('urlfetch returned truncated content')

            if rangemethod:
                if status == 200:
                    logger.debug('Got 200 response to range request')
                    resheaders[UPSTREAM_206] = 'False'

                    # If we added the Range header and got back a 200, just
                    # send back the response as-is. The upstream requester
                    # isn't expecting a 206 in this case.

                    # If the Range header was already present and we got back
                    # a 200, change to 206 (slicing content accordingly)
                    # to comply with the last paragraph (re proxies) of
                    # http://tools.ietf.org/html/rfc2616#section-14.35.2
                    # Note: We can only do this for untruncated urlfetch
                    # results, since urlfetch overwrites Content-Length
                    # (http://code.google.com/p/googleappengine/issues/detail?id=4878)
                    # and we have no Content-Range header from upstream, so we
                    # don't know the content length of the untruncated content
                    # and therefore can't populate Content-Range ourselves.
                    # XXX https://github.com/getlantern/laeproxy/issues/5
                    if not rangeadded and not trunc:
                        logger.debug('Converting 200 response to 206')
                        total = contentlen
                        needslice = False
                        if nbytesrequested:
                            if end is None:
                                # since the length of the requested range is
                                # known, it must be of the form "-x"
                                assert start < 0, 'Expected upstream range request of the form "-x"'
                                start += contentlen
                                end = contentlen - 1
                                logger.debug('Adjusted (start, end): (%d, %d)' % (start, end))
                            needslice = contentlen > nbytesrequested
                        elif singlerange:
                            assert end is None, 'Expected upstream range request of the form "x-"'
                            end = contentlen - 1
                            logger.debug('Populated end: %d' % end)
                            if start > end:
                                logger.debug('Requested start position is beyond end position, not satisfiable')
                                resheaders['content-range'] = 'bytes */%d' % total
                                return self.error(416)
                            needslice = start != 0
                        if needslice:
                            logger.debug('Slicing content [%d:%d]' % (start, end+1))
                            # XXX cache discarded content for subsequent requests if cache policy allows
                            content = content[start:end+1]
                        res.set_status(206)
                        resheaders['content-range'] = 'bytes %d-%d/%d' % (start, end, total)
                        logger.debug('Sending Content-Range: %d-%d/%d' % (start, end, total))

                elif status == 206:
                    resheaders[UPSTREAM_206] = 'True'
                    crange = fheaders.get('content-range', '')
                    resheaders[UPSTREAM_CONTENT_RANGE] = crange
                    # if we added the Range header, it's against the HTTP spec
                    # to send 206 downstream, so change to 200
                    if rangeadded:
                        logger.debug('Changing 206 to 200 and stripping content-range')
                        res.set_status(200)
                        ignoreheaders.add('content-range')
                    try:
                        assert crange.startswith('bytes '), 'Content-Range only supported in bytes'
                        sent, total = crange[6:].split('/', 1)
                        start, end = [int(i) for i in sent.split('-', 1)]
                        total = int(total)
                    except Exception, e:
                        logger.info('Error parsing Content-Range %r: %r' % (crange, e))
                        logger.debug(format_exc())
                    else:
                        logger.debug('Parsed Content-Range: %d-%d/%d' % (start, end, total))

            finalcontentlen = len(content) # could have been sliced above
            if finalcontentlen > RANGE_REQ_SIZE:
                diff = finalcontentlen - RANGE_REQ_SIZE
                logger.info('Content is %d bytes, max is %d. Truncating %d bytes.' % (finalcontentlen, RANGE_REQ_SIZE, diff))
                content = content[:RANGE_REQ_SIZE]
                resheaders[TRUNC_HEADER_KEY] = 'True'

            finalcontentlen = len(content) # could have been sliced further
            # adjust content-range for truncation if necessary
            if res.status == 206:
                try:
                    realend = start + finalcontentlen - 1
                    if end != realend:
                        crange = 'bytes %d-%d/%d' % (start, realend, total)
                        resheaders['content-range'] = crange
                        logger.debug('Changed "end" from %d to %d. Sending Content-Range: %s' % (end, realend, crange))
                except Exception, e:
                    log.error('Error adjusting Content-Range for truncation: %r' % e)
                    logger.debug(format_exc())

            # copy non-ignored headers from urlfetched response
            ignored = []
            for k, v in fheaders.iteritems():
                if k.lower() not in ignoreheaders:
                    resheaders[k] = v
                else:
                    ignored.append(k)
            if ignored:
                logger.debug('Stripped response headers: %r' % ignored)

            logger.debug('final response headers: %r' % resheaders)
            res.out.write(content)

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
