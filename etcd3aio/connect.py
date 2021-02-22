import itertools as it, collections as cs, dataclasses as dc
import os, sys, socket, time, asyncio

import etcd3aio
import grpclib.exceptions as grpc_ex


err_fmt = lambda err: f'[{err.__class__.__name__}] {err}'

def token_bucket(spec, negative_tokens=True):
	'''Spec: { interval_seconds: float | float_a/float_b }[:burst_float]
			Examples: 1/4:5 (interval=0.25s, rate=4/s, burst=5), 5, 0.5:10, 20:30.
		Expects a number of tokens (can be float, default: 1)
			and *always* subtracts these.
		Yields either None if there's enough
			tokens or delay (in seconds, float) until when there will be.'''
	try:
		try: interval, burst = spec.rsplit(':', 1)
		except (ValueError, AttributeError): interval, burst = spec, 1.0
		else: burst = float(burst)
		if isinstance(interval, str):
			try: a, b = interval.split('/', 1)
			except ValueError: interval = float(interval)
			else: interval = float(a) / float(b)
		if min(interval, burst) < 0: raise ValueError()
	except: raise ValueError('Invalid format for rate-limit: {!r}'.format(spec))
	# log.debug('tbf parameters: interval={:.1f}, burst={:.1f}', interval, burst)
	tokens, rate, ts_sync = max(0, burst - 1), interval**-1, time.monotonic()
	val = (yield) or 1
	while True:
		ts = time.monotonic()
		ts_sync, tokens = ts, min(burst, tokens + (ts - ts_sync) * rate)
		val, tokens = (None, tokens - val) if tokens >= val else\
			((val - tokens) / rate, (tokens - val) if negative_tokens else tokens)
		val = (yield val) or 1

class aio_timeout:
	'''Context that raises asyncio.TimeoutError after specified timeout.
		If err_ev is supplied, exits the context without exception and sets event instead.'''
	def __init__(self, timeout, err_ev=None):
		loop, self._err_ev = asyncio.get_running_loop(), err_ev
		self._timeout, self._timeout_call = False, loop.call_at(
			loop.time() + timeout, self._timeout_set, asyncio.current_task() )
	def _timeout_set(self, task): self._timeout = task.cancel() or True
	async def __aenter__(self): return self
	async def __aexit__(self, err_t, err, err_tb):
		if err_t is asyncio.CancelledError and self._timeout:
			if self._err_ev:
				self._err_ev.set()
				return True # suppress CancelledError
			raise asyncio.TimeoutError
		self._timeout_call.cancel()


class EtcdEndpoint(cs.namedtuple('EtcdEndpoint', 'host port family resolved')):
	__str__ = lambda s: f'<EtcdEP {s.host}:{s.port}>'

def str_part(s, sep, default=None):
	'''Splits string by separator, always returning two values.
		Separator can be empty to split on spaces. Examples:
			str_part("user@host", "<@", "root"), str_part("host:port", ":>")
			word, rest = str_part("some random words", "<<")'''
	seps = sep.strip(c := sep.strip('<>') or None)
	fill_left = seps.startswith('<')
	ss = s.split(c, 1) if fill_left else s.rsplit(c, 1)
	if len(ss) == 2: return ss
	if not fill_left: return [s, default]
	return [default, s] if seps != '<<' else [s, default]

def parse_etcd_endpoints(ep_str_iter, port_default=2379, family_default=0):
	'''Returns list of EtcdEndpoint(host, port, family, ...) tuples for later getaddrinfo calls.
		ep_str_iter can be a space/comma-separated string or an iterable of those.
		Square brackets can be used to force using IPv6 address family for endpoint.'''
	if isinstance(ep_str_iter, str): ep_str_iter = [ep_str_iter]
	ep_str_iter = it.chain.from_iterable(map(str.split, ep_str_iter))
	ep_str_iter = it.chain.from_iterable(ep.split(',') for ep in ep_str_iter)
	ep_list = list()
	for host in ep_str_iter:
		port, family = port_default, family_default
		if host.count(':') > 1: host, port = str_part(host, ']:>', port)
		else: host, port = str_part(host, ':>', port)
		if '[' in host: family = socket.AF_INET6
		ep_list.append(EtcdEndpoint(host.strip('[]'), int(port), family, False))
	return ep_list


class EtcdConnectionError(Exception): pass

class EtcdConf:
	# Keys have etcd_* prefix to make using more general compatible objects easier
	etcd_ep_list = None # list of EtcdEndpoint, e.g. from parse_etcd_endpoints
	etcd_conn_timeout = 15.0 # timeout on single connection attempt (incl. dns query)
	etcd_conn_timeout_fail = 70.0 # exit if can't connect within this time
	etcd_conn_retry_tbf = '9:5' # token bucket rate limit, retry every 9s, burst=5
	def __init__(self, **kws):
		for k,v in kws.items():
			if getattr(self, k, ...) is ...: raise KeyError(k)
			setattr(self, k, v)

async def reconnect_iter(conf, log=None):
	'''Async iterator to loops over specified etcd endpoints until
			timeout_fail to pick any live one, and yields connected client for it.
		Any grpclib StreamTerminatedError should be handled to anext() the generator.
		Does not close any produced clients - make sure to do it when stopping loop.
		Expects EtcdConf or compatible object with connection parameters.
		Raises EtcdConnectionError upon exceeding timeout_fail limit.
		Logger can be passed as "log" to send debug-level connection info msgs,
			and is expected to be able to handle new-style str.format templates.'''
	if not log:
		class NullLogger: __getattr__ = lambda s,k: (lambda *a,**k: None)
		log = NullLogger()
	loop = asyncio.get_running_loop()
	conn_retry_tb = token_bucket(conf.etcd_conn_retry_tbf)
	conn_fail_deadline = None
	ep, ep_queue, ep_timeout = None, cs.deque(), asyncio.Event()
	while True:
		# Pick next available endpoint
		if not ep:
			if not ep_queue: ep_queue.extend(conf.etcd_ep_list)
			ep = ep_queue.popleft()

		# Reconnect delay and deadline
		if not conn_fail_deadline:
			conn_fail_deadline = loop.time() + conf.etcd_conn_timeout_fail
		elif loop.time() > conn_fail_deadline:
			raise EtcdConnectionError( 'Failed to connect to any of the specified'
				f' etcd cluster instances within timeout ({conf.etcd_conn_timeout_fail:.1f}s)' )
		delay = next(conn_retry_tb)
		if delay:
			log.debug('Delay before reconnection attempt: {:.1f}s', delay)
			await asyncio.sleep(delay)

		# Total dns + connection timeout
		ep_timeout.clear()
		async with aio_timeout(conf.etcd_conn_timeout, ep_timeout):

			# etcd host can be resolved to multiple IPs, putting all but one to ep_queue front
			if not ep.resolved:
				try:
					addrinfo = await loop.getaddrinfo(
						ep.host, str(ep.port), family=ep.family,
						type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP )
					if len(addrinfo) < 0: raise RuntimeError('Empty getaddrinfo response')
				except (socket.gaierror, socket.error, RuntimeError) as err:
					log.debug('Skipping {} - dns/getaddrinfo error: {}', ep, err_fmt(err))
					ep = None
				else:
					addrinfo_ep_list = list(
						EtcdEndpoint(*sock_addr[:2], sock_af, True)
						for sock_af, sock_t, sock_p, name, sock_addr in addrinfo )
					ep = addrinfo_ep_list[0]
					ep_queue.extend(addrinfo_ep_list[1:])

			# Connection attempt
			if ep:
				log.debug('Connecting to {} (timeout={:.1f})', ep, conf.etcd_conn_timeout)
				try:
					etcd_client = etcd3aio.client(
						ep.host, ep.port, timeout=conf.etcd_conn_timeout )
					await etcd_client.status()
				except (ConnectionError, OSError) as err:
					log.debug('Skipping {} - connection failed: {}', ep, err_fmt(err))
					ep = None

		# Cycle to next endpoint in ep_queue
		if ep_timeout.is_set():
			log.debug('Connection timeout for {} [{:.1fs}]', conf.etcd_conn_timeout, ep)
			ep = None
		if not ep: continue
		conn_fail_deadline = None # reset on successful connection
		log.debug('Connected to {}', ep)

		yield etcd_client
