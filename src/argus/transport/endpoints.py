"""Sandbox transport endpoints (architecture: hostlet ↔ sidecar over vsock / UDS).

An Endpoint names one end of the hostlet↔SandboxAgent link, serialized to a single
string so it can cross the sandbox env boundary (the driver env whitelist). Three
schemes, one per substrate family:

- `tcp://host:port`     loopback TCP — the M1 all-in-one default (and today's
                        process-driver sandbox: agent ↔ hostlet over 127.0.0.1);
- `unix:///abs/path`    Unix domain socket — same-host control plane, no TCP
                        stack involved; used to exercise the non-TCP path
                        end-to-end without a VM;
- `vsock://cid:port`    virtio-vsock — the VM world (Firecracker/runsc-VM):
                        a guest connects back to the host on `cid=2`
                        (VMADDR_CID_HOST); the host binds a vsock listener.

URL forms accept both a bare transport scheme and an `http+` prefix so the same
string doubles as an HTTP base URL for the agent's stdlib HTTP client
(`http+unix:///path`, `http+vsock://2:PORT`). The base path (everything after
the authority) is carried separately for request routing.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Endpoint:
    """Address of one end of a sandbox transport link."""

    scheme: str                 # "tcp" | "unix" | "vsock"
    address: str                # host (tcp) | socket path (unix) | cid (vsock)
    port: int = 0

    def authority(self) -> str:
        """Authority component used for an HTTP Host header / routing."""
        if self.scheme == "unix":
            return f"unix:{self.address}"
        return f"{self.address}:{self.port}"

    def to_url(self) -> str:
        if self.scheme == "unix":
            return f"unix://{self.address}"
        return f"{self.scheme}://{self.address}:{self.port}"

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.to_url()


def parse_endpoint(text: str) -> Endpoint:
    """Parse a bare transport endpoint string (`tcp://…`, `unix://…`, `vsock://…`)."""
    scheme, _, rest = text.partition("://")
    scheme = scheme.lower()
    if scheme == "unix":
        if not rest:
            raise ValueError(f"unix endpoint needs a socket path: {text!r}")
        return Endpoint(scheme="unix", address=rest)
    if scheme in ("tcp", "vsock"):
        host, _, port = rest.rpartition(":")
        if not port or not port.isdigit():
            raise ValueError(f"endpoint needs a numeric port: {text!r}")
        return Endpoint(scheme=scheme, address=host or "127.0.0.1", port=int(port))
    raise ValueError(f"unsupported endpoint scheme: {text!r}")


def parse_url(url: str) -> tuple[Endpoint, str]:
    """Parse an HTTP base URL into (Endpoint, base_path).

    Accepts `http://host:port[/base]`, `http+unix:///path[/base]` and
    `http+vsock://cid:port[/base]`. A bare `host:port` (no scheme) defaults to
    TCP loopback, backwards-compatible with the M1 URL form.
    """
    text = url
    scheme = ""
    if "://" in text:
        scheme, _, text = text.partition("://")
    scheme = scheme.removeprefix("http+")
    if scheme in ("tcp", "http", "vsock"):
        authority, _, base = text.partition("/")
        host, _, port = authority.rpartition(":")
        if not port or not port.isdigit():
            raise ValueError(f"URL needs a numeric port: {url!r}")
        return Endpoint(
            scheme="tcp" if scheme == "http" else scheme,
            address=host or "127.0.0.1",
            port=int(port),
        ), ("/" + base if base else "")
    if scheme == "":
        # bare `host:port` authority -> TCP loopback (M1 backwards-compatible)
        host, _, port = text.partition(":")
        if not port or not port.isdigit():
            raise ValueError(f"URL needs a numeric port: {url!r}")
        return Endpoint(scheme="tcp", address=host or "127.0.0.1", port=int(port)), ""
    if scheme == "unix":
        if not text.startswith("/"):
            raise ValueError(f"URL needs a socket path: {url!r}")
        # the socket path is the whole path; agent routes are appended by callers
        return Endpoint(scheme="unix", address=text), ""
    raise ValueError(f"unsupported URL scheme: {url!r}")
