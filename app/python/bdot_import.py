"""Import county BDOT10K shapefiles, preserving attributes, into PostGIS."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sys

import psycopg
from psycopg import sql
from pyproj import CRS, Transformer
import shapefile
from shapely import force_2d, make_valid, to_wkb
from shapely.geometry import (GeometryCollection, MultiLineString, MultiPoint,
                              MultiPolygon, shape)
from shapely.ops import transform

from load import database_options


GEOMETRY_TYPES = {1: 'MultiPoint', 2: 'MultiLineString', 3: 'MultiPolygon'}
SUFFIX_DIMENSIONS = {'p': 1, 'l': 2, 'a': 3}


@dataclass
class LayerInfo:
    path: Path
    table: str
    dimension: int
    fields: list
    crs: CRS
    encoding: str
    records: int


def discover_layers(directory: Path, county_code: str) -> list[LayerInfo]:
    if not re.fullmatch(r'\d{4}', county_code):
        raise ValueError('County code must contain four digits.')
    result = []
    for path in sorted(directory.rglob('*.shp')):
        match = re.search(r'\.(\d{4})__(OT_[A-Z0-9]+_[ALP])\.shp$', path.name, re.I)
        if not match or match[1] != county_code:
            raise ValueError(f'Shapefile does not belong to county {county_code}: {path.name}')
        encoding_path = path.with_suffix('.cpg')
        encoding = encoding_path.read_text(encoding='ascii').strip() if encoding_path.exists() else 'utf-8'
        if encoding.upper() == '65001':
            encoding = 'utf-8'
        crs = CRS.from_wkt(path.with_suffix('.prj').read_text())
        with shapefile.Reader(str(path), encoding=encoding) as reader:
            fields = reader.fields[1:]
            names = [field[0].lower() for field in fields]
            if len(set(names)) != len(names) or any(
                not re.fullmatch(r'[a-z_][a-z0-9_]*', name)
                or name in {'county_code', 'source_fid', 'geom', 'geom_wkb'} for name in names
            ):
                raise ValueError(f'Conflicting or unsupported field names: {path.name}')
            table = match[2].lower()
            result.append(LayerInfo(path, table, SUFFIX_DIMENSIONS[table[-1]],
                                    fields, crs, encoding, len(reader)))
    if not result:
        raise ValueError('No BDOT10K shapefiles found.')
    if len({layer.table for layer in result}) != len(result):
        raise ValueError('Duplicate BDOT10K classes in the source directory.')
    return result


def field_type(field) -> sql.SQL:
    kind = field[1]
    if kind in ('C', 'M'):
        return sql.SQL('text')
    if kind in ('N', 'F'):
        return sql.SQL('numeric')
    if kind == 'D':
        return sql.SQL('date')
    if kind == 'L':
        return sql.SQL('boolean')
    raise ValueError(f'Unsupported DBF field type: {kind}')


def normalize_geometry(geometry, dimension: int):
    geometry = force_2d(geometry)
    repaired = not geometry.is_valid
    if repaired:
        geometry = make_valid(geometry)
    singular = {1: 'Point', 2: 'LineString', 3: 'Polygon'}[dimension]
    parts = []

    def collect(item):
        if item.is_empty:
            return
        if item.geom_type == singular:
            parts.append(item)
        elif item.geom_type.startswith('Multi') or isinstance(item, GeometryCollection):
            for component in item.geoms:
                collect(component)

    collect(geometry)
    constructors = {1: MultiPoint, 2: MultiLineString, 3: MultiPolygon}
    result = constructors[dimension](parts)
    if not result.is_valid:
        raise ValueError('Geometry repair did not produce a valid geometry.')
    return result, repaired


def source_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    for extension in ('.shp', '.shx', '.dbf', '.prj', '.cpg'):
        component = path.with_suffix(extension)
        if not component.exists():
            continue
        digest.update(extension.encode('ascii'))
        with component.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
    return digest.hexdigest()


def import_layers(connection, layers: list[LayerInfo], county_code: str, year: int) -> dict:
    reports = []
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended('psip.bdot.pipeline', 0))")
        cursor.execute('CREATE SCHEMA IF NOT EXISTS bdot')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bdot._import_manifest (
                county_code text NOT NULL, layer_name text NOT NULL,
                source_file text NOT NULL, source_sha256 text NOT NULL,
                source_year integer NOT NULL, source_crs text NOT NULL,
                dimension smallint NOT NULL CHECK (dimension BETWEEN 1 AND 3),
                source_count bigint NOT NULL, imported_count bigint NOT NULL,
                repaired_count bigint NOT NULL, empty_count bigint NOT NULL,
                imported_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (county_code, layer_name)
            )
        ''')
        for number, layer in enumerate(layers, 1):
            print(f'[{number}/{len(layers)}] Import {layer.table}: {layer.records} records',
                  file=sys.stderr, flush=True)
            table = sql.Identifier('bdot', layer.table)
            field_names = [field[0].lower() for field in layer.fields]
            declarations = [sql.SQL('{} {}').format(sql.Identifier(name), field_type(field))
                            for name, field in zip(field_names, layer.fields)]
            cursor.execute(sql.SQL('''
                CREATE TABLE IF NOT EXISTS {} (
                    county_code text NOT NULL, source_fid bigint NOT NULL,
                    {}, geom geometry({}, 2180) NOT NULL,
                    PRIMARY KEY (county_code, source_fid)
                )
            ''').format(table, sql.SQL(', ').join(declarations),
                        sql.SQL(GEOMETRY_TYPES[layer.dimension])))
            cursor.execute(sql.SQL('''
                CREATE TEMP TABLE incoming_bdot (
                    source_fid bigint NOT NULL, {}, geom_wkb bytea NOT NULL
                ) ON COMMIT DROP
            ''').format(sql.SQL(', ').join(declarations)))
            copy_fields = ['source_fid', *field_names, 'geom_wkb']
            repaired = empty = imported = 0
            transformer = None if layer.crs.equals(CRS.from_epsg(2180)) else Transformer.from_crs(
                layer.crs, 'EPSG:2180', always_xy=True)
            with shapefile.Reader(str(layer.path), encoding=layer.encoding) as reader:
                with cursor.copy(sql.SQL('COPY incoming_bdot ({}) FROM STDIN').format(
                    sql.SQL(', ').join(map(sql.Identifier, copy_fields)))) as copy:
                    for item in reader.iterShapeRecords():
                        source_fid = item.record.oid + 1
                        values = list(item.record)
                        if 'teryt' in field_names:
                            teryt = str(values[field_names.index('teryt')]).strip()
                            if teryt != county_code:
                                raise ValueError(f'Wrong TERYT {teryt}: {layer.table}/{source_fid}')
                        if item.shape.shapeType == shapefile.NULL:
                            raise ValueError(f'Null source geometry: {layer.table}/{source_fid}')
                        geometry = shape(item.shape.__geo_interface__)
                        if transformer:
                            geometry = transform(transformer.transform, geometry)
                        geometry, changed = normalize_geometry(geometry, layer.dimension)
                        repaired += changed
                        empty += geometry.is_empty
                        copy.write_row((source_fid, *values, to_wkb(geometry)))
                        imported += 1
            if imported != layer.records:
                raise ValueError(f'Record count changed during import: {layer.table}')
            cursor.execute(sql.SQL('DELETE FROM {} WHERE county_code = %s').format(table), (county_code,))
            target_names = ['county_code', 'source_fid', *field_names, 'geom']
            source_names = ['source_fid', *field_names]
            cursor.execute(sql.SQL('''
                INSERT INTO {} ({}) SELECT %s, {},
                    ST_SetSRID(ST_GeomFromWKB(geom_wkb), 2180)
                FROM incoming_bdot
            ''').format(table, sql.SQL(', ').join(map(sql.Identifier, target_names)),
                        sql.SQL(', ').join(map(sql.Identifier, source_names))), (county_code,))
            cursor.execute(sql.SQL('CREATE INDEX IF NOT EXISTS {} ON {} USING gist (geom)').format(
                sql.Identifier(layer.table + '_geom_idx'), table))
            cursor.execute(sql.SQL('ANALYZE {}').format(table))
            cursor.execute(sql.SQL('''
                SELECT count(*) AS records,
                       bool_and(ST_IsValid(geom) AND ST_SRID(geom) = 2180) AS valid
                FROM {} WHERE county_code = %s
            ''').format(table), (county_code,))
            validation = cursor.fetchone()
            if validation['records'] != imported or (imported and not validation['valid']):
                raise RuntimeError(f'Import validation failed: {layer.table}')
            fingerprint = source_fingerprint(layer.path)
            cursor.execute('''
                INSERT INTO bdot._import_manifest
                (county_code, layer_name, source_file, source_sha256, source_year,
                 source_crs, dimension, source_count, imported_count, repaired_count, empty_count)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (county_code, layer_name) DO UPDATE SET
                    source_file=EXCLUDED.source_file, source_sha256=EXCLUDED.source_sha256,
                    source_year=EXCLUDED.source_year, source_crs=EXCLUDED.source_crs,
                    dimension=EXCLUDED.dimension, source_count=EXCLUDED.source_count,
                    imported_count=EXCLUDED.imported_count, repaired_count=EXCLUDED.repaired_count,
                    empty_count=EXCLUDED.empty_count, imported_at=now()
            ''', (county_code, layer.table, layer.path.name, fingerprint, year,
                  layer.crs.to_string(), layer.dimension, layer.records, imported, repaired, empty))
            cursor.execute('DROP TABLE incoming_bdot')
            reports.append({'layer': layer.table, 'dimension': layer.dimension,
                            'records': imported, 'repaired': repaired, 'empty': empty,
                            'source_sha256': fingerprint})
    return {'county_code': county_code, 'year': year, 'srid': 2180,
            'layer_count': len(reports), 'total_records': sum(row['records'] for row in reports),
            'layers': reports}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--county-code', default='0602')
    parser.add_argument('--year', type=int, default=2025)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    try:
        layers = discover_layers(args.source, args.county_code)
        with psycopg.connect(**database_options()) as connection:
            result = import_layers(connection, layers, args.county_code, args.year)
        document = json.dumps(result, ensure_ascii=False, indent=2)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(document + '\n', encoding='utf-8')
        print(document)
    except (OSError, ValueError, RuntimeError, psycopg.Error) as error:
        parser.exit(1, f'BDOT import failed: {error}\n')


if __name__ == '__main__':
    main()
