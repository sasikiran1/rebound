-- FieldDesk schema. Indexes are created after bulk load (see indexes.sql).

CREATE TABLE regions (
    id          smallint PRIMARY KEY,
    code        text NOT NULL UNIQUE,
    grid_x      smallint NOT NULL,
    grid_y      smallint NOT NULL
);

CREATE TABLE region_distances (
    from_region smallint NOT NULL REFERENCES regions(id),
    to_region   smallint NOT NULL REFERENCES regions(id),
    distance    smallint NOT NULL,
    PRIMARY KEY (from_region, to_region)
);

CREATE TABLE skills (
    id          smallint PRIMARY KEY,
    code        text NOT NULL UNIQUE
);

CREATE TABLE service_types (
    id          smallint PRIMARY KEY,
    code        text NOT NULL UNIQUE,
    name        text NOT NULL,
    skill_id    smallint NOT NULL REFERENCES skills(id)
);

CREATE TABLE technicians (
    id          integer PRIMARY KEY,
    name        text NOT NULL,
    home_region smallint NOT NULL REFERENCES regions(id)
);

CREATE TABLE technician_skills (
    technician_id integer NOT NULL REFERENCES technicians(id),
    skill_id      smallint NOT NULL REFERENCES skills(id),
    PRIMARY KEY (technician_id, skill_id)
);

CREATE TABLE technician_regions (
    technician_id integer NOT NULL REFERENCES technicians(id),
    region_id     smallint NOT NULL REFERENCES regions(id),
    PRIMARY KEY (technician_id, region_id)
);

CREATE TABLE customers (
    id          integer PRIMARY KEY,
    name        text NOT NULL,
    region_id   smallint NOT NULL REFERENCES regions(id)
);

CREATE TABLE slots (
    id            integer PRIMARY KEY,
    technician_id integer NOT NULL REFERENCES technicians(id),
    slot_date     date NOT NULL,
    starts_at     timestamp NOT NULL
);

CREATE TABLE appointments (
    id              integer PRIMARY KEY,
    customer_id     integer NOT NULL REFERENCES customers(id),
    service_type_id smallint NOT NULL REFERENCES service_types(id),
    slot_id         integer NOT NULL REFERENCES slots(id),
    status          text NOT NULL CHECK (status IN ('scheduled', 'cancelled', 'completed'))
);

CREATE TABLE appointment_changes (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    appointment_id  integer NOT NULL REFERENCES appointments(id),
    from_slot_id    integer NOT NULL REFERENCES slots(id),
    to_slot_id      integer NOT NULL REFERENCES slots(id),
    idempotency_key text NOT NULL,
    applied_at      timestamptz NOT NULL DEFAULT clock_timestamp()
);
