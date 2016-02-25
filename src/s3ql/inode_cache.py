'''
inode_cache.py - this file is part of S3QL.

Copyright © 2008 Nikolaus Rath <Nikolaus@rath.org>

This work can be distributed under the terms of the GNU GPLv3.
'''

from .logging import logging # Ensure use of custom logger class
from .database import NoSuchRowError
import llfuse

log = logging.getLogger(__name__)

CACHE_SIZE = 100
ATTRIBUTES = ('mode', 'refcount', 'uid', 'gid', 'size', 'locked',
              'rdev', 'atime', 'mtime', 'ctime', 'id')
ATTRIBUTE_STR = ', '.join(ATTRIBUTES)
UPDATE_ATTRS = ('mode', 'refcount', 'uid', 'gid', 'size', 'locked',
              'rdev', 'atime', 'mtime', 'ctime')
UPDATE_STR = ', '.join('%s=?' % x for x in UPDATE_ATTRS)

MAX_INODE = 2 ** 32 - 1

class _Inode:
    '''An inode with its attributes'''

    __slots__ = ATTRIBUTES + ('dirty', 'generation')

    def __init__(self, generation):
        super().__init__()
        self.dirty = False
        self.generation = generation

    def entry_attributes(self):
        attr = llfuse.EntryAttributes()
        attr.st_nlink = self.refcount
        attr.st_blocks = (self.size + 511) // 512
        attr.st_ino = self.id

        # Timeout, can effectively be infinite since attribute changes
        # are only triggered by the kernel's own requests
        attr.attr_timeout = 3600
        attr.entry_timeout = 3600

        # We want our blocksize for IO as large as possible to get large
        # write requests
        attr.st_blksize = 128 * 1024

        attr.st_mode = self.mode
        attr.st_uid = self.uid
        attr.st_gid = self.gid
        attr.st_size = self.size
        attr.st_rdev = self.rdev
        attr.st_atime_ns = int(self.atime*1e9+0.5)
        attr.st_mtime_ns = int(self.mtime*1e9+0.5)
        attr.st_ctime_ns = int(self.ctime*1e9+0.5)
        attr.generation = self.generation

        return attr

    def __eq__(self, other):
        # Ill defined - should we compare the inode id or all the attributes?
        # What does it even mean to have the same id but different attributes?
        # Maybe we should we raise an Exception in that case?
        return NotImplemented

    def __hash__(self):
        return self.id

    def copy(self):
        copy = _Inode(self.generation)

        for attr in ATTRIBUTES:
            setattr(copy, attr, getattr(self, attr))

        return copy

    def __setattr__(self, name, value):
        if name != 'dirty':
            object.__setattr__(self, 'dirty', True)
        object.__setattr__(self, name, value)


class InodeCache(object):
    '''
    This class maps the `inode` SQL table to a dict, caching the rows.

    If the cache is full and a row is not in the cache, the least-recently
    retrieved row is deleted from the cache. This means that accessing
    cached rows will *not* change the order of their expiration.

    Attributes:
    -----------
    :attrs:   inode indexed dict holding the attributes
    :cached_rows: list of the inodes that are in cache
    :pos:    position of the most recently retrieved inode in
             'cached_rows'.

    Notes
    -----

    Callers should keep in mind that the changes of the returned inode
    object will only be written to the database if the inode is still
    in the cache when its attributes are updated: it is possible for
    the caller to keep a reference to an inode when that
    inode has already been expired from the InodeCache. Modifications
    to this inode object will be lost(!).

    Callers should therefore use the returned inode objects only
    as long as they can guarantee that no other calls to InodeCache
    are made that may result in expiration of inodes from the cache.

    Moreover, the caller must make sure that he does not call
    InodeCache methods while a database transaction is active that
    may be rolled back. This would rollback database updates
    performed by InodeCache, which are generally for inodes that
    are expired from the cache and therefore *not* directly related
    to the effects of the current method call.
    '''

    def __init__(self, db, inode_gen):
        self.attrs = dict()
        self.cached_rows = list()
        self.db = db
        self.generation = inode_gen

        # Fill the cache with dummy data, so that we don't have to
        # check if the cache is full or not (it will always be full)
        for _ in range(CACHE_SIZE):
            self.cached_rows.append(None)

        self.pos = 0


    def __delitem__(self, inode):
        if self.db.execute('DELETE FROM inodes WHERE id=?', (inode,)) != 1:
            raise KeyError('No such inode')
        try:
            del self.attrs[inode]
        except KeyError:
            pass

    def __getitem__(self, id_):
        try:
            return self.attrs[id_]
        except KeyError:
            try:
                inode = self.getattr(id_)
            except NoSuchRowError:
                raise KeyError('No such inode: %d' % id_)

            old_id = self.cached_rows[self.pos]
            self.cached_rows[self.pos] = id_
            self.pos = (self.pos + 1) % CACHE_SIZE
            if old_id is not None:
                try:
                    old_inode = self.attrs[old_id]
                except KeyError:
                    # We may have deleted that inode
                    pass
                else:
                    del self.attrs[old_id]
                    self.setattr(old_inode)
            self.attrs[id_] = inode
            return inode

    def getattr(self, id_): #@ReservedAssignment
        attrs = self.db.get_row("SELECT %s FROM inodes WHERE id=? " % ATTRIBUTE_STR,
                                  (id_,))
        inode = _Inode(self.generation)

        for (i, id_) in enumerate(ATTRIBUTES):
            setattr(inode, id_, attrs[i])

        inode.dirty = False

        return inode

    def create_inode(self, **kw):

        bindings = tuple(kw[x] for x in ATTRIBUTES if x in kw)
        columns = ', '.join(x for x in ATTRIBUTES if x in kw)
        values = ', '.join('?' * len(kw))

        id_ = self.db.rowid('INSERT INTO inodes (%s) VALUES(%s)' % (columns, values),
                            bindings)
        if id_ > MAX_INODE - 1:
            self.db.execute('DELETE FROM inodes WHERE id=?', (id_,))
            raise OutOfInodesError()

        return self[id_]


    def setattr(self, inode):
        if not inode.dirty:
            return
        inode.dirty = False

        self.db.execute("UPDATE inodes SET %s WHERE id=?" % UPDATE_STR,
                        [ getattr(inode, x) for x in UPDATE_ATTRS ] + [inode.id])

    def flush_id(self, id_):
        if id_ in self.attrs:
            self.setattr(self.attrs[id_])

    def destroy(self):
        '''Flush all entries and empty cache'''

        for i in range(len(self.cached_rows)):
            id_ = self.cached_rows[i]
            self.cached_rows[i] = None
            if id_ is not None:
                try:
                    inode = self.attrs[id_]
                except KeyError:
                    # We may have deleted that inode
                    pass
                else:
                    del self.attrs[id_]
                    self.setattr(inode)

        assert len(self.attrs) == 0

    def flush(self):
        '''Flush all entries to database'''

        # We don't want to use dict.itervalues() since
        # the dict may change while we iterate
        for i in range(len(self.cached_rows)):
            id_ = self.cached_rows[i]
            if id_ is not None:
                try:
                    inode = self.attrs[id_]
                except KeyError:
                    # We may have deleted that inode
                    pass
                else:
                    self.setattr(inode)

    def __del__(self):
        if len(self.attrs) > 0:
            raise RuntimeError('InodeCache instance was destroyed without calling destroy()')



class OutOfInodesError(Exception):

    def __str__(self):
        return 'Could not find free rowid in inode table'
