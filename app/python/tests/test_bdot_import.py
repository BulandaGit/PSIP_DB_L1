import sys
from pathlib import Path
import tempfile
import unittest

from pyproj import CRS
import shapefile
from shapely.geometry import GeometryCollection, LineString, Point, Polygon

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bdot_import import discover_layers, normalize_geometry, source_fingerprint


class BdotImportTests(unittest.TestCase):
    def test_repair_polygon_and_keep_polygon_parts(self):
        bow_tie = Polygon([(0, 0), (2, 2), (2, 0), (0, 2), (0, 0)])
        result, repaired = normalize_geometry(bow_tie, 3)
        self.assertTrue(repaired)
        self.assertTrue(result.is_valid)
        self.assertEqual(result.geom_type, 'MultiPolygon')
        self.assertAlmostEqual(result.area, 2)

    def test_preserve_geometry_dimension_and_remove_z(self):
        result, repaired = normalize_geometry(Point(1, 2, 3), 1)
        self.assertFalse(repaired)
        self.assertEqual(result.geom_type, 'MultiPoint')
        self.assertFalse(result.has_z)
        line, _ = normalize_geometry(GeometryCollection([Point(0, 0), LineString([(0, 0), (1, 1)])]), 2)
        self.assertAlmostEqual(line.length, 2 ** 0.5)

    def test_discovery_metadata_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'PL.PZGiK.3700.BDOT10k.0602__OT_TEST_P.shp'
            with shapefile.Writer(str(path), shapeType=shapefile.POINT, encoding='utf-8') as writer:
                writer.field('TERYT', 'C', size=4)
                writer.point(700000, 350000)
                writer.record('0602')
            path.with_suffix('.prj').write_text(CRS.from_epsg(2180).to_wkt())
            path.with_suffix('.cpg').write_text('UTF-8')
            layers = discover_layers(Path(directory), '0602')
            self.assertEqual(len(layers), 1)
            self.assertEqual((layers[0].table, layers[0].dimension, layers[0].records), ('ot_test_p', 1, 1))
            before = source_fingerprint(path)
            path.with_suffix('.cpg').write_text('utf-8')
            self.assertNotEqual(before, source_fingerprint(path))
            with self.assertRaises(ValueError):
                discover_layers(Path(directory), '1201')


if __name__ == '__main__':
    unittest.main()
