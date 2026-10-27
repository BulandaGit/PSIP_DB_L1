"""Aggregate BDOT10K in EPSG:2180 and expose result geometries in EPSG:3857."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys

import psycopg
from psycopg import sql

from load import database_options


METRICS = {1: ('count', 'count', 'bigint'),
           2: ('length_m', 'm', 'double precision'),
           3: ('area_m2', 'm2', 'double precision')}
BASE_COLUMNS = ('county_code', 'h3_id', 'resolution', 'geom', 'geom_clipped',
                'area_m2', 'area_in_county_m2')


def metric_query(grid_table, parts_table, dimension):
    """Return h3_id/value. Shared edges have one deterministic owner."""
    if dimension == 1:
        return sql.SQL('''
            WITH points AS MATERIALIZED (
                SELECT (ST_DumpPoints(geom)).geom AS geom FROM {parts}
            )
            SELECT owner.h3_id, count(*)::bigint AS value
            FROM points p CROSS JOIN LATERAL (
                SELECT g.h3_id FROM {grid} g
                WHERE g.county_code = %s
                  AND g.geom_clipped && ST_Expand(p.geom, 0.0000001)
                  AND ST_DWithin(g.geom_clipped, p.geom, 0.0000001)
                ORDER BY ST_Covers(g.geom_clipped,p.geom) DESC,
                         ST_Distance(g.geom_clipped,p.geom), g.h3_id COLLATE "C"
                LIMIT 1
            ) owner GROUP BY owner.h3_id
        ''').format(grid=grid_table, parts=parts_table)
    if dimension not in (2, 3):
        raise ValueError('Unsupported geometry dimension')
    prefix = sql.SQL('''
        WITH pieces AS MATERIALIZED (
            SELECT g.h3_id, g.county_code, g.geom_clipped AS cell_geom,
                   CASE WHEN ST_Covers(g.geom_clipped,b.geom) THEN b.geom
                   ELSE ST_CollectionExtract(ST_Intersection(b.geom,g.geom_clipped), {dimension}) END AS geom
            FROM {grid} g JOIN {parts} b
              ON g.geom_clipped && b.geom AND ST_Intersects(g.geom_clipped,b.geom)
            WHERE g.county_code = %s
        )
    ''').format(grid=grid_table, parts=parts_table, dimension=sql.Literal(dimension))
    if dimension == 3:
        return prefix + sql.SQL('SELECT h3_id,sum(ST_Area(geom)) AS value FROM pieces GROUP BY h3_id')
    # A line exactly on a common edge belongs to the cell with the smaller H3 ID.
    # Ordinary lines cross boundaries only at points; their lengths are unchanged.
    return prefix + sql.SQL('''
        SELECT p.h3_id, sum(CASE
            WHEN ST_Length(ST_Intersection(p.geom,ST_Boundary(p.cell_geom))) > 0.00000001
            THEN ST_Length(ST_Difference(p.geom, COALESCE((
                SELECT ST_UnaryUnion(ST_Collect(n.geom_clipped)) FROM {grid} n
                WHERE n.county_code=p.county_code AND n.h3_id COLLATE "C" < p.h3_id COLLATE "C"
                  AND n.geom_clipped && p.geom AND ST_Intersects(n.geom_clipped,p.geom)
            ), ST_GeomFromText('POLYGON EMPTY',2180))))
            ELSE ST_Length(p.geom) END) AS value
        FROM pieces p GROUP BY p.h3_id
    ''').format(grid=grid_table)


def prepare_parts(cursor, layer, dimension, county_code, source_table=None, county_table=None):
    cursor.execute(sql.SQL('''
        CREATE TEMP TABLE analysis_clipped ON COMMIT DROP AS
        WITH normalized AS (
            SELECT source_fid, county_code,
                   CASE WHEN {dimension}=2 AND NOT ST_IsSimple(geom)
                   THEN ST_UnaryUnion(geom) ELSE geom END AS geom
            FROM {table}
        ), clipped AS MATERIALIZED (
            SELECT b.source_fid,
                   CASE WHEN ST_Covers(p.geom,b.geom) THEN b.geom
                   ELSE ST_CollectionExtract(ST_Intersection(b.geom,p.geom),{dimension}) END AS geom
            FROM normalized b CROSS JOIN {county} p
            WHERE p.county_code=%s AND b.county_code=%s
              AND b.geom && p.geom AND ST_Intersects(b.geom,p.geom)
        )
        SELECT source_fid,geom FROM clipped WHERE NOT ST_IsEmpty(geom)
    ''').format(table=source_table or sql.Identifier('bdot', layer),
                county=county_table or sql.Identifier('grid', 'powiat'), dimension=sql.Literal(dimension)),
        (county_code, county_code))
    measure = {1: 'ST_NPoints', 2: 'ST_Length', 3: 'ST_Area'}[dimension]
    cursor.execute(sql.SQL('SELECT COALESCE(sum({}(geom)),0)::double precision AS value FROM analysis_clipped').format(sql.SQL(measure)))
    reference=cursor.fetchone()['value']
    # ST_Subdivide returns no rows for zero-dimensional geometries. Keep points
    # directly, and calculate the independent reference BEFORE subdivision.
    geometry=sql.SQL('geom' if dimension==1 else 'ST_Subdivide(geom,256)')
    cursor.execute(sql.SQL('CREATE TEMP TABLE analysis_parts ON COMMIT DROP AS SELECT source_fid,{} AS geom FROM analysis_clipped').format(geometry))
    cursor.execute(sql.SQL('SELECT COALESCE(sum({}(geom)),0)::double precision AS value FROM analysis_parts').format(sql.SQL(measure)))
    actual=cursor.fetchone()['value']
    tolerance=0 if dimension==1 else max(0.01 if dimension==3 else 0.001,abs(reference)*1e-8)
    if abs(actual-reference)>tolerance:
        raise RuntimeError(f'Subdivision changed source measure: {layer}, reference={reference}, parts={actual}')
    cursor.execute('DROP TABLE analysis_clipped')
    cursor.execute('CREATE INDEX ON analysis_parts USING gist(geom)')
    cursor.execute('ANALYZE analysis_parts')
    return reference


def initialize_results(cursor, resolutions, county_code):
    for level in resolutions:
        table = sql.Identifier('grid', f'h3_r{level}_bdot')
        cursor.execute(sql.SQL('''
            CREATE TABLE IF NOT EXISTS {} (
                county_code text NOT NULL, h3_id text NOT NULL, resolution smallint NOT NULL,
                geom geometry(Polygon,2180) NOT NULL,
                geom_clipped geometry(MultiPolygon,2180) NOT NULL,
                area_m2 double precision NOT NULL, area_in_county_m2 double precision NOT NULL,
                PRIMARY KEY(county_code,h3_id)
            )
        ''').format(table))
        cursor.execute(sql.SQL('DELETE FROM {} WHERE county_code=%s').format(table), (county_code,))
        columns = sql.SQL(',').join(map(sql.Identifier, BASE_COLUMNS))
        cursor.execute(sql.SQL('INSERT INTO {} ({}) SELECT {} FROM {} WHERE county_code=%s').format(
            table, columns, columns, sql.Identifier('grid', f'h3_r{level}')), (county_code,))
        if not cursor.rowcount:
            raise RuntimeError(f'No grid cells for county {county_code}, resolution {level}')
        cursor.execute(sql.SQL('CREATE INDEX IF NOT EXISTS {} ON {} USING gist(geom_clipped)').format(
            sql.Identifier(f'h3_r{level}_bdot_geom_idx'), table))
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS grid.bdot_metrics (
            county_code text NOT NULL, layer_name text NOT NULL, metric_column text NOT NULL,
            dimension smallint NOT NULL, unit text NOT NULL, source_year integer NOT NULL,
            PRIMARY KEY(county_code,layer_name)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS grid.bdot_aggregation_checks (
            county_code text NOT NULL, resolution smallint NOT NULL, layer_name text NOT NULL,
            metric_column text NOT NULL, unit text NOT NULL, reference_total double precision NOT NULL,
            allocated_total double precision NOT NULL, difference double precision NOT NULL,
            tolerance double precision NOT NULL, passed boolean NOT NULL,
            calculated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(county_code,resolution,layer_name)
        )
    ''')


def create_3857_views(cursor, resolutions):
    for level in resolutions:
        name = f'h3_r{level}_bdot'
        cursor.execute('''SELECT column_name FROM information_schema.columns
                          WHERE table_schema='grid' AND table_name=%s ORDER BY ordinal_position''', (name,))
        expressions = []
        for record in cursor.fetchall():
            column = record['column_name']
            if column in ('geom', 'geom_clipped'):
                geometry_type = 'Polygon' if column == 'geom' else 'MultiPolygon'
                expressions.append(sql.SQL('ST_Transform({},3857)::geometry({},3857) AS {}').format(
                    sql.Identifier(column), sql.SQL(geometry_type), sql.Identifier(column)))
            else:
                expressions.append(sql.Identifier(column))
        cursor.execute(sql.SQL('CREATE OR REPLACE VIEW {} AS SELECT {} FROM {}').format(
            sql.Identifier('grid', name + '_3857'), sql.SQL(',').join(expressions), sql.Identifier('grid', name)))
        cursor.execute(sql.SQL('COMMENT ON VIEW {} IS {}').format(
            sql.Identifier('grid', name + '_3857'),
            sql.Literal('Geometries: EPSG:3857. All area/length metrics were calculated in EPSG:2180.')))
    cursor.execute('''CREATE OR REPLACE VIEW grid.powiat_3857 AS SELECT county_code,county_name,
                      ST_Transform(geom,3857)::geometry(MultiPolygon,3857) AS geom FROM grid.powiat''')


def aggregate(connection, county_code, resolutions):
    reports = []
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended('psip.grid.loader',0))")
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended('psip.bdot.pipeline',0))")
        cursor.execute('''SELECT layer_name,dimension,source_year,imported_count FROM bdot._import_manifest
                          WHERE county_code=%s ORDER BY layer_name''', (county_code,))
        layers = cursor.fetchall()
        if not layers:
            raise RuntimeError('Import BDOT10K before calculating aggregates.')
        initialize_results(cursor, resolutions, county_code)
        for number, layer in enumerate(layers, 1):
            name, dimension = layer['layer_name'], layer['dimension']
            suffix, unit, data_type = METRICS[dimension]
            column = name + '_' + suffix
            print(f'[{number}/{len(layers)}] Aggregate {name} in EPSG:2180', file=sys.stderr, flush=True)
            reference = prepare_parts(cursor, name, dimension, county_code)
            cursor.execute('''INSERT INTO grid.bdot_metrics VALUES(%s,%s,%s,%s,%s,%s)
                              ON CONFLICT(county_code,layer_name) DO UPDATE SET
                              metric_column=EXCLUDED.metric_column,dimension=EXCLUDED.dimension,
                              unit=EXCLUDED.unit,source_year=EXCLUDED.source_year''',
                           (county_code, name, column, dimension, unit, layer['source_year']))
            for level in resolutions:
                output = sql.Identifier('grid', f'h3_r{level}_bdot')
                cursor.execute(sql.SQL('ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} {} NOT NULL DEFAULT 0').format(
                    output, sql.Identifier(column), sql.SQL(data_type)))
                query = metric_query(sql.Identifier('grid', f'h3_r{level}'), sql.Identifier('analysis_parts'), dimension)
                cursor.execute(sql.SQL('''WITH values_per_cell AS ({}) UPDATE {} o SET {}=v.value
                                         FROM values_per_cell v WHERE o.county_code=%s AND o.h3_id=v.h3_id''').format(
                    query, output, sql.Identifier(column)), (county_code, county_code))
                cursor.execute(sql.SQL('''SELECT COALESCE(sum({}),0)::double precision AS total,
                                         count(*) FILTER(WHERE {} IS NULL OR {} < 0) AS invalid
                                         FROM {} WHERE county_code=%s''').format(
                    sql.Identifier(column), sql.Identifier(column), sql.Identifier(column), output), (county_code,))
                statistics = cursor.fetchone()
                difference = abs(statistics['total'] - reference)
                tolerance = 0 if dimension == 1 else max(0.01 if dimension == 3 else 0.001, abs(reference) * 1e-8)
                if statistics['invalid'] or difference > tolerance:
                    raise RuntimeError(f'Conservation check failed: {name}, r{level}, reference={reference}, allocated={statistics["total"]}, difference={difference}, tolerance={tolerance}')
                cursor.execute('''INSERT INTO grid.bdot_aggregation_checks
                    (county_code,resolution,layer_name,metric_column,unit,reference_total,
                     allocated_total,difference,tolerance,passed)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,true)
                    ON CONFLICT(county_code,resolution,layer_name) DO UPDATE SET
                    metric_column=EXCLUDED.metric_column,unit=EXCLUDED.unit,
                    reference_total=EXCLUDED.reference_total,allocated_total=EXCLUDED.allocated_total,
                    difference=EXCLUDED.difference,tolerance=EXCLUDED.tolerance,passed=true,calculated_at=now()
                ''', (county_code,level,name,column,unit,reference,statistics['total'],difference,tolerance))
                reports.append({'layer': name, 'resolution': level, 'column': column, 'unit': unit,
                                'reference_total': reference, 'allocated_total': statistics['total'],
                                'difference': difference, 'tolerance': tolerance, 'passed': True})
            cursor.execute('DROP TABLE analysis_parts')
        create_3857_views(cursor, resolutions)
        for level in resolutions:
            cursor.execute(sql.SQL('ANALYZE {}').format(sql.Identifier('grid',f'h3_r{level}_bdot')))
    return {'county_code':county_code, 'calculation_epsg':2180, 'result_epsg':3857,
            'layer_count':len(layers), 'resolutions':resolutions, 'checks':reports}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--county-code',default='0602')
    parser.add_argument('--resolutions',type=int,nargs='+',choices=range(16),default=[6,7,8,9])
    parser.add_argument('--report',type=Path)
    parser.add_argument('--csv',type=Path)
    args=parser.parse_args()
    try:
        with psycopg.connect(**database_options()) as connection:
            result=aggregate(connection,args.county_code,sorted(set(args.resolutions)))
        document=json.dumps(result,ensure_ascii=False,indent=2)
        if args.report:
            args.report.parent.mkdir(parents=True,exist_ok=True)
            args.report.write_text(document+'\n',encoding='utf-8')
        if args.csv:
            args.csv.parent.mkdir(parents=True,exist_ok=True)
            with args.csv.open('w',encoding='utf-8-sig',newline='') as stream:
                writer=csv.DictWriter(stream,fieldnames=list(result['checks'][0]))
                writer.writeheader();writer.writerows(result['checks'])
        print(document)
    except (OSError,ValueError,RuntimeError,psycopg.Error) as error:
        parser.exit(1,f'BDOT aggregation failed: {error}\n')


if __name__=='__main__':
    main()
