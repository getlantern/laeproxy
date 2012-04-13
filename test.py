"""
Standing docstring conventions:
    Liu: the lantern instance getting access at a censored network.
    _cover: the undercover laeproxy giving access at the open internet.
    freelolcats.org: the blacklisted web server at the open internet.
"""

import sys

import gaedriver
import requests
import unittest2
from multiprocessing import Process
from webob import Request, Response
from wsgiref.simple_server import make_server

import proxy


TEST_CONFIG_FILE = './gaedriver.conf'
GAEDRIVER_APP_TOKEN = None

MOCKSERVER_PORT = 5678
mockserver_proc = None

test_online = False


class MockServer(object):
    def __call__(self, environ, start_response):
        req = Request(environ)
        size = req.GET.get('size')
        if size is None:
            body = 'hello'
        else:
            print "Sending", size, "bytes!"
            body = "X" * int(size)
        resp = Response(body)
        return resp(environ, start_response)

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

    def test_mock_server(self):
        """
        Minimum test that exercises the mock server.
        """
        res = requests.get("%secho?what=hello" % self.mock_root)
        self.assertEquals(res.text, 'hello')

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

    def test_range_ignoring_server(self):
        """
        If the following are true of a request:
          - the file Liu requests is too big,
          - Liu provides a correct range in his request,
          - freelolcats.org is ignoring range.
        Then the following should happen:
          - Liu gets a 206 status code, and
          - Liu gets the right range headers and body anyway.
        """
        start = 1000
        end = 1100
        res = self._sized_request(proxy.RES_MAXBYTES + 1, 
                                  range_start=start, 
                                  range_end=end)
        self.assertEquals(res.status_code, 206)
        self.assertEquals(len(res.text), end-start+1)
        self.assertEquals(res.headers['content-range'].lower(),
                          "bytes %s-%s/%s" % (start, end, proxy.RES_MAXBYTES + 1))

    def _sized_request(self, size, range_start=0, range_end=None):
        "Utility."
        if range_end is None:
            range_end = range_start + size - 1
        url = "%s?size=%d" % (self.mock_root, size)
        return requests.get(url, headers={'Range': 'bytes=%s-%s' % (range_start, range_end)})

if __name__ == '__main__':
    unittest2.main()
