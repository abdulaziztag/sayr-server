"""Выкатка: deploy/update.sh, всё, что до рестарта.

Скрипт гоняется настоящим bash против настоящего git: «GitHub» — голый
репозиторий во временной папке, до него git ходит через заглушку ssh.
uv, alembic, pg_dump, systemctl и curl — заглушки: пишут, как их позвали
и на каком коммите стоял каталог, и падают, когда велит STUB_*.

Главное свойство: упало что угодно до рестарта — каталог и .venv
возвращаются на прежний коммит. Иначе служба крутится на старом коде,
а первый же рестарт поднял бы новый код на старой схеме.

Проверку «запускать от root» тест вырезает из копии скрипта: EUID в bash
не подделать, а тест от root не гоняют.
"""

import fcntl
import os
import pty
import select
import shutil
import signal
import subprocess
import termios
import time
from pathlib import Path

import pytest

UPDATE_SH = Path(__file__).resolve().parent.parent / "deploy" / "update.sh"
ROOT_CHECK = '[[ $EUID -eq 0 ]] || die "запускать от root" 2'

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None, reason="нет bash или git"
)

STUBS = {
    # git по ssh: последний аргумент — команда «той стороны» с путём
    # к репозиторию на GitHub; подменяем путь на голый репозиторий
    "ssh": """#!/bin/sh
for last; do :; done
cmd=${last%% *}
exec git "${cmd#git-}" "$STUB_BARE"
""",
    # С STUB_UV_HOLD sync ставит метку sync-<коммит> и ждёт файла go:
    # тест сам решает, когда sync закончится, без гонки со временем.
    # Ctrl-C ожидание обрывает, как оборвал бы настоящий uv. Через trap —
    # наверняка: без него sh, чей sleep сигнал разминул, счёл бы Ctrl-C
    # обработанным и ждал дальше. Если INT на входе игнорировался (возврат
    # в update.sh), trap его не включит — так велит POSIX
    "uv": """#!/bin/sh
head=$(git rev-parse HEAD)
echo "uv $* @ $head" >>"$STUB_LOG"
[ "$1" = sync ] || exit 0
if [ -n "${STUB_UV_HOLD:-}" ]; then
  trap 'exit 130' INT
  : >"$STUB_UV_HOLD/sync-$head"
  until [ -e "$STUB_UV_HOLD/go" ]; do sleep 0.05; done
fi
if [ "$head" = "${STUB_UV_FAIL_ON:-}" ]; then
  echo "error: не собралось" >&2
  exit 1
fi
echo "synced @ $head" >>"$STUB_LOG"
""",
    "sudo": """#!/bin/sh
while [ $# -gt 0 ]; do
  case $1 in -n) shift ;; -u) shift 2 ;; *) break ;; esac
done
exec "$@"
""",
    "pg_dump": r"""#!/bin/sh
echo "pg_dump $*" >>"$STUB_LOG"
case ${STUB_PG_DUMP:-ok} in
  ok) printf -- '-- dump of %s\n\n--\n-- PostgreSQL database dump complete\n--\n\n\\unrestrict abc\n\n' "$1" ;;
  empty) ;;
  fail) echo "pg_dump: error: connection failed" >&2; exit 1 ;;
esac
""",
    "systemctl": """#!/bin/sh
echo "systemctl $*" >>"$STUB_LOG"
""",
    "journalctl": "#!/bin/sh\n",
    "flock": "#!/bin/sh\n",
    "curl": """#!/bin/sh
exit ${STUB_CURL_RC:-0}
""",
    # Скрипт зовёт его по полному пути из .venv — туда кладётся ссылка
    "alembic": """#!/bin/sh
echo "alembic $* @ $(git rev-parse HEAD)" >>"$STUB_LOG"
[ -z "${STUB_ALEMBIC_FAIL:-}" ] || { echo "миграция упала" >&2; exit 1; }
""",
}


def _git(*args: str, cwd: Path, env: dict) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture(scope="module")
def bin_dir(tmp_path_factory):
    # Одни на модуль: macOS проверяет каждый новый исполняемый файл
    # при первом запуске, и свежие заглушки на каждый тест стоили бы секунды
    path = tmp_path_factory.mktemp("bin")
    for name, body in STUBS.items():
        (path / name).write_text(body)
        (path / name).chmod(0o755)
    return path


@pytest.fixture
def server(tmp_path, bin_dir):
    """Боевой сервер в миниатюре: каталог на C1, на «GitHub» уже C2."""
    script = tmp_path / "sayr-update"
    source = UPDATE_SH.read_text()
    assert source.count(ROOT_CHECK) == 1, "проверка root переехала — поправьте тест"
    script.write_text(source.replace(ROOT_CHECK, ": # в тесте не root"))

    log = tmp_path / "calls.log"
    log.touch()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        # Чужие настройки git (подпись коммитов, insteadOf) тесту не нужны
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "STUB_LOG": str(log),
        "STUB_BARE": str(tmp_path / "github.git"),
    }

    _git("init", "-q", "--bare", "-b", "main", str(tmp_path / "github.git"), cwd=tmp_path, env=env)
    src = tmp_path / "src"
    _git("init", "-q", "-b", "main", str(src), cwd=tmp_path, env=env)
    (src / ".gitignore").write_text(".env\n.venv/\n")
    (src / "app.txt").write_text("v1\n")
    _git("add", ".", cwd=src, env=env)
    _git("commit", "-q", "-m", "c1", cwd=src, env=env)
    _git("push", "-q", str(tmp_path / "github.git"), "main", cwd=src, env=env)
    c1 = _git("rev-parse", "HEAD", cwd=src, env=env)

    app = tmp_path / "app"
    _git("clone", "-q", str(tmp_path / "github.git"), str(app), cwd=tmp_path, env=env)
    _git("remote", "set-url", "origin", "git@github.com:abdulaziztag/sayr-server.git", cwd=app, env=env)
    (app / ".env").write_text("SAYR_ADMIN_PASSWORD=x\n")
    (app / ".venv" / "bin").mkdir(parents=True)
    (app / ".venv" / "bin" / "alembic").symlink_to(bin_dir / "alembic")

    (src / "app.txt").write_text("v2\n")
    _git("commit", "-q", "-am", "c2", cwd=src, env=env)
    _git("push", "-q", str(tmp_path / "github.git"), "main", cwd=src, env=env)
    c2 = _git("rev-parse", "HEAD", cwd=src, env=env)

    (tmp_path / "deploy_key").write_text("")
    (tmp_path / "known_hosts").write_text("")
    hold = tmp_path / "hold"
    hold.mkdir()
    env.update({
        "SAYR_APP_DIR": str(app),
        "SAYR_UV": str(bin_dir / "uv"),
        "SAYR_LOCK": str(tmp_path / "deploy.lock"),
        "SAYR_KEY": str(tmp_path / "deploy_key"),
        "SAYR_KNOWN_HOSTS": str(tmp_path / "known_hosts"),
        "SAYR_DUMP_DIR": str(tmp_path / "dumps"),
        "SAYR_RESTORE_LOG": str(tmp_path / "restore.log"),
        "SAYR_HEALTH_TRIES": "2",
        "SAYR_HEALTH_DELAY": "0",
    })

    class Server:
        dumps = tmp_path / "dumps"

        def __init__(self):
            self.c1, self.c2 = c1, c2

        def command(self, **extra: str):
            return ["bash", str(script)], {**env, **extra}

        def held_command(self):
            """Команда, чей uv sync ждёт release()."""
            return self.command(STUB_UV_HOLD=str(hold))

        def sync_started(self, commit: str) -> bool:
            return (hold / f"sync-{commit}").exists()

        def release(self) -> None:
            (hold / "go").touch()

        def run(self, **extra: str) -> subprocess.CompletedProcess:
            args, full = self.command(**extra)
            return subprocess.run(args, env=full, capture_output=True, text=True, timeout=WAIT_LIMIT)

        def head(self) -> str:
            return _git("rev-parse", "HEAD", cwd=app, env=env)

        def calls(self, prefix: str) -> list[str]:
            return [c for c in log.read_text().splitlines() if c.startswith(prefix)]

    srv = Server()
    yield srv
    # Упавший на полпути тест не оставит висеть скрипт в ожидании
    srv.release()


def test_удачный_деплой_переключает_код_и_перезапускает(server):
    run = server.run()
    assert run.returncode == 0, run.stderr
    assert server.head() == server.c2
    assert server.calls("alembic") == [f"alembic upgrade head @ {server.c2}"]
    assert "systemctl restart sayr" in server.calls("systemctl")


def test_упавший_uv_sync_возвращает_каталог_на_прежний_коммит(server):
    run = server.run(STUB_UV_FAIL_ON=server.c2)
    assert run.returncode != 0
    assert server.head() == server.c1, "каталог остался на новом коде"
    syncs = [c for c in server.calls("uv sync")]
    assert syncs[-1].endswith(f"@ {server.c1}"), ".venv не пересобран под старый код"
    assert server.calls("alembic") == []
    assert "systemctl restart sayr" not in server.calls("systemctl")


def test_упавшая_миграция_возвращает_каталог_на_прежний_коммит(server):
    run = server.run(STUB_ALEMBIC_FAIL="1")
    assert run.returncode != 0
    assert server.head() == server.c1
    assert server.calls("uv sync")[-1].endswith(f"@ {server.c1}")
    assert "systemctl restart sayr" not in server.calls("systemctl")


@pytest.mark.parametrize("mode", ["fail", "empty"])
def test_без_дампа_миграции_не_катятся(server, mode):
    run = server.run(STUB_PG_DUMP=mode)
    assert run.returncode != 0
    assert "SAYR_PREDEPLOY_DUMP=0" in run.stderr, "не подсказали, как осознанно без дампа"
    assert server.calls("alembic") == []
    assert server.head() == server.c1
    assert "systemctl restart sayr" not in server.calls("systemctl")
    assert list(server.dumps.iterdir()) == [], "недодамп остался лежать"


def test_без_дампа_можно_только_явно(server):
    run = server.run(SAYR_PREDEPLOY_DUMP="0", STUB_PG_DUMP="fail")
    assert run.returncode == 0, run.stderr
    assert server.calls("pg_dump") == []
    assert server.calls("alembic") == [f"alembic upgrade head @ {server.c2}"]
    assert server.head() == server.c2


def test_дампы_перед_деплоем_копятся_и_последние_10_хранятся(server):
    server.dumps.mkdir()
    for day in range(1, 13):
        (server.dumps / f"pre-deploy-202001{day:02d}-120000.sql.gz").write_bytes(b"old")
    # Прежний единственный файл и ручные дампы ротация не трогает
    (server.dumps / "pre-deploy.sql.gz").write_bytes(b"legacy")
    (server.dumps / "pre-rename.sql.gz").write_bytes(b"manual")

    run = server.run()
    assert run.returncode == 0, run.stderr

    names = sorted(p.name for p in server.dumps.iterdir())
    stamped = [n for n in names if n.startswith("pre-deploy-")]
    assert len(stamped) == 10
    assert not stamped[-1].startswith("pre-deploy-2020"), "свежий дамп не лёг"
    assert "pre-deploy-20200103-120000.sql.gz" not in stamped
    assert "pre-deploy-20200104-120000.sql.gz" in stamped
    assert {"pre-deploy.sql.gz", "pre-rename.sql.gz"} <= set(names)


def test_молчащий_healthz_по_прежнему_откатывает_код(server):
    run = server.run(STUB_CURL_RC="7")
    assert run.returncode == 1
    assert server.head() == server.c1
    assert server.calls("systemctl restart sayr") == ["systemctl restart sayr"] * 2


# Потолок, а не пауза: без нагрузки метки появляются за доли секунды, но
# рядом с параллельными сборками iOS и Android минуты не хватало
WAIT_LIMIT = 180


def _wait_for_sync(
    server, commit: str, fail: str = "до uv sync не дошло", drain: int | None = None
) -> None:
    """Ждёт, пока скрипт под held_command() встанет в uv sync на commit.

    drain — pty, из которого по пути вычитывается вывод: буфер терминала
    невелик, и переполненный он остановил бы скрипт раньше sync."""
    deadline = time.monotonic() + WAIT_LIMIT
    while not server.sync_started(commit):
        assert time.monotonic() < deadline, fail
        if drain is not None and select.select([drain], [], [], 0.05)[0]:
            os.read(drain, 4096)
        else:
            time.sleep(0.05)


def _assert_back_on_c1(server, returncode: int) -> None:
    assert returncode != 0
    assert server.head() == server.c1, "каталог остался на новом коде"
    assert server.calls("uv sync")[-1].endswith(f"@ {server.c1}"), ".venv не пересобран под старый код"
    assert server.calls("synced")[-1:] == [f"synced @ {server.c1}"], "uv sync возврата не дошёл до конца"
    assert "systemctl restart sayr" not in server.calls("systemctl")


def test_оборванный_деплой_тоже_возвращает_каталог(server):
    # Actions отменили прогон посреди uv sync. Сам sync при этом проходит
    # (release после сигнала): вернуть каталог обязан сигнал, а не упавшая
    # команда
    args, env = server.held_command()
    proc = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _wait_for_sync(server, server.c2)
    proc.send_signal(signal.SIGTERM)
    server.release()
    proc.communicate(timeout=WAIT_LIMIT)
    _assert_back_on_c1(server, proc.returncode)


def test_второй_ctrl_c_не_обрывает_возврат(server):
    # Нетерпеливый второй Ctrl-C приходит, пока каталог возвращается.
    # Ctrl-C бьёт по всей группе процессов терминала — и по uv тоже:
    # без защиты .venv остался бы собранным наполовину. Оба sync ждут
    # release(), поэтому первый Ctrl-C попадает в sync нового кода,
    # а второй — в sync возврата, как бы ни тормозила машина
    args, env = server.held_command()
    proc = subprocess.Popen(
        args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
    )
    _wait_for_sync(server, server.c2)
    os.killpg(proc.pid, signal.SIGINT)
    _wait_for_sync(server, server.c1, "возврат не начался")
    os.killpg(proc.pid, signal.SIGINT)
    server.release()
    proc.communicate(timeout=WAIT_LIMIT)

    _assert_back_on_c1(server, proc.returncode)
    assert server.calls("synced") == [f"synced @ {server.c1}"], "uv sync возврата оборван"


def test_оборванный_ssh_без_терминала_возвращает_каталог(server):
    # Так зовёт Actions: ssh root@vps sayr-update, без pty. Связь упала —
    # SIGHUP не приходит, зато любая следующая запись в stdout или stderr
    # бьёт в закрытый канал: SIGPIPE, а при нём — EPIPE. Возврат каталога
    # обязан пережить и то и другое, ему самому писать уже некуда.
    # Popen возвращает ребёнку SIGPIPE по умолчанию, как и sshd
    args, env = server.held_command()
    proc = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _wait_for_sync(server, server.c2)
    proc.stdout.close()
    proc.stderr.close()
    server.release()
    proc.wait(timeout=WAIT_LIMIT)
    _assert_back_on_c1(server, proc.returncode)


def test_оборванный_ssh_с_терминалом_возвращает_каталог(server):
    # Руками выкатывают из терминала: ssh с pty. Обрыв — SIGHUP, а запись
    # в пропавший терминал дальше отвечает EIO
    master, slave = pty.openpty()
    args, env = server.held_command()

    def own_terminal():
        # Свой сеанс (start_new_session) и pty — управляющий терминал
        # сеанса, как у шелла, который поднял sshd
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    proc = subprocess.Popen(
        args, env=env, stdin=slave, stdout=slave, stderr=slave,
        start_new_session=True, preexec_fn=own_terminal,
    )
    os.close(slave)
    try:
        _wait_for_sync(server, server.c2, drain=master)
    finally:
        os.close(master)
    server.release()
    proc.wait(timeout=WAIT_LIMIT)
    _assert_back_on_c1(server, proc.returncode)
