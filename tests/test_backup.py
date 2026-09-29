"""Ночной бэкап: deploy/backup.sh и его таймер.

Скрипт гоняется настоящим bash, а sudo, pg_dump, rsync и rclone
подменены заглушками в PATH: они пишут, как их позвали, и ведут себя
так, как велит переменная STUB_*. Каталоги — во временной папке, так что
тест не трогает ни базу, ни /var/backups.

Главное свойство — громкость: пустой, оборванный или несостоявшийся дамп
обязан дать ненулевой код (служба failed, видно в journalctl), а не лечь
в daily/ под видом бэкапа.
"""

import gzip
import os
import shutil
import subprocess
import tarfile
from datetime import date
from pathlib import Path

import pytest

from app.config import Settings

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
BACKUP_SH = DEPLOY / "backup.sh"
# Настоящие — до того, как заглушки встанут впереди в PATH
REAL_RSYNC = shutil.which("rsync")
REAL_TAR = shutil.which("tar")

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or REAL_TAR is None, reason="нет bash или tar"
)

# Хвост настоящего pg_dump 16.10+: после «dump complete» ещё \unrestrict
_DUMP = r"""#!/bin/sh
echo "pg_dump $*" >>"$STUB_LOG"
case ${STUB_PG_DUMP:-ok} in
  ok) printf -- '-- dump of %s %s\nCREATE TABLE t ();\n\n--\n-- PostgreSQL database dump complete\n--\n\n\\unrestrict abc\n\n' "$1" "${STUB_MARK:-}" ;;
  empty) ;;
  cut) printf -- '-- dump of %s\nCREATE TABLE t (\n' "$1" ;;
  fail) printf -- '-- dump of %s\n' "$1"; echo "pg_dump: error: connection failed" >&2; exit 1 ;;
esac
"""

STUBS = {
    # sudo -n -u postgres КОМАНДА… → КОМАНДА…
    "sudo": """#!/bin/sh
while [ $# -gt 0 ]; do
  case $1 in -n) shift ;; -u) shift 2 ;; *) break ;; esac
done
exec "$@"
""",
    "pg_dump": _DUMP,
    # STUB_REAL_RSYNC — отдать вызов настоящему rsync (копия в локальный
    # каталог), чтобы проверить, что на той стороне реально остаётся
    "rsync": """#!/bin/sh
echo "rsync $*" >>"$STUB_LOG"
[ -z "${STUB_REAL_RSYNC:-}" ] || exec "$STUB_REAL_RSYNC" "$@"
exit ${STUB_REMOTE_RC:-0}
""",
    # Настоящий tar, но код выхода можно подменить: 1 у GNU tar — «файл
    # менялся, пока читали», 2 — архив не вышел
    "tar": f"""#!/bin/sh
"{REAL_TAR}" "$@" || exit $?
exit ${{STUB_TAR_RC:-0}}
""",
    "rclone": """#!/bin/sh
echo "rclone $*" >>"$STUB_LOG"
exit ${STUB_REMOTE_RC:-0}
""",
}


def _week() -> str:
    iso = date.today().isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


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
def box(tmp_path, bin_dir):
    """Сервер в миниатюре: каталог приложения с media и .env."""
    app_dir = tmp_path / "app"
    for sub in ("photos", "gpx", "thumbs", "reports"):
        (app_dir / "media" / sub).mkdir(parents=True)
    (app_dir / "media" / "photos" / "a.jpg").write_bytes(b"jpg")
    (app_dir / "media" / "thumbs" / "a.jpg").write_bytes(b"thumb")
    (app_dir / ".env").write_text("SAYR_ADMIN_PASSWORD=x\n")

    log = tmp_path / "calls.log"
    log.touch()

    class Box:
        dest = tmp_path / "backups"
        app = app_dir
        calls = log

        def run(self, **env: str) -> subprocess.CompletedProcess:
            full = {
                **os.environ,
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "STUB_LOG": str(log),
                "SAYR_APP_DIR": str(app_dir),
                "SAYR_BACKUP_DIR": str(self.dest),
                **env,
            }
            # conftest ставит свою SAYR_MEDIA_DIR, здесь — media приложения
            for key in ("SAYR_MEDIA_DIR", "SAYR_BACKUP_REMOTE"):
                if key not in env:
                    full.pop(key, None)
            return subprocess.run(
                ["bash", str(BACKUP_SH)], env=full, capture_output=True, text=True, timeout=60
            )

        def names(self, sub: str, root: Path | None = None) -> list[str]:
            return sorted(p.name for p in ((root or self.dest) / sub).iterdir())

        def fill(self, daily: int, weekly: int, root: Path | None = None) -> None:
            """Старые бэкапы 2020 года: daily ночей и weekly недель."""
            root = root or self.dest
            (root / "daily").mkdir(parents=True, exist_ok=True)
            (root / "weekly").mkdir(parents=True, exist_ok=True)
            for day in range(1, daily + 1):
                (root / "daily" / f"db-2020-01-{day:02d}.sql.gz").write_bytes(b"old")
            for week in range(1, weekly + 1):
                (root / "weekly" / f"db-2020-W{week:02d}.sql.gz").write_bytes(b"old")
                (root / "weekly" / f"media-2020-W{week:02d}.tar.gz").write_bytes(b"old")

        def remote_calls(self, tool: str) -> list[str]:
            return [c for c in self.calls.read_text().splitlines() if c.startswith(tool)]

    return Box()


def test_ночью_дамп_ложится_в_daily_а_первый_за_неделю_ещё_и_в_weekly(box):
    run = box.run()
    assert run.returncode == 0, run.stderr

    stamp = date.today().isoformat()
    assert box.names("daily") == [f"db-{stamp}.sql.gz"]
    assert box.names("weekly") == [f"db-{_week()}.sql.gz", f"media-{_week()}.tar.gz"]

    dump = gzip.decompress((box.dest / "daily" / f"db-{stamp}.sql.gz").read_bytes()).decode()
    assert "-- dump of sayr" in dump
    assert "pg_dump sayr" in box.calls.read_text()

    with tarfile.open(box.dest / "weekly" / f"media-{_week()}.tar.gz") as tar:
        members = tar.getnames()
    assert "media/photos/a.jpg" in members
    assert not [m for m in members if m.startswith("media/thumbs")], "миниатюры пересобираются"

    # В дампе телефоны: читать его может только root
    assert (box.dest / "daily" / f"db-{stamp}.sql.gz").stat().st_mode & 0o077 == 0


def test_без_адреса_копии_говорит_что_её_нет(box):
    run = box.run()
    assert run.returncode == 0, run.stderr
    assert "копия вне сервера не настроена" in run.stderr
    assert "rsync" not in box.calls.read_text()
    # Нет строки в .env — не ошибка: bash 3.2 кричал об этом через ERR
    assert "ОШИБКА" not in run.stderr


@pytest.mark.parametrize("mode", ["fail", "empty", "cut"])
def test_несостоявшийся_дамп_роняет_службу_и_не_ложится_в_daily(box, mode):
    run = box.run(STUB_PG_DUMP=mode)
    assert run.returncode not in (0, 3), run.stdout
    assert box.names("daily") == [], "недодамп выдан за бэкап"
    assert box.names("weekly") == []


def test_упавший_дамп_не_трогает_прежние_бэкапы(box):
    assert box.run().returncode == 0
    before = box.names("daily")
    assert box.run(STUB_PG_DUMP="fail").returncode != 0
    assert box.names("daily") == before


@pytest.mark.parametrize(
    ("env", "why"),
    [
        ({"SAYR_MEDIA_DIR": "/нет/такого"}, "нет каталога"),
        ({"STUB_TAR_RC": "2"}, "tar не отработал"),
        # Диск общий с базой: архив, который его забьёт, хуже, чем никакого
        ({"SAYR_BACKUP_MIN_FREE_MB": str(10**12)}, "не делаю"),
    ],
)
def test_не_вышел_архив_media_дамп_всё_равно_ротируется_и_уезжает(box, env, why):
    box.fill(daily=20, weekly=0)
    run = box.run(SAYR_BACKUP_REMOTE="backup@storage:/srv/sayr", **env)

    assert run.returncode not in (0, 3), "архив не сделан, а служба зелёная"
    assert why in run.stderr
    assert f"media-{_week()}.tar.gz" not in box.names("weekly")
    assert not [n for n in box.names("weekly") if n.endswith(".tmp")], "недоархив остался лежать"
    # Свежий дамп на месте, ротация прошла, копия уехала
    daily = box.names("daily")
    assert f"db-{date.today().isoformat()}.sql.gz" in daily and len(daily) == 14
    assert len(box.remote_calls("rsync")) == 2


def test_код_1_у_tar_не_роняет_ночь(box):
    # GNU tar отвечает 1, если файл менялся, пока его читали: на архив
    # пришлась загрузка через /report. Архив при этом целый
    run = box.run(STUB_TAR_RC="1")
    assert run.returncode == 0, run.stderr
    assert "не мгновенный снимок" in run.stderr
    assert f"media-{_week()}.tar.gz" in box.names("weekly")


def test_каталог_media_берётся_из_env_приложения(box, tmp_path):
    # SAYR_MEDIA_DIR приложение читает из .env — бэкап обязан найти его там же
    other = tmp_path / "store" / "files"
    (other / "photos").mkdir(parents=True)
    (other / "photos" / "b.jpg").write_bytes(b"jpg")
    (box.app / ".env").write_text(f"SAYR_ADMIN_PASSWORD=x\nSAYR_MEDIA_DIR={other}\n")

    run = box.run()
    assert run.returncode == 0, run.stderr
    with tarfile.open(box.dest / "weekly" / f"media-{_week()}.tar.gz") as tar:
        assert "files/photos/b.jpg" in tar.getnames()


def test_хранятся_14_ночей_и_8_недель(box):
    daily = box.dest / "daily"
    weekly = box.dest / "weekly"
    daily.mkdir(parents=True)
    weekly.mkdir()
    for day in range(1, 21):
        (daily / f"db-2020-01-{day:02d}.sql.gz").write_bytes(b"old")
    for week in range(1, 11):
        (weekly / f"db-2020-W{week:02d}.sql.gz").write_bytes(b"old")
        (weekly / f"media-2020-W{week:02d}.tar.gz").write_bytes(b"old")
    # Посторонний файл рядом ротация не трогает
    (daily / "pre-rename.sql.gz").write_bytes(b"manual")

    run = box.run()
    assert run.returncode == 0, run.stderr

    kept = box.names("daily")
    assert len([n for n in kept if n.startswith("db-")]) == 14
    assert f"db-{date.today().isoformat()}.sql.gz" in kept
    # Сегодняшний и 13 самых свежих из старых
    assert "db-2020-01-08.sql.gz" in kept and "db-2020-01-07.sql.gz" not in kept
    assert "pre-rename.sql.gz" in kept

    kept = box.names("weekly")
    assert len([n for n in kept if n.startswith("db-")]) == 8
    assert len([n for n in kept if n.startswith("media-")]) == 8
    assert f"db-{_week()}.sql.gz" in kept and "db-2020-W03.sql.gz" not in kept


def test_второй_прогон_за_неделю_не_переписывает_недельный(box):
    assert box.run(STUB_MARK="первый").returncode == 0
    assert box.run(STUB_MARK="второй").returncode == 0

    stamp = date.today().isoformat()
    daily = gzip.decompress((box.dest / "daily" / f"db-{stamp}.sql.gz").read_bytes()).decode()
    weekly = gzip.decompress((box.dest / "weekly" / f"db-{_week()}.sql.gz").read_bytes()).decode()
    assert "второй" in daily, "ночной дамп — самый свежий"
    assert "первый" in weekly, "недельный — первый за неделю, его не подменяют"


def test_при_полном_наборе_копия_через_rsync_повторяет_ротацию(box):
    box.fill(daily=13, weekly=7)
    run = box.run(SAYR_BACKUP_REMOTE="backup@storage:/srv/sayr/")
    assert run.returncode == 0, run.stderr

    daily, weekly = box.remote_calls("rsync")
    assert daily.startswith("rsync -a --delete ") and weekly.startswith("rsync -a --delete ")
    assert daily.endswith(f"{box.dest}/daily backup@storage:/srv/sayr/")
    assert weekly.endswith(f"{box.dest}/weekly backup@storage:/srv/sayr/")
    for call in (daily, weekly):
        assert "BatchMode=yes" in call, "ночью на вопрос о пароле никто не ответит"
        # Связь, умершая без разрыва, не должна держать службу вечно
        assert "--timeout=600" in call and "ServerAliveInterval=30" in call


def test_при_неполном_наборе_rsync_там_ничего_не_стирает(box):
    # Сервер пересобран или каталог бэкапов стёрт: здесь один свежий дамп
    run = box.run(SAYR_BACKUP_REMOTE="backup@storage:/srv/sayr/")
    assert run.returncode == 0, run.stderr
    calls = box.remote_calls("rsync")
    assert len(calls) == 2
    assert not [c for c in calls if "--delete" in c], "зеркало стёрло бы историю на той стороне"


def test_копия_через_rclone(box):
    run = box.run(SAYR_BACKUP_REMOTE="rclone:box:sayr")
    assert run.returncode == 0, run.stderr
    assert box.remote_calls("rclone") == [
        f"rclone copy {box.dest}/daily box:sayr/daily",
        f"rclone copy {box.dest}/weekly box:sayr/weekly",
    ]


def test_rclone_при_полном_наборе_синхронизирует(box):
    box.fill(daily=13, weekly=7)
    run = box.run(SAYR_BACKUP_REMOTE="rclone:box:sayr")
    assert run.returncode == 0, run.stderr
    assert box.remote_calls("rclone") == [
        f"rclone sync {box.dest}/daily box:sayr/daily",
        f"rclone sync {box.dest}/weekly box:sayr/weekly",
    ]


# Дальше rsync настоящий, а «та сторона» — каталог рядом: проверяется,
# что на ней действительно остаётся, а не только с какими ключами позвали
real_rsync = pytest.mark.skipif(REAL_RSYNC is None, reason="нет rsync")


@real_rsync
def test_пустой_каталог_здесь_не_стирает_историю_там(box, tmp_path):
    remote = tmp_path / "remote"
    box.fill(daily=13, weekly=7, root=remote)

    run = box.run(SAYR_BACKUP_REMOTE=str(remote), STUB_REAL_RSYNC=REAL_RSYNC)
    assert run.returncode == 0, run.stderr

    daily = box.names("daily", remote)
    assert len(daily) == 14 and f"db-{date.today().isoformat()}.sql.gz" in daily
    weekly = box.names("weekly", remote)
    assert len([n for n in weekly if n.startswith("db-")]) == 8
    assert len([n for n in weekly if n.startswith("media-")]) == 8


@real_rsync
def test_при_полном_наборе_там_уходит_то_же_что_и_здесь(box, tmp_path):
    remote = tmp_path / "remote"
    box.fill(daily=14, weekly=8, root=remote)
    box.fill(daily=13, weekly=7)

    run = box.run(SAYR_BACKUP_REMOTE=str(remote), STUB_REAL_RSYNC=REAL_RSYNC)
    assert run.returncode == 0, run.stderr
    assert box.names("daily", remote) == box.names("daily")
    assert box.names("weekly", remote) == box.names("weekly")
    assert "db-2020-01-14.sql.gz" not in box.names("daily", remote)


def test_не_доехавшая_копия_видна_по_коду_а_локальный_бэкап_цел(box):
    run = box.run(SAYR_BACKUP_REMOTE="backup@storage:/srv/sayr", STUB_REMOTE_RC="12")
    assert run.returncode == 3
    # Код выхода — в коде, а не хвостом в тексте ошибки
    assert "локальный бэкап цел\n" in run.stderr
    assert box.names("daily") == [f"db-{date.today().isoformat()}.sql.gz"]


def test_адрес_копии_берётся_из_env_приложения(box):
    (box.app / ".env").write_text(
        'SAYR_ADMIN_PASSWORD=x\nSAYR_BACKUP_REMOTE="backup@storage:/srv/sayr"\n'
    )
    run = box.run()
    assert run.returncode == 0, run.stderr
    assert box.calls.read_text().splitlines()[-1].endswith(" backup@storage:/srv/sayr/")


def test_адрес_копии_в_env_не_роняет_приложение(tmp_path):
    # Незнакомый ключ SAYR_ в .env pydantic-settings не пропускает: без поля
    # в Settings строка для бэкапа положила бы весь сервис
    env = tmp_path / ".env"
    env.write_text("SAYR_BACKUP_REMOTE=backup@storage:/srv/sayr\n")
    assert Settings(_env_file=env).backup_remote == "backup@storage:/srv/sayr"


def test_служба_бэкапа_запускает_скрипт_из_рабочей_копии():
    # Прежний backup.sh звали из cron по пути /opt/sayr, которого на бою нет.
    # Юнит обязан смотреть туда, где код лежит на самом деле
    unit = (DEPLOY / "sayr-backup.service").read_text()
    exec_start = next(l for l in unit.splitlines() if l.startswith("ExecStart="))
    assert exec_start == "ExecStart=/bin/bash /root/Projects/sayr-server/deploy/backup.sh"
    assert "WorkingDirectory=/root/Projects/sayr-server" in unit
    # У oneshot срока по умолчанию нет: зависший прогон держал бы службу
    # activating, таймер пропускал бы ночи, а failed не появилось бы
    assert "TimeoutStartSec=3h" in unit.splitlines()

    timer = (DEPLOY / "sayr-backup.timer").read_text()
    assert "OnCalendar=" in timer and "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer
