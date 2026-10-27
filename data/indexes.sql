CREATE INDEX slots_date_tech ON slots (slot_date, technician_id);
CREATE INDEX slots_tech ON slots (technician_id);
CREATE INDEX technician_skills_skill ON technician_skills (skill_id, technician_id);
CREATE INDEX technician_regions_region ON technician_regions (region_id, technician_id);
CREATE INDEX customers_region ON customers (region_id);
CREATE INDEX appointments_customer ON appointments (customer_id);
-- Double booking is impossible: at most one scheduled appointment per slot.
CREATE UNIQUE INDEX appointments_one_scheduled_per_slot ON appointments (slot_id) WHERE status = 'scheduled';
CREATE UNIQUE INDEX appointment_changes_idempotency_key ON appointment_changes (idempotency_key);
CREATE INDEX appointment_changes_appointment ON appointment_changes (appointment_id);

GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO fielddesk;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO fielddesk;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO oracle_ro;
