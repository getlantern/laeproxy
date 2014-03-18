# Lantern App Engine Proxy

Free proxy anyone can deploy to App Engine for use with Lantern desktop
clients.


## Overview

laeproxy is a proxy designed to run on Google App Engine. To work within GAE's
limits, it only accepts requests within a certain size, and in the case of GET
requests, for content within a certain size (via the Range header). The local
proxy in Lantern desktop clients has baked-in support for this,
automatically converting regular GET requests from the browser into one or more
range requests to laeproxy, whose responses it combines into a single response
back to the browser.


## Getting Started

Install the [App Engine Python SDK](https://developers.google.com/appengine/downloads#Google_App_Engine_SDK_for_Python)
(e.g. `brew install google-app-engine`).

Clone laeproxy:

    git clone git://github.com/getlantern/laeproxy.git

Run from App Engine's development server:

    cd laeproxy
    dev_appserver.py .

Make a test request:

    curl -H'Range: bytes=0-300' -v localhost:8080/http/www.google.com/humans.txt
    ...
    < HTTP/1.1 206 Partial Content
    < Server: Development/1.0
    < Date: Wed, 30 Jan 2013 06:46:36 GMT
    < X-laeproxy-result: Retrieved from network 2013-01-30 06:46:36.328209
    < X-laeproxy-upstream-status-code: 206
    < X-laeproxy-upstream-server: sffe
    ...
    <
    Google is built by a large team of engineers, designers, researchers...
    

## Running tests

Install the requirements for running the functional tests:

    sudo pip install unittest2 gaedriver multiprocessing webob==1.1.1

Configure gaedriver.conf appropriately,
make sure laeproxy is running locally if you're testing it in the dev\_appserver,
and then run `./test.py`.


## Further Reading

- https://github.com/getlantern/lantern#readme
