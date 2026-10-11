-- First-boot database setup for the compose stack (issue #941).
--
-- PostgreSQL's entrypoint runs every *.sql file in
-- /docker-entrypoint-initdb.d through psql as POSTGRES_USER, and only on a
-- first boot against an empty data directory. This is the container
-- equivalent of the two `createdb`/`psql` floor steps the setup blocks in
-- README, CONTRIBUTING and AGENTS spell out (tests/test_docs_setup.py pins
-- them): a fresh `docker compose up` gets the same databases without the
-- reader running psql by hand.

-- The application database. Its TABLES are deliberately not created here:
-- startup applies backend/schema* through db.apply_schema(), which is
-- idempotent, content-addressed and needs the database to exist and
-- nothing more (SV-SCHEMA-AUTOAPPLY).
CREATE DATABASE claudit;

-- The auth database, external to this repository — claudit owns no schema
-- for it beyond the floor the login lookup reads.
CREATE DATABASE claudit_auth;

-- psql runs this file, so a meta-command can switch databases for the
-- statement below (a plain SQL file cannot CREATE a table in another
-- database).
\connect claudit_auth

-- Exactly the shape db.schema_check() accepts, and what the three docs'
-- setup blocks build by hand: user_id an integer type, config JSONB. The
-- table starts empty — there is no credentialed account until one is
-- provisioned out of band (README "Auth").
CREATE TABLE users (
    user_id BIGINT PRIMARY KEY,
    config  JSONB NOT NULL DEFAULT '{}'::jsonb
);