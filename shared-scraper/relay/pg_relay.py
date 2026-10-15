"""
Postgres TCP relay — runs on a machine that has plain internet access (no
VPN), forwarding raw TCP connections on port 5432 straight through to
Contabo's Postgres. Exists because harsh-local-01 needs ProtonVPN active
to reach IP India, but its free-tier plan force-routes ALL traffic through
the VPN tunnel with no split-tunneling, so it can't reliably reach
Contabo's Postgres directly while the VPN is on.

harsh-local-01 points PG_HOST at THIS machine's LAN IP instead of
Contabo's public IP. This relay does no protocol inspection at all — it's
a dumb byte-for-byte forward, so Postgres's own client-cert auth
(pg_hba.conf's "cert" rule) still applies exactly as if harsh-local-01
connected directly; this relay never sees any credentials in a usable
form (TLS is end-to-end between harsh-local-01 and Contabo).

Usage:
    python pg_relay.py

Runs in the foreground; Ctrl+C to stop. For a persistent setup, run this
under NSSM, Task Scheduler, or similar so it survives reboots.
"""

import socket
import threading
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 5432
CONTABO_HOST = "62.146.239.158"
CONTABO_PORT = 5432
BUFFER_SIZE = 65536


def _pump(src: socket.socket, dst: socket.socket, label: str) -> None:
    try:
        while True:
            data = src.recv(BUFFER_SIZE)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _handle_client(client_sock: socket.socket, client_addr) -> None:
    log.info("Connection from %s:%d", *client_addr)
    try:
        upstream = socket.create_connection((CONTABO_HOST, CONTABO_PORT), timeout=15)
    except OSError as exc:
        log.warning("Could not reach Contabo for %s:%d: %s", *client_addr, exc)
        client_sock.close()
        return

    t1 = threading.Thread(target=_pump, args=(client_sock, upstream, "client->contabo"), daemon=True)
    t2 = threading.Thread(target=_pump, args=(upstream, client_sock, "contabo->client"), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    client_sock.close()
    upstream.close()
    log.info("Connection from %s:%d closed", *client_addr)


def main() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((LISTEN_HOST, LISTEN_PORT))
    server.listen(50)
    log.info("Relaying %s:%d -> %s:%d", LISTEN_HOST, LISTEN_PORT, CONTABO_HOST, CONTABO_PORT)
    try:
        while True:
            client_sock, client_addr = server.accept()
            threading.Thread(target=_handle_client, args=(client_sock, client_addr), daemon=True).start()
    except KeyboardInterrupt:
        log.info("Shutting down.")
    finally:
        server.close()


if __name__ == "__main__":
    main()
