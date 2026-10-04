"""Disk Space Map, live edition.

Run:  python server.py        then open http://127.0.0.1:8765

Scans C: and D: once, then listens to Windows file-change notifications
(ReadDirectoryChangesW) and rescans only the folders that changed.
Read-only: it never modifies anything on disk. Listens on localhost only.
"""
import os, sys, json, time, threading, shutil, ctypes, struct, queue, webbrowser
from ctypes import wintypes
from collections import deque
from concurrent.futures import ProcessPoolExecutor
import multiprocessing as mp
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
DRIVES = ['C:\\', 'D:\\']
HOST, PORT = '127.0.0.1', 8765
LEAF = 20 * 1024**2          # files this big appear as their own block in the treemap
BIG = 100 * 1024**2          # files this big appear in the "largest files" table
CLOUD_MASK = 0x400000 | 0x40000 | 0x1000   # online-only cloud placeholders
DAY = 86400
FEED_MIN = 1024**2           # changes smaller than 1 MB stay out of the activity feed

CAT_BY_NAME = {
    'node_modules': 'node_modules', '.git': 'Git history (.git)',
    'temp': 'Temp folders', 'tmp': 'Temp folders',
    'cache': 'App caches', 'caches': 'App caches', 'code cache': 'App caches', 'gpucache': 'App caches',
    'shadercache': 'App caches', 'cache_data': 'App caches', 'cachestorage': 'App caches',
    '.cache': 'App caches', 'inetcache': 'App caches', 'webcache': 'App caches',
    '.dropbox.cache': 'App caches', 'service worker': 'App caches', 'dxcache': 'App caches',
    'vaultcache': 'Epic Vault cache',
    '.gradle': 'Dev package stores', '.m2': 'Dev package stores', '.nuget': 'Dev package stores',
    'npm-cache': 'Dev package stores', '.cargo': 'Dev package stores', '.rustup': 'Dev package stores',
    'pip': 'Dev package stores', 'yarn': 'Dev package stores', 'pnpm': 'Dev package stores',
    '$recycle.bin': 'Recycle Bin', 'windows.old': 'Windows.old',
    'softwaredistribution': 'Windows Update cache',
    'crashdumps': 'Crash dumps', 'minidump': 'Crash dumps', 'livekernelreports': 'Crash dumps',
    'steamapps': 'Steam library', 'epic games': 'Epic Games library',
}


# ======================================================================
# Scanning. These functions also run inside worker processes.
# A scanned folder is a tuple:
#   (name, own_bytes, own_files, own_newest_day, own_cloud_bytes, big_files|None, category|None, children|None)
# ======================================================================
_counter = None


def _init_worker(counter):
    global _counter
    _counter = counter


def new_stats():
    return {'ext': {}, 'years': {}, 'files': 0, 'dirs': 0, 'err': 0}


def merge_stats(a, b):
    for key in ('ext', 'years'):
        for k, (s, n) in b[key].items():
            x = a[key].setdefault(k, [0, 0]); x[0] += s; x[1] += n
    for k in ('files', 'dirs', 'err'):
        a[k] += b[k]


def dir_cat(name, has_pyvenv):
    c = CAT_BY_NAME.get(name.lower())
    return c or ('Python venvs' if has_pyvenv else None)


class _Progress:
    __slots__ = ('pending',)

    def __init__(self):
        self.pending = 0

    def add(self, b):
        self.pending += b
        if self.pending > 256 * 1024**2:
            self.flush()

    def flush(self):
        if _counter is not None and self.pending:
            with _counter.get_lock():
                _counter.value += self.pending
        self.pending = 0


def read_entries(path, stats):
    """List one folder. Returns (own_bytes, own_files, newest_day, cloud_bytes, big, subdirs, has_pyvenv) or None."""
    try:
        with os.scandir(path) as it:
            entries = list(it)
    except OSError:
        if stats is not None:
            stats['err'] += 1
        return None
    own_s = own_n = own_m = own_cl = 0
    big, subdirs, pyvenv = None, [], False
    for e in entries:
        try:
            if e.is_dir(follow_symlinks=False):
                if not e.is_junction():
                    subdirs.append(e)
                continue
            if e.is_symlink():
                continue
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        size = st.st_size
        if st.st_file_attributes & CLOUD_MASK:
            own_cl += size
            continue
        own_s += size; own_n += 1
        mt = int(st.st_mtime // DAY)
        if mt > own_m:
            own_m = mt
        nm = e.name
        if nm.lower() == 'pyvenv.cfg':
            pyvenv = True
        if size >= LEAF:
            if big is None:
                big = []
            big.append((nm, size, mt))
        if stats is not None:
            dot = nm.rfind('.')
            ext = nm[dot + 1:].lower() if 0 < dot < len(nm) - 1 and len(nm) - dot <= 12 else '(none)'
            x = stats['ext'].get(ext)
            if x is None:
                stats['ext'][ext] = [size, 1]
            else:
                x[0] += size; x[1] += 1
            try:
                yr = time.gmtime(max(st.st_mtime, 0)).tm_year
            except (OverflowError, OSError, ValueError):
                yr = 1970
            y = stats['years'].get(yr)
            if y is None:
                stats['years'][yr] = [size, 1]
            else:
                y[0] += size; y[1] += 1
    if stats is not None:
        stats['files'] += own_n; stats['dirs'] += 1
    return own_s, own_n, own_m, own_cl, big, subdirs, pyvenv


def scan_tuple(path, name, stats, prog):
    r = read_entries(path, stats)
    if r is None:
        return None
    own_s, own_n, own_m, own_cl, big, subdirs, pyvenv = r
    if prog:
        prog.add(own_s)
    kids = None
    for e in subdirs:
        t = scan_tuple(e.path, e.name, stats, prog)
        if t is not None:
            if kids is None:
                kids = []
            kids.append(t)
    return (name, own_s, own_n, own_m, own_cl, big, dir_cat(name, pyvenv), kids)


def worker(path, name):
    sys.setrecursionlimit(100000)
    stats, prog = new_stats(), _Progress()
    t = scan_tuple(path, name, stats, prog)
    prog.flush()
    return t, stats


def split_here(path):
    p = path.lower().rstrip('\\')
    depth = p.count('\\')
    if p.startswith('c:\\users\\'):
        return depth < 4
    return depth < 2


# ======================================================================
# In-memory folder tree
# ======================================================================
class Dir:
    __slots__ = ('name', 'parent', 'kids', 'own_s', 'own_n', 'own_m', 'own_cl', 'big', 'cat', 's', 'n', 'm', 'cl')

    def path(self):
        parts, d = [], self
        while d is not None:
            parts.append(d.name); d = d.parent
        parts.reverse()
        return '\\'.join(parts) if len(parts) > 1 else parts[0] + '\\'


def from_tuple(t, parent):
    d = Dir()
    d.name, d.own_s, d.own_n, d.own_m, d.own_cl, d.big, d.cat, kids = t
    d.parent = parent
    d.kids = {k[0].lower(): from_tuple(k, d) for k in kids} if kids else None
    totals(d)
    return d


def totals(d):
    s, n, m, cl = d.own_s, d.own_n, d.own_m, d.own_cl
    if d.kids:
        for k in d.kids.values():
            s += k.s; n += k.n; cl += k.cl
            if k.m > m:
                m = k.m
    d.s, d.n, d.m, d.cl = s, n, m, cl


LOCK = threading.RLock()
ROOTS = {}                    # 'C:' -> Dir
DRIVE_INFO = {}               # 'C:' -> {'stats':..., 'scannedAt':...}
SCAN = {'active': False, 'done': 0, 'total': 1, 'started': 0, 'finished': 0, 'phase': ''}
DIRTY = {}                    # lowercase folder path -> real path
DIRTY_LOCK = threading.Lock()
MISSED = {d.rstrip('\\'): False for d in DRIVES}
FEED = deque(maxlen=300)
STATE = {'version': 0, 'lastChange': 0, 'watching': []}
CLIENTS = set()
CLIENTS_LOCK = threading.Lock()


def broadcast(msg):
    data = json.dumps(msg, separators=(',', ':'))
    with CLIENTS_LOCK:
        for q in list(CLIENTS):
            try:
                q.put_nowait(data)
            except queue.Full:
                pass


def find(path):
    """Deepest known folder on the way to `path` -> (Dir, parts_not_found)."""
    parts = [p for p in path.rstrip('\\').split('\\') if p]
    if not parts:
        return None, []
    d = ROOTS.get(parts[0].upper())
    if d is None:
        return None, parts
    for i, p in enumerate(parts[1:]):
        k = d.kids.get(p.lower()) if d.kids else None
        if k is None:
            return d, parts[1 + i:]
        d = k
    return d, []


# ======================================================================
# Full scan (startup, and the "Rescan everything" button)
# ======================================================================
def build_skeleton(pool, path, name, stats, main_prog):
    r = read_entries(path, stats)
    if r is None:
        return None
    own_s, own_n, own_m, own_cl, big, subdirs, pyvenv = r
    main_prog[0] += own_s
    sk = {'t': [name, own_s, own_n, own_m, own_cl, big, dir_cat(name, pyvenv)], 'subs': []}
    for e in subdirs:
        if split_here(e.path):
            s = build_skeleton(pool, e.path, e.name, stats, main_prog)
            if s:
                sk['subs'].append(s)
        else:
            sk['subs'].append(pool.submit(worker, e.path, e.name))
    return sk


def resolve(sk, stats):
    kids = []
    for s in sk['subs']:
        if isinstance(s, dict):
            t = resolve(s, stats)
        else:
            t, st = s.result()
            merge_stats(stats, st)
        if t is not None:
            kids.append(t)
    return tuple(sk['t']) + (kids or None,)


def full_scan():
    with LOCK:
        if SCAN['active']:
            return
        SCAN.update(active=True, done=0, started=time.time(), phase='Scanning')
    try:
        SCAN['total'] = sum(shutil.disk_usage(d).used for d in DRIVES)
        counter = mp.Value('d', 0.0)
        main_prog = [0]
        stop = threading.Event()

        def ticker():
            while not stop.wait(0.5):
                SCAN['done'] = counter.value + main_prog[0]
                broadcast({'type': 'scan', 'scan': scan_state()})
        threading.Thread(target=ticker, daemon=True).start()

        results = {}
        with ProcessPoolExecutor(max_workers=max(4, os.cpu_count() or 4),
                                 initializer=_init_worker, initargs=(counter,)) as pool:
            skels = []
            for d in DRIVES:
                stats = new_stats()
                skels.append((d, build_skeleton(pool, d, d.rstrip('\\'), stats, main_prog), stats))
            for d, sk, stats in skels:
                results[d.rstrip('\\')] = (resolve(sk, stats), stats)
        stop.set()
        SCAN['phase'] = 'Building index'
        SCAN['done'] = SCAN['total']
        broadcast({'type': 'scan', 'scan': scan_state()})
        new_roots = {k: from_tuple(t, None) for k, (t, st) in results.items()}
        now = time.strftime('%Y-%m-%d %H:%M')
        with LOCK:
            ROOTS.clear(); ROOTS.update(new_roots)
            for k, (t, st) in results.items():
                st['ext'] = dict(sorted(st['ext'].items(), key=lambda kv: -kv[1][0])[:400])
                DRIVE_INFO[k] = {'stats': st, 'scannedAt': now}
                MISSED[k] = False
            STATE['version'] += 1
            invalidate()
        print(f'full scan finished in {time.time() - SCAN["started"]:.0f}s', flush=True)
    finally:
        SCAN.update(active=False, finished=time.time(), phase='')
        broadcast({'type': 'scan', 'scan': scan_state()})
        broadcast({'type': 'reload'})


def scan_state():
    return {'active': SCAN['active'], 'done': SCAN['done'], 'total': SCAN['total'], 'phase': SCAN['phase'],
            'started': SCAN['started'], 'finished': SCAN['finished']}


# ======================================================================
# Live updates: listen for changes, resync only the folders involved
# ======================================================================
k32 = ctypes.WinDLL('kernel32', use_last_error=True)
k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
k32.ReadDirectoryChangesW.restype = wintypes.BOOL
k32.ReadDirectoryChangesW.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.BOOL,
                                      wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID, wintypes.LPVOID]
NOTIFY = 0x1 | 0x2 | 0x4 | 0x8 | 0x10   # file name, dir name, attributes, size, last write
INVALID_HANDLE = wintypes.HANDLE(-1).value


def watch(drive):
    letter = drive.rstrip('\\')
    h = k32.CreateFileW(drive, 0x0001, 0x7, None, 3, 0x02000000, None)
    if h is None or h == INVALID_HANDLE:
        print(f'cannot watch {drive}: error {ctypes.get_last_error()}', flush=True)
        return
    STATE['watching'].append(letter)
    buf = ctypes.create_string_buffer(1 << 20)
    got = wintypes.DWORD()
    while True:
        ok = k32.ReadDirectoryChangesW(h, buf, len(buf), True, NOTIFY, ctypes.byref(got), None, None)
        if not ok:
            time.sleep(1)
            continue
        if got.value == 0:            # buffer overflow: Windows dropped some notifications
            MISSED[letter] = True
            continue
        raw, off, batch = buf.raw, 0, {}
        while True:
            nxt, action, ln = struct.unpack_from('<III', raw, off)
            name = raw[off + 12: off + 12 + ln].decode('utf-16-le', 'replace')
            parent = os.path.dirname(drive + name)
            batch[parent.lower()] = parent
            if nxt == 0:
                break
            off += nxt
        with DIRTY_LOCK:
            DIRTY.update(batch)


def resync(d):
    """Re-read one known folder (not its subfolders), scan new subfolders, drop removed ones.
    Returns d, or d's parent if d itself no longer exists, or None if it can't be read."""
    path = d.path()
    r = read_entries(path, None)
    if r is None:
        if d.parent is not None and not os.path.exists(path):
            if d.parent.kids:
                d.parent.kids.pop(d.name.lower(), None)
                if not d.parent.kids:
                    d.parent.kids = None
            return d.parent
        return None
    own_s, own_n, own_m, own_cl, big, subdirs, pyvenv = r
    d.own_s, d.own_n, d.own_m, d.own_cl, d.big = own_s, own_n, own_m, own_cl, big
    d.cat = dir_cat(d.name, pyvenv)
    seen = set()
    for e in subdirs:
        key = e.name.lower(); seen.add(key)
        if not d.kids or key not in d.kids:
            t = scan_tuple(e.path, e.name, None, None)
            if t is not None:
                if d.kids is None:
                    d.kids = {}
                d.kids[key] = from_tuple(t, d)
    if d.kids:
        for key in [k for k in d.kids if k not in seen]:
            del d.kids[key]
        if not d.kids:
            d.kids = None
    return d


def apply_changes():
    while True:
        time.sleep(1.0)
        if SCAN['active'] or not ROOTS:
            continue
        with DIRTY_LOCK:
            if not DIRTY:
                continue
            batch = list(DIRTY.values()); DIRTY.clear()
        changed, feed = [], []
        t0 = time.time()
        with LOCK:
            targets = {}
            for p in batch:
                d, rest = find(p)
                if d is not None:
                    targets[id(d)] = d
            for d in targets.values():
                before = d.s
                p = d.path()
                top = resync(d)
                if top is None:
                    continue
                x = top
                while x is not None:          # recompute totals up to the drive root
                    totals(x); x = x.parent
                if top is d:
                    delta, size = d.s - before, d.s
                else:                          # the folder was deleted
                    delta, size = -before, 0
                if delta == 0 and top is d:
                    continue
                changed.append(p)
                if abs(delta) >= FEED_MIN:
                    feed.append({'t': time.time(), 'p': p, 'd': delta, 's': size})
            if changed:
                STATE['version'] += 1
                STATE['lastChange'] = time.time()
                invalidate()
        for f in feed:
            FEED.appendleft(f)
        if changed:
            broadcast({'type': 'change', 'paths': changed[:400], 'feed': feed, 'ms': int((time.time() - t0) * 1000)})


def ticker_state():
    while True:
        time.sleep(2)
        with LOCK:
            s = state_json()
        broadcast({'type': 'state', 'state': s})


# ======================================================================
# API payloads
# ======================================================================
def node_json(d, depth):
    o = {'n': d.name, 's': d.s, 'f': d.n, 'm': d.m}
    if d.cl:
        o['cl'] = d.cl
    if d.kids or d.big:
        o['h'] = 1
    if depth > 0:
        items = sorted(d.kids.values(), key=lambda k: -k.s)[:120] if d.kids else []
        leaves = [{'n': b[0], 's': b[1], 't': 1, 'm': b[2]} for b in (d.big or ())]
        merged = sorted(items + leaves, key=lambda x: -(x.s if isinstance(x, Dir) else x['s']))
        out = []
        for x in merged:
            size = x.s if isinstance(x, Dir) else x['s']
            if size < LEAF or len(out) >= 80:
                break
            out.append(node_json(x, depth - 1) if isinstance(x, Dir) else x)
        if out:
            o['c'] = out
    return o


def state_json():
    drives = []
    for d in DRIVES:
        k = d.rstrip('\\')
        du = shutil.disk_usage(d)
        root = ROOTS.get(k)
        info = DRIVE_INFO.get(k, {})
        st = info.get('stats', {})
        top = []
        if root and root.kids:
            top = [{'n': c.name, 's': c.s, 'h': 1} for c in root.kids.values()]
        if root and root.big:
            top += [{'n': b[0], 's': b[1], 't': 1} for b in root.big]
        top = sorted(top, key=lambda x: -x['s'])[:10]
        drives.append({'drive': k, 'total': du.total, 'used': du.used, 'free': du.free,
                       'scanned': root.s if root else 0, 'files': root.n if root else 0,
                       'cloud': root.cl if root else 0, 'errors': st.get('err', 0),
                       'scannedAt': info.get('scannedAt'), 'missed': MISSED.get(k, False), 'top': top})
    return {'drives': drives, 'scan': scan_state(), 'version': STATE['version'],
            'lastChange': STATE['lastChange'], 'watching': STATE['watching'], 'now': time.time(),
            'ready': bool(ROOTS)}


_cache = {}


def invalidate():
    _cache.clear()


def cached(key, fn):
    if key not in _cache:
        _cache[key] = fn()
    return _cache[key]


def walk_dirs():
    stack = list(ROOTS.values())
    while stack:
        d = stack.pop()
        yield d
        if d.kids:
            stack.extend(d.kids.values())


def big_files():
    out = []
    for d in walk_dirs():
        if d.big:
            base = None
            for nm, s, m in d.big:
                if s >= BIG:
                    base = base or d.path().rstrip('\\')
                    out.append([base + '\\' + nm, s, m])
    out.sort(key=lambda r: -r[1])
    return out[:3000]


def categories():
    out = []
    stack = list(ROOTS.values())
    while stack:
        d = stack.pop()
        if d.cat and d.parent is not None:
            if d.s >= 1024**2:
                out.append([d.cat, d.path(), d.s])
            continue
        if d.kids:
            stack.extend(d.kids.values())
    out.sort(key=lambda r: -r[2])
    return out


def cold_folders():
    today = int(time.time() // DAY)
    out = []
    stack = [(r, 0) for r in ROOTS.values()]
    while stack:
        d, depth = stack.pop()
        if depth >= 2 and d.s >= 2 * 1024**3 and d.m and today - d.m > 730:
            out.append({'p': d.path(), 's': d.s, 'm': d.m})
            continue
        if d.kids:
            stack.extend((k, depth + 1) for k in d.kids.values())
    out.sort(key=lambda r: -r['s'])
    return out[:30]


def types_years():
    return {k: {'ext': v['stats']['ext'], 'years': v['stats']['years'], 'files': v['stats']['files'],
                'scannedAt': v['scannedAt']} for k, v in DRIVE_INFO.items()}


# ======================================================================
# HTTP
# ======================================================================
ALLOWED_HOSTS = {f'{HOST}:{PORT}', f'localhost:{PORT}'}


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _ok_host(self):
        if self.headers.get('Host', '') not in ALLOWED_HOSTS:   # blocks DNS-rebinding pages
            self.send_error(403)
            return False
        return True

    def _json(self, obj, code=200):
        body = json.dumps(obj, separators=(',', ':'), ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._ok_host():
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ('/', '/index.html'):
            body = open(os.path.join(HERE, 'index.html'), 'rb').read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
        elif u.path == '/api/state':
            with LOCK:
                self._json(state_json())
        elif u.path == '/api/node':
            path = q.get('path', ['C:'])[0]
            with LOCK:
                d, rest = find(path)
                if d is None or rest:
                    return self._json({'error': 'not_found', 'path': path}, 404)
                self._json(node_json(d, 2))
        elif u.path == '/api/big':
            with LOCK:
                self._json(cached('big', big_files))
        elif u.path == '/api/cats':
            with LOCK:
                self._json(cached('cats', categories))
        elif u.path == '/api/cold':
            with LOCK:
                self._json(cached('cold', cold_folders))
        elif u.path == '/api/types':
            with LOCK:
                self._json(types_years())
        elif u.path == '/api/feed':
            self._json(list(FEED))
        elif u.path == '/api/events':
            self.sse()
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._ok_host():
            return
        if urlparse(self.path).path == '/api/rescan':
            if self.headers.get('X-Disk-Map') != '1':      # custom header: other websites can't send it
                return self._json({'error': 'forbidden'}, 403)
            if not SCAN['active']:
                threading.Thread(target=full_scan, daemon=True).start()
            return self._json({'ok': True})
        self.send_error(404)

    def sse(self):
        q = queue.Queue(maxsize=500)
        with CLIENTS_LOCK:
            CLIENTS.add(q)
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'keep-alive')
            self.end_headers()
            with LOCK:
                first = json.dumps({'type': 'state', 'state': state_json()})
            self.wfile.write(f'data: {first}\n\n'.encode()); self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                    self.wfile.write(f'data: {msg}\n\n'.encode())
                except queue.Empty:
                    self.wfile.write(b': ping\n\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            with CLIENTS_LOCK:
                CLIENTS.discard(q)


def main():
    sys.setrecursionlimit(100000)
    threading.stack_size(64 * 1024 * 1024)   # deep folder trees recurse while indexing
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    for d in DRIVES:
        threading.Thread(target=watch, args=(d,), daemon=True).start()
    threading.Thread(target=apply_changes, daemon=True).start()
    threading.Thread(target=ticker_state, daemon=True).start()
    threading.Thread(target=full_scan, daemon=True).start()
    url = f'http://{HOST}:{PORT}/'
    print(f'Disk Space Map is running at {url}  (Ctrl+C to stop)', flush=True)
    if '--no-browser' not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    mp.freeze_support()
    main()
