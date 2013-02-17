import tornado.ioloop
import tornado.web
import tornado.httpserver
import tornado.httputil

import os
import sha
import sh

from celery import Celery

from storages import storages
import settings
from ns import nslib

address = settings.internal_ip
celery = Celery('tasks', broker='redis://%s:15002/0' % address)


@celery.task
def process_chunk(num, chunk_file, hash):
    datadir = settings.datadir

    chunk_size = os.stat(chunk_file).st_size

    try:
        storage_name = nslib.find_server(hash)
        nslib.set_chunk_size(hash, chunk_size)
    except nslib.NSLibException, e:
        with file(os.path.join(datadir, 'process_chunk.log'), 'a+') as f:
            f.write("Failed store chunk %s for file (%d). Message: %s\n" %
                (chunk_file, num, hash, e.message))
        return

    storage = storages[storage_name]
    storage.store_chunk(chunk_file, hash)

    nslib.chunk_ready_on_storage(hash, storage_name)

    with file(os.path.join(datadir, 'process_chunk.log'), 'a+') as f:
        f.write("Chunk saved %s in %s (%d)\n" %
            (chunk_file, storage_name, num))


@celery.task
def register_file(path, hashes):
    datadir = settings.datadir

    try:
        nslib.add_file(path, hashes)
    except nslib.FSError, e:
        with file(os.path.join(datadir, 'process_chunk.log'), 'a+') as f:
            f.write("Failed file save %s. Exception: %s\n" % (path, e.message))
        return

    with file(os.path.join(datadir, 'process_chunk.log'), 'a+') as f:
        f.write("File saved %s\n" % path)


class MainHandler(tornado.web.RequestHandler):
    # Need to implement all methods for FUSE filesystem.
    # https://github.com/terencehonles/fusepy/blob/master/examples/sftp.py

    # ======= GET ============

    @tornado.web.asynchronous
    def get(self, path):
        path = "/" + path
        if path[-1] == '/':
            try:
                files = nslib.ls(path)
            except nslib.FSError:
                raise tornado.web.HTTPError(404, "No such file or directory")

            for fl in files:
                self.write("<a href='/data%s'>%s</a><br />" % (fl, fl))
            self.finish()
            return

        try:
            chunks = zip(*nslib.get_file_chunks(path))
        except nslib.FSError:
            raise tornado.web.HTTPError(404, "No such file or directory")

        self.generator = self.write_generator(chunks)
        tornado.ioloop.IOLoop.instance().add_callback(self.write_callback)

    @tornado.web.asynchronous
    def write_callback(self):
        try:
            data = self.generator.next()
            self.write(data)
            self.flush()
            tornado.ioloop.IOLoop.instance().add_callback(self.write_callback)
        except StopIteration:
            self.finish()

    def write_generator(self, chunks):
        datadir = settings.datadir

        for chunk, server in chunks:
            data = storages[server].get_chunk(chunk)
            yield data

            with file(os.path.join(datadir, 'process_chunk.log'), 'a+') as f:
                f.write("Chunk %s received from %s (size %d)\n" %
                    (chunk, server.strip(), len(data)))

    # ======= POST ============

    def put(self, path):
        path = "/" + path

        self.path = path

        with file(os.path.join(settings.datadir, 'process_chunk.log'), 'a+') as f:
            f.write("Uploaded %s size: %d\n" %
                (path, self.request.content_length))

        self.chunks = self.request.body
        register_file.delay(self.path, self.chunks)
        self.write('Uploaded %d bytes\n' % self.request.content_length)
        self.finish()

#https://gist.github.com/joshmarshall/870216


class BodyStreamHandler(tornado.httpserver.HTTPParseBody):
    content_left = 0

    def __call__(self):
        self.content_left = self.content_length

        self.read_bytes = 0
        self.chunk_num = 0
        self.chunks = []

        with file(os.path.join(settings.datadir, 'process_chunk.log'), 'a+') as f:
            f.write("Start uploading size: %d\n" %
                (self.content_length))

        self.read_chunk()

    def read_chunk(self):
        buffer_size = settings.chunk_size
        if self.content_left < buffer_size:
            buffer_size = self.content_left
        self.stream.read_bytes(buffer_size,  self.data_callback)

    def data_callback(self, data=None):
        self.content_left -= len(data)

        hash = sha.sha(data).hexdigest()

        TMP = os.path.join(settings.tmpdir, "cache", "%s.%s.chunk" % (self.chunk_num, hash))
        sh.mkdir('-p', sh.dirname(TMP).strip())
        with file(TMP, "wb") as f:
            f.write(data)

        with file(os.path.join(settings.datadir, 'process_chunk.log'), 'a+') as f:
            f.write("Received %d (%s)\n" %
                (len(data), hash))

        self.chunks.append(hash)
        process_chunk.delay(self.chunk_num, TMP, hash)

        self.chunk_num += 1

        if self.content_left > 0:
            tornado.ioloop.IOLoop.instance().add_callback(self.read_chunk)
        else:
            self.request.body = self.chunks
            self.request.content_length = self.content_length
            self.done()
