#!/usr/bin/env python2.7

from constants import *
from gaedriver import load_config_from_file, setup_app, teardown_app
from multiprocessing import Process
from requests import get
from unittest2 import TestCase, main
from webob import Request, Response
from wsgiref.simple_server import make_server

TEST_CONFIG_FILE = './gaedriver.conf'
GAEDRIVER_APP_TOKEN = None

MOCKSERVER_PORT = 5678
mockserver_proc = None

# whether to include tests requiring a remote server
TEST_REMOTE = True # allow passing this from command line or via config


class MockServer(object):
    '''
    Requests like::

        /<command>[/<arg1>[/<arg2>]...][?<kwarg1>=<kwval1>[&<kwarg2>=<kwval2>]...]
    
    are dispatched to handlers like::

        self._handle_<command>(request, response, *<args>, **<kwargs>)

    Handlers are expected to update the response accordingly.

    The server is run in a subprocess and is accessed when the test runner makes
    requests like::

        http://localhost:<LAEPROXY_PORT>/http/localhost:<MOCKSERVER_PORT>/<command>...

    '''

    def __call__(self, environ, start_response):
        req = Request(environ)
        path = req.path.split('/')
        assert path[1] and not path[0]
        res = Response()
        try:
            handler = getattr(self, '_handle_' + path[1])
        except AttributeError:
            res.status_int = 404
        else:
            args = path[2:]
            # XXX assumes no unquoting necessary
            kw = dict((str(k),
                True if v == 'True' else
                False if v == 'False' else
                str(v)) for k, v in req.GET.iteritems())
            handler(req, res, *args, **kw)
        return res(environ, start_response)

    def _handle_echo(self, req, res, msg=''):
        '''
        Creates a response body matching the value of the 'msg' parameter.
        '''
        res.text = unicode(msg)

    def _handle_size(self, req, res, size=URLFETCH_RES_MAXBYTES, ignore_range=False):
        '''
        Creates a dummy response body of the requested size.

        If ignore_range is False and a Range header is sent of the form
        'bytes=x-y', it will be honored.

        We don't have to bother with range requests of other forms because
        laeproxy does not accept them (tested for in
        test_unsatisfiable_ranges_rejected).
        '''
        size = int(size)
        if not ignore_range and req.range:
            try:
                # XXX removed in webob 1.2b1
                # (see http://docs.webob.org/en/latest/news.html)
                # but app engine python 2.7 runtime uses webob 1.1.1
                ranges = req.range.ranges
                assert ranges
                start, end = ranges[0][0], ranges[0][1]
                res.status_int = 206
                res.headers['content-range'] = 'bytes %d-%d/%d' % (start, end-1, size)
                total = end - start # webob uses uninclusive end so no need to add 1
                if total < size:
                    size = total
            except Exception as e:
                res.status_int = 400
                res.text = u'No size passed in via query string or Range header\n%s' % e
        res.text = u'-' * size


class LaeproxyTest(TestCase):

    def __init__(self, *args, **kw):
        TestCase.__init__(self, *args, **kw)
        self.maxDiff = None

    def setUp(self):
        self.config = load_config_from_file(TEST_CONFIG_FILE)
        self.app_root = 'http://%s/http/localhost:%d/' % (self.config.app_hostname, MOCKSERVER_PORT)

    def _make_mockserver_req(self, path, headers={}, **params):
       # always make range requests
        if 'range' not in set(i.lower() for i in headers.iterkeys()):
            headers['range'] = 'bytes=0-%d' % (RANGE_REQ_SIZE-1)
        # XXX assumes no quoting necessary
        params = '&'.join('%s=%s' % (k, v) for k, v in params.iteritems()) if params else ''
        return get(self.app_root + path + '?' + params, headers=headers)

    def test_echo(self):
        msg = 'hello'
        res = self._make_mockserver_req('echo', msg=msg)
        self.assertEquals(res.text, msg)

    def test_unsatisfiable_ranges_rejected(self):
        '''
        Tests that laeproxy rejects requests without a
        "Range: bytes=x-y" header that it can satisfy.
        '''
        UNSATISFIABLE_RANGES = (
            '',
            'garbage',
            'bytes=5-',      # open-ended
            'bytes=-5',      # tail
            'bytes=2-1',     # nonsensical
            'bytes=4-5,7-8', # multipart
            'bytes=0-%d' % RANGE_REQ_SIZE, # one byte too big
            )
        for i in UNSATISFIABLE_RANGES:
            res = self._make_mockserver_req('echo', headers={'range': i})
            self.assertEquals(res.status_code, 400)
            # laeproxy never even forwarded the request
            self.assertNotIn(H_UPSTREAM_STATUS_CODE, res.headers)

    def test_range_honoring_server(self):
        '''
        If destination server honors range headers, requesting a range up
        to RANGE_REQ_SIZE should succeed.
        '''
        size = RANGE_REQ_SIZE
        res = self._make_mockserver_req('size', size=size)
        self.assertEquals(len(res.text), size)
        self.assertEquals(res.status_code, 206)

    def test_range_ignoring_server(self):
        '''
        If destination server ignores Range headers and the requested entity
        exceeds URLFETCH_RES_MAXBYTES resulting in a truncated response,
        laeproxy signals this with a header.
        '''
        res = self._make_mockserver_req('size', size=URLFETCH_RES_MAXBYTES + 1,
            ignore_range=True, headers={'range': 'bytes=0-%d' % (RANGE_REQ_SIZE-1)})
        self.assertEquals(res.status_code, 200)
        self.assertEquals(len(res.text), URLFETCH_RES_MAXBYTES)
        self.assertIn(H_TRUNCATED, res.headers)
        self.assertEquals(res.headers[H_UPSTREAM_STATUS_CODE], '200')

    if TEST_REMOTE:
        def test_google_humanstxt(self):
            url_direct = 'http://www.google.com/humans.txt'
            url_proxied = 'http://%s/http/www.google.com/humans.txt' % self.config.app_hostname
            res_direct = get(url_direct, headers={'range': 'bytes=0-300'})
            res_proxied = get(url_proxied, headers={'range': 'bytes=0-300'})
            self.assertEquals(res_direct.text, res_proxied.text)


def start_server():
    httpd = make_server('localhost', MOCKSERVER_PORT, MockServer())
    httpd.serve_forever()

def setUpModule():
    global GAEDRIVER_APP_TOKEN, mockserver_proc
    config = load_config_from_file(TEST_CONFIG_FILE)
    # setup_app() will either deploy the app referenced in
    # config or start it with dev_appserver. The particular action
    # depends on the cluster_hostname attribute. If it points to
    # localhost (e.g., localhost:8080), dev_appserver is used. Any other
    # value will trigger a deployment.
    GAEDRIVER_APP_TOKEN = setup_app(config)
    mockserver_proc = Process(target=start_server)
    mockserver_proc.start()

def tearDownModule():
    config = load_config_from_file(TEST_CONFIG_FILE)
    # Once the tests are completed, use teardown_app() to
    # clean up. For apps started with dev_appserver, this will stop
    # the dev_appserver. For deployed apps, this is currently a NOP.
    teardown_app(config, GAEDRIVER_APP_TOKEN)
    mockserver_proc.terminate()

if __name__ == '__main__':
    main()
