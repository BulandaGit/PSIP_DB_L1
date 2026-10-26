"""Generate full H3 cells covering one county, including its boundary.

GeoJSON and the public dataclasses use WGS84 longitude/latitude coordinates.
H3's boundary API returns latitude/longitude pairs, so their order is reversed
when creating Shapely polygons. Each resolution is generated independently.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
from numbers import Integral
from pathlib import Path
from typing import Iterable

import h3
from shapely import get_coordinates, make_valid, union_all
from shapely.geometry import MultiPolygon, Polygon, mapping, shape
from shapely.geometry.base import BaseGeometry


DEFAULT_BOUNDARY = Path(__file__).resolve().parent.parent / "data" / "powiat_bilgorajski.geojson"
DEFAULT_RESOLUTIONS = (6, 7, 8, 9)


@dataclass(frozen=True)
class County:
    code: str
    name: str
    geometry: BaseGeometry


@dataclass(frozen=True)
class GridCell:
    h3_id: str
    resolution: int
    geometry: Polygon


def _polygonal_geometry(geometry: BaseGeometry) -> Polygon | MultiPolygon:
    """Validate WGS84 geometry and repair it only if the input is invalid.

    A repair that produces nonpolygonal pieces is rejected instead of silently
    discarding those pieces. Empty, nonfinite and projected coordinates are
    rejected before passing the geometry to H3.
    """
    if not isinstance(geometry, (Polygon, MultiPolygon)):
        raise ValueError("County geometry must be a Polygon or MultiPolygon.")
    if geometry.is_empty:
        raise ValueError("County geometry must not be empty.")
    if geometry.has_z:
        raise ValueError("County geometry must contain two-dimensional WGS84 coordinates.")
    coordinates = get_coordinates(geometry)
    if not all(math.isfinite(float(value)) for point in coordinates for value in point):
        raise ValueError("County coordinates must be finite.")
    min_x, min_y, max_x, max_y = geometry.bounds
    if not (-180 <= min_x <= max_x <= 180 and -90 <= min_y <= max_y <= 90):
        raise ValueError("County coordinates must be WGS84 longitude/latitude in degrees.")

    if not geometry.is_valid:
        geometry = make_valid(geometry)
    if not isinstance(geometry, (Polygon, MultiPolygon)):
        raise ValueError("Repair did not produce only polygonal geometry; inspect the boundary.")
    if geometry.is_empty or not geometry.is_valid or geometry.area <= 0:
        raise ValueError("County boundary must have valid, nonempty polygonal area.")
    return geometry


def _county_metadata(code: object, name: object) -> tuple[str, str]:
    if not isinstance(code, str) or not code.strip():
        raise ValueError("county_code must be a nonempty string.")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("county_name must be a nonempty string.")
    return code.strip(), name.strip()


def read_county(path: str | Path) -> County:
    """Read a GeoJSON FeatureCollection with exactly one county feature."""
    with Path(path).open(encoding="utf-8-sig") as source:
        document = json.load(source)
    if not isinstance(document, dict) or document.get("type") != "FeatureCollection":
        raise ValueError("Boundary must be a GeoJSON FeatureCollection.")
    features = document.get("features")
    if not isinstance(features, list) or len(features) != 1:
        raise ValueError("Boundary must contain exactly one county feature.")
    # RFC 7946 GeoJSON is WGS84. Reject explicit declarations of another CRS.
    if document.get("crs") is not None:
        crs = document["crs"]
        crs_name = crs.get("properties", {}).get("name") if isinstance(crs, dict) else None
        if crs_name not in {
            "EPSG:4326", "urn:ogc:def:crs:EPSG::4326",
            "urn:ogc:def:crs:OGC:1.3:CRS84", "OGC:CRS84",
        }:
            raise ValueError("Boundary must be transformed to WGS84 before generation.")
    feature = features[0]
    if not isinstance(feature, dict) or feature.get("type") != "Feature":
        raise ValueError("Boundary must contain a valid GeoJSON Feature.")
    properties = feature.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("County properties are missing.")
    code, name = _county_metadata(properties.get("county_code"), properties.get("county_name"))
    geometry_json = feature.get("geometry")
    if not isinstance(geometry_json, dict):
        raise ValueError("County geometry is missing.")
    try:
        geometry = shape(geometry_json)
    except (TypeError, ValueError, KeyError) as error:
        raise ValueError("Invalid county GeoJSON geometry.") from error
    return County(code, name, _polygonal_geometry(geometry))


def _resolutions(values: Iterable[int]) -> list[int]:
    try:
        requested = list(values)
    except TypeError as error:
        raise ValueError("Resolutions must be an iterable of integers.") from error
    if not requested:
        raise ValueError("At least one H3 resolution is required.")
    if any(isinstance(value, bool) or not isinstance(value, Integral) for value in requested):
        raise ValueError("H3 resolutions must be integers between 0 and 15.")
    if any(not 0 <= value <= 15 for value in requested):
        raise ValueError("H3 resolutions must be between 0 and 15.")
    return sorted({int(value) for value in requested})


def generate_cells(county: County, resolutions: Iterable[int]) -> list[GridCell]:
    """Return deterministic full cells covering every polygon of the county.

    ``overlap`` includes cells intersecting the county even when their centers
    lie outside it. This API is experimental; the project pins h3==4.5.0.
    A coverage check rejects missing fragments. Its tiny area tolerance only
    accommodates floating-point differences along shared cell edges.
    """
    levels = _resolutions(resolutions)
    _county_metadata(county.code, county.name)
    boundary = _polygonal_geometry(county.geometry)
    h3_shape = h3.geo_to_h3shape(boundary)
    result: list[GridCell] = []
    for resolution in levels:
        identifiers = sorted(set(h3.h3shape_to_cells_experimental(
            h3_shape, resolution, contain="overlap"
        )))
        if not identifiers:
            raise RuntimeError(f"H3 generated no cells at resolution {resolution}.")
        cells = []
        for identifier in identifiers:
            polygon = Polygon([(lng, lat) for lat, lng in h3.cell_to_boundary(identifier)])
            if polygon.is_empty or not polygon.is_valid or polygon.area <= 0:
                raise RuntimeError(f"H3 returned an invalid boundary for {identifier}.")
            cells.append(GridCell(identifier, resolution, polygon))
        coverage = union_all([cell.geometry for cell in cells])
        missing_area = boundary.difference(coverage).area
        tolerance = max(boundary.area * 1e-10, 1e-12)
        if missing_area > tolerance:
            raise RuntimeError(
                f"Grid at resolution {resolution} does not cover the whole county "
                f"(uncovered area: {missing_area:.12g} square degrees)."
            )
        result.extend(cells)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate H3 grids covering one county.")
    parser.add_argument("--boundary", type=Path, default=DEFAULT_BOUNDARY)
    parser.add_argument("--resolutions", type=int, nargs="+", default=list(DEFAULT_RESOLUTIONS))
    parser.add_argument("--output", type=Path, required=True, help="Destination GeoJSON file.")
    arguments = parser.parse_args(argv)
    try:
        county = read_county(arguments.boundary)
        cells = generate_cells(county, arguments.resolutions)
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))
    document = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "h3_id": cell.h3_id,
                    "resolution": cell.resolution,
                    "county_code": county.code,
                    "county_name": county.name,
                },
                "geometry": mapping(cell.geometry),
            }
            for cell in cells
        ],
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    with arguments.output.open("w", encoding="utf-8", newline="\n") as destination:
        json.dump(document, destination, ensure_ascii=False, separators=(",", ":"))
        destination.write("\n")
    for resolution in sorted({cell.resolution for cell in cells}):
        count = sum(cell.resolution == resolution for cell in cells)
        print(f"H3 resolution {resolution}: {count} cells")
    print(f"Saved {arguments.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
