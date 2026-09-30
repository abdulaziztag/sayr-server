"""Страница удаления аккаунта: Google Play требует её ссылкой в анкете
«Безопасность данных» — удалить должно быть можно и без приложения."""


async def test_удаление_аккаунта_по_русски(client):
    r = await client.get("/delete-account")
    assert r.status_code == 200
    assert "«Профиль» → «Удалить аккаунт»" in r.text
    # Без приложения — письмом, с подтверждением номера
    assert "mailto:" in r.text and "кодом" in r.text
    assert 'href="/uz/delete-account"' in r.text
    # Заявки на вход удаление обезличивает, но не стирает: по ним считаются
    # лимиты. Страница для Google Play должна говорить об этом прямо
    assert "Заявки на вход — тоже без номера" in r.text


async def test_удаление_аккаунта_по_узбекски(client):
    r = await client.get("/uz/delete-account")
    assert r.status_code == 200
    assert '<html lang="uz">' in r.text
    assert "«Profil» → «Akkauntni o‘chirish»" in r.text
    assert "mailto:" in r.text
    assert 'href="/delete-account"' in r.text
    assert "Kirish so‘rovlari ham raqamsiz qoladi" in r.text


async def test_политика_про_вход_через_telegram_и_apple_и_попутчиков(client):
    r = await client.get("/privacy")
    assert r.status_code == 200
    text = r.text
    # Устаревшее «анкету пока не видит никто» ушло вместе с выходом попутчиков
    assert "Сейчас — никто" not in text
    assert "<b>Вход через Telegram.</b>" in text
    assert "<b>Вход с Apple.</b>" in text
    assert "отозвать доступ Sayr к вашему Apple ID" in text
    assert "<h2>Попутчики</h2>" in text
    assert "Sayr Admin" in text and "Храним полгода" in text and "/leave" in text
    assert "Обновлено 30 сентября 2026" in text


async def test_удаление_аккаунта_про_apple(client):
    ru = (await client.get("/delete-account")).text
    assert "Telegram, Apple или номер телефона" in ru
    assert "доступ Sayr к вашему Apple ID отзывается" in ru
    uz = (await client.get("/uz/delete-account")).text
    assert "Telegram, Apple yoki telefon raqami" in uz
    assert "Apple ID‘ingizga kirish huquqi" in uz
