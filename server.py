#!/usr/bin/env python3
import http.server
import http.client
import socket
import ssl
import sys
import time
import threading
import urllib.parse

import json
import os
import re
PORT = int(os.environ.get("PORT", 8080))
RBN_HOST = "reversebeacon.net"
HASH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rbn_hash.txt")

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

def load_hash():
    try:
        return open(HASH_FILE).read().strip()
    except FileNotFoundError:
        return "ab6db5"

def save_hash(h):
    with open(HASH_FILE, "w") as f:
        f.write(h)

current_hash = load_hash()

class PersistentRBN:
    def __init__(self):
        self._lock = threading.Lock()
        self._conn = None
        self._last_used = 0
        self._cache = {}
        self._cache_lock = threading.Lock()

    def _connect(self):
        if self._conn:
            try:
                self._conn.request("HEAD", "/")
                self._conn.getresponse().read()
            except Exception:
                self._conn = None
        if not self._conn:
            self._conn = http.client.HTTPSConnection(
                RBN_HOST, timeout=10, context=ssl_ctx
            )
        self._last_used = time.time()

    def get(self, path, timeout=8):
        cache_key = path
        with self._cache_lock:
            if cache_key in self._cache:
                ts, data = self._cache[cache_key]
                if time.time() - ts < 3:
                    return data

        with self._lock:
            try:
                self._connect()
                self._conn.request("GET", path, headers={
                    "User-Agent": "Mozilla/5.0",
                    "Connection": "keep-alive"
                })
                resp = self._conn.getresponse()
                data = resp.read()
                if resp.status == 200:
                    with self._cache_lock:
                        self._cache[cache_key] = (time.time(), data)
                    return data
                elif resp.status == 400:
                    return data
                else:
                    self._conn = None
                    return None
            except Exception as e:
                self._conn = None
                print(f"[RBN] Error: {e}", flush=True)
                return None

rbn = PersistentRBN()

class DualStackHTTPServer(http.server.ThreadingHTTPServer):
    address_family = socket.AF_INET6
    allow_reuse_address = True

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()

class ProxyHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        global current_hash
        if self.path.startswith("/api/hash"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"hash": current_hash}).encode())
        elif self.path.startswith("/api/spots"):
            qs = self.path[len("/api/spots"):]
            path = "/spots.php" + qs
            print(f"[PROXY] {path}")
            sys.stdout.flush()
            data = rbn.get(path)
            if data:
                try:
                    j = json.loads(data)
                    if j.get("error") == 888 and j.get("ver_h"):
                        new_h = j["ver_h"]
                        current_hash = new_h
                        save_hash(new_h)
                        print(f"[HASH] refreshed -> {new_h}", flush=True)
                        path = "/spots.php" + re.sub(r'h=[^&]+', 'h=' + new_h, qs, count=1)
                        data = rbn.get(path)
                except (json.JSONDecodeError, KeyError):
                    pass
            if data:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(502)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Proxy error: upstream unavailable")
        elif self.path == "/" or self.path == "/index.html":
            self.path = "/spotter.html"
            super().do_GET()
        else:
            super().do_GET()

    def log_message(self, format, *args):
        print(format % args)

def keepalive():
    while True:
        time.sleep(30)
        with rbn._lock:
            if rbn._conn and time.time() - rbn._last_used > 60:
                try:
                    rbn._conn.close()
                except Exception:
                    pass
                rbn._conn = None

if __name__ == "__main__":
    threading.Thread(target=keepalive, daemon=True).start()
    server = DualStackHTTPServer(("::", PORT), ProxyHandler)
    print(f"Server running at http://localhost:{PORT}")
    server.serve_forever()
