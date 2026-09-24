import httpx
import psycopg
import pytest

from app.db import make_pool
from tests.conftest import insert_lot


def lot_numbers(response) -> list[str]:
    return [lot["lot_number"] for lot in response.json()["items"]]


def test_lists_active_lots_newest_first(client, conn):
    insert_lot(conn, "3", first_seen_at="2026-09-03T00:00Z")
    insert_lot(conn, "1", first_seen_at="2026-09-05T00:00Z")
    insert_lot(conn, "9", first_seen_at="2026-09-01T00:00Z")
    insert_lot(conn, "2", first_seen_at="2026-09-09T00:00Z", removed_at="2026-09-18T00:00Z")

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
    # Same first_seen_at throughout, so the lot number breaks the tie and paging is stable.
    for n in range(1, 6):
        insert_lot(conn, str(n))

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


def test_detail_carries_every_lot_column(client, conn):
    """The lot page shows all of them, so none of the synced columns may be dropped."""
    insert_lot(
        conn, "1", vehicle_type="AUTOMOBILE", engine="2.4L 4", cylinders="4", fuel_type="GAS",
        transmission="AUTOMATIC", drive="FRONT", sale_status="PURE SALE", sale_date="2026-10-01",
        yard_code="123", yard_name="CA - VALLEJO", seller_name="INSURANCE CO",
        location_zip="94590", location_country="USA", offsite_address="645 W UNION ST",
        offsite_city="ALLENTOWN", offsite_state="PA", offsite_zip="18101",
        source_created_at="2026-09-01T10:00", source_updated_at="2026-09-19T10:00Z",
    )

    lot = client.get("/api/lots/copart/1").json()

    assert lot["vehicle_type"] == "AUTOMOBILE"
    assert lot["engine"] == "2.4L 4"
    assert lot["cylinders"] == "4"
    assert lot["fuel_type"] == "GAS"
    assert lot["transmission"] == "AUTOMATIC"
    assert lot["drive"] == "FRONT"
    assert lot["sale_status"] == "PURE SALE"
    assert lot["sale_date"] == "2026-10-01"
    assert lot["yard_code"] == "123"
    assert lot["yard_name"] == "CA - VALLEJO"
    assert lot["seller_name"] == "INSURANCE CO"
    assert lot["location_zip"] == "94590"
    assert lot["location_country"] == "USA"
    assert [lot[f"offsite_{k}"] for k in ("address", "city", "state", "zip")] == [
        "645 W UNION ST", "ALLENTOWN", "PA", "18101"]
    assert lot["source_created_at"].startswith("2026-09-01T10:00")
    assert lot["source_updated_at"].startswith("2026-09-19T10:00")
    assert "attributes" not in lot and "row_hash" not in lot


def test_pool_connections_are_read_only(db_url):
    with (
        make_pool(db_url) as pool,
        pool.connection() as c,
        pytest.raises(psycopg.errors.ReadOnlySqlTransaction),
    ):
        c.execute("INSERT INTO lots (source, lot_number) VALUES ('x', '1')")


PHOTO_LIST = "http://inventoryv2.copart.io/v1/lotImages/1?country=us"
THUMB = "https://cs.copart.com/v1/AUTH_svc/lpp/0926/a_thb.jpg"


def photos(sequence: int, name: str) -> dict:
    return {"sequence": sequence, "link": [
        {"url": f"https://cs.copart.com/{name}_thb.jpg", "isThumbNail": True},
        {"url": f"https://cs.copart.com/{name}_ful.jpg"},
        {"url": f"https://cs.copart.com/{name}_hrs.jpg", "isHdImage": True},
        {"url": f"https://cs.copart.com/{name}.mp3", "isEngineSound": True},
    ]}


def test_images_lists_every_photo(client, conn, http_handler):
    insert_lot(conn, "1", image_url=PHOTO_LIST, thumbnail_url=THUMB)
    http_handler.fn = lambda request: httpx.Response(200, json={"lotImages": [
        photos(2, "second"), photos(1, "first"),
    ]})

    response = client.get("/api/lots/copart/1/images")

    assert response.status_code == 200
    # Copart's own order (by sequence), thumbnail and full size of each photo.
    assert response.json() == {"images": [
        {"thumb": "https://cs.copart.com/first_thb.jpg",
         "full": "https://cs.copart.com/first_ful.jpg"},
        {"thumb": "https://cs.copart.com/second_thb.jpg",
         "full": "https://cs.copart.com/second_ful.jpg"},
    ]}


def test_images_ignore_urls_off_copart(client, conn, http_handler):
    insert_lot(conn, "1", image_url=PHOTO_LIST, thumbnail_url=None)
    http_handler.fn = lambda request: httpx.Response(200, json={"lotImages": [
        {"sequence": 1, "link": [{"url": "https://evil.example/a_ful.jpg"}]},
        {"sequence": 2, "link": [{"url": "https://cs.copart.com/ok_ful.jpg"}]},
    ]})

    response = client.get("/api/lots/copart/1/images")

    assert response.json() == {"images": [{"thumb": None,
                                          "full": "https://cs.copart.com/ok_ful.jpg"}]}


def test_images_never_ask_a_host_the_caller_chose(client, conn, http_handler):
    """The stored URL decides what is fetched; an unknown host is not called at all."""
    insert_lot(conn, "1", image_url="https://evil.example/v1/lotImages/1", thumbnail_url=THUMB)
    http_handler.fn = lambda request: pytest.fail(f"fetched {request.url}")

    assert client.get("/api/lots/copart/1/images").json() == {"images": [
        {"thumb": THUMB, "full": THUMB.replace("_thb.", "_ful.")},
    ]}


@pytest.mark.parametrize("answer", [
    httpx.Response(404),
    httpx.Response(200, text="not json"),
    httpx.Response(200, json={"error": "nope"}),
])
def test_images_fall_back_to_the_thumbnail(client, conn, http_handler, answer):
    insert_lot(conn, "1", image_url=PHOTO_LIST, thumbnail_url=THUMB)
    http_handler.fn = lambda request: answer

    assert client.get("/api/lots/copart/1/images").json() == {"images": [
        {"thumb": THUMB, "full": THUMB.replace("_thb.", "_ful.")},
    ]}


def test_images_of_a_lot_without_photos(client, conn, http_handler):
    insert_lot(conn, "1", image_url=None, thumbnail_url=None)
    insert_lot(conn, "2", removed_at="2026-09-18T00:00Z")

    assert client.get("/api/lots/copart/1/images").json() == {"images": []}
    assert client.get("/api/lots/copart/2/images").status_code == 404  # removed
    assert client.get("/api/lots/copart/404/images").status_code == 404
