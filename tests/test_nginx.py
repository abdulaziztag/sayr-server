"""Лимиты частоты в deploy/nginx-sayr.conf.

nginx в тестах не поднимается, поэтому конфиг разбирается здесь же,
а выбор location повторяет правило nginx: точное совпадение, иначе
самый длинный префикс, но регулярные выражения по порядку его
перебивают (кроме ^~). Этого хватает, чтобы поймать главные ошибки:
лимит не на той ручке, лимит на каталоге или отчётах шлюза, забытый
заголовок в скопированном location.

Синтаксис на настоящем nginx проверяется на сервере: nginx -t перед reload.
"""

import re
from pathlib import Path

import pytest

CONF = Path(__file__).resolve().parent.parent / "deploy" / "nginx-sayr.conf"


def _parse(text: str) -> list:
    tokens = re.findall(r'"[^"]*"|[{};]|[^\s{};]+', re.sub(r"#[^\n]*", "", text))

    def block(i: int) -> tuple[list, int]:
        items, args = [], []
        while i < len(tokens):
            tok = tokens[i]
            i += 1
            if tok == ";":
                items.append((args, None))
                args = []
            elif tok == "{":
                body, i = block(i)
                items.append((args, body))
                args = []
            elif tok == "}":
                return items, i
            else:
                args.append(tok)
        return items, i

    return block(0)[0]


TOP = _parse(CONF.read_text())
SERVER = next(body for args, body in TOP if args == ["server"])  # первый — https
LOCATIONS = [(args[1:], body) for args, body in SERVER if args and args[0] == "location"]


def _directives(body: list, name: str) -> list[list[str]]:
    return [args[1:] for args, _ in body if args and args[0] == name]


def _pick(uri: str) -> list:
    """Какой location nginx выберет для адреса."""
    for spec, body in LOCATIONS:
        if spec[0] == "=" and spec[1] == uri:
            return body
    prefix = None
    for spec, body in LOCATIONS:
        path = spec[-1] if spec[0] in ("^~",) or len(spec) == 1 else None
        if path and uri.startswith(path) and (prefix is None or len(path) > len(prefix[0])):
            prefix = (path, spec, body)
    if prefix and prefix[1][0] == "^~":
        return prefix[2]
    for spec, body in LOCATIONS:
        if spec[0] in ("~", "~*"):
            flags = re.IGNORECASE if spec[0] == "~*" else 0
            if re.search(spec[1], uri, flags):
                return body
    assert prefix, f"для {uri} нет location"
    return prefix[2]


def _zone(uri: str) -> str | None:
    limits = _directives(_pick(uri), "limit_req")
    if not limits:
        return None
    return dict(a.split("=", 1) for a in limits[0] if "=" in a)["zone"]


@pytest.mark.parametrize(
    ("uri", "zone"),
    [
        # Коды входа платные
        ("/api/v1/auth/request", "sayr_auth"),
        ("/api/v1/auth/verify", "sayr_auth"),
        # Отчёты шлюза идут с адресов Telegram на всех сразу
        ("/api/v1/auth/callback/telegram", None),
        # Пароли — от перебора; сама админка после входа без лимита
        ("/admin/login", "sayr_login"),
        ("/seasons/review/login", "sayr_login"),
        ("/admin/", None),
        ("/admin/place/list", None),
        # Открытые формы
        ("/report", "sayr_forms"),
        ("/android-testers", "sayr_forms"),
        ("/uz/report", None),
        # Телеметрия и нажатия — в разных зонах, чтобы одно не съедало другое
        ("/api/v1/events", "sayr_api"),
        ("/api/v1/push/devices", "sayr_api"),
        ("/api/v1/push/devices/abc123", "sayr_api"),
        ("/api/v1/places/chimgan/intents", "sayr_intents"),
        ("/api/v1/places/chimgan/pace", "sayr_intents"),
        # Каталог и всё остальное читается без лимита
        ("/api/v1/places", None),
        ("/api/v1/places/chimgan", None),
        ("/api/v1/sync", None),
        ("/media/photos/a.jpg", None),
        ("/", None),
    ],
)
def test_лимит_стоит_ровно_на_ручках_которые_жгут_деньги_или_пароли(uri, zone):
    assert _zone(uri) == zone


def test_зоны_объявлены_вне_server_и_с_префиксом_sayr():
    # Файл включается внутрь http {} рядом с чужими сайтами: зоны и map там
    # общие на всех, и совпавшее имя сломало бы соседа или нас
    zones = {}
    for args, _ in TOP:
        if args and args[0] == "limit_req_zone":
            opts = dict(a.split("=", 1) for a in args[2:] if "=" in a)
            name = opts["zone"].split(":")[0]
            assert name.startswith("sayr_"), name
            zones[name] = args[1]
    assert set(zones) == {"sayr_auth", "sayr_login", "sayr_forms", "sayr_api", "sayr_intents"}
    assert set(zones.values()) == {"$sayr_limit_key"}
    assert not _directives(SERVER, "limit_req_zone"), "limit_req_zone внутри server nginx не примет"

    for _, body in LOCATIONS:
        for args in _directives(body, "limit_req"):
            zone = dict(a.split("=", 1) for a in args if "=" in a)["zone"]
            assert zone in zones, f"limit_req ссылается на необъявленную зону {zone}"


def test_get_и_head_не_считаются():
    # Страницу входа и форму можно обновлять сколько угодно — считаем отправки
    maps = [(args, body) for args, body in TOP if args[:1] == ["map"]]
    assert [args for args, _ in maps] == [["map", "$request_method", "$sayr_limit_key"]]
    table = {args[0]: args[1] for args, _ in maps[0][1]}
    assert table == {"GET": '""', "HEAD": '""', "default": "$binary_remote_addr"}


def test_сверх_лимита_отвечает_429():
    assert _directives(SERVER, "limit_req_status") == [["429"]]


def test_скопированные_location_проксируют_как_корневой():
    # Без X-Forwarded-Proto не встанет cookie админки, без X-Real-IP
    # приложение увидит вместо людей 127.0.0.1
    def proxying(body: list) -> list:
        return sorted(args for args, _ in body if args and args[0].startswith("proxy_"))

    root = proxying(_pick("/"))
    assert ["proxy_set_header", "X-Forwarded-Proto", "$scheme"] in root
    for spec, body in LOCATIONS:
        assert proxying(body) == root, f"location {' '.join(spec)} проксирует иначе"


def test_строки_certbot_не_тронуты():
    text = CONF.read_text()
    for line in [
        "    listen [::]:443 ssl; # managed by Certbot",
        "    listen 443 ssl; # managed by Certbot",
        "    ssl_certificate /etc/letsencrypt/live/sayr.duckdns.org/fullchain.pem; # managed by Certbot",
        "    ssl_certificate_key /etc/letsencrypt/live/sayr.duckdns.org/privkey.pem; # managed by Certbot",
        "    include /etc/letsencrypt/options-ssl-nginx.conf; # managed by Certbot",
        "    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem; # managed by Certbot",
        "    return 404; # managed by Certbot",
    ]:
        assert line in text.splitlines(), line
