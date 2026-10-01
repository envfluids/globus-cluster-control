"""HTTPS CONNECT proxy that simulates a network outage while a flag file exists.

    python flaky_proxy.py <port> <flagfile>

While <flagfile> exists the proxy stops listening, so new connections are
refused, and open tunnels are torn down -- what a client sees when Wi-Fi drops.
"""

import os
import select
import socket
import sys
import threading
import time

PORT, FLAG = int(sys.argv[1]), sys.argv[2]


def down():
    return os.path.exists(FLAG)


def tunnel(client):
    try:
        req = b""
        while b"\r\n\r\n" not in req:
            chunk = client.recv(4096)
            if not chunk:
                return
            req += chunk
        host, port = req.split()[1].decode().rsplit(":", 1)
        upstream = socket.create_connection((host, int(port)), timeout=15)
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        socks = [client, upstream]
        while not down():
            r, _, _ = select.select(socks, [], [], 0.5)
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (upstream if s is client else client).sendall(data)
    except OSError:
        pass
    finally:
        client.close()


def listen():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", PORT))
    srv.listen(32)
    return srv


srv = listen()
while True:
    if down():
        if srv:
            srv.close()
            srv = None
        time.sleep(0.2)
        continue
    srv = srv or listen()
    if select.select([srv], [], [], 0.2)[0]:
        conn, _ = srv.accept()
        threading.Thread(target=tunnel, args=(conn,), daemon=True).start()
