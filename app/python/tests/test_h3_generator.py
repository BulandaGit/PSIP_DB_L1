"""Spatial checks for the county generator, independent of PostgreSQL."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

import h3
from shapely import union_all
from shapely.geometry import MultiPolygon, Point, Polygon, box, mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from h3_generator import County, generate_cells, main, read_county


class H3GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.boundary = box(20.0, 50.0, 20.04, 50.04)
        self.county = County("0000", "powiat testowy", self.boundary)

    def document(self, geometry=None):
        return {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "properties": {"county_code": "0000", "county_name": "powiat testowy"},
                "geometry": mapping(self.boundary if geometry is None else geometry),
            }],
        }

    def test_ids_coordinates_and_boundary_coverage(self):
        cells = generate_cells(self.county, [7, 8])
        for resolution in (7, 8):
            level = [cell for cell in cells if cell.resolution == resolution]
            self.assertTrue(level)
            self.assertTrue(all(h3.is_valid_cell(cell.h3_id) for cell in level))
            self.assertTrue(all(h3.get_resolution(cell.h3_id) == resolution for cell in level))
            self.assertTrue(all(cell.geometry.is_valid for cell in level))
            coverage = union_all([cell.geometry for cell in level])
            self.assertLess(self.boundary.difference(coverage).area, 1e-12)
            center_id = h3.latlng_to_cell(50.02, 20.02, resolution)
            center_cell = next(cell for cell in level if cell.h3_id == center_id)
            self.assertTrue(center_cell.geometry.covers(Point(20.02, 50.02)))
            # Border cells remain complete instead of being clipped to the AOI.
            self.assertTrue(any(cell.geometry.difference(self.boundary).area > 0 for cell in level))

    def test_deterministic_order_and_duplicate_resolutions(self):
        first = generate_cells(self.county, [8, 7, 8])
        second = generate_cells(self.county, iter([7, 8]))
        first_keys = [(cell.resolution, cell.h3_id) for cell in first]
        self.assertEqual(first_keys, sorted(set(first_keys)))
        self.assertEqual(first_keys, [(cell.resolution, cell.h3_id) for cell in second])
        self.assertEqual([cell.geometry.wkb for cell in first], [cell.geometry.wkb for cell in second])

    def test_multipolygon_with_hole_and_small_detached_component(self):
        outer = box(20.0, 50.0, 20.12, 50.12)
        hole = box(20.035, 50.035, 20.085, 50.085)
        ring = Polygon(outer.exterior.coords, [hole.exterior.coords])
        island = box(20.15, 50.03, 20.1501, 50.0301)
        boundary = MultiPolygon([ring, island])
        cells = generate_cells(County("0000", "powiat testowy", boundary), [9])
        coverage = union_all([cell.geometry for cell in cells])
        self.assertLess(boundary.difference(coverage).area, 1e-12)
        self.assertTrue(coverage.covers(island.representative_point()))
        self.assertFalse(coverage.covers(Point(20.06, 50.06)))
        self.assertFalse(any(hole.covers(cell.geometry) for cell in cells))
        self.assertEqual(len(cells), len({cell.h3_id for cell in cells}))

    def test_empty_and_invalid_resolution_inputs(self):
        for values in ([], [-1], [16], [True], [7.5], ["7"], None):
            with self.subTest(values=values), self.assertRaises(ValueError):
                generate_cells(self.county, values)

    def test_empty_nonpolygonal_and_projected_geometry_are_rejected(self):
        for geometry in (Polygon(), Point(20, 50), box(400000, 500000, 401000, 501000)):
            with self.subTest(geometry=geometry.geom_type), self.assertRaises(ValueError):
                generate_cells(County("0000", "powiat testowy", geometry), [7])

    def test_polygonal_repair_and_rejection_of_collapsed_geometry(self):
        bow_tie = Polygon([(20, 50), (20.04, 50.04), (20.04, 50), (20, 50.04), (20, 50)])
        collapsed = Polygon([(20, 50), (20.01, 50), (20.02, 50), (20, 50)])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "county.geojson"
            path.write_text(json.dumps(self.document(bow_tie)), encoding="utf-8")
            repaired = read_county(path)
            self.assertIsInstance(repaired.geometry, MultiPolygon)
            self.assertTrue(repaired.geometry.is_valid)
            self.assertTrue(generate_cells(repaired, [8]))
            path.write_text(json.dumps(self.document(collapsed)), encoding="utf-8")
            with self.assertRaises(ValueError):
                read_county(path)

    def test_geojson_contract_and_cli_output(self):
        with tempfile.TemporaryDirectory() as directory:
            boundary = Path(directory) / "county.geojson"
            output = Path(directory) / "result" / "grid.geojson"
            boundary.write_text(json.dumps(self.document()), encoding="utf-8")
            county = read_county(boundary)
            self.assertEqual((county.code, county.name), ("0000", "powiat testowy"))
            self.assertEqual(main(["--boundary", str(boundary), "--resolutions", "7", "8", "--output", str(output)]), 0)
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(result["type"], "FeatureCollection")
            self.assertEqual({feature["properties"]["resolution"] for feature in result["features"]}, {7, 8})
            self.assertTrue(all(feature["geometry"]["type"] == "Polygon" for feature in result["features"]))
            self.assertTrue(all(feature["properties"]["county_code"] == "0000" for feature in result["features"]))

    def test_geojson_requires_one_feature_metadata_and_wgs84(self):
        invalid_documents = []
        empty = self.document()
        empty["features"] = []
        invalid_documents.append(empty)
        multiple = self.document()
        multiple["features"] *= 2
        invalid_documents.append(multiple)
        missing_name = self.document()
        del missing_name["features"][0]["properties"]["county_name"]
        invalid_documents.append(missing_name)
        projected = self.document()
        projected["crs"] = {"type": "name", "properties": {"name": "EPSG:2180"}}
        invalid_documents.append(projected)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "county.geojson"
            for document in invalid_documents:
                with self.subTest(document=document), self.assertRaises(ValueError):
                    path.write_text(json.dumps(document), encoding="utf-8")
                    read_county(path)


if __name__ == "__main__":
    unittest.main()
