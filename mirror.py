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

# this header is added to content we serve
EIGEN_HEADER_KEY = 'X-Mirrorrr'
# various values corresponding to possible results of proxy requests
RETRIEVED_FROM_NET = 'Retrieved from network %s'
RETRIEVED_FROM_CACHE = 'Retrieved from cache'
IGNORED_RECURSIVE = 'Ignored recursive request'
RESPONSE_TOO_LARGE = 'Response too large'
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

class MirrorHandler(webapp.RequestHandler):

    def make_handler(method):
        assert method in METHODS, 'unsupported method: %s' % method
        def handler(self, *args, **kw):
            req = self.request
            res = self.response
            resheaders = res.headers
            logging.debug('req.url: %s' % req.url)
            path = req.path_qs.lstrip('/')
            try:
                scheme, rest = path.split('/', 1)
                split = rest.split('/', 1)
                host = split[0]
                rest = split[1] if len(split) > 1 else ''
            except ValueError:
                return self.error(404)
            host = unquote(host)
            if req.host == host:
                logging.info('Ignoring recursive request %s' % req.url)
                resheaders[EIGEN_HEADER_KEY] = IGNORED_RECURSIVE
                return
            url = scheme + '://' + host + '/' + rest
            payload = req.body if method in PAYLOAD_METHODS else None
            headers = req.headers
            for i in IGNORE_HEADERS_REQ:
                headers.pop(i, None)
            # http://code.google.com/p/googleappengine/issues/detail?id=739
            headers.update(cache_control='no-cache,max-age=0', pragma='no-cache')
            try:
                # XXX http://code.google.com/appengine/docs/python/urlfetch/fetchfunction.html
                fetched = urlfetch.fetch(url,
                    payload=payload,
                    method=method,
                    headers=headers,
                    allow_truncated=False,
                    follow_redirects=False,
                    deadline=10,
                    validate_certificate=True,
                    )
            except urlfetch.InvalidURLError, e:
                return self.error(404)
            except urlfetch.ResponseTooLargeError:
                resheaders[EIGEN_HEADER_KEY] = RESPONSE_TOO_LARGE
                return self.error(400) # XXX
            except Exception, e:
                logging.error('urlfetch(url=%s) => %s' % (url, e))
                if DEBUG: logging.debug(format_exc())
                resheaders[EIGEN_HEADER_KEY] = UNEXPECTED_ERROR % e
                return self.error(500)
            res.set_status(fetched.status_code)
            for k, v in fetched.headers.iteritems():
                if k.lower() not in IGNORE_HEADERS_RES:
                    resheaders[k] = v
            resheaders[EIGEN_HEADER_KEY] = RETRIEVED_FROM_NET % now()
            res.out.write(fetched.content)
        handler.func_name = method
        return handler

    for method in METHODS:
        locals()[method] = make_handler(method)

app = webapp.WSGIApplication((
    (r'/([^/]+).*', MirrorHandler),
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
