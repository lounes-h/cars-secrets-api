from tests.test_auth import verified_user


def signed_in(client, mailer) -> dict[str, str]:
    verified_user(client, mailer)
    token = client.post("/api/auth/login", json={"email": "dana@example.com",
                                                 "password": "long-enough-pw"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def put(client, headers, filters: str) -> dict:
    response = client.put("/api/me/lot-preferences", json={"filters": filters}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_preferences_need_a_signed_in_dealer(client):
    assert client.get("/api/me/lot-preferences").status_code == 401
    assert client.put("/api/me/lot-preferences", json={"filters": ""}).status_code == 401


def test_a_new_dealer_has_none(client, mailer):
    headers = signed_in(client, mailer)

    response = client.get("/api/me/lot-preferences", headers=headers)

    assert response.json() == {"filters": "", "usage": {}}


def test_filters_are_kept_and_each_change_counts_as_a_use(client, mailer):
    headers = signed_in(client, mailer)

    assert put(client, headers, "title=salvage&sort=sale") == {
        "filters": "title=salvage&sort=sale", "usage": {"title": 1, "sort": 1}}
    # Same title, new damage: only damage is one more use.
    assert put(client, headers, "title=salvage&damage=SIDE&sort=sale")["usage"] == {
        "title": 1, "sort": 1, "damage": 1}
    # Changing a value counts; removing one does not; unknown parameters are not counted.
    assert put(client, headers, "title=clean&title=salvage&evil=1&page=3")["usage"] == {
        "title": 2, "sort": 1, "damage": 1}
    assert client.get("/api/me/lot-preferences", headers=headers).json()["filters"] == (
        "title=clean&title=salvage&evil=1&page=3")
    assert put(client, headers, "")["filters"] == ""  # cleared, usage kept
    assert client.get("/api/me/lot-preferences", headers=headers).json()["usage"]["title"] == 2


def test_filters_are_capped(client, mailer):
    headers = signed_in(client, mailer)

    response = client.put("/api/me/lot-preferences", json={"filters": "q=" + "x" * 2000},
                          headers=headers)

    assert response.status_code == 422
