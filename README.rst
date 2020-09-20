etcd3aio - etcd client for asyncio
==================================

This project is a fork of `hron/etcd3aio`_, which is based on `python-etcd3`_.

It's a python 3.8+ module that implements modern asyncio grpclib_-based client for etcd3_.

This fork has couple local fixes on top of `hron/etcd3aio`_, but does not use
testing setup and code from the source repo, as I didn't bother figuring out how
to use it, so it's probably broken here.

.. _hron/etcd3aio: https://github.com/hron/etcd3aio
.. _python-etcd3: https://github.com/kragniz/
.. _grpclib: https://github.com/vmagamedov/grpclib
.. _etcd3: https://etcd.io/docs/latest/


Basic usage
-----------

::

  import etcd3aio

  etcd = etcd3aio.client()
  await etcd.get('foo')
  await etcd.put('bar', 'doot')
  await etcd.delete('bar')

  # locks
  lock = etcd.lock('thing')
  await lock.acquire()
  # do something
  await lock.release()

  async with etcd.lock('doot-machine') as lock:
    # do something

  # transactions
  await etcd.transaction(
    compare=[
      etcd.transactions.value('/doot/testing') == 'doot',
      etcd.transactions.version('/doot/testing') > 0,
    ],
    success=[
      etcd.transactions.put('/doot/testing', 'success'),
    ],
    failure=[
      etcd.transactions.put('/doot/testing', 'failure'),
    ]
  )

  # watch key
  watch_count = 0
  events_iterator, cancel = await etcd.watch("/doot/watch")
  async for event in events_iterator:
    print(event)
    watch_count += 1
    if watch_count > 10:
      await cancel()

  # watch prefix
  watch_count = 0
  events_iterator, cancel = await etcd.watch_prefix("/doot/watch/prefix/")
  async for event in events_iterator:
    print(event)
    watch_count += 1
    if watch_count > 10:
      await cancel()

  # receive watch events via callback function
  def watch_callback(event):
    print(event)

  watch_id = await etcd.add_watch_callback("/anotherkey", watch_callback)

  # cancel watch
  await etcd.cancel_watch(watch_id)

  # receive watch events for a prefix via callback function
  def watch_callback(event):
    print(event)
