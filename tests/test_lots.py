import psycopg
import pytest

from app.db import make_pool
from tests.conftest import insert_lot


def lot_numbers(response) -> list[str]:
    return [lot["lot_number"] for lot in response.json()["items"]]


def test_lists_active_lots_soonest_sale_first(client, conn):
    insert_lot(conn, "3", sale_at="2026-10-03T15:00Z")
    insert_lot(conn, "1", sale_at="2026-10-01T15:00Z")
    insert_lot(conn, "9", sale_at=None)
    insert_lot(conn, "2", sale_at="2026-10-02T15:00Z", removed_at="2026-09-18T00:00Z")

    response = client.get("/api/lots")

    assert response.status_code == 200
    assert lot_numbers(response) == ["1", "3", "9"]
    assert response.json()["total"] == 3
    first = response.json()["items"][0]
    assert first["model"] == "CAMRY"
    assert first["listing_url"] == "https://www.copart.com/lot/1"
    assert "attributes" not in first


@pytest.mark.parametrize(
    ("q", "expected"),
    [
        ("2", ["2"]),                # lot number, exact
        ("vin00000000000003", ["3"]),   # VIN, case-insensitive
        ("hon", ["1"]),              # make prefix
        ("civ", ["1"]),              # model prefix
        ("%", []),                   # wildcards are literal
        ("_", []),
    ],
)
def test_search(client, conn, q, expected):
    insert_lot(conn, "1", make="HONDA", model_group="CIVIC")
    insert_lot(conn, "2", make="TOYOTA", model_group="CAMRY")
    insert_lot(conn, "3", make="FORD", model_group="F150")

    assert lot_numbers(client.get("/api/lots", params={"q": q})) == expected


def test_filters(client, conn):
    insert_lot(conn, "1", make="HONDA", year=2018)
    insert_lot(conn, "2", make="HONDA", year=2021)
    insert_lot(conn, "3", make="TOYOTA", year=2021)
    insert_lot(conn, "4", source="iaai", make="TOYOTA", year=2022)

    assert lot_numbers(client.get("/api/lots", params={"make": "honda"})) == ["1", "2"]
    assert lot_numbers(client.get("/api/lots", params={"year_min": 2021})) == ["2", "3", "4"]
    assert lot_numbers(client.get("/api/lots", params={"year_max": 2018})) == ["1"]
    assert lot_numbers(client.get("/api/lots", params={"source": "iaai"})) == ["4"]


def test_pagination(client, conn):
    for n in range(1, 6):
        insert_lot(conn, str(n), sale_at=f"2026-10-0{n}T15:00Z")

    response = client.get("/api/lots", params={"limit": 2, "offset": 2})

    assert lot_numbers(response) == ["3", "4"]
    assert response.json()["total"] == 5
    assert client.get("/api/lots", params={"limit": 101}).status_code == 422
    assert client.get("/api/lots", params={"offset": -1}).status_code == 422


def test_detail(client, conn):
    insert_lot(conn, "1", odometer=54321.0, high_bid=2500)
    insert_lot(conn, "2", removed_at="2026-09-18T00:00Z")
    insert_lot(conn, "3", source="iaai")

    response = client.get("/api/lots/copart/1")

    assert response.status_code == 200
    lot = response.json()
    assert lot["lot_number"] == "1"
    assert lot["odometer"] == 54321
    assert lot["high_bid"] == 2500
    assert lot["listing_url"] == "https://www.copart.com/lot/1"
    assert client.get("/api/lots/iaai/3").json()["listing_url"] is None
    assert client.get("/api/lots/copart/2").status_code == 404  # removed
    assert client.get("/api/lots/copart/404").status_code == 404


def test_pool_connections_are_read_only(db_url):
    with (
        make_pool(db_url) as pool,
        pool.connection() as c,
        pytest.raises(psycopg.errors.ReadOnlySqlTransaction),
    ):
        c.execute("INSERT INTO lots (source, lot_number) VALUES ('x', '1')")
