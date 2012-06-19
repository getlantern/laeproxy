METHODS = frozenset(('delete', 'get', 'head', 'put', 'post'))
RANGE_METHODS = frozenset(('get',))
PAYLOAD_METHODS = frozenset(('put', 'post'))

# http://code.google.com/appengine/docs/python/urlfetch/overview.html#Quotas_and_Limits
URLFETCH_REQ_MAXBYTES = 1024 * 1024 * 5 # 5MB
URLFETCH_RES_MAXBYTES = 1024 * 1024 * 32
# http://code.google.com/appengine/docs/python/urlfetch/fetchfunction.html
URLFETCH_REQ_MAXSECS = 60
# http://code.google.com/appengine/docs/python/runtime.html#Quotas_and_Limits
GAE_REQ_MAXBYTES = 1024 * 1024 * 32
GAE_RES_MAXBYTES = 1024 * 1024 * 32
GAE_REQ_MAXSECS = 60

RANGE_REQ_SIZE = 2000000 # bytes. corresponds to Lantern's CHUNK_SIZE.

H_LAEPROXY_VER = 'X-laeproxy-version' # stamp responses with our version number
# absence of the following 2 headers means we responded before forwarding the request
H_UPSTREAM_SERVER = 'X-laeproxy-upstream-server'
H_UPSTREAM_STATUS_CODE = 'X-laeproxy-upstream-status-code'
H_UPSTREAM_CONTENT_RANGE = 'X-laeproxy-upstream-content-range' # if 206
H_TRUNCATED = 'X-laeproxy-truncated' # indicates urlfetch truncated response

H_LAEPROXY_RESULT = 'X-laeproxy-result' # possible results:
RETRIEVED_FROM_NET = 'Retrieved from network %s'
IGNORED_RECURSIVE = 'Ignored recursive request'
REQ_TOO_LARGE = 'Request size exceeds urlfetch limit'
MISSED_DEADLINE_URLFETCH = 'Missed urlfetch deadline'
MISSED_DEADLINE_GAE = ' Missed GAE deadline'
EXCEEDED_URLFETCH_QUOTA = 'Exceeded urlfetch quota'
UNEXPECTED_ERROR = 'Unexpected error: %r'

# remove hop-by-hop headers
# http://www.w3.org/Protocols/rfc2616/rfc2616-sec13.html#sec13.5.1
HOPBYHOP = frozenset((
    'connection',
    'keep-alive',
    'proxy-authenticate',
    'proxy-authorization',
    'te',
    'trailer',
    'transfer-encoding',
    'upgrade',
    ))
