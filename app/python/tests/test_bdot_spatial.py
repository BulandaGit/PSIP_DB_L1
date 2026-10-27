import os
from pathlib import Path
import sys
import unittest

import psycopg
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bdot_aggregate import metric_query, prepare_parts
from load import database_options


@unittest.skipUnless(os.environ.get('RUN_BDOT_SQL_TESTS') == '1', 'Requires a PostGIS connection')
class BdotSpatialTests(unittest.TestCase):
    def setUp(self):
        self.connection = psycopg.connect(**database_options())
        self.connection.execute('''CREATE TEMP TABLE bdot_test_grid
            (h3_id text, county_code text, geom_clipped geometry(Polygon,2180))''')
        self.connection.execute('''INSERT INTO bdot_test_grid VALUES
            ('a','0602',ST_MakeEnvelope(600000,400000,600010,400010,2180)),
            ('b','0602',ST_MakeEnvelope(600010,400000,600020,400010,2180))''')
        self.connection.execute('CREATE TEMP TABLE bdot_test_parts(source_fid bigint,geom geometry(Geometry,2180))')

    def tearDown(self):
        self.connection.rollback()
        self.connection.close()

    def add(self, wkt):
        self.connection.execute('INSERT INTO bdot_test_parts VALUES(1,ST_GeomFromText(%s,2180))', (wkt,))

    def values(self, dimension):
        query = metric_query(sql.Identifier('bdot_test_grid'), sql.Identifier('bdot_test_parts'), dimension)
        rows = self.connection.execute(query, ('0602',)).fetchall()
        return {row['h3_id']: float(row['value']) for row in rows}

    def test_points_and_multipoints_have_one_boundary_owner(self):
        self.add('MULTIPOINT((600005 400005),(600010 400005),(600015 400005),(600000 400000),(600020 400010))')
        self.assertEqual(self.values(1), {'a': 3, 'b': 2})

    def test_polygon_area_is_split_between_cells(self):
        self.add('POLYGON((600005 400000,600015 400000,600015 400010,600005 400010,600005 400000))')
        self.assertEqual(self.values(3), {'a': 50, 'b': 50})

    def test_crossing_line_and_shared_edge_are_not_double_counted(self):
        self.add('LINESTRING(600000 400005,600020 400005)')
        self.add('LINESTRING(600010 400000,600010 400010)')
        self.assertEqual(self.values(2), {'a': 20, 'b': 10})

    def test_outside_geometry_does_not_enter_cells(self):
        self.add('POINT(600030 400005)')
        self.assertEqual(self.values(1), {})

    def test_retraced_line_is_normalized_before_reference_and_cell_measurement(self):
        self.connection.execute('CREATE TEMP TABLE bdot_test_source(county_code text,source_fid bigint,geom geometry(LineString,2180))')
        self.connection.execute('CREATE TEMP TABLE bdot_test_county(county_code text,geom geometry(Polygon,2180))')
        self.connection.execute("INSERT INTO bdot_test_county VALUES('0602',ST_MakeEnvelope(600000,400000,600020,400010,2180))")
        self.connection.execute("""INSERT INTO bdot_test_source VALUES('0602',1,
            ST_GeomFromText('LINESTRING(600000 400005,600020 400005,600010 400005,600015 400005)',2180))""")
        with self.connection.cursor() as cursor:
            reference = prepare_parts(cursor,'test',2,'0602',source_table=sql.Identifier('bdot_test_source'),
                                      county_table=sql.Identifier('bdot_test_county'))
            query=metric_query(sql.Identifier('bdot_test_grid'),sql.Identifier('analysis_parts'),2)
            rows=cursor.execute(query,('0602',)).fetchall()
        self.assertEqual(reference,20)
        self.assertEqual({row['h3_id']:float(row['value']) for row in rows},{'a':10,'b':10})

    def test_prepare_points_preserves_points_and_clips_before_counting(self):
        self.connection.execute('CREATE TEMP TABLE bdot_test_source(county_code text,source_fid bigint,geom geometry(MultiPoint,2180))')
        self.connection.execute('CREATE TEMP TABLE bdot_test_county(county_code text,geom geometry(Polygon,2180))')
        self.connection.execute("INSERT INTO bdot_test_county VALUES('0602',ST_MakeEnvelope(600000,400000,600020,400010,2180))")
        self.connection.execute("""INSERT INTO bdot_test_source VALUES('0602',1,ST_GeomFromText(
            'MULTIPOINT((600005 400005),(600010 400005),(600015 400005),(600000 400000),(600020 400010),(600030 400005))',2180))""")
        with self.connection.cursor() as cursor:
            reference=prepare_parts(cursor,'test',1,'0602',source_table=sql.Identifier('bdot_test_source'),
                                    county_table=sql.Identifier('bdot_test_county'))
            rows=cursor.execute(metric_query(sql.Identifier('bdot_test_grid'),sql.Identifier('analysis_parts'),1),('0602',)).fetchall()
        self.assertEqual(reference,5)
        self.assertEqual({row['h3_id']:int(row['value']) for row in rows},{'a':3,'b':2})


if __name__ == '__main__':
    unittest.main()
