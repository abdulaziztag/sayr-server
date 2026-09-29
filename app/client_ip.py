"""Адрес отправителя — ключом для счётчиков «не чаще N в час».

Адрес берём у uvicorn: он верит X-Forwarded-For только от nginx
на 127.0.0.1 (forwarded_allow_ips по умолчанию), так что подделать его
заголовком снаружи нельзя.

IPv4 считаем поштучно, IPv6 — сетью /64. Провайдер выдаёт абоненту
как минимум /64, и внутри неё адрес меняется хоть на каждый запрос:
счётчик по точному адресу такой скрипт не заметил бы вовсе. Nginx
слушает и [::]:443, так что это не теория.
"""

import ipaddress

from fastapi import Request


def limit_key(request: Request) -> str:
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host or "?"
    if isinstance(ip, ipaddress.IPv6Address):
        # ::ffff:1.2.3.4 — это IPv4 в обёртке, и считать его надо поштучно
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.IPv6Network((ip, 64), strict=False))
    return str(ip)
