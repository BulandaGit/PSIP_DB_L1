"""Wyodrębnij jeden powiat z danych zajęć nr 1 i zapisz GeoJSON WGS84."""

import argparse
import json
from pathlib import Path

import shapefile
from pyproj import CRS, Transformer
from shapely import make_valid
from shapely.geometry import mapping, shape
from shapely.ops import transform


def prepare_county(source: Path, output: Path, code: str, encoding: str) -> dict:
    dbf = source.with_suffix('.dbf')
    if not dbf.exists():
        # Plik z pierwszych zajęć ma podwójne rozszerzenie .dbf.dbf.
        dbf = source.with_suffix('.dbf.dbf')
    source_crs = CRS.from_wkt(source.with_suffix('.prj').read_text())
    transformer = Transformer.from_crs(source_crs, 'EPSG:4326', always_xy=True)
    with shapefile.Reader(shp=str(source), shx=str(source.with_suffix('.shx')),
                          dbf=str(dbf), encoding=encoding) as reader:
        matches = [record for record in reader.iterShapeRecords()
                   if str(record.record.as_dict().get('JPT_KOD_JE', '')).strip() == code]
    if len(matches) != 1:
        raise ValueError(f'Oczekiwano jednego powiatu {code}, znaleziono: {len(matches)}')
    record = matches[0]
    properties = record.record.as_dict()
    geometry = transform(transformer.transform, shape(record.shape.__geo_interface__))
    if not geometry.is_valid:
        geometry = make_valid(geometry)
    if geometry.is_empty or geometry.geom_type not in ('Polygon', 'MultiPolygon'):
        raise ValueError('Granica powiatu nie jest poprawnym poligonem.')
    document = {
        'type': 'FeatureCollection',
        'features': [{
            'type': 'Feature',
            'properties': {
                'county_code': code,
                'county_name': properties['JPT_NAZWA_'].strip(),
                'source': 'data/powiat.shp (dane z ćwiczenia nr 1)',
                'source_crs': source_crs.to_string(),
                'source_version': properties.get('WERSJA_OD', '').strip(),
            },
            'geometry': mapping(geometry),
        }],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, ensure_ascii=False, separators=(',', ':')) + '\n',
                      encoding='utf-8')
    return document['features'][0]['properties']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path,
                        default=Path(__file__).resolve().parents[2] / 'data' / 'powiat.shp')
    parser.add_argument('--output', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'data' / 'powiat_bilgorajski.geojson')
    parser.add_argument('--county-code', default='0602')
    parser.add_argument('--encoding', default='utf-8')
    args = parser.parse_args()
    print(json.dumps(prepare_county(args.source, args.output, args.county_code, args.encoding),
                     ensure_ascii=False))
