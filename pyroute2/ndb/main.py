'''
NDB
===

An experimental module that may obsolete IPDB.

Examples::

    from pyroute2 import NDB
    from pprint import pprint

    ndb = NDB()
    # ...
    for line ndb.routes.csv():
        print(line)
    # ...
    for record in ndb.interfaces.summary():
        print(record)
    # ...
    pprint(ndb.interfaces['eth0'])

    # ...
    pprint(ndb.interfaces[{'system': 'localhost',
                           'IFLA_IFNAME': 'eth0'}])

Multiple sources::

    from pyroute2 import NDB
    from pyroute2 import IPRoute
    from pyroute2 import NetNS

    nl = {'localhost': IPRoute(),
          'netns0': NetNS('netns0'),
          'docker': NetNS('/var/run/docker/netns/f2d2ba3e5987')}

    ndb = NDB(nl=nl)

    # ...

    for system, source in nl.items():
        source.close()
    ndb.close()

Different DB providers. PostgreSQL access requires psycopg2 module::

    from pyroute2 import NDB

    # SQLite3 -- simple in-memory DB
    ndb = NDB(db_provider='sqlite3')

    # SQLite3 -- same as above
    ndb = NDB(db_provider='sqlite3',
              db_spec=':memory:')

    # SQLite3 -- file DB
    ndb = NDB(db_provider='sqlite3',
              db_spec='test.db')

    # PostgreSQL -- local DB
    ndb = NDB(db_provider='psycopg2',
              db_spec={'dbname': 'test'})

    # PostgreSQL -- remote DB
    ndb = NDB(db_provider='psycopg2',
              db_spec={'dbname': 'test',
                       'host': 'db1.example.com'})

Performance
-----------

\~100K routes, simple NDB start in a 2 CPU VM. Times are not absolute and
can be used only as a reference to compare DB alternatives.

SQLite3, in-memory DB, transaction size does not matter -- **ca 30 secs**::

    Command being timed: "python e3.py"
    User time (seconds): 29.39
    System time (seconds): 2.77
    Percent of CPU this job got: 105%
    Elapsed (wall clock) time (h:mm:ss or m:ss): 0:30.47
    Average shared text size (kbytes): 0
    Average unshared data size (kbytes): 0
    Average stack size (kbytes): 0
    Average total size (kbytes): 0
    Maximum resident set size (kbytes): 86840
    Average resident set size (kbytes): 0
    Major (requiring I/O) page faults: 0
    Minor (reclaiming a frame) page faults: 25083
    Voluntary context switches: 649483
    Involuntary context switches: 553
    Swaps: 0
    File system inputs: 0
    File system outputs: 32
    Socket messages sent: 0
    Socket messages received: 0
    Signals delivered: 0
    Page size (bytes): 4096
    Exit status: 0

Local PostgreSQL via UNIX socket, transaction size 10K .. 50K --
**ca 1 minute**::

    Command being timed: "python e3.py"
    User time (seconds): 30.09
    System time (seconds): 3.70
    Percent of CPU this job got: 54%
    Elapsed (wall clock) time (h:mm:ss or m:ss): 1:02.43
    Average shared text size (kbytes): 0
    Average unshared data size (kbytes): 0
    Average stack size (kbytes): 0
    Average total size (kbytes): 0
    Maximum resident set size (kbytes): 65824
    Average resident set size (kbytes): 0
    Major (requiring I/O) page faults: 0
    Minor (reclaiming a frame) page faults: 18999
    Voluntary context switches: 496725
    Involuntary context switches: 26281
    Swaps: 0
    File system inputs: 0
    File system outputs: 8
    Socket messages sent: 0
    Socket messages received: 0
    Signals delivered: 0
    Page size (bytes): 4096
    Exit status: 0

Local PostgreSQL via UNIX socket, w/o transactions -- **ca 8 minutes**::

    Command being timed: "python e3.py"
    User time (seconds): 100.40
    System time (seconds): 19.16
    Percent of CPU this job got: 24%
    Elapsed (wall clock) time (h:mm:ss or m:ss): 8:03.51
    Average shared text size (kbytes): 0
    Average unshared data size (kbytes): 0
    Average stack size (kbytes): 0
    Average total size (kbytes): 0
    Maximum resident set size (kbytes): 234680
    Average resident set size (kbytes): 0
    Major (requiring I/O) page faults: 0
    Minor (reclaiming a frame) page faults: 62016
    Voluntary context switches: 716556
    Involuntary context switches: 14259
    Swaps: 0
    File system inputs: 0
    File system outputs: 64
    Socket messages sent: 0
    Socket messages received: 0
    Signals delivered: 0
    Page size (bytes): 4096
    Exit status: 0

'''
import json
import time
import atexit
import sqlite3
import logging
import weakref
import threading
import traceback
from functools import partial
from pyroute2 import config
from pyroute2 import IPRoute
from pyroute2.netlink.nlsocket import NetlinkMixin
from pyroute2.ndb import dbschema
from pyroute2.ndb.interface import (Interface,
                                    Bridge,
                                    Vlan)
from pyroute2.ndb.address import Address
from pyroute2.ndb.route import Route
from pyroute2.ndb.neighbour import Neighbour
try:
    import queue
except ImportError:
    import Queue as queue
try:
    import psycopg2
except ImportError:
    psycopg2 = None
log = logging.getLogger(__name__)


def target_adapter(value):
    #
    # MPLS target adapter for SQLite3
    #
    return json.dumps(value)


sqlite3.register_adapter(list, target_adapter)
MAX_REPORT_LINES = 100


class ShutdownException(Exception):
    pass


class InvalidateHandlerException(Exception):
    pass


class Report(object):

    def __init__(self, generator):
        self.generator = generator

    def __iter__(self):
        return self.generator

    def __repr__(self):
        counter = 0
        ret = []
        for record in self.generator:
            ret.append(repr(record))
            counter += 1
            if counter > MAX_REPORT_LINES:
                ret.append('(...)')
                break
        return '\n'.join(ret)

    def __len__(self):
        counter = 0
        for _ in self.generator:
            counter += 1
        return counter


class View(dict):
    '''
    The View() object returns RTNL objects on demand::

        ifobj1 = ndb.interfaces['eth0']
        ifobj2 = ndb.interfaces['eth0']
        # ifobj1 != ifobj2
    '''
    classes = {'interfaces': Interface,
               'vlan': Vlan,
               'bridge': Bridge,
               'addresses': Address,
               'routes': Route,
               'neighbours': Neighbour}

    def __init__(self, ndb, table):
        self.ndb = ndb
        self.table = table

    def get(self, key, table=None):
        return self.__getitem__(key, table)

    def __getitem__(self, key, table=None):
        #
        # Construct a weakref handler for events.
        #
        # If the referent doesn't exist, raise the
        # exception to remove the handler from the
        # chain.
        #

        def wr_handler(wr, fname, *argv):
            try:
                return getattr(wr(), fname)(*argv)
            except:
                # check if the weakref became invalid
                if wr() is None:
                    raise InvalidateHandlerException()
                raise

        iclass = self.classes[table or self.table]
        ret = iclass(self, key)
        wr = weakref.ref(ret)
        self.ndb._rtnl_objects.add(wr)
        for event, fname in ret.event_map.items():
            #
            # Do not trust the implicit scope and pass the
            # weakref explicitly via partial
            #
            (self
             .ndb
             .register_handler(event,
                               partial(wr_handler, wr, fname)))

        return ret

    def __setitem__(self, key, value):
        raise NotImplementedError()

    def __delitem__(self, key):
        raise NotImplementedError()

    def keys(self):
        raise NotImplementedError()

    def items(self):
        raise NotImplementedError()

    def values(self):
        raise NotImplementedError()

    def _dump(self, match=None):
        iclass = self.classes[self.table]
        cls = iclass.msg_class or self.ndb.schema.classes[iclass.table]
        keys = self.ndb.schema.compiled[iclass.view or iclass.table]['names']
        values = []

        if isinstance(match, dict):
            spec = ' WHERE '
            conditions = []
            for key, value in match.items():
                if cls.name2nla(key) in keys:
                    key = cls.name2nla(key)
                if key not in keys:
                    raise KeyError('key %s not found' % key)
                conditions.append('rs.f_%s = %s' % (key, self.ndb.schema.plch))
                values.append(value)
            spec = ' WHERE %s' % ' AND '.join(conditions)
        else:
            spec = ''
        if iclass.dump and iclass.dump_header:
            yield iclass.dump_header
            with self.ndb.schema.db_lock:
                for stmt in iclass.dump_pre:
                    self.ndb.schema.execute(stmt)
                for record in (self
                               .ndb
                               .schema
                               .execute(iclass.dump + spec, values)):
                    yield record
                for stmt in iclass.dump_post:
                    self.ndb.schema.execute(stmt)
        else:
            yield ('target', 'tflags') + tuple([cls.nla2name(x) for x in keys])
            with self.ndb.schema.db_lock:
                for record in (self
                               .ndb
                               .schema
                               .execute('SELECT * FROM %s AS rs %s'
                                        % (iclass.view or iclass.table, spec),
                                        values)):
                    yield record

    def _csv(self, match=None, dump=None):
        if dump is None:
            dump = self._dump(match)
        for record in dump:
            row = []
            for field in record:
                if isinstance(field, int):
                    row.append('%i' % field)
                elif field is None:
                    row.append('')
                else:
                    row.append("'%s'" % field)
            yield ','.join(row)

    def _summary(self):
        iclass = self.classes[self.table]
        if iclass.summary is not None:
            if iclass.summary_header is not None:
                yield iclass.summary_header
            for record in (self
                           .ndb
                           .schema
                           .fetch(iclass.summary)):
                yield record
        else:
            header = tuple(['f_%s' % x for x in
                            ('target', ) +
                            self.ndb.schema.indices[iclass.table]])
            yield header
            key_fields = ','.join(header)
            for record in (self
                           .ndb
                           .schema
                           .fetch('SELECT %s FROM %s'
                                  % (key_fields,
                                     iclass.view or iclass.table))):
                yield record

    def csv(self, *argv, **kwarg):
        return Report(self._csv(*argv, **kwarg))

    def dump(self, *argv, **kwarg):
        return Report(self._dump(*argv, **kwarg))

    def summary(self, *argv, **kwarg):
        return Report(self._summary(*argv, **kwarg))


class Source(object):
    '''
    The RNTL source. The channel that is used to init the source
    must comply to IPRoute API, must support the async_cache. If
    the channel starts additional threads, they must be joined
    in the channel.close()

    The reason to keep two separate channels (command and async)
    is that the command channel even being subscribed to async
    events does not receive all the updates initiated by an RTNL
    API call, say, route() or address().

    Thus we need a separate channel that will receive all the events.
    '''

    def __init__(self, evq, target, channel, event=None):
        # the event queue to send events to
        self.evq = evq
        # the target id -- just in case
        self.target = target
        # RTNL API
        self.nl = channel
        self.nl.bind(async_cache=True, clone_socket=True)
        #
        self.started = event

    def start(self):
        #
        # Initial load -- enqueue the data
        #
        self.evq.put((self.target, self.nl.get_links()))
        self.evq.put((self.target, self.nl.get_addr()))
        self.evq.put((self.target, self.nl.get_neighbours()))
        self.evq.put((self.target, self.nl.get_routes()))
        if self.started is not None:
            self.evq.put((self.target, (self.started, )))

        #
        # The source thread routine -- get events from the
        # channel and forward them into the common event queue
        #
        # The routine exists on an event with error code == 104
        #
        def t(event_queue, target, channel):
            while True:
                msg = tuple(channel.get())
                if msg[0]['header']['error'] and \
                        msg[0]['header']['error'].code == 104:
                            return
                event_queue.put((target, msg))

        #
        # Start source thread
        self.th = (threading
                   .Thread(target=t,
                           args=(self.evq, self.target, self.nl),
                           name='NDB event source: %s' % (self.target)))
        self.th.start()

    def close(self):
        self.nl.close()
        self.th.join()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


class NDB(object):

    def __init__(self,
                 nl=None,
                 db_provider='sqlite3',
                 db_spec=':memory:',
                 rtnl_log=False):

        self.ctime = self.gctime = time.time()
        self.schema = None
        self._db = None
        self._dbm_thread = None
        self._dbm_ready = threading.Event()
        self._global_lock = threading.Lock()
        self._event_map = None
        self._event_queue = queue.Queue()
        #
        # fix sources prime
        if nl is None:
            self._nl = {'localhost': IPRoute()}
        elif isinstance(nl, NetlinkMixin):
            self._nl = {'localhost': nl}
        elif isinstance(nl, dict):
            self._nl = nl

        self.nl = {}
        self._db_provider = db_provider
        self._db_spec = db_spec
        self._db_rtnl_log = rtnl_log
        atexit.register(self.close)
        self._rtnl_objects = set()
        self._dbm_ready.clear()
        self._dbm_thread = threading.Thread(target=self.__dbm__,
                                            name='NDB main loop')
        self._dbm_thread.setDaemon(True)
        self._dbm_thread.start()
        self._dbm_ready.wait()
        self.interfaces = View(self, 'interfaces')
        self.addresses = View(self, 'addresses')
        self.routes = View(self, 'routes')
        self.neighbours = View(self, 'neighbours')
        self.vlans = View(self, 'vlan')
        self.bridges = View(self, 'bridge')

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def register_handler(self, event, handler):
        if event not in self._event_map:
            self._event_map[event] = []
        self._event_map[event].append(handler)

    def execute(self, *argv, **kwarg):
        return self.schema.execute(*argv, **kwarg)

    def close(self):
        with self._global_lock:
            if hasattr(atexit, 'unregister'):
                atexit.unregister(self.close)
            else:
                try:
                    atexit._exithandlers.remove((self.close, (), {}))
                except ValueError:
                    pass
            if self.schema:
                self._event_queue.put(('localhost', (ShutdownException(), )))
                for target, source in self.nl.items():
                    source.close()
                self._dbm_thread.join()
                self.schema.commit()
                self.schema.close()

    def __initdb__(self):
        with self._global_lock:
            #
            # close the current db, if opened
            if self.schema:
                self.schema.commit()
                self.schema.close()
            #
            # ACHTUNG!
            # check_same_thread=False
            #
            # Please be very careful with the DB locks!
            #
            if self._db_provider == 'sqlite3':
                self._db = sqlite3.connect(self._db_spec,
                                           check_same_thread=False)
            elif self._db_provider == 'psycopg2':
                self._db = psycopg2.connect(**self._db_spec)

            if self.schema:
                self.schema.db = self._db

    def disconnect_source(self, target, flush=True):
        '''
        Disconnect an event source from the DB. Raise KeyError if
        there is no such source.

        :param target: node name or UUID
        '''
        # close the source
        self.nl[target].close()
        del self.nl[target]
        #
        if flush:
            self.schema.flush(target)

    def connect_source(self, target, channel, event=None):
        '''
        Connect an event source to the DB. All arguments are required.

        :param target: node name or UUID, any hashable value
        :param nl: an IPRoute channel to init Source() class
        :param event: an optional Event() to send in the end

        The source connection is an async process so there should be
        a way to wain until it is registered. One can provide an Event()
        that will be set by the main NDB loop when the source is
        connected.
        '''
        #
        if event is not None:
            event.clear()
        #
        # flush the DB
        self.schema.flush(target)
        #
        # register the channel
        if target in self.nl:
            self.disconnect_source(target)
        self.nl[target] = Source(self._event_queue, target, channel, event)
        self.nl[target].start()

    def __dbm__(self):

        # init the events map
        self._event_map = event_map = {type(self._dbm_ready):
                                       [lambda t, x: x.set()]}
        event_queue = self._event_queue

        def default_handler(target, event):
            if isinstance(event, Exception):
                raise event
            logging.warning('unsupported event ignored: %s' % type(event))

        self.__initdb__()
        self.schema = dbschema.init(self._db,
                                    self._db_provider,
                                    self._db_rtnl_log,
                                    id(threading.current_thread()))
        for target, channel in self._nl.items():
            self.connect_source(target, channel, None)
        event_queue.put(('localhost', (self._dbm_ready, )))

        for (event, handlers) in self.schema.event_map.items():
            for handler in handlers:
                self.register_handler(event, handler)

        while True:
            target, events = event_queue.get()
            for event in events:
                handlers = event_map.get(event.__class__, [default_handler, ])
                for handler in tuple(handlers):
                    try:
                        handler(target, event)
                    except InvalidateHandlerException:
                        try:
                            handlers.remove(handler)
                        except:
                            log.error('could not invalidate event handler:\n%s'
                                      % traceback.format_exc())
                    except ShutdownException:
                        return
                    except:
                        log.error('could not load event:\n%s\n%s'
                                  % (event, traceback.format_exc()))
                if time.time() - self.gctime > config.gc_timeout:
                    self.gctime = time.time()
                    for wr in tuple(self._rtnl_objects):
                        if wr() is None:
                            self._rtnl_objects.remove(wr)
