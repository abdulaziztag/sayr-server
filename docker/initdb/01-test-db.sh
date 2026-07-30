#!/bin/bash
# Отдельная БД для pytest; PostGIS в основной БД включает сам образ postgis/postgis.
set -e
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres -c "CREATE DATABASE sayr_test OWNER $POSTGRES_USER;"
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d sayr_test -c "CREATE EXTENSION IF NOT EXISTS postgis;"
