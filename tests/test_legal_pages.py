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
