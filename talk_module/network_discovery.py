"""
Ricerca dispositivi audio nella rete.
- Client web connessi (WebSocket)
- Scan mDNS/Bonjour per servizi audio (opzionale)
- Dispositivi manuali (IP)
"""

import asyncio
import socket
from typing import Optional

# Client connessi: {client_id: {ip, name, ws_ref}}
_network_clients: dict[str, dict] = {}


def register_web_client(client_id: str, client_ip: str, ws=None) -> None:
    """Registra un client web connesso."""
    _network_clients[client_id] = {
        "ip": client_ip,
        "name": f"Dispositivo {client_ip}",
        "type": "web",
    }


def unregister_web_client(client_id: str) -> None:
    """Rimuove client disconnesso."""
    _network_clients.pop(client_id, None)


def list_network_clients() -> list[dict]:
    """Lista dispositivi di rete (client web connessi)."""
    return [
        {"type": "network", "value": f"net_{cid}", "name": info["name"], "ip": info["ip"]}
        for cid, info in _network_clients.items()
    ]


def get_client_id_from_ip(ip: str) -> Optional[str]:
    """Ritorna client_id per IP."""
    for cid, info in _network_clients.items():
        if info.get("ip") == ip:
            return cid
    return None


async def scan_subnet_for_hosts(subnet: str = None, timeout: float = 0.5) -> list[str]:
    """
    Scansiona la sottorete per host attivi (ping).
    Ritorna lista di IP che rispondono.
    """
    if not subnet:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            my_ip = s.getsockname()[0]
            s.close()
            parts = my_ip.rsplit(".", 1)
            subnet = parts[0] + ".0" if len(parts) == 2 else "192.168.1.0"
        except Exception:
            subnet = "192.168.1.0"
    base = ".".join(subnet.split(".")[:3])
    results = []
    for i in range(1, 255):
        ip = f"{base}.{i}"
        try:
            proc = await asyncio.create_subprocess_exec(
                "ping", "-c", "1", "-W", "1", ip,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=timeout)
            if proc.returncode == 0:
                results.append(ip)
        except (asyncio.TimeoutError, FileNotFoundError, OSError):
            pass
    return results
