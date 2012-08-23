try:
    import cPickle as pickle        # NOQA
except ImportError:
    import pickle                   # NOQA

from django.core.cache.backends.memcached import MemcachedCache
from memcachepool.pool import ClientPool
import socket
import random
import time
import errno


# XXX using python-memcached style pickling
# but maybe we could use something else like
# json
#
# at least this makes it compatible with
# existing data
def serialize(data):
    return pickle.dumps(data)


def unserialize(data):
    return pickle.loads(data)


# XXX not sure if keeping the base BaseMemcachedCache class has anymore value
class UMemcacheCache(MemcachedCache):
    "An implementation of a cache binding using python-memcached"
    def __init__(self, server, params):
        import umemcache
        super(MemcachedCache, self).__init__(server, params,
                                         library=umemcache,
                                         value_not_found_exception=ValueError)
        # see how to pass the pool value
        self.maxsize = int(params.get('MAX_POOL_SIZE', 35))
        self.blacklist_time = int(params.get('BLACKLIST_TIME', 60))
        self.socktimeout = int(params.get('SOCKET_TIMEOUT', 4))
        self._pool = ClientPool(self._get_client, maxsize=self.maxsize)
        self._blacklist = {}

    def _pick_server(self):
        # update the blacklist
        for server, age in self._blacklist.items():
            if time.time() - age > self.blacklist_time:
                del self._blacklist[server]

        # pick a server in the list
        choices = [server for server in self._servers
                   if server not in self._blacklist]

        if choices == []:
            return None

        return random.choice(choices)

    def _blacklist_server(self, server):
        # blacklist a server
        self._blacklist[server] = time.time()

    def _get_client(self):
        server = self._pick_server()
        last_error = None

        # until my merge makes it upstream
        # see : https://github.com/esnme/ultramemcache/pull/9
        #
        # we are going to fallback on the poor's man technique
        # to define the socket timeout.
        #
        # Unfortunately, this change impacts all sockets created
        # in the interim in the same process, but that should
        # not be a problem since we'll usually set this
        # timeout to 5 seconds, which is long enough for any
        # protocol
        if hasattr(self._lib.Client, 'sock'):
            def create_client(server):
                cli = self._lib.Client(server)
                cli.sock.settimeout(self.socktimeout)
                return cli
        else:
            def create_client(client):
                old = socket.getdefaulttimeout()
                socket.setdefaulttimeout(self.socktimeout)
                try:
                    return self._lib.Client(server)
                finally:
                    socket.setdefaulttimeout(old)

        while server is not None:
            cli = create_client(server)
            try:
                cli.connect()
                return cli
            except (socket.timeout, socket.error), e:
                if not isinstance(e, socket.timeout):
                    if e.errno != errno.ECONNREFUSED:
                        # unmanaged case yet
                        raise

                # well that's embarrassing, let's blacklist this one
                # and try again
                self._blacklist_server(server)
                server = self._pick_server()
                last_error = e

        if last_error is not None:
            raise last_error
        else:
            raise socket.timeout('No server left in the pool')

    def add(self, key, value, timeout=0, version=None):
        value = serialize(value)
        key = self.make_key(key, version=version)

        with self._pool.reserve() as conn:
            return conn.add(key, value, self._get_memcache_timeout(timeout))

    def get(self, key, default=None, version=None):
        key = self.make_key(key, version=version)
        with self._pool.reserve() as conn:
            val = conn.get(key)

        if val is None:
            return default

        return unserialize(val[0])

    def set(self, key, value, timeout=0, version=None):
        value = serialize(value)
        key = self.make_key(key, version=version)
        with self._pool.reserve() as conn:
            conn.set(key, value, self._get_memcache_timeout(timeout))

    def delete(self, key, version=None):
        key = self.make_key(key, version=version)
        with self._pool.reserve() as conn:
            conn.delete(key)

    def get_many(self, keys, version=None):
        if keys == {}:
            return {}

        new_keys = map(lambda x: self.make_key(x, version=version), keys)

        ret = {}
        with self._pool.reserve() as conn:
            for key in new_keys:
                res = conn.get(key)
                if res is None:
                    continue
                ret[key] = res[0]

        if ret:
            res = {}
            m = dict(zip(new_keys, keys))

            for k, v in ret.items():
                res[m[k]] = unserialize(v)

            return res

        return ret

    def close(self, **kwargs):
        # XXX none of your business Django
        pass

    def incr(self, key, delta=1, version=None):
        key = self.make_key(key, version=version)
        try:
            with self._pool.reserve() as conn:
                val = conn.incr(key, delta)

        # python-memcache responds to incr on non-existent keys by
        # raising a ValueError, pylibmc by raising a pylibmc.NotFound
        # and Cmemcache returns None. In all cases,
        # we should raise a ValueError though.
        except self.LibraryValueNotFoundException:
            val = None
        if val is None:
            raise ValueError("Key '%s' not found" % key)
        return val

    def decr(self, key, delta=1, version=None):
        key = self.make_key(key, version=version)
        try:
            with self._pool.reserve() as conn:
                val = conn.decr(key, delta)

        # python-memcache responds to incr on non-existent keys by
        # raising a ValueError, pylibmc by raising a pylibmc.NotFound
        # and Cmemcache returns None. In all cases,
        # we should raise a ValueError though.
        except self.LibraryValueNotFoundException:
            val = None
        if val is None:
            raise ValueError("Key '%s' not found" % key)
        return val

    def set_many(self, data, timeout=0, version=None):
        safe_data = {}
        for key, value in data.items():
            key = self.make_key(key, version=version)
            safe_data[key] = serialize(value)

        with self._pool.reserve() as conn:
            for key, value in safe_data.items():
                conn.set(key, value, self._get_memcache_timeout(timeout))

    def delete_many(self, keys, version=None):
        with self._pool.reserve() as conn:
            for key in keys:
                conn.delete(self.make_key(key, version=version))

    def clear(self):
        with self._pool.reserve() as conn:
            conn._cache.flush_all()