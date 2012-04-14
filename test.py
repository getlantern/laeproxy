"""
Standing docstring conventions:
    Liu: the lantern instance getting access at a censored network.
    _cover: the undercover laeproxy giving access at the open internet.
    freelolcats.org: the blacklisted web server at the open internet.
"""

from multiprocessing import Process

import gaedriver
import requests
import unittest2
from webob import Request, Response
from wsgiref.simple_server import make_server

import proxy


TEST_CONFIG_FILE = './gaedriver.conf'
GAEDRIVER_APP_TOKEN = None

MOCKSERVER_PORT = 5678
mockserver_proc = None

test_online = False


class MockServer(object):
    """
    You call this server like this:
        http://localhost:<LAEPROXY_PORT>/http/localhost:<MOCKSERVER_PORT>/<command>[/<args>...][?<kwarg1>=<kwval1>[&<kwarg2>=<kwval2>]...]
    
    The requests are dispatched to procedures like
        self._handle_<command>(request, response, *<args>)
    Such handlers are expected to update the response as appropriate.
    """
    def __call__(self, environ, start_response):
        req = Request(environ)
        path = req.path.split('/')
        assert path[1] and not path[0]
        res = Response()
        getattr(self, "_handle_" + path[1])(req, res, *path[2:])
        return res(environ, start_response)

    def _handle_size(self, req, res, size_str):
        """
        Return an arbitrary string of the requested size.

        Range headers will be honored unless a kwarg ignore_range=True
        is passed in the request.
        """
        ignore_range = req.GET.get('ignore_range', 'False')
        assert ignore_range in ['True', 'False']
        if ignore_range == 'True':
            length = int(size_str)
        else:
            length = req.range.end + 1 - req.range.start
        res.text = u"X" * length

    def _handle_echo(self, req, res, *what):
        "Return the string that makes the rest of the path."
        res.text = u"/".join(map(unicode, what))
       
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


class AppTest(unittest2.TestCase):

    def setUp(self):
        self.config = gaedriver.load_config_from_file(TEST_CONFIG_FILE)
        self.mock_root = 'http://%s/http/localhost:%d/' % (self.config.app_hostname, MOCKSERVER_PORT)

    if test_online:
        def test_google_humanstxt(self):
            """
            It's good to have some test to a nonlocal server.
            """
            url_direct = 'http://www.google.com/humans.txt'
            url_proxied = 'http://%s/http/www.google.com/humans.txt' % self.config.app_hostname
            res_direct = requests.get(url_direct)
            res_proxied = requests.get(url_proxied)
            self.assertEquals(res_direct.text, res_proxied.text)

    def test_echo(self):
        """
        Minimum test that exercises the mock server.
        """
        res = requests.get("%secho/hello" % self.mock_root)
        self.assertEquals(res.text, 'hello')
        res = requests.get("%secho/world" % self.mock_root)
        self.assertEquals(res.text, 'world')

    def test_just_below_lae_response_limit(self):
        """
        A Liu request with a range just below _cover's own
        RES_MAXBYTES should just succeed.
        """
        res = self._sized_request(proxy.RES_MAXBYTES)
        self.assertEquals(len(res.text), proxy.RES_MAXBYTES)

    def test_just_above_lae_response_limit(self):
        """
        A Liu request with a range just above _cover's own
        RES_MAXBYTES should just fail.
        """
        res = self._sized_request(proxy.RES_MAXBYTES + 1)
        self.assertEquals(res.status_code, 503)

    def test_range_honoring_server(self):
        """
        If the following are true of a request:
          - the file Liu requests is too big,
          - Liu provides a correct range in his request,
          - freelolcats.org HONORS range.
        Then the following should happen:
          - Liu gets a 206 status code, and
          - Liu gets the right range headers and body.
        """
        #XXX: This test fails at this moment, because the mock server is not
        #     sending proper content-length headers.  Now, _cover is
        #     populating content-length in the case where freelolcats.org
        #     ignores range requests, so should it do the same here?
        res = self._test_range(ignore_range='False')
        self.assertEquals(res.headers[proxy.UPSTREAM_206], 'True')

    def test_range_ignoring_server(self):
        """
        If freelolcats.org ignores range, the consequences for Liu
        are the same as if it was supported, with the exception that
        a laeproxy-specific header will signal this condition.
        """
        res = self._test_range(ignore_range='True')
        self.assertEquals(res.headers[proxy.UPSTREAM_206], 'False')

    # Utility methods.

    def _test_range(self, ignore_range):
        start = 1000
        end = 1100
        res = self._sized_request(proxy.RES_MAXBYTES + 1, 
                                  range_start=start, 
                                  range_end=end,
                                  extra=dict(ignore_range=ignore_range))
        self.assertEquals(res.status_code, 206)
        self.assertEquals(len(res.text), end-start+1)
        self.assertEquals(res.headers['content-range'].lower(),
                          "bytes %s-%s/%s" % (start, end, proxy.RES_MAXBYTES + 1))
        return res

    def _sized_request(self, size, range_start=0, range_end=None, extra=None):
        if range_end is None:
            range_end = range_start + size - 1
        url = "%ssize/%d" % (self.mock_root, size)
        if extra:
            url += "?" + "&".join("%s=%s" % (k, v) for k, v in extra.iteritems())
        return requests.get(url, headers={'Range': 'bytes=%s-%s' % (range_start, range_end)})

if __name__ == '__main__':
    unittest2.main()
