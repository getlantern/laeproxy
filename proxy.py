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

DEBUG = environ.get('SERVER_SOFTWARE', '').startswith('Dev')
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

RANGE_REQ_SIZE = GAE_RES_MAXBYTES - 32 # wiggle room?

_RANGE_REQ_SUFFIX = '. Range requested: bytes=%d-%d'

# stamp our responses with this header
EIGEN_HEADER_KEY = 'X-laeproxy'
# various values corresponding to possible results of proxy requests
RETRIEVED_FROM_NET = 'Retrieved from network %s'
RETRIEVED_FROM_CACHE = 'Retrieved from cache'
IGNORED_RECURSIVE = 'Ignored recursive request'
REQ_TOO_LARGE = 'Request size exceeds urlfetch limit'
RES_TOO_LARGE = 'Response too large' + _RANGE_REQ_SUFFIX
MISSED_DEADLINE_URLFETCH = 'Missed urlfetch deadline' + _RANGE_REQ_SUFFIX
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

        def handler(self, *args, **kw):
            req = self.request
            res = self.response
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
            payload = req.body if method in PAYLOAD_METHODS else None
            if payload and len(payload) >= URLFETCH_REQ_MAXBYTES:
                resheaders[EIGEN_HEADER_KEY] = REQ_TOO_LARGE
                return self.error(413)

            # check headers
            reqheaders = req.headers
            for i in IGNORE_HEADERS_REQ:
                reqheaders.pop(i, None)

            # always make range requests to avoid urlfetch.ResponseTooLargeError
            start = 0
            end = RANGE_REQ_SIZE
            rangeadded = True
            if 'range' in reqheaders:
                rangeadded = False
                # check that range is within limits
                try:
                    start, end = [int(i) for i in
                        reqheaders['range'].lstrip('bytes=').split('-', 1)]
                    if end - start >= RANGE_REQ_SIZE:
                        newend = start + RANGE_REQ_SIZE - 1
                        logging.info('Requested range (%d-%d) too large, '
                            'shortening to %d-%d' % (start, end, start, newend))
                        end = newend
                except:
                    logging.debug('Error checking range: %s' % format_exc())
            reqheaders['range'] = 'bytes=%d-%d' % (start, end)

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
                resheaders[EIGEN_HEADER_KEY] = MISSED_DEADLINE_URLFETCH % (start, end)
                return self.error(504)
            except urlfetch.ResponseTooLargeError:
                # upstream server doesn't support range requests?
                resheaders[EIGEN_HEADER_KEY] = RES_TOO_LARGE % (start, end)
                return self.error(503)
            except Exception, e:
                logging.error('Unexpected error: %s' % e)
                logging.debug(format_exc())
                resheaders[EIGEN_HEADER_KEY] = UNEXPECTED_ERROR % e
                return self.error(500)

            status = fetched.status_code
            res.set_status(status)

            # change to 200 if we changed to range request and got entire entity
            if rangeadded and status == 206:
                try:
                    sent, total = fetched.headers[
                        'content-range'].lstrip('bytes ').split('/', 1)
                    start, end = [int(i) for i in sent.split('-', 1)]
                    total = int(total)
                    if start == 0 and end == total - 1:
                        logging.debug('Retrieved entire entity, changing 206 to 200')
                        res.set_status(200)
                    # XXX necessary to strip content-range header?  probably ignored if status is 200.
                except:
                    logging.debug('Error checking content-range: %s' % format_exc())

            for k, v in fetched.headers.iteritems():
                if k.lower() not in IGNORE_HEADERS_RES:
                    resheaders[k] = v

            res.out.write(fetched.content)

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
