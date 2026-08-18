#!/usr/bin/env python3
"""Expose a loopback-only host proxy to Docker bridge builds."""

import argparse
import select
import socket
import socketserver


class RelayHandler(socketserver.BaseRequestHandler):
    target_host = "127.0.0.1"
    target_port = 7890

    def handle(self):
        upstream = socket.create_connection(
            (self.target_host, self.target_port), timeout=15
        )
        try:
            sockets = [self.request, upstream]
            while True:
                readable, _, _ = select.select(sockets, [], [], 60)
                if not readable:
                    continue
                for source in readable:
                    data = source.recv(1024 * 1024)
                    if not data:
                        return
                    target = upstream if source is self.request else self.request
                    target.sendall(data)
        finally:
            upstream.close()


class RelayServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="172.18.0.1")
    parser.add_argument("--listen-port", type=int, default=17890)
    parser.add_argument("--target-host", default="127.0.0.1")
    parser.add_argument("--target-port", type=int, default=7890)
    args = parser.parse_args()
    RelayHandler.target_host = args.target_host
    RelayHandler.target_port = args.target_port
    with RelayServer((args.listen_host, args.listen_port), RelayHandler) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
