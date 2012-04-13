import gaedriver
import requests
import unittest2

TEST_CONFIG_FILE = './gaedriver.conf'
GAEDRIVER_APP_TOKEN = None

def setUpModule():
    global GAEDRIVER_APP_TOKEN
    config = gaedriver.load_config_from_file(TEST_CONFIG_FILE)
    # gaedriver.setup_app() will either deploy the app referenced in
    # config or start it with dev_appserver. The particular action
    # depends on the cluster_hostname attribute. If it points to
    # localhost (e.g., localhost:8080), dev_appserver is used. Any other
    # value will trigger a deployment.
    GAEDRIVER_APP_TOKEN = gaedriver.setup_app(config)

def tearDownModule():
    config = gaedriver.load_config_from_file(TEST_CONFIG_FILE)
    # Once the tests are completed, use gaedriver.teardown_app() to
    # clean up. For apps started with dev_appserver, this will stop
    # the dev_appserver. For deployed apps, this is currently a NOP.
    gaedriver.teardown_app(config, GAEDRIVER_APP_TOKEN)


class AppTest(unittest2.TestCase):

    def setUp(self):
        self.config = gaedriver.load_config_from_file(TEST_CONFIG_FILE)

    def test_homepage(self):
        url_direct = 'http://www.google.com/humans.txt'
        url_proxied = 'http://%s/http/www.google.com/humans.txt' % self.config.app_hostname
        res_direct = requests.get(url_direct)
        res_proxied = requests.get(url_proxied)
        self.assertEquals(res_direct.text, res_proxied.text)
