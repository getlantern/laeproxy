import gaedriver
import requests
import unittest2
from multiprocessing import Process
from wsgiref.simple_server import make_server
from wsgiref.util import setup_testing_defaults

TEST_CONFIG_FILE = './gaedriver.conf'
GAEDRIVER_APP_TOKEN = None

MOCKSERVER_PORT = 5678
mockserver_proc = None

def simple_app(environ, start_response):
    setup_testing_defaults(environ)
    status = '200 OK'
    headers = [('Content-type', 'text/plain')]
    start_response(status, headers)
    return ['hello']

def start_server():
    httpd = make_server('localhost', MOCKSERVER_PORT, simple_app)
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

    def __init__(self, *args, **kw):
        unittest2.TestCase.__init__(self, *args, **kw)
        self.maxDiff = None

    def setUp(self):
        self.config = gaedriver.load_config_from_file(TEST_CONFIG_FILE)

    def test_google_humanstxt(self):
        url_direct = 'http://www.google.com/humans.txt'
        url_proxied = 'http://%s/http/www.google.com/humans.txt' % self.config.app_hostname
        res_direct = requests.get(url_direct)
        res_proxied = requests.get(url_proxied, headers=dict(range='bytes=0-2000000'))
        self.assertEquals(res_direct.text, res_proxied.text)

    def test_mock_server(self):
        url = 'http://%s/http/localhost:%d/' % (self.config.app_hostname, MOCKSERVER_PORT)
        res = requests.get(url, headers=dict(range='bytes=0-2000000'))
        self.assertEquals(res.text, 'hello')
