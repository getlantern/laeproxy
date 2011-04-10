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

DEVELOPMENT = environ.get('SERVER_SOFTWARE', '').startswith('Dev')
#DEBUG = DEVELOPMENT
DEBUG = True
if DEBUG:
    logging.getLogger().setLevel(logging.DEBUG)

def _breakpoint():
    if not DEVELOPMENT: return
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
# specifies full length of an entity that has been truncated
UNTRUNC_LEN = 'X-laeproxy-untruncated-content-length'
# various values corresponding to possible results of proxy requests
RETRIEVED_FROM_NET = 'Retrieved from network %s'
IGNORED_RECURSIVE = 'Ignored recursive request'
REQ_TOO_LARGE = 'Request size exceeds urlfetch limit'
MISSED_DEADLINE_URLFETCH = 'Missed urlfetch deadline'
MISSED_DEADLINE_GAE = 'Missed GAE deadline'
UNEXPECTED_ERROR = 'Unexpected error: %r'

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
            # in development, this doesn't stick at module scope :\
            if DEVELOPMENT and DEBUG:
                logging.getLogger().setLevel(logging.DEBUG)

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
                # always make range requests to avoid hitting app engine limits
                if not req.range:
                    if reqheaders['range']:
                        logging.debug('Bad Range specified upstream ("%s"), ignoring' % reqheaders['range'])
                    rangeadded = True # we are adding the range header
                    start = 0
                    end = RANGE_REQ_SIZE - 1
                    nbytesrequested = end - start + 1 # endpoints are inclusive
                    rangestr = 'bytes=%d-%d' % (start, end)
                    reqheaders['range'] = rangestr
                    logging.debug('Added Range: %s' % rangestr)
                else:
                    rangeadded = False # range header already present
                    singlerange = False # only a single range given
                    nbytesrequested = None # will be calculated if possible
                    ranges = req.range.ranges
                    if len(ranges) == 1:
                        singlerange = True
                        start, end = ranges[0]
                        if end is not None:
                            end += 1 # webob uses uninclusive end
                            nbytesrequested = end - start + 1
                        elif start < 0:
                            nbytesrequested = -start
                    rangestr = reqheaders['range']
                    if nbytesrequested:
                        logging.debug('Range specified upstream: %s (%d bytes)' % (rangestr, nbytesrequested))
                        if nbytesrequested > RANGE_REQ_SIZE:
                            logging.warn('Upstream range request size exceeds App Engine response size limit')
                    else:
                        logging.debug('Range specified upstream: %s (could not determine length)' % rangestr)

            # XXX http://code.google.com/p/googleappengine/issues/detail?id=739
            # reqheaders.update(cache_control='no-cache,max-age=0', pragma='no-cache')

            try:
                fetched = urlfetch.fetch(url,
                    payload=payload,
                    method=method,
                    headers=reqheaders,
                    allow_truncated=True,
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
            except Exception, e:
                logging.error('Unexpected error: %r' % e)
                logging.debug(format_exc())
                resheaders[EIGEN_HEADER_KEY] = UNEXPECTED_ERROR % e
                return self.error(500)

            fheaders = fetched.headers
            logging.debug('urlfetch response headers: %r' % fheaders)
            for k, v in fheaders.iteritems():
                if k.lower() not in IGNORE_HEADERS_RES:
                    resheaders[k] = v

            status = fetched.status_code
            res.set_status(status)
            content = fetched.content
            contentlen = len(content)

            truncated = fetched.content_was_truncated
            # will be populated with length of untruncated content if possible
            untrunclen = None
            if truncated:
                logging.warn('urlfetch returned truncated content')
                if method == GET and status == 200:
                    logging.debug('Attempting to determine untruncated length via HEAD')
                    try:
                        hd = urlfetch.fetch(url,
                            method=HEAD,
                            headers=reqheaders,
                            follow_redirects=False,
                            deadline=URLFETCH_REQ_MAXSECS,
                            validate_certificate=True,
                            )
                        untrunclen = hd.headers.get('content-length')
                        logging.debug('untruncated length: %r' % untrunclen)
                        if untrunclen:
                            resheaders[UNTRUNC_LEN] = untrunclen
                            untrunclen = int(untrunclen)
                    except Exception, e:
                        logging.error('Error handling HEAD request: %r' % e)
                        logging.debug(format_exc())

            if rangemethod:
                if status == 200:
                    logging.debug('Got 200 response to range request')

                    # if we added the range header and got back a 200, just
                    # return it as is. if urlfetch truncated it, we'll have to
                    # truncate it further when we check for exceeding
                    # RANGE_REQ_SIZE at the end.

                    # change to 206 if range request made by upstream requester
                    # but upstream server does not support range requests
                    # see last paragraph (re proxies) of
                    # http://tools.ietf.org/html/rfc2616#section-14.35.2
                    if not rangeadded and (not truncated or untrunclen):
                        logging.debug('Converting 200 response to 206')
                        if truncated:
                            logging.debug('Using untruncated content length (%d) as total' % untrunclen)
                            total = untrunclen
                        else:
                            total = contentlen
                        needslice = False
                        if nbytesrequested:
                            if end is None:
                                # since the length of the requested range is
                                # known, it must be of the form "-x"
                                assert start < 0, 'Expected upstream range request of the form "-x"'
                                start += contentlen
                                end = contentlen - 1
                                logging.debug('Adjusted (start, end): (%d, %d)' % (start, end))
                            needslice = contentlen != nbytesrequested
                        elif singlerange:
                            assert end is None, 'Expected upstream range request of the form "x-"'
                            end = contentlen - 1
                            logging.debug('Populated end: %d' % end)
                            if start > end:
                                logging.debug('Requested start position is beyond end position, not satisfiable')
                                resheaders['content-range'] = 'bytes */%d' % total
                                return self.error(416)
                            needslice = start != 0
                        if needslice:
                            logging.debug('Slicing content [%d:%d]' % (start, end+1))
                            # XXX cache discarded content for subsequent requests if cache policy allows
                            content = content[start:end+1]
                        res.set_status(206)
                        resheaders['content-range'] = 'bytes %d-%d/%d' % (start, end, total)
                        logging.debug('Adjusted Content-Range: %d-%d/%d' % (start, end, total))

                elif status == 206:
                    try:
                        crange = fheaders.get('content-range', '')
                        assert crange.startswith('bytes '), 'Content-Range only supported in bytes'
                        sent, total = crange[6:].split('/', 1)
                        start, end = [int(i) for i in sent.split('-', 1)]
                        total = int(total)
                    except:
                        logging.info('Error parsing Content-Range: %r' % crange)
                        logging.debug(format_exc())
                    else:
                        logging.debug('Parsed Content-Range: %d-%d/%d' % (start, end, total))

                        # if urlfetch truncated, we'll have to truncate further
                        # when we check for exceeding RANGE_REQ_SIZE at the end.
                        # we adjust content-range if necessary then.

                        # change to 200 if we converted to range request and
                        # got back entire entity in a 206
                        if not truncated and rangeadded and start == 0 and end == total - 1:
                            logging.debug('Retrieved entire entity, changing 206 to 200')
                            res.set_status(200)
                            del resheaders['content-range']

            finalcontentlen = len(content) # could have been sliced above
            if finalcontentlen > RANGE_REQ_SIZE:
                diff = finalcontentlen - RANGE_REQ_SIZE
                logging.info('Response is %d bytes, max is %d. Truncating %d bytes!' % (finalcontentlen, RANGE_REQ_SIZE, diff))
                content = content[:RANGE_REQ_SIZE]
                resheaders[TRUNC_HEADER_KEY] = '%s' % True
                if res.status == 206:
                    # adjust content-range for truncation
                    newend = end - diff
                    resheaders['content-range'] = 'bytes %d-%d/%d' % (start, newend, total)
                    logging.debug('Changed "end" from %d to %d' % (end, newend))

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
    if DEVELOPMENT:
        from wsgiref.handlers import CGIHandler
        CGIHandler().run(app)
    else:
        from google.appengine.ext.webapp.util import run_wsgi_app
        run_wsgi_app(app)

if __name__ == "__main__":
    main()
