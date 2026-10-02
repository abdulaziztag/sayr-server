"""Страница «Статистика»: данные по разделам и сама страница."""

from datetime import date, datetime, timedelta

from sqlalchemy import delete, select

from app import stats_dashboard
from app.db import SessionLocal
from app.models import (
    Announcement,
    ApiEvent,
    AppUpdate,
    CohortRetention,
    DailyCount,
    DailyPlatform,
    DailyStat,
    Device,
    Place,
    TripIntent,
)
from app.stats import week_start


async def _clear():
    async with SessionLocal() as session:
        for table in (ApiEvent, DailyStat, DailyCount, DailyPlatform, CohortRetention,
                      Device, TripIntent, Announcement):
            await session.execute(delete(table))
        await session.commit()


def _at(day: date, hour: int = 12) -> datetime:
    return datetime.combine(day, datetime.min.time()).astimezone() + timedelta(hours=hour)


async def _data(**kw) -> dict:
    async with SessionLocal() as session:
        return await stats_dashboard.dashboard(session, **kw)


async def test_survives_empty_tables_and_normalises_params():
    await _clear()
    d = await _data(period_days=15, sort="bogus")
    assert d["period"] == 30 and d["sort"] == "opens"
    assert d["window"] == 30
    assert set(d) >= {"overview", "launch", "places", "trail", "app"}
    assert len(d["overview"]["tiles"]) == 6
    assert d["places"]["top"] == [] and d["places"]["look"] == []
    assert d["client_data"] is False
    assert "Данные пойдут" in d["since_text"]
    assert d["total_devices"] == 0
    week = await _data(period_days=7)
    assert week["period"] == 7 and week["window"] == 7
    quarter = await _data(period_days=90)
    assert quarter["period"] == 90 and quarter["window"] == 30


async def test_top_places_sort_and_service_lists():
    """Топ сортируется параметром, служебные списки срабатывают на порогах."""
    await _clear()
    yesterday = date.today() - timedelta(days=1)
    async with SessionLocal() as session:
        session.add_all([
            # Много открытий, ни одной навигации — «смотрят, но не идут»
            DailyCount(day=yesterday, kind="place", key="test-waterfall", events=20, devices=9),
            # Навигаций пять, сходов 4 из 5 и финиш 1 из 5 — «трек под вопросом»
            DailyCount(day=yesterday, kind="place", key="test-peak", events=5, devices=3),
            DailyCount(day=yesterday, kind="nav_start", key="test-peak", events=5, devices=4),
            DailyCount(day=yesterday, kind="nav_offtrail", key="test-peak", events=4, devices=3),
            DailyCount(day=yesterday, kind="nav_finish", key="test-peak", events=1, devices=1),
            # Здоровый трек: пять навигаций, четыре финиша, без сходов
            DailyCount(day=yesterday, kind="place", key="test-lake", events=8, devices=5),
            DailyCount(day=yesterday, kind="nav_start", key="test-lake", events=5, devices=5),
            DailyCount(day=yesterday, kind="nav_finish", key="test-lake", events=4, devices=4),
            DailyCount(day=yesterday, kind="rec_finish", key="test-lake", events=2, devices=2),
            # Сегодня — из сырья, а не из свёрток
            ApiEvent(kind="place", slug="test-lake", device="d-today", ts=_at(date.today())),
            # Признак обновлённых клиентов — запуск приложения
            ApiEvent(kind="app_open", device="d-today", ts=_at(date.today())),
        ])
        await session.commit()

    d = await _data()
    top = d["places"]["top"]
    assert [r["name"] for r in top] == ["Тестовый водопад", "Тестовое озеро", "Тестовый пик"]
    lake = top[1]
    assert lake["opens"] == 9 and lake["nav"] == 5 and lake["finish"] == 4 and lake["rec"] == 2
    assert lake["finish_share_text"] == "80 %"
    assert lake["devices"] == 1  # уникальные — только из сырья за окно

    by_nav = await _data(sort="nav")
    assert [r["slug"] for r in by_nav["places"]["top"][:2]] == ["test-lake", "test-peak"]
    by_share = await _data(sort="share")
    assert by_share["places"]["top"][0]["slug"] == "test-lake"

    assert [r["slug"] for r in d["places"]["look"]] == ["test-waterfall"]
    track = d["places"]["track"]
    assert [r["slug"] for r in track] == ["test-peak"]
    assert "сходов 80 %" in track[0]["reason"] and "финишей 20 %" in track[0]["reason"]
    assert d["client_data"] is True
    assert d["places"]["has_nav"] is True


async def test_look_list_needs_ten_opens_and_track_list_needs_five_navs():
    await _clear()
    yesterday = date.today() - timedelta(days=1)
    async with SessionLocal() as session:
        session.add_all([
            DailyCount(day=yesterday, kind="place", key="test-waterfall", events=9, devices=9),
            DailyCount(day=yesterday, kind="nav_start", key="test-peak", events=4, devices=4),
            DailyCount(day=yesterday, kind="nav_offtrail", key="test-peak", events=4, devices=4),
        ])
        await session.commit()
    d = await _data()
    assert d["places"]["look"] == []
    assert d["places"]["track"] == []
    # Навигации есть, а запусков приложения нет — так бывает только
    # от ручной проверки; пустые блоки по-прежнему говорят про версию
    assert d["client_data"] is False


async def test_channels_and_cohorts():
    await _clear()
    today = date.today()
    yesterday = today - timedelta(days=1)
    cohort = week_start(today - timedelta(days=21))
    async with SessionLocal() as session:
        session.add_all([
            DailyCount(day=yesterday, kind="landing", key="gorets", events=40, devices=0),
            DailyCount(day=yesterday, kind="store_ios", key="gorets", events=10, devices=0),
            DailyCount(day=yesterday, kind="store_android", key="gorets", events=2, devices=0),
            DailyCount(day=yesterday, kind="landing", key="", events=10, devices=0),
            CohortRetention(cohort_week=cohort, week_index=0, devices=20),
            CohortRetention(cohort_week=cohort, week_index=1, devices=5),
            CohortRetention(cohort_week=cohort, week_index=2, devices=2),
            # Когорта, пришедшая до начала свёрток: нулевой недели нет,
            # размер неизвестен — в проценты и в плитку не попадает
            CohortRetention(cohort_week=cohort - timedelta(days=7), week_index=1, devices=9),
            CohortRetention(cohort_week=cohort - timedelta(days=7), week_index=2, devices=4),
        ])
        await session.commit()
    d = await _data()
    channels = {c["mark"]: c for c in d["launch"]["channels"]}
    assert channels["gorets"]["visits"] == 40
    assert channels["gorets"]["clicks"] == 12
    assert channels["gorets"]["conv"] == "30 %"
    # Десять визитов без клика — честные 0 %, а не прочерк: прочерк только без визитов
    assert channels["без метки"]["clicks"] == 0 and channels["без метки"]["conv"] == "0 %"
    assert list(channels) == ["gorets", "без метки"]  # по визитам

    grid = d["launch"]["cohorts"]
    assert "<title>" in grid
    assert f"{cohort.strftime('%d.%m')} · 20 · нед. 1: 25 %" in grid
    assert "нед. 2: 10 %" in grid
    assert d["launch"]["cohort_weeks"] == 1
    returned = d["overview"]["tiles"][3]
    assert returned["label"] == "Вернулись через неделю"
    assert returned["value"] == "25 %"


async def test_returns_next_day_and_within_week():
    await _clear()
    today = date.today()
    async with SessionLocal() as session:
        a, b, c = today - timedelta(days=3), today - timedelta(days=3), today - timedelta(days=10)
        session.add_all([
            Device(device="d-a", first_seen=a),
            Device(device="d-b", first_seen=b),
            Device(device="d-c", first_seen=c),
            ApiEvent(kind="catalog", device="d-a", ts=_at(a)),
            ApiEvent(kind="catalog", device="d-a", ts=_at(a + timedelta(days=1))),
            ApiEvent(kind="catalog", device="d-b", ts=_at(b)),
            ApiEvent(kind="catalog", device="d-c", ts=_at(c)),
            ApiEvent(kind="catalog", device="d-c", ts=_at(c + timedelta(days=5))),
        ])
        await session.commit()
    d = await _data()
    # D1: d-a вернулся назавтра, d-b нет, d-c нет — 1 из 3
    assert d["launch"]["d1"] == "33 %"
    # D7: закрытая неделя только у d-c, и он вернулся — 1 из 1
    assert d["launch"]["d7"] == "100 %"
    assert d["launch"]["fresh"] == 3


async def test_overview_tiles_mix_rollups_and_today():
    await _clear()
    today = date.today()
    yesterday = today - timedelta(days=1)
    async with SessionLocal() as session:
        lake_id = (
            await session.execute(select(Place.id).where(Place.slug == "test-lake"))
        ).scalar_one()
        session.add_all([
            DailyStat(day=yesterday, active_devices=4, new_devices=2),
            DailyCount(day=yesterday, kind="app_open", key="", events=9, devices=4),
            Device(device="d1", first_seen=yesterday),
            Device(device="d2", first_seen=today),
            ApiEvent(kind="app_open", device="d1", ts=_at(today)),
            ApiEvent(kind="app_open", device="d2", ts=_at(today)),
            ApiEvent(kind="rec_finish", slug="test-lake", device="d1", ts=_at(today)),
            TripIntent(place_id=lake_id, day=today + timedelta(days=2), device_id="d2"),
        ])
        await session.commit()
    d = await _data(period_days=7)
    tiles = {t["label"]: t for t in d["overview"]["tiles"]}
    assert tiles["Активные устройства"]["value"] == "2"
    assert tiles["Новые"]["value"] == "3"  # 2 из свёртки + 1 сегодня
    assert tiles["Сессии"]["value"] == "11"  # 9 из свёртки + 2 сегодня
    assert tiles["Нажали «Пойду»"]["value"] == "50 %"
    assert tiles["Записали выход"]["value"] == "50 %"
    assert d["places"]["upcoming"][0]["name"] == "Тестовое озеро"


async def test_funnel_counts_unique_devices():
    await _clear()
    today = date.today()
    async with SessionLocal() as session:
        session.add_all([
            ApiEvent(kind="catalog", device="u1", ts=_at(today)),
            ApiEvent(kind="catalog", device="u2", ts=_at(today)),
            ApiEvent(kind="catalog", device="u3", ts=_at(today)),
            ApiEvent(kind="place", slug="test-peak", device="u1", ts=_at(today)),
            ApiEvent(kind="place", slug="test-peak", device="u1", ts=_at(today)),
            ApiEvent(kind="place", slug="test-lake", device="u2", ts=_at(today)),
            ApiEvent(kind="nav_start", slug="test-peak", device="u1", ts=_at(today, 8)),
            ApiEvent(kind="nav_finish", slug="test-peak", device="u1", ts=_at(today, 13)),
        ])
        await session.commit()
    d = await _data()
    steps = dict(d["trail"]["steps"])
    assert steps["Активные"] == 3
    assert steps["Открыли место"] == 2
    assert steps["Запустили навигатор"] == 1
    assert steps["Дошли до финиша"] == 1
    assert steps["Записали выход"] == 0
    assert "<title>Открыли место: 2 (67 % от предыдущего)</title>" in d["trail"]["funnel"]
    assert d["trail"]["has_nav"] is True
    # День недели навигации — по ташкентскому времени
    weekday = (_at(today, 8).astimezone().weekday())
    assert f"<title>{stats_dashboard.WEEKDAYS[weekday]}: 1</title>" in d["trail"]["weekdays"]


async def test_recording_sticker_and_files_blocks():
    await _clear()
    yesterday = date.today() - timedelta(days=1)
    async with SessionLocal() as session:
        session.add_all([
            DailyCount(day=yesterday, kind="rec_start", key="test-peak", events=4, devices=3),
            DailyCount(day=yesterday, kind="rec_finish", key="test-peak", events=2, devices=2),
            DailyCount(day=yesterday, kind="rec_pause", key="test-peak", events=3, devices=2),
            DailyCount(day=yesterday, kind="rec_autoresume", key="", events=1, devices=1),
            DailyCount(day=yesterday, kind="rec_share_file", key="", events=1, devices=1),
            DailyCount(day=yesterday, kind="sticker_open", key="test-peak", events=10, devices=5),
            DailyCount(day=yesterday, kind="sticker_save", key="test-peak", events=4, devices=3),
            DailyCount(day=yesterday, kind="sticker_share", key="test-peak", events=2, devices=2),
            DailyCount(day=yesterday, kind="sticker_layout", key="3", events=6, devices=4),
            DailyCount(day=yesterday, kind="file_open", key="gpx", events=5, devices=4),
            DailyCount(day=yesterday, kind="file_save", key="gpx", events=2, devices=2),
        ])
        await session.commit()
    d = await _data()
    rec = d["trail"]["recording"]
    assert (rec["started"], rec["finished"], rec["finish_share"]) == (4, 2, "50 %")
    assert rec["pauses"] == "1,5" and rec["autoresumes"] == "0,5"
    assert rec["shared_files"] == 1
    sticker = d["trail"]["sticker"]
    assert (sticker["opened"], sticker["saved_share"], sticker["shared_share"]) == (10, "40 %", "20 %")
    assert "<title>раскладка 3: 6</title>" in sticker["layouts"]
    gpx = d["trail"]["files"][0]
    assert (gpx["ext"], gpx["opened"], gpx["saved"], gpx["share"]) == ("GPX", 5, 2, "40 %")
    assert d["trail"]["has_files"] and d["trail"]["has_rec"] and d["trail"]["has_sticker"]


async def test_app_versions_below_threshold_and_headerless_row():
    await _clear()
    today = date.today()
    async with SessionLocal() as session:
        await session.merge(AppUpdate(platform="ios", min_version="1.7.1"))
        session.add_all([
            Device(device="ios-new", first_seen=today, platform="ios", app_version="1.7.1",
                   lang="ru", os_major="26", last_seen=today),
            Device(device="ios-old", first_seen=today, platform="ios", app_version="1.6.0",
                   lang="uz", os_major="18", last_seen=today),
            Device(device="and-1", first_seen=today, platform="android", app_version="1.7.0",
                   lang="ru", os_major="14", last_seen=today),
            Device(device="aug-1", first_seen=today),
            *[ApiEvent(kind="catalog", device=dev, ts=_at(today))
              for dev in ("ios-new", "ios-old", "and-1", "aug-1")],
            DailyCount(day=today - timedelta(days=1), kind="onboarding", key="done", events=6, devices=6),
            DailyCount(day=today - timedelta(days=1), kind="onboarding", key="skip:2", events=2, devices=2),
            DailyCount(day=today - timedelta(days=1), kind="onboarding", key="lang:uz", events=3, devices=3),
            DailyCount(day=today - timedelta(days=1), kind="permission", key="notif:yes", events=3, devices=3),
            DailyCount(day=today - timedelta(days=1), kind="permission", key="notif:no", events=1, devices=1),
            DailyCount(day=today - timedelta(days=1), kind="reminder_open", key="eve", events=2, devices=2),
        ])
        await session.commit()
    d = await _data()
    app = d["app"]
    share = {r["platform"]: r for r in app["platform_share"]}
    assert share["iOS"]["devices"] == 2 and share["Android"]["devices"] == 1
    assert share["без заголовка"]["devices"] == 1 and share["без заголовка"]["share"] == "25 %"
    versions = {(r["platform"], r["version"]): r for r in app["versions"]}
    assert versions[("iOS", "1.7.1")]["below"] is False
    assert versions[("iOS", "1.6.0")]["below"] is True
    assert versions[("iOS", "1.6.0")]["share"] == "50 %"
    assert versions[("Android", "1.7.0")]["below"] is False
    assert ("без заголовка", "без заголовка") in versions
    assert {r["lang"]: r["devices"] for r in app["langs"]} == {"ru": 2, "uz": 1}
    assert app["onboarding"]["done_share"] == "75 %"
    assert app["onboarding"]["skips"] == [("2", 2)]
    # Смена языка не считается ни дошедшим, ни пропуском
    assert (app["onboarding"]["lang_uz"], app["onboarding"]["lang_ru"]) == (3, 0)
    assert app["permissions"][0]["share"] == "75 %"
    assert app["reminders"] == {"weekly": 0, "eve": 2}
    assert "<title>" in app["platforms"]  # стопка за сегодня из сырья
    assert app["has_header"] is True


async def test_pushes_join_open_events():
    await _clear()
    yesterday = date.today() - timedelta(days=1)
    async with SessionLocal() as session:
        note = Announcement(title="Снег в Чимгане", body="…", send_at=_at(yesterday).replace(tzinfo=None),
                            status="sent", sent_count=10, failed_count=1)
        session.add(note)
        await session.flush()
        session.add(DailyCount(day=yesterday, kind="push_open", key=str(note.id), events=4, devices=4))
        await session.commit()
    d = await _data()
    push = d["app"]["pushes"][0]
    assert (push["title"], push["sent"], push["failed"], push["opened"], push["share"]) == (
        "Снег в Чимгане", 10, 1, 4, "40 %")
    assert d["app"]["has_push_opens"] is True


async def test_page_renders_every_section_for_each_period(admin_client):
    await _clear()
    for period in (7, 90):
        resp = await admin_client.get(f"/admin/stats?period={period}")
        assert resp.status_code == 200, resp.text[:300]
        for head in ("Обзор", "Запуск и каналы", "Места", "Тропа и запись", "Приложение",
                     "Вход и попутчики"):
            assert f">{head}</h2>" in resp.text, head
        assert f'?period={period}&amp;sort=opens" aria-current="page"' in resp.text
        assert "Данные пойдут" in resp.text  # клиентских событий нет — блоки говорят про версию
        assert "/static/stats.css" in resp.text
    odd = await admin_client.get("/admin/stats?period=15")
    assert '?period=30&amp;sort=opens" aria-current="page"' in odd.text


async def test_page_requires_admin(client):
    resp = await client.get("/admin/stats?period=7", follow_redirects=False)
    assert resp.status_code in (302, 307)


# --- Вход и попутчики (02.10.2026) ------------------------------------------


def _rolled(day: date, kind: str, key: str, events: int) -> DailyCount:
    return DailyCount(day=day, kind=kind, key=key, events=events, devices=0)


async def test_accounts_section_without_data():
    """Исходов нет — нули; попыток нет — прочерки и «данные пойдут»,
    а не ноль как факт"""
    await _clear()
    a = (await _data())["accounts"]
    assert a["client"] is False
    assert a["since_text"] == "Данные пойдут с ближайшего обновления приложения (iOS и Android)."
    assert [r["label"] for r in a["logins"]] == ["Telegram", "Apple", "По номеру", "Всего"]
    assert all(r["started"] == "—" and r["share"] == "—" for r in a["logins"])
    assert all(r["new"] == 0 and r["back"] == 0 for r in a["logins"])
    assert a["login_chart"] is None
    assert "нет данных" in a["funnel"]
    assert a["has_rooms"] is False
    assert a["telegram"] == {"start": "—", "ok": 0, "taken": 0}
    assert a["profile"]["signup"] == "—" and a["profile"]["first"] == 0


async def test_accounts_since_text_names_versions(monkeypatch):
    monkeypatch.setattr(stats_dashboard, "ACCOUNT_EVENTS_SINCE", {"ios": "1.9.6", "android": "1.8.1"})
    await _clear()
    a = (await _data())["accounts"]
    assert a["since_text"] == "Данные пойдут с версий iOS 1.9.6 и Android 1.8.1."
    # Остальные разделы — со своими версиями
    assert "1.7.2" in (await _data())["since_text"]


async def test_accounts_section_mixes_server_outcomes_and_client_tries():
    await _clear()
    today = date.today()
    y = today - timedelta(days=1)
    async with SessionLocal() as session:
        session.add_all([
            _rolled(y, "login", "telegram:new", 3), _rolled(y, "login", "telegram:back", 2),
            _rolled(y, "login", "apple:new", 1), _rolled(y, "login", "phone:back", 4),
            _rolled(y, "auth_start", "telegram", 6), _rolled(y, "auth_start", "apple", 2),
            _rolled(y, "auth_start", "phone", 5),
            _rolled(y, "auth_cancel", "telegram", 1), _rolled(y, "auth_fail", "phone", 1),
            _rolled(y, "tg_link_start", "", 2), _rolled(y, "tg_link", "ok", 1),
            _rolled(y, "tg_link", "taken", 1),
            _rolled(y, "profile_open", "signup", 3), _rolled(y, "profile_save", "first", 3),
            _rolled(y, "profile_save", "edit", 2),
            _rolled(y, "date_pick", "test-peak", 20),
            _rolled(y, "room_create", "open", 2), _rolled(y, "room_create", "own", 1),
            _rolled(y, "room_ask", "", 4), _rolled(y, "room_approve", "", 2),
            _rolled(y, "room_decline", "", 1), _rolled(y, "room_join", "", 1),
            _rolled(y, "tg_group", "ready", 2), _rolled(y, "tg_group", "joined", 4),
            _rolled(y, "room_leave", "self", 1), _rolled(y, "room_leave", "removed", 1),
            _rolled(y, "room_cancel", "", 1), _rolled(y, "room_report", "", 1),
            _rolled(y, "room_block", "", 1),
            _rolled(y, "room_share", "own", 2), _rolled(y, "calendar_month", "test-peak", 3),
            _rolled(y, "go_fail", "test-lake", 1),
            # Сегодня — из сырья: серверные события, у заявки ключа нет
            ApiEvent(kind="room_ask", device=None, ts=_at(today)),
            ApiEvent(kind="login", slug="telegram:new", device="d-today", ts=_at(today)),
        ])
        await session.commit()

    a = (await _data())["accounts"]
    assert a["client"] is True
    telegram, apple, phone, total = a["logins"]
    assert (telegram["started"], telegram["cancelled"], telegram["failed"],
            telegram["new"], telegram["back"], telegram["share"]) == (6, 1, 0, 4, 2, "100 %")
    assert (apple["new"], apple["back"], apple["share"]) == (1, 0, "50 %")
    # Отмены у входа по номеру не бывает — прочерк, а не ноль
    assert (phone["cancelled"], phone["failed"], phone["share"]) == ("—", 1, "80 %")
    assert (total["started"], total["cancelled"], total["new"], total["back"], total["share"]) == (
        13, 1, 5, 6, "85 %")
    assert a["steps"] == [
        ("Выбрали дату", 20),
        ("Комната или заявка", 8),
        ("Вступили в комнату", 3),
        ("Вошли в группу", 4),
    ]
    # В группу входит и организатор — шаг больше предыдущего, и это честно
    assert "<title>Вошли в группу: 4 (133 % от предыдущего)</title>" in a["funnel"]
    assert "<title>Apple — Новый аккаунт: 1</title>" in a["login_chart"]
    assert a["telegram"] == {"start": 2, "ok": 1, "taken": 1}
    assert a["profile"] == {"signup": 3, "rooms": 0, "edit": 0, "first": 3, "saved": 2}
    assert a["has_rooms"] is True
    rooms = a["rooms"]
    assert (rooms["open"], rooms["own"], rooms["ask"], rooms["approve"], rooms["decline"]) == (
        2, 1, 5, 2, 1)
    assert (rooms["groups"], rooms["entered"], rooms["left_self"], rooms["left_removed"]) == (
        2, 4, 1, 1)
    assert (rooms["cancel"], rooms["report"], rooms["block"]) == (1, 1, 1)
    assert a["extras"] == {"room_open": 0, "share_open": 0, "share_own": 2, "calendar": 3,
                           "go_fail": 1}


async def test_page_renders_accounts_section_with_and_without_data(admin_client):
    await _clear()
    empty = await admin_client.get("/admin/stats?period=7")
    assert empty.status_code == 200
    assert 'href="#people"' in empty.text and 'id="people"' in empty.text
    assert "Комнат за период не открывали." in empty.text
    assert "Входов за период не было." in empty.text
    assert "Данные пойдут с ближайшего обновления" in empty.text

    y = date.today() - timedelta(days=1)
    async with SessionLocal() as session:
        session.add_all([
            _rolled(y, "login", "telegram:new", 2), _rolled(y, "auth_start", "telegram", 3),
            _rolled(y, "date_pick", "test-peak", 5), _rolled(y, "room_create", "open", 1),
            _rolled(y, "room_join", "", 1), _rolled(y, "tg_group", "joined", 1),
        ])
        await session.commit()
    full = await admin_client.get("/admin/stats?period=7")
    assert full.status_code == 200, full.text[:300]
    assert "Комнат за период не открывали." not in full.text
    assert "Данные пойдут с ближайшего обновления" not in full.text
    assert "<title>Telegram — Новый аккаунт: 2</title>" in full.text
    assert "<title>Вошли в группу: 1 (100 % от предыдущего)</title>" in full.text
