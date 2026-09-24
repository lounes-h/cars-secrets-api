-- The API database's tables as cars-secrets-data-ingest creates them (`api_sync.ensure_schema`):
-- its worker owns `public` here and keeps `lots` in step: active lots, and removed lots until
-- their auction is over, with only the served columns. ponytail: re-copy when the ingest adds a
-- column the API exposes.
CREATE TABLE lots (
    source              text NOT NULL,
    lot_number          text NOT NULL,
    yard_code           text,
    yard_name           text,
    sale_date           date,
    sale_at             timestamptz,
    vehicle_type        text,
    year                integer,
    make                text,
    model_group         text,
    model_detail        text,
    trim                text,
    body_style          text,
    color               text,
    damage_primary      text,
    damage_secondary    text,
    title_state         text,
    title_type          text,
    has_keys            text,
    vin                 text,
    odometer            numeric,
    odometer_brand      text,
    est_retail_value    numeric,
    repair_cost         numeric,
    engine              text,
    drive               text,
    transmission        text,
    fuel_type           text,
    cylinders           text,
    runs_drives         text,
    sale_status         text,
    high_bid            numeric,
    buy_it_now_price    numeric,
    currency            text,
    location_city       text,
    location_state      text,
    location_zip        text,
    location_country    text,
    offsite_address     text,
    offsite_city        text,
    offsite_state       text,
    offsite_zip         text,
    seller_name         text,
    image_url           text,
    thumbnail_url       text,
    source_created_at   timestamp,
    source_updated_at   timestamptz,
    first_seen_at       timestamptz NOT NULL,
    last_changed_at     timestamptz NOT NULL,
    removed_at          timestamptz,
    PRIMARY KEY (source, lot_number)
);
CREATE INDEX lots_listing ON lots (first_seen_at DESC, lot_number) WHERE removed_at IS NULL;
CREATE INDEX lots_lot_number ON lots (lot_number);
CREATE INDEX lots_vin ON lots (vin);
CREATE INDEX lots_make_model ON lots (make, model_group);
CREATE INDEX lots_removed ON lots (removed_at) WHERE removed_at IS NOT NULL;

CREATE TABLE ingest_sync (
    id           boolean PRIMARY KEY DEFAULT true CHECK (id),
    sync_version bigint NOT NULL,
    synced_at    timestamptz NOT NULL
);
