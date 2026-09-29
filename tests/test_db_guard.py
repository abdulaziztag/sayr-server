"""Сьют не трогает базу, в имени которой нет «test».

conftest начинает с drop_all, так что строка к рабочей базе, оставшаяся
в окружении, стёрла бы её. Проверяем отдельным pytest в подпроцессе,
только сбором (--collect-only): так ни одна фикстура не запускается,
а порт 1 в адресе гарантирует, что даже сломанная защита никуда
не дотянется.
"""

import os
import subprocess
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent


def _collect(database_url: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "--collect-only", str(Path(__file__))],
        cwd=SERVER_DIR,
        env={**os.environ, "SAYR_DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_без_test_в_имени_базы_сьют_не_стартует():
    run = _collect("postgresql+psycopg://sayr:sayr@127.0.0.1:1/sayr")
    assert run.returncode != 0, run.stdout[-500:]
    assert "тесты её сотрут" in run.stderr + run.stdout


def test_test_в_любом_месте_имени_годится():
    # Параллельные прогоны заводят себе базы вроде sayr_test_ops —
    # точного суффикса защита не требует
    run = _collect("postgresql+psycopg://sayr:sayr@127.0.0.1:1/sayr_test_ops")
    assert run.returncode == 0, run.stderr[-500:] + run.stdout[-500:]


def test_хост_с_test_не_делает_базу_тестовой():
    run = _collect("postgresql+psycopg://sayr:sayr@test-db:1/sayr")
    assert run.returncode != 0, run.stdout[-500:]


def test_test_внутри_другого_слова_не_считается():
    # sayr_latest — естественное имя для базы со свежим дампом с боя
    for name in ("sayr_latest", "sayr_contest"):
        run = _collect(f"postgresql+psycopg://sayr:sayr@127.0.0.1:1/{name}")
        assert run.returncode != 0, name
        assert "тесты её сотрут" in run.stderr + run.stdout, name
