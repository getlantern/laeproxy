#!/usr/bin/env python2.7

import gaedriver
import requests
import unittest2
from constants import *
from multiprocessing import Process
from webob import Request, Response # XXX expecting webob 1.1.1, what GAE Python 2.7 runtime uses
from wsgiref.simple_server import make_server

TEST_CONFIG_FILE = './gaedriver.conf'
GAEDRIVER_APP_TOKEN = None

MOCKSERVER_PORT = 5678
mockserver_proc = None

# whether to include tests requiring a remote server
TEST_REMOTE = True # allow passing this from command line or via config


class MockServer(object):
    '''
    Requests to the server like
        http://localhost:<LAEPROXY_PORT>/http/localhost:<MOCKSERVER_PORT>/<command>[/<args>...][?<kwarg1>=<kwval1>[&<kwarg2>=<kwval2>]...]
    
    are dispatched to handlers like
        self._handle_<command>(request, response, *<args>)

    Such handlers are expected to update the response accordingly.
    '''

    def __call__(self, environ, start_response):
        req = Request(environ)
        path = req.path.split('/')
        assert path[1] and not path[0]
        res = Response()
        getattr(self, '_handle_' + path[1])(req, res, *path[2:])
        return res(environ, start_response)

    def _handle_echo(self, req, res):
        '''
        Sets a response body matching the value of the 'msg' query string
        parameter.
        '''
        res.text = req.GET.get('msg')

    def _handle_size(self, req, res):
        '''
        Sets a dummy response body of the requested size.

        Uses the Range header (expected to be of the form 'bytes=X-Y')
        to determine the size, unless a 'size' query string parameter is
        passed, in which case it is used instead.
        '''
        size = req.GET.get('size')
        if size:
            size = int(size)
        else:
            ranges = req.range.ranges # removed in webob 1.2b1 (http://docs.webob.org/en/latest/news.html) but app engine python 2.7 runtime uses webob 1.1.1
            assert ranges
            start, end = ranges[0][0], ranges[0][1]
            size = end - start # webob uses uninclusive end so no need to add 1
            res.status_int = 206
        res.text = u'-' * size
       


class LaeproxyTest(unittest2.TestCase):

    def __init__(self, *args, **kw):
        unittest2.TestCase.__init__(self, *args, **kw)
        self.maxDiff = None

    def setUp(self):
        self.config = gaedriver.load_config_from_file(TEST_CONFIG_FILE)
        self.mock_root = 'http://%s/http/localhost:%d/' % (self.config.app_hostname, MOCKSERVER_PORT)

    def _make_mockserver_req(self, path, params={}, headers={},
            size=RANGE_REQ_SIZE):
        if 'range' not in set(i.lower() for i in headers.iterkeys()):
            headers['range'] = 'bytes=0-%d' % (size-1)
        params = '&'.join('%s=%s' % (k, v) for k, v in params.iteritems()) if params else '' # XXX assumes no quoting necessary
        return requests.get(self.mock_root + path + '?' + params, headers=headers)

    def test_echo(self):
        msg = 'hello'
        res = self._make_mockserver_req('echo', params={'msg': msg})
        self.assertEquals(res.text, msg)

    def test_invalid_range(self):
        for i in ('', 'garbage', 'bytes=0-%d' % RANGE_REQ_SIZE, # too big
                'bytes=5-', 'bytes=-5', 'bytes=2-1', 'bytes=4-5,7-8'):
            res = self._make_mockserver_req('echo', headers={'range': i})
            self.assertEquals(res.status_code, 400)
        
    def test_rangereq_matches_rangereqsize(self):
        size = RANGE_REQ_SIZE
        res = self._make_mockserver_req('size', size=size)
        self.assertEquals(len(res.text), size)
        self.assertEquals(res.status_code, 206)

    def test_range_ignoring_server(self):
        '''
        If destination server ignores Range headers and the requested entity
        exceeds URLFETCH_RES_MAXBYTES, laeproxy signals this with a header.
        '''
        res = self._make_mockserver_req('size',
            params={'size': URLFETCH_RES_MAXBYTES + 1})
        self.assertEquals(len(res.text), URLFETCH_RES_MAXBYTES)
        self.assertEquals(res.headers[H_TRUNCATED], 'true')
        self.assertEquals(res.headers[H_UPSTREAM_STATUS_CODE], '200')

    if TEST_REMOTE:
        def test_google_humanstxt(self):
            url_direct = 'http://www.google.com/humans.txt'
            url_proxied = 'http://%s/http/www.google.com/humans.txt' % self.config.app_hostname
            res_direct = requests.get(url_direct, headers={'range': 'bytes=0-300'})
            res_proxied = requests.get(url_proxied, headers={'range': 'bytes=0-300'})
            self.assertEquals(res_direct.text, res_proxied.text)


def start_server():
    httpd = make_server('localhost', MOCKSERVER_PORT, MockServer())
    httpd.serve_forever()

def setUpModule():
    global GAEDRIVER_APP_TOKEN, mockserver_proc
    config = gaedriver.load_config_from_file(TEST_CONFIG_FILE)
    # gaedriver.setup_app() will either deploy the app referenced in
    # config or start it with dev_appserver. The particular action
    # depends on the cluster_hostname attribute. If it points to
    # localhost (e.g., localhost:8080), dev_appserver is used. Any other
    # value will trigger a deployment.
    GAEDRIVER_APP_TOKEN = gaedriver.setup_app(config)
    mockserver_proc = Process(target=start_server)
    mockserver_proc.start()

def tearDownModule():
    config = gaedriver.load_config_from_file(TEST_CONFIG_FILE)
    # Once the tests are completed, use gaedriver.teardown_app() to
    # clean up. For apps started with dev_appserver, this will stop
    # the dev_appserver. For deployed apps, this is currently a NOP.
    gaedriver.teardown_app(config, GAEDRIVER_APP_TOKEN)
    mockserver_proc.terminate()

if __name__ == '__main__':
    unittest2.main()
