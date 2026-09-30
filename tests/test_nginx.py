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


def _zones(uri: str) -> set[str]:
    return {
        dict(a.split("=", 1) for a in args if "=" in a)["zone"]
        for args in _directives(_pick(uri), "limit_req")
    }


def _status(uri: str) -> str:
    """Чем nginx ответит сверх лимита: своё у location, иначе от server."""
    own = _directives(_pick(uri), "limit_req_status")
    return (own or _directives(SERVER, "limit_req_status"))[0][0]


@pytest.mark.parametrize(
    ("uri", "zones"),
    [
        # Коды входа платные: отправка ещё и под общим потолком — от смены
        # адреса на каждый запрос
        ("/api/v1/auth/request", {"sayr_auth", "sayr_auth_all"}),
        ("/api/v1/auth/verify", {"sayr_auth"}),
        # Вход через Telegram и Apple и привязка Telegram: каждый запрос может
        # сходить к Telegram или Apple от нашего имени
        ("/api/v1/auth/telegram", {"sayr_auth"}),
        ("/api/v1/auth/apple", {"sayr_auth"}),
        ("/api/v1/me/telegram", {"sayr_auth"}),
        ("/api/v1/me", set()),
        # Отчёты шлюза идут с адресов Telegram на всех сразу
        ("/api/v1/auth/callback/telegram", set()),
        # Пароли — от перебора; сама админка после входа без лимита
        ("/admin/login", {"sayr_login"}),
        ("/seasons/review/login", {"sayr_login"}),
        ("/admin/", set()),
        ("/admin/place/list", set()),
        # Открытые формы
        ("/report", {"sayr_forms"}),
        ("/android-testers", {"sayr_forms"}),
        ("/uz/report", set()),
        # Телеметрия и нажатия — в разных зонах, чтобы одно не съедало другое
        ("/api/v1/events", {"sayr_api"}),
        ("/api/v1/push/devices", {"sayr_api"}),
        ("/api/v1/push/devices/abc123", {"sayr_api"}),
        ("/api/v1/places/chimgan/intents", {"sayr_intents"}),
        ("/api/v1/places/chimgan/pace", {"sayr_intents"}),
        # Каталог и всё остальное читается без лимита
        ("/api/v1/places", set()),
        ("/api/v1/places/chimgan", set()),
        ("/api/v1/sync", set()),
        ("/media/photos/a.jpg", set()),
        ("/", set()),
    ],
)
def test_лимит_стоит_ровно_на_ручках_которые_жгут_деньги_или_пароли(uri, zones):
    assert _zones(uri) == zones


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
    assert zones == {
        "sayr_auth": "$sayr_limit_key",
        "sayr_auth_all": "$sayr_limit_all",
        "sayr_login": "$sayr_limit_key",
        "sayr_forms": "$sayr_limit_key",
        "sayr_api": "$sayr_limit_key",
        "sayr_intents": "$sayr_limit_key",
    }
    assert not _directives(SERVER, "limit_req_zone"), "limit_req_zone внутри server nginx не примет"

    for _, body in LOCATIONS:
        for args in _directives(body, "limit_req"):
            zone = dict(a.split("=", 1) for a in args if "=" in a)["zone"]
            assert zone in zones, f"limit_req ссылается на необъявленную зону {zone}"


def test_get_и_head_не_считаются():
    # Страницу входа и форму можно обновлять сколько угодно — считаем отправки
    maps = {args[2]: body for args, body in TOP if args[:2] == ["map", "$request_method"]}
    assert set(maps) == {"$sayr_limit_key", "$sayr_limit_all"}
    tables = {var: {args[0]: args[1] for args, _ in body} for var, body in maps.items()}
    assert tables["$sayr_limit_key"] == {"GET": '""', "HEAD": '""', "default": "$binary_remote_addr"}
    # Общий потолок: ключ один на всех, но тоже только у отправок
    assert tables["$sayr_limit_all"] == {"GET": '""', "HEAD": '""', "default": "all"}


@pytest.mark.parametrize(
    "uri", ["/api/v1/auth/request", "/api/v1/auth/verify", "/admin/login", "/report"]
)
def test_людям_сверх_лимита_отвечает_429(uri):
    # Экран входа 429 понимает и говорит «слишком много попыток»
    assert _status(uri) == "429"


@pytest.mark.parametrize(
    "uri",
    [
        "/api/v1/events",
        "/api/v1/push/devices",
        "/api/v1/push/devices/abc123",
        "/api/v1/places/chimgan/intents",
        "/api/v1/places/chimgan/pace",
    ],
)
def test_телеметрии_сверх_лимита_отвечает_5xx_а_не_4xx(uri):
    # Вышедшие приложения считают любой 4xx окончательным отказом: пачку
    # событий стирают (Analytics.flush), голос за темп помечают доставленным
    # (TripOutcomeSync). На 429 телефоны за одним NAT молча теряли бы данные,
    # на 5xx очередь остаётся и уходит позже
    assert _status(uri).startswith("5"), _status(uri)


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
