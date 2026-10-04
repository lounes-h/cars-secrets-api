from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import psycopg
import pytest

from app.db import make_pool
from app.lots import Counted, CountQuery, LotQuery, count_lots, list_lots, sale_windows, zone
from tests.conftest import insert_lot


def lot_numbers(response) -> list[str]:
    return [lot["lot_number"] for lot in response.json()["items"]]


def test_lists_active_lots_newest_first(client, conn):
    insert_lot(conn, "3", first_seen_at="2026-09-03T00:00Z")
    insert_lot(conn, "1", first_seen_at="2026-09-05T00:00Z", predicted_final_price=4250.5)
    insert_lot(conn, "9", first_seen_at="2026-09-01T00:00Z")
    insert_lot(conn, "2", first_seen_at="2026-09-09T00:00Z", removed_at="2026-09-18T00:00Z")

    response = client.get("/api/lots")

    assert response.status_code == 200
    assert lot_numbers(response) == ["1", "3", "9"]
    assert response.json()["total"] == 3
    first = response.json()["items"][0]
    assert first["model"] == "CAMRY"
    assert first["listing_url"] == "https://www.copart.com/lot/1"
    assert first["predicted_final_price"] == 4250.5  # a JSON number
    assert response.json()["items"][1]["predicted_final_price"] is None
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
    insert_lot(conn, "1", odometer=54321.0, high_bid=2500, predicted_final_price=3175)
    insert_lot(conn, "2", removed_at="2026-09-18T00:00Z")
    insert_lot(conn, "3", source="iaai")

    response = client.get("/api/lots/copart/1")

    assert response.status_code == 200
    lot = response.json()
    assert lot["lot_number"] == "1"
    assert lot["odometer"] == 54321
    assert lot["high_bid"] == 2500
    assert lot["predicted_final_price"] == 3175
    assert lot["listing_url"] == "https://www.copart.com/lot/1"
    assert client.get("/api/lots/iaai/3").json()["listing_url"] is None
    assert client.get("/api/lots/iaai/3").json()["predicted_final_price"] is None  # not priced
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


# --- search, filters, order and counts ---------------------------------------------------------

def listed(client, **params) -> list[str]:
    response = client.get("/api/lots", params=params)
    assert response.status_code == 200, response.text
    return sorted(lot_numbers(response))


def test_list_filters_match_any_of_their_values(client, conn):
    insert_lot(conn, "1", title_state="CA", title_type="CQ", runs_drives="RUN & DRIVE VERIFIED",
               has_keys="YES", sale_status="PURE SALE", damage_primary="FRONT END",
               damage_secondary="SIDE", location_state="CA", vehicle_type="V")
    insert_lot(conn, "2", title_state="ca ", title_type=" as", runs_drives="VEHICLE STARTS",
               has_keys="NO", sale_status="ON MINIMUM BID", damage_primary="REAR END",
               location_state="TX", vehicle_type="K", offsite_city="ALLENTOWN")
    insert_lot(conn, "3", title_state="CA", title_type="AN", runs_drives="DEFAULT",
               has_keys="EXM", sale_status="ON APPROVAL", damage_primary="HAIL")
    insert_lot(conn, "4", title_state="ZZ", title_type="??", runs_drives=None, source="iaai")

    assert listed(client, title="clean") == ["1"]
    assert listed(client, title=["salvage", "nonrepairable"]) == ["2", "3"]
    assert listed(client, run="verified") == ["1"]
    assert listed(client, run="starts") == ["2"]
    assert listed(client, run="unverified") == ["3", "4"]  # DEFAULT is nobody checked it
    assert listed(client, keys=["no", "exempt"]) == ["2", "3"]
    assert listed(client, sale_type="pure") == ["1"]
    assert listed(client, sale_type=["minimum", "approval"]) == ["2", "3"]
    assert listed(client, damage=["FRONT END", "HAIL"]) == ["1", "3"]
    assert listed(client, damage2="SIDE") == ["1"]
    assert listed(client, state="TX") == ["2"]
    assert listed(client, vehicle_type=["V", "K"]) == ["1", "2"]
    assert listed(client, location="offsite") == ["2"]
    assert listed(client, location="yard") == ["1", "3", "4"]
    assert listed(client, source=["copart", "iaai"]) == ["1", "2", "3", "4"]
    assert listed(client, title="clean", run="starts") == []  # filters narrow together


def test_list_ranges_and_buy_now(client, conn):
    insert_lot(conn, "1", odometer=50000, est_retail_value=8000, buy_it_now_price=3000,
               high_bid=0)
    insert_lot(conn, "2", odometer=150000, est_retail_value=20000, buy_it_now_price=0,
               high_bid=4000)
    insert_lot(conn, "3", odometer=None, est_retail_value=None, buy_it_now_price=None,
               high_bid=None)

    assert listed(client, odometer_max=100000) == ["1"]
    assert listed(client, retail_min=10000) == ["2"]
    assert listed(client, retail_max=10000) == ["1"]
    assert listed(client, buy_now=True) == ["1"]
    assert listed(client, bin_max=5000) == ["1"]  # a Buy now price, not an absent one
    assert listed(client, bid_max=1000) == ["1", "3"]  # no bid yet is a $0 bid
    assert client.get("/api/lots", params={"odometer_max": -1}).status_code == 422


def test_list_make_and_model(client, conn):
    insert_lot(conn, "1", make="TOYOTA", model_group="RAV4")
    insert_lot(conn, "2", make="TOYOTA", model_group="RAV4 BASE")
    insert_lot(conn, "3", make="TOYOTA", model_group="RAV4X")
    insert_lot(conn, "4", make="HONDA", model_group="CR-V")
    insert_lot(conn, "5", make="TOYOTA", model_group="RA_4 X")

    assert listed(client, make="toyota") == ["1", "2", "3", "5"]
    assert listed(client, make="TOYOTA", model="rav4") == ["1", "2"]  # Copart's RAV4 groups
    assert listed(client, model="RA_4") == ["5"]  # LIKE wildcards are literal


def test_search_words_all_match(client, conn):
    insert_lot(conn, "1", make="TOYOTA", model_group="PRIUS", fuel_type="HYBRID ENGINE",
               color="WHITE")
    insert_lot(conn, "2", make="TOYOTA", model_group="CAMRY", trim="XLE HYBRID", color="BLACK")
    insert_lot(conn, "3", make="FORD", model_group="F150", drive="4x4", body_style="PICKUP")

    assert listed(client, q="hybrid") == ["1", "2"]
    assert listed(client, q="hybrid white") == ["1"]
    assert listed(client, q="toyota xle") == ["2"]
    assert listed(client, q="pickup 4x4") == ["3"]


@pytest.mark.parametrize(("sale", "expected"), [
    ("today", ["1", "2"]),           # Saturday, in New York
    ("tomorrow", ["3"]),
    ("this_week", ["1", "2", "3"]),  # to Sunday night
    ("next_week", ["4"]),            # Monday to Sunday
    ("future", ["5"]),               # no time and no day yet
])
def test_sale_windows_in_the_dealers_zone(conn, sale, expected):
    now = datetime(2026, 10, 3, 21, 0, tzinfo=ZoneInfo("America/New_York"))
    insert_lot(conn, "1", sale_at="2026-10-03T13:00Z")   # 9 am Saturday
    insert_lot(conn, "2", sale_at="2026-10-04T02:00Z")   # 10 pm Saturday, Sunday in UTC
    insert_lot(conn, "3", sale_at="2026-10-04T16:00Z")   # Sunday
    insert_lot(conn, "4", sale_at="2026-10-05T16:00Z")   # next Monday
    insert_lot(conn, "5", sale_at=None, sale_date=None)
    insert_lot(conn, "6", sale_at=None, sale_date="2026-10-20")  # a day but no time: neither

    page = list_lots(conn, LotQuery(sale=sale, tz="America/New_York"), now=now)

    assert sorted(lot.lot_number for lot in page.items) == expected


def test_an_unknown_zone_is_utc(client, conn):
    assert zone("Mars/Olympus") == ZoneInfo("UTC")
    assert zone("../etc/passwd") == ZoneInfo("UTC")
    assert zone("America") == ZoneInfo("UTC")
    response = client.get("/api/lots", params={"tz": "Mars/Olympus", "sale": "today"})
    assert response.status_code == 200


def test_sale_windows_cross_a_clock_change():
    # Sunday 1 November 2026: New York falls back an hour, so that day lasts 25 hours.
    windows = sale_windows(datetime(2026, 11, 1, 12, tzinfo=ZoneInfo("America/New_York")))

    start, stop = windows["today"]
    assert stop.astimezone(UTC) - start.astimezone(UTC) == timedelta(hours=25)
    assert windows["this_week"] == (start, stop)  # Sunday is the week's last day
    assert windows["next_week"][0].date().isoformat() == "2026-11-02"


@pytest.mark.parametrize(("sort", "expected"), [
    ("newest", ["2", "3", "1"]),
    ("sale", ["1", "2", "3"]),      # soonest first, no sale time last
    ("year", ["3", "1", "2"]),      # newest model year first
    ("mileage", ["2", "1", "3"]),   # lowest first, unknown last
    ("retail", ["1", "3", "2"]),    # lowest first; no value ($0) is not the lowest
])
def test_sorts(client, conn, sort, expected):
    insert_lot(conn, "1", first_seen_at="2026-09-01T00:00Z", sale_at="2026-10-01T10:00Z",
               year=2019, odometer=80000, est_retail_value=5000)
    insert_lot(conn, "2", first_seen_at="2026-09-09T00:00Z", sale_at="2026-10-02T10:00Z",
               year=2015, odometer=10000, est_retail_value=0)
    insert_lot(conn, "3", first_seen_at="2026-09-05T00:00Z", sale_at=None,
               year=2022, odometer=None, est_retail_value=9000)

    assert lot_numbers(client.get("/api/lots", params={"sort": sort})) == expected
    assert client.get("/api/lots", params={"sort": "cheapest"}).status_code == 422


def test_rejects_unknown_filter_values(client):
    assert client.get("/api/lots", params={"title": "rebuilt"}).status_code == 422
    assert client.get("/api/lots", params={"sale": "yesterday"}).status_code == 422
    assert client.get("/api/lots", params={"damage": ["X"] * 51}).status_code == 422


def counted(client, **params) -> dict[str, list[tuple[str, int]]]:
    response = client.get("/api/lots/counts", params=params)
    assert response.status_code == 200, response.text
    return {name: [(v["value"], v["count"]) for v in values]
            for name, values in response.json()["facets"].items()}


def test_counts_leave_out_their_own_filter(client, conn):
    insert_lot(conn, "1", damage_primary="FRONT END", runs_drives="RUN & DRIVE VERIFIED")
    insert_lot(conn, "2", damage_primary="FRONT END", runs_drives="VEHICLE STARTS")
    insert_lot(conn, "3", damage_primary="SIDE", runs_drives="RUN & DRIVE VERIFIED")
    insert_lot(conn, "4", damage_primary="HAIL", runs_drives="VEHICLE STARTS",
               removed_at="2026-09-18T00:00Z")

    counts = counted(client, damage="SIDE", run="verified", facets=["damage", "run"])

    # Damage counts ignore the damage choice but keep "runs and drives"; and the other way round.
    assert counts["damage"] == [("FRONT END", 1), ("SIDE", 1)]
    assert counts["run"] == [("verified", 1)]  # no lot with SIDE damage only starts: left out


def test_counts_of_every_filter(client, conn):
    insert_lot(conn, "1", title_state="CA", title_type="CQ", has_keys="YES", make="TOYOTA",
               model_group="CAMRY", sale_status="PURE SALE", buy_it_now_price=3000,
               sale_at=None, sale_date=None, location_state="CA", vehicle_type="V")
    insert_lot(conn, "2", title_state="CA", title_type="AS", has_keys="NO", make="TOYOTA",
               model_group="RAV4", sale_at="2026-10-03T15:00Z", location_state="CA",
               vehicle_type="V", offsite_address="645 W UNION ST")
    insert_lot(conn, "3", title_state="CA", title_type="AS", has_keys=None, make="HONDA",
               model_group="CIVIC", sale_at="2026-10-06T15:00Z", location_state="TX")

    response = client.get("/api/lots/counts", params={"facets": list(Counted.__args__)})
    counts = {name: [(v["value"], v["count"]) for v in values]
              for name, values in response.json()["facets"].items()}

    assert counts["title"] == [("salvage", 2), ("clean", 1)]
    assert counts["keys"] == [("no", 1), ("yes", 1)]  # no stated keys is no option
    assert counts["make"] == [("TOYOTA", 2), ("HONDA", 1)]
    assert "model" not in counts  # models are listed only within a make
    assert counts["sale_type"] == [("pure", 1)]
    assert counts["buy_now"] == [("yes", 1)]
    assert counts["location"] == [("yard", 2), ("offsite", 1)]
    assert counts["state"] == [("CA", 2), ("TX", 1)]
    assert counts["source"] == [("copart", 3)]
    assert ("future", 1) in counts["sale"]
    assert counted(client, make="toyota", facets="model")["model"] == [("CAMRY", 1), ("RAV4", 1)]
    assert counted(client, facets="model") == {}


def test_counts_of_sale_windows(conn):
    now = datetime(2026, 10, 3, 9, 0, tzinfo=ZoneInfo("UTC"))  # a Saturday
    insert_lot(conn, "1", sale_at="2026-10-03T15:00Z")
    insert_lot(conn, "2", sale_at="2026-10-04T15:00Z")
    insert_lot(conn, "3", sale_at="2026-10-06T15:00Z")
    insert_lot(conn, "4", sale_at=None, sale_date=None)

    counts = count_lots(conn, CountQuery(sale="today", facets=["sale"]), now=now).facets["sale"]

    assert sorted((v.value, v.count) for v in counts) == [
        ("future", 1), ("next_week", 1), ("this_week", 2), ("today", 1), ("tomorrow", 1)]


def test_counts_need_a_known_filter(client):
    assert client.get("/api/lots/counts").status_code == 422
    assert client.get("/api/lots/counts", params={"facets": "price"}).status_code == 422


# --- interpreting a search ---------------------------------------------------------------------

def many(conn, prefix: str, n: int = 10, **columns) -> None:
    """Enough lots of a make for it to be recognised as one."""
    for i in range(n):
        insert_lot(conn, f"{prefix}{i}", **columns)


def interpreted(client, q: str) -> dict:
    response = client.get("/api/lots/interpret", params={"q": q})
    assert response.status_code == 200
    return response.json()


def test_interpret_a_lot_number_or_vin(client, conn):
    insert_lot(conn, "41633100", vin="JH4CU2F69DC000001")
    insert_lot(conn, "41633101", vin="JH4CU2F69DC000002")
    insert_lot(conn, "41633101", source="iaai")
    insert_lot(conn, "41633102", removed_at="2026-09-18T00:00Z")

    assert interpreted(client, "41633100")["lot"] == {"source": "copart",
                                                      "lot_number": "41633100"}
    assert interpreted(client, "jh4cu2f69dc000002")["lot"] == {"source": "copart",
                                                               "lot_number": "41633101"}
    # Two auctions with the same lot number: no one lot to open, so the number stays a search.
    assert interpreted(client, "41633101") == {"lot": None, "make": None, "model": None,
                                               "year_min": None, "year_max": None,
                                               "q": "41633101"}
    assert interpreted(client, "41633102")["lot"] is None  # removed
    assert interpreted(client, "41633100 x")["lot"] is None  # one word only


def test_interpret_make_model_and_years(client, conn):
    many(conn, "t", make="TOYOTA", model_group="RAV4")
    many(conn, "m", make="MERCEDES-BENZ", model_group="C-CLASS")
    many(conn, "l", make="LAND ROVER", model_group="DEFENDER")
    insert_lot(conn, "w1", make="WHITE", model_group="TRUCK")  # a rare make: a colour here

    assert interpreted(client, "2018-2021 toyota rav4") == {
        "lot": None, "make": "TOYOTA", "model": "RAV4", "year_min": 2018, "year_max": 2021,
        "q": ""}
    assert interpreted(client, "rav4 2019")["make"] == "TOYOTA"  # a model names its make
    assert interpreted(client, "2021 2018")["year_min"] == 2018
    assert interpreted(client, "mercedes benz c class") == {
        "lot": None, "make": "MERCEDES-BENZ", "model": "C-CLASS", "year_min": None,
        "year_max": None, "q": ""}
    land = interpreted(client, "land rover hybrid")
    assert (land["make"], land["model"], land["q"]) == ("LAND ROVER", None, "hybrid")
    assert interpreted(client, "white awd")["make"] is None
    assert interpreted(client, "white awd")["q"] == "white awd"
    assert interpreted(client, "")["q"] == ""


def test_interpret_a_model_two_makes_share(client, conn):
    many(conn, "r", n=15, make="RAM", model_group="1500")
    many(conn, "g", n=10, make="GMC", model_group="1500")
    many(conn, "c", n=100, make="TOYOTA", model_group="CAMRY")
    many(conn, "x", n=11, make="TOYT", model_group="CAMRY")  # a make, but 1 Camry in 10 at most

    assert interpreted(client, "1500")["make"] is None  # neither has nearly all of them
    assert interpreted(client, "1500")["q"] == "1500"
    assert interpreted(client, "camry")["make"] == "TOYOTA"
    assert interpreted(client, "gmc 1500")["model"] == "1500"  # with the make, no doubt left
