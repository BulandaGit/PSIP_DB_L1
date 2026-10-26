"""Generate county H3 grids and load them into the PostGIS grid schema."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from shapely.geometry import mapping

from h3_generator import (
    DEFAULT_BOUNDARY,
    DEFAULT_RESOLUTIONS,
    County,
    GridCell,
    generate_cells,
    read_county,
)


AREA_TOLERANCE_M2 = 1.0


def database_options() -> dict:
    """Read connection settings without including a password in source or logs."""
    password = os.environ.get("PGPASSWORD")
    if not password:
        raise ValueError("Set PGPASSWORD before loading or checking the database.")
    return {
        "host": os.environ.get("PGHOST", "postgis"),
        "port": int(os.environ.get("PGPORT", "5432")),
        "user": os.environ.get("PGUSER", "psip"),
        "dbname": os.environ.get("PGDATABASE", "psip"),
        "password": password,
        "connect_timeout": 15,
        "application_name": "psip_h3_loader",
        "row_factory": dict_row,
    }


def load_grids(connection, county: County, cells: list[GridCell], resolutions: list[int]) -> None:
    """Replace this county's requested grids; all other data remain intact."""
    with connection.cursor() as cursor:
        # Serialize cooperating loaders, including creation of shared tables.
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended('psip.grid.loader', 0))")
        cursor.execute("CREATE SCHEMA IF NOT EXISTS grid")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS grid.powiat (
                county_code text PRIMARY KEY,
                county_name text NOT NULL,
                geom geometry(MultiPolygon, 2180) NOT NULL
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS powiat_geom_idx
            ON grid.powiat USING gist (geom)
        """)
        cursor.execute("""
            INSERT INTO grid.powiat (county_code, county_name, geom)
            VALUES (%s, %s, ST_Multi(ST_Transform(
                ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), 2180)))
            ON CONFLICT (county_code) DO UPDATE
            SET county_name = EXCLUDED.county_name, geom = EXCLUDED.geom
        """, (county.code, county.name, json.dumps(mapping(county.geometry))))

        cursor.execute("""
            CREATE TEMP TABLE incoming_h3 (
                h3_id text PRIMARY KEY,
                resolution smallint NOT NULL,
                geometry_json text NOT NULL
            ) ON COMMIT DROP
        """)
        with cursor.copy("COPY incoming_h3 (h3_id, resolution, geometry_json) FROM STDIN") as copy:
            for cell in cells:
                copy.write_row((cell.h3_id, cell.resolution, json.dumps(mapping(cell.geometry))))

        for resolution in resolutions:
            table_name = f"h3_r{resolution}"
            table = sql.Identifier("grid", table_name)
            cursor.execute(sql.SQL("""
                CREATE TABLE IF NOT EXISTS {} (
                    county_code text NOT NULL REFERENCES grid.powiat(county_code),
                    h3_id text NOT NULL,
                    resolution smallint NOT NULL CHECK (resolution = {}),
                    geom geometry(Polygon, 2180) NOT NULL,
                    geom_clipped geometry(MultiPolygon, 2180) NOT NULL,
                    area_m2 double precision NOT NULL CHECK (area_m2 > 0),
                    area_in_county_m2 double precision NOT NULL CHECK (area_in_county_m2 > 0),
                    PRIMARY KEY (county_code, h3_id)
                )
            """).format(table, sql.Literal(resolution)))
            for column in ("geom", "geom_clipped"):
                cursor.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} USING gist ({})").format(
                    sql.Identifier(f"{table_name}_{column}_idx"), table, sql.Identifier(column)
                ))
            cursor.execute(sql.SQL("DELETE FROM {} WHERE county_code = %s").format(table),
                           (county.code,))
            cursor.execute(sql.SQL("""
                WITH projected AS MATERIALIZED (
                    SELECT h3_id, resolution,
                           ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(geometry_json), 4326),
                                        2180)::geometry(Polygon, 2180) AS geom
                    FROM incoming_h3 WHERE resolution = %s
                ), clipped AS MATERIALIZED (
                    SELECT h.h3_id, h.resolution, h.geom,
                           CASE WHEN ST_Covers(p.geom, h.geom) THEN ST_Multi(h.geom)
                           ELSE ST_Multi(ST_CollectionExtract(ST_Intersection(h.geom, p.geom), 3)) END
                               ::geometry(MultiPolygon, 2180) AS geom_clipped
                    FROM projected h CROSS JOIN grid.powiat p
                    WHERE p.county_code = %s
                )
                INSERT INTO {} (county_code, h3_id, resolution, geom, geom_clipped,
                                area_m2, area_in_county_m2)
                SELECT %s, h3_id, resolution, geom, geom_clipped,
                       ST_Area(geom), ST_Area(geom_clipped)
                FROM clipped
                WHERE NOT ST_IsEmpty(geom_clipped) AND ST_Area(geom_clipped) > 0
            """).format(table), (resolution, county.code, county.code))


def validate_grids(connection, county: County, resolutions: list[int]) -> dict:
    """Check geometry, coverage and checksums, raising before commit on errors."""
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT county_name, ST_Area(geom) AS area_m2,
                   ST_IsValid(geom) AND NOT ST_IsEmpty(geom) AND ST_SRID(geom) = 2180 AS valid,
                   ST_Area(ST_SymDifference(geom, ST_Multi(ST_Transform(
                       ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), 2180)))) AS boundary_difference_m2
            FROM grid.powiat WHERE county_code = %s
        """, (json.dumps(mapping(county.geometry)), county.code))
        boundary = cursor.fetchone()
        if not boundary or not boundary["valid"] or boundary["area_m2"] <= 0:
            raise RuntimeError(f"Missing or invalid county boundary: {county.code}")
        if boundary["county_name"] != county.name or boundary["boundary_difference_m2"] > AREA_TOLERANCE_M2:
            raise RuntimeError("The database county differs from the supplied boundary file.")

        grids = []
        for resolution in resolutions:
            table_name = f"h3_r{resolution}"
            cursor.execute(sql.SQL("""
                WITH statistics AS (
                    SELECT count(*) AS cell_count,
                           md5(string_agg(h3_id, ',' ORDER BY h3_id COLLATE "C")) AS ids_md5,
                           bool_and(ST_IsValid(geom) AND NOT ST_IsEmpty(geom)
                                    AND ST_SRID(geom) = 2180
                                    AND ST_IsValid(geom_clipped) AND NOT ST_IsEmpty(geom_clipped)
                                    AND ST_SRID(geom_clipped) = 2180
                                    AND resolution = %s AND area_m2 > 0
                                    AND area_in_county_m2 > 0) AS valid,
                           sum(ST_Area(geom_clipped)) AS clipped_area_m2,
                           ST_UnaryUnion(ST_Collect(geom_clipped)) AS coverage
                    FROM {} WHERE county_code = %s
                )
                SELECT s.cell_count, s.ids_md5, s.valid, s.clipped_area_m2,
                       ST_Area(ST_Difference(p.geom, s.coverage)) AS uncovered_m2,
                       ST_Area(ST_Difference(s.coverage, p.geom)) AS outside_m2,
                       abs(s.clipped_area_m2 - ST_Area(s.coverage)) AS overlap_m2
                FROM statistics s CROSS JOIN grid.powiat p WHERE p.county_code = %s
            """).format(sql.Identifier("grid", table_name)), (resolution, county.code, county.code))
            result = cursor.fetchone()
            if not result or not result["cell_count"] or not result["valid"]:
                raise RuntimeError(f"Empty or invalid grid: grid.{table_name}")
            for metric in ("uncovered_m2", "outside_m2", "overlap_m2"):
                if result[metric] is None or result[metric] > AREA_TOLERANCE_M2:
                    raise RuntimeError(f"grid.{table_name}: {metric}={result[metric]} exceeds 1 m2.")
            result.pop("valid")
            grids.append({"table": f"grid.{table_name}", "resolution": resolution, **result})

    return {
        "county_code": county.code,
        "county_name": county.name,
        "srid": 2180,
        "county_area_m2": boundary["area_m2"],
        "grids": grids,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--boundary", type=Path, default=DEFAULT_BOUNDARY)
    parser.add_argument("--resolutions", type=int, choices=range(16), nargs="+",
                        default=list(DEFAULT_RESOLUTIONS))
    parser.add_argument("--validate-only", action="store_true",
                        help="Check existing grids without changing the database.")
    args = parser.parse_args(argv)
    try:
        county = read_county(args.boundary)
        resolutions = sorted(set(args.resolutions))
        options = database_options()
        cells = [] if args.validate_only else generate_cells(county, resolutions)
        # A failure in loading or validation rolls back every database change.
        with psycopg.connect(**options) as connection:
            if args.validate_only:
                connection.execute("SET TRANSACTION READ ONLY")
            else:
                load_grids(connection, county, cells, resolutions)
            result = validate_grids(connection, county, resolutions)
        result["operation"] = "validated" if args.validate_only else "loaded"
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    except (OSError, ValueError, RuntimeError, psycopg.Error) as error:
        parser.exit(1, f"Grid operation failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
