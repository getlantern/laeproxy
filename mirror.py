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

from urllib import splittype, splithost
from wsgiref.handlers import CGIHandler

from google.appengine.api import urlfetch
from google.appengine.ext import webapp

import logging


## DEBUG = False
## EXPIRATION_DELTA_SECONDS = 60

DEBUG = True
EXPIRATION_DELTA_SECONDS = 1
logging.getLogger().setLevel(logging.DEBUG)

def breakpoint():
    import sys, pdb
    for attr in ('stdin', 'stdout', 'stderr'):
        setattr(sys, attr, getattr(sys, '__%s__' % attr))
    pdb.set_trace()

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
RETRIEVED_FROM_NET = 'Retrieved from network'
RETRIEVED_FROM_CACHE = 'Retrieved from cache'
RESPONSE_TOO_LARGE = 'Response too large'

IGNORE_HEADERS_REQ = frozenset((
    'content-length',
    'host',
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
            afternetloc = splithost(splittype(req.url)[1])[1][1:]
            try:
                scheme, rest = afternetloc.split('/', 1)
            except ValueError:
                return self.error(404)
            url = scheme + '://' + rest
            payload = req.body if method in PAYLOAD_METHODS else None
            headers = dict((k, v) for (k, v) in req.headers.iteritems()
                if k.lower() not in IGNORE_HEADERS_REQ)
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
                res.headers[EIGEN_HEADER_KEY] = RESPONSE_TOO_LARGE
                return self.error(400) # XXX
            except Exception, e:
                logging.error('urlfetch(url=%s) => %s' % (url, e))
                res.headers[EIGEN_HEADER_KEY] = str(e)
                raise
            # XXX check for EIGEN_HEADER_KEY to avoid potential loops?
            #if fetched.headers.get(EIGEN_HEADER_KEY) is not None:
            #    return
            res.status = fetched.status_code
            for k, v in fetched.headers.iteritems():
                if k.lower() not in IGNORE_HEADERS_RES:
                    res.headers[k] = v
            res.headers[EIGEN_HEADER_KEY] = RETRIEVED_FROM_NET
            res.out.write(fetched.content) # XXX just return resp?
        handler.func_name = method
        return handler

    for method in METHODS:
        locals()[method] = make_handler(method)

app = webapp.WSGIApplication((
    (r'/([^/]+).*', MirrorHandler),
    ), debug=DEBUG)

def main():
    CGIHandler().run(app)

if __name__ == "__main__":
    main()
