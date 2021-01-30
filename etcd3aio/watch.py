import asyncio
import logging

import grpclib

import etcd3aio.etcdrpc as etcdrpc
import etcd3aio.events as events
import etcd3aio.exceptions as exceptions
import etcd3aio.utils as utils

_log = logging.getLogger(__name__)


class Watch(object):

    def __init__(self, watch_id, iterator=None, etcd_client=None):
        self.watch_id = watch_id
        self.etcd_client = etcd_client
        self.iterator = iterator

    async def cancel(self):
        await self.etcd_client.cancel_watch(self.watch_id)

    def iterator(self):
        if self.iterator is not None:
            return self.iterator

        raise ValueError('Undefined iterator')


class Watcher(object):

    def __init__(self, watchstub, timeout=None, call_credentials=None,
                 metadata=None):
        self.timeout = timeout
        self._watch_stub = watchstub
        self._credentials = call_credentials
        self._metadata = metadata

        self._lock = asyncio.Lock()
        self._request_queue = asyncio.Queue(maxsize=10)
        self._callbacks = {}
        self._stream_task = None
        self._new_watch_cond = asyncio.Condition(lock=self._lock)
        self._new_watch = None

    async def add_callback(self, key, callback, range_end=None,  # noqa: C901
                           start_revision=None, progress_notify=False,
                           filters=None, prev_kv=False):
        rq = self._create_watch_request(key, range_end=range_end,
                                        start_revision=start_revision,
                                        progress_notify=progress_notify,
                                        filters=filters, prev_kv=prev_kv)

        async with self._lock:
            await self._setup_stream()

            # Only one create watch request can be pending at a time, so if
            # there one already, then wait for it to complete first.
            while self._new_watch:
                await self._new_watch_cond.wait()

            # Submit a create watch request.
            new_watch = _NewWatch(callback)
            new_watch_cond = None
            await self._request_queue.put(rq)
            self._new_watch = new_watch

            try:
                # Wait for the request to be completed, or timeout.
                try:
                    await asyncio.wait_for(self._new_watch_cond.wait(), self.timeout)
                    new_watch_cond = self._new_watch_cond
                except asyncio.TimeoutError:
                    raise exceptions.WatchTimedOut()

                # If the request not completed yet, then raise a timeout
                # exception.
                if new_watch.id is None and new_watch.err is None:
                    raise exceptions.WatchTimedOut()

                # Raise an exception if the watch request failed.
                if new_watch.err:
                    raise new_watch.err
            finally:
                # Wake up threads stuck on add_callback call if any.
                self._new_watch = None
                if new_watch_cond: new_watch_cond.notify_all()

            return new_watch.id

    async def cancel(self, watch_id):
        async with self._lock:
            callback = self._callbacks.pop(watch_id, None)
            if not callback:
                return

            await self._cancel_no_lock(watch_id)

    async def _setup_stream(self):
        if self._stream_task:
            return

        async def stream_task(metadata):
            try:
                async with self._watch_stub.Watch.open(metadata=metadata) as stream:
                    # Wait for first watch request to actually open http/2 stream for recv_message() task
                    await stream.send_message(await self._request_queue.get())
                    tasks = {
                        asyncio.create_task(self._request_queue.get(), name='request'),
                        asyncio.create_task(stream.recv_message(), name='response'),
                    }
                    while True:
                        try:
                            done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                            for task in done:
                                if task.get_name() == 'request':
                                    await stream.send_message(await task)
                                    tasks.add(asyncio.create_task(self._request_queue.get(), name='request'))
                                else:
                                    await self._handle_response(await task)
                                    tasks.add(asyncio.create_task(stream.recv_message(), name='response'))
                        except asyncio.CancelledError:
                            tasks = asyncio.gather(*tasks)
                            tasks.cancel()
                            await tasks
            except Exception as err:
                await self._handle_steam_termination(err)

        def stream_task_callback(task):
            if not task.cancelled() and (err := task.exception()):
                _log.error('Unhanled error in watcher stream: %s', err, exc_info=err)
            self._stream_task = None

        self._stream_task = asyncio.create_task(stream_task(self._metadata))
        self._stream_task.add_done_callback(stream_task_callback)

    async def _handle_steam_termination(self, err):
        async with self._lock:
            if self._new_watch:
                self._new_watch.err = err
                self._new_watch_cond.notify_all()

            callbacks = self._callbacks
            self._callbacks = {}

            # Rotate request queue. This way we can terminate one gRPC
            # stream and initiate another one whilst avoiding a race
            # between them over requests in the queue.
            await self._request_queue.put(None)
            self._request_queue = asyncio.Queue(maxsize=10)
        for callback in callbacks.values():
            await _safe_callback(callback, err)

    def close(self):
        """Close the streams for sending and receiving watch events."""
        if self._stream_task:
            self._stream_task.cancel()

    async def _handle_response(self, rs):
        async with self._lock:
            if rs.created:
                # If the new watch request has already expired then cancel the
                # created watch right away.
                if not self._new_watch:
                    await self._cancel_no_lock(rs.watch_id)
                    return

                if rs.compact_revision != 0:
                    self._new_watch.err = exceptions.RevisionCompactedError(
                        rs.compact_revision)
                    return

                self._callbacks[rs.watch_id] = self._new_watch.callback
                self._new_watch.id = rs.watch_id
                self._new_watch_cond.notify_all()

            callback = self._callbacks.get(rs.watch_id)

        # Ignore leftovers from canceled watches.
        if not callback:
            return

        # The watcher can be safely reused, but adding a new event
        # to indicate that the revision is already compacted
        # requires api change which would break all users of this
        # module. So, raising an exception if a watcher is still
        # alive.
        if rs.compact_revision != 0:
            err = exceptions.RevisionCompactedError(rs.compact_revision)
            await _safe_callback(callback, err)
            await self.cancel(rs.watch_id)
            return

        for event in rs.events:
            await _safe_callback(callback, events.new_event(event))

    async def _cancel_no_lock(self, watch_id):
        cancel_watch = etcdrpc.WatchCancelRequest()
        cancel_watch.watch_id = watch_id
        rq = etcdrpc.WatchRequest(cancel_request=cancel_watch)
        await self._request_queue.put(rq)

    @staticmethod
    def _create_watch_request(key, range_end=None, start_revision=None,
                              progress_notify=False, filters=None,
                              prev_kv=False):
        create_watch = etcdrpc.WatchCreateRequest()
        create_watch.key = utils.to_bytes(key)
        if range_end is not None:
            create_watch.range_end = utils.to_bytes(range_end)
        if start_revision is not None:
            create_watch.start_revision = start_revision
        if progress_notify:
            create_watch.progress_notify = progress_notify
        if filters is not None:
            create_watch.filters = filters
        if prev_kv:
            create_watch.prev_kv = prev_kv
        return etcdrpc.WatchRequest(create_request=create_watch)


class _NewWatch(object):
    def __init__(self, callback):
        self.callback = callback
        self.id = None
        self.err = None


async def _safe_callback(callback, event_or_err):
    try:
        await callback(event_or_err)

    except Exception:
        _log.exception('Watch callback failed')
