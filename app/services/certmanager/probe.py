"""Low-cost controller liveness probe; never imports the ACME/FastAPI stack."""
import json
import socket
import sys


def main(*, require_https=False):
    body = b'{"action":"status"}'
    request = (b"POST /control HTTP/1.1\r\nHost: certificate\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: "
               + str(len(body)).encode()+b"\r\n\r\n"+body)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(4)
        stream.connect("/run/frontiercloud-certificate/control.sock")
        stream.sendall(request)
        response = bytearray()
        while chunk := stream.recv(16384):
            response.extend(chunk)
            if len(response) > 131072:
                raise RuntimeError("certificate health response too large")
    headers, payload = response.split(b"\r\n\r\n", 1)
    status = json.loads(payload)
    if not headers.startswith(b"HTTP/1.1 200") or not status.get("controller_running"):
        raise RuntimeError("certificate controller unavailable")
    if require_https and not (status.get("https_ready") and status.get("serving_matches")):
        raise RuntimeError("certificate controller is live; HTTPS is still pending or unverified")


if __name__ == "__main__":
    if sys.argv[1:] not in ([], ["--https-ready"]):
        raise SystemExit("certificate probe only accepts --https-ready")
    main(require_https=bool(sys.argv[1:]))
