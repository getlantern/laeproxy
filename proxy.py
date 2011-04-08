#!/usr/bin/env python

# Lantern App Engine Proxy
#
# Based on <http://code.google.com/p/mirrorrr> by Brett Slatkin
# <bslatkin@gmail.com> (see original copyright notice below)
#
# Modified for use with Lantern <http://www.getlantern.org> by
# Brave New Software <http://www.bravenewsoftware.org>
#
# Any copyrightable modifications by Brave New Software are copyright 2011
# Brave New Software and are hereby redistributed under the terms of the
# license of the original work, which follows:
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

import logging

now = datetime.utcnow

#DEBUG = environ.get('SERVER_SOFTWARE', '').startswith('Dev')
DEBUG = True
if DEBUG:
    logging.getLogger().setLevel(logging.DEBUG)

def _breakpoint():
    if not DEBUG: return
    import sys, pdb
    def swap():
        for i in ('stdin', 'stdout', 'stderr'):
            i_ = '__%s__' % i
            tmp = getattr(sys, i)
            setattr(sys, i, getattr(sys, i_))
            setattr(sys, i_, tmp)
    swap()
    pdb.set_trace()
    swap()

DELETE = 'delete'
GET = 'get'
HEAD = 'head'
PUT = 'put'
POST = 'post'
METHODS = frozenset((DELETE, GET, HEAD, PUT, POST))
RANGE_METHODS = frozenset((GET,))
PAYLOAD_METHODS = frozenset((PUT, POST))

# http://code.google.com/appengine/docs/python/urlfetch/overview.html#Quotas_and_Limits
URLFETCH_REQ_MAXBYTES = 1024 * 1024 # 1MB
URLFETCH_RES_MAXBYTES = 1024 * 1024 * 32
# http://code.google.com/appengine/docs/python/urlfetch/fetchfunction.html
URLFETCH_REQ_MAXSECS = 10
# http://code.google.com/appengine/docs/python/runtime.html#Quotas_and_Limits
GAE_REQ_MAXBYTES = 1024 * 1024 * 10
GAE_RES_MAXBYTES = 1024 * 1024 * 10
GAE_REQ_MAXSECS = 30

RANGE_REQ_SIZE = GAE_RES_MAXBYTES - 2048 # wiggle room?

# stamp our responses with this header
EIGEN_HEADER_KEY = 'X-laeproxy'
# indicates truncated response
TRUNC_HEADER_KEY = 'X-laeproxy-truncated'
# various values corresponding to possible results of proxy requests
RETRIEVED_FROM_NET = 'Retrieved from network %s'
IGNORED_RECURSIVE = 'Ignored recursive request'
REQ_TOO_LARGE = 'Request size exceeds urlfetch limit'
RES_TOO_LARGE = 'Response size exceeds urlfetch limit'
MISSED_DEADLINE_URLFETCH = 'Missed urlfetch deadline'
MISSED_DEADLINE_GAE = 'Missed GAE deadline'
UNEXPECTED_ERROR = 'Unexpected error: %s'

IGNORE_HEADERS_REQ = frozenset((
    'content-length',
    'host',
    'vary',
    'via',
    'x-forwarded-for',
    ))

IGNORE_HEADERS_RES = frozenset((
    # Ignore hop-by-hop headers
    # http://www.w3.org/Protocols/rfc2616/rfc2616-sec13.html#sec13.5.1
    'connection',
    'keep-alive',
    'proxy-authenticate',
    'proxy-authorization',
    'te',
    'trailers',
    'transfer-encoding',
    'upgrade',
    ))

class LaeproxyHandler(webapp.RequestHandler):

    def make_handler(method):
        assert method in METHODS, 'unsupported method: %s' % method
        rangemethod = method in RANGE_METHODS
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
            if req.host == host:
                logging.info('Ignoring recursive request %s' % req.url)
                resheaders[EIGEN_HEADER_KEY] = IGNORED_RECURSIVE
                return self.error(404)
            url = scheme + '://' + host + '/' + rest

            # check payload
            payload = req.body if payloadmethod else None
            if payload and len(payload) >= URLFETCH_REQ_MAXBYTES:
                resheaders[EIGEN_HEADER_KEY] = REQ_TOO_LARGE
                return self.error(413)

            # check headers
            for i in IGNORE_HEADERS_REQ:
                reqheaders.pop(i, None)

            if rangemethod:
                # always make range requests to avoid urlfetch.ResponseTooLargeError
                if not req.range:
                    rangeadded = True
                    start = 0
                    end = RANGE_REQ_SIZE - 1
                    nbytesrequested = end - start + 1 # endpoints are inclusive
                    rangestr = 'bytes=%d-%d' % (start, end)
                    reqheaders['range'] = rangestr
                    logging.debug('Added Range: %s' % rangestr)
                else:
                    rangeadded = False
                    nbytesrequested = None # will be calculated if possible
                    ranges = req.range.ranges
                    if len(ranges) == 1:
                        start, end = ranges[0]
                        if end is not None:
                            end += 1 # webob uses uninclusive end
                            nbytesrequested = end - start + 1
                        elif start < 0:
                            nbytesrequested = -start
                    rangestr = reqheaders['range']
                    if nbytesrequested:
                        logging.debug('Range specified upstream: %s '
                           '(%d bytes)' % (rangestr, nbytesrequested))
                    else:
                        logging.debug('Range specified upstream: %s '
                           '(could not determine length)' % rangestr)

            # XXX http://code.google.com/p/googleappengine/issues/detail?id=739
            # reqheaders.update(cache_control='no-cache,max-age=0', pragma='no-cache')

            try:
                fetched = urlfetch.fetch(url,
                    payload=payload,
                    method=method,
                    headers=reqheaders,
                    allow_truncated=False,
                    follow_redirects=False,
                    deadline=URLFETCH_REQ_MAXSECS,
                    validate_certificate=True,
                    )
                resheaders[EIGEN_HEADER_KEY] = RETRIEVED_FROM_NET % now()
            except urlfetch.InvalidURLError:
                return self.error(404)
            except urlfetch.DownloadError:
                resheaders[EIGEN_HEADER_KEY] = MISSED_DEADLINE_URLFETCH
                return self.error(504)
            except urlfetch.ResponseTooLargeError:
                # upstream server doesn't support range requests?
                resheaders[EIGEN_HEADER_KEY] = RES_TOO_LARGE
                return self.error(503)
            except Exception, e:
                logging.error('Unexpected error: %s' % e)
                logging.debug(format_exc())
                resheaders[EIGEN_HEADER_KEY] = UNEXPECTED_ERROR % e
                return self.error(500)

            status = fetched.status_code
            res.set_status(status)
            content = fetched.content
            contentlength = len(content)

            for k, v in fetched.headers.iteritems():
                if k.lower() not in IGNORE_HEADERS_RES:
                    resheaders[k] = v

            if rangemethod:
                # change to 200 if we converted to range request and got back
                # entire entity in a 206
                if rangeadded and status == 206:
                    try:
                        crange = fetched.headers.get('content-range', '')
                        sent, total = crange.lstrip('bytes ').split('/', 1)
                        start, end = [int(i) for i in sent.split('-', 1)]
                        total = int(total)
                    except:
                        logging.debug('Error parsing content-range "%s": %s' % (crange, format_exc()))
                    else:
                        if start == 0 and end == total - 1:
                            logging.debug('Retrieved entire entity, changing 206 to 200')
                            res.set_status(200)
                            del resheaders['content-range']

                # change to 206 if range request of determinate length made by
                # upstream requester but upstream server gave back 200
                # http://tools.ietf.org/html/rfc2616#section-14.35.2
                elif not rangeadded and nbytesrequested and status == 200:
                    logging.debug('Converting 200 response to 206')
                    if end is None:
                        end = contentlength - 1
                    if contentlength != nbytesrequested: 
                        logging.debug('Slicing content')
                        content = content[start:end+1]
                        contentlength = nbytesrequested
                    res.set_status(206)
                    resheaders['content-range'] = 'bytes %d-%d/%d' % (
                        start, end, nbytesrequested)

            if contentlength > RANGE_REQ_SIZE:
                logging.info('Response too large, truncating!')
                content = content[:RANGE_REQ_SIZE]
                resheaders[TRUNC_HEADER_KEY] = 'True'

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
    ), debug=DEBUG)

def main():
    if DEBUG:
        from wsgiref.handlers import CGIHandler
        CGIHandler().run(app)
    else:
        from google.appengine.ext.webapp.util import run_wsgi_app
        run_wsgi_app(app)

if __name__ == "__main__":
    main()
