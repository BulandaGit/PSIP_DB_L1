"""Export results to GeoPackage and prepare portable/live QGIS projects.

Run with the Python supplied with QGIS. PGPASSWORD is read by libpq and is never
stored in the live project's connection URI.
"""

import argparse
import json
import math
from pathlib import Path

from qgis.core import (
    Qgis, QgsApplication, QgsCoordinateReferenceSystem, QgsDataSourceUri,
    QgsFeatureRequest, QgsFillSymbol, QgsGraduatedSymbolRenderer, QgsMapRendererParallelJob,
    QgsMapSettings, QgsProject, QgsReferencedRectangle, QgsRendererRange,
    QgsVectorFileWriter, QgsVectorLayer,
)
from qgis.PyQt.QtCore import QSize
from qgis.PyQt.QtGui import QColor


COUNTS = {6: 76, 7: 426, 8: 2630, 9: 17361}
THEMES = [
    ('ot_bubd_a_area_m2', 'Powierzchnia budynków [m²]', ['#fee8c8','#fdbb84','#e34a33','#b30000']),
    ('ot_skdr_l_length_m', 'Długość dróg [m]', ['#deebf7','#9ecae1','#3182bd','#08519c']),
    ('ot_oipr_p_count', 'Liczba punktów OIPR', ['#e5f5e0','#a1d99b','#31a354','#006d2c']),
]


def database_layer(schema, table, geometry, key, title, expected=None):
    uri = QgsDataSourceUri()
    uri.setConnection('127.0.0.1','5445','psip','psip','')
    uri.setDataSource(schema,table,geometry,"\"county_code\" = '0602'",key)
    layer = QgsVectorLayer(uri.uri(False),title,'postgres')
    if not layer.isValid():
        raise RuntimeError('Invalid database layer: ' + schema + '.' + table)
    if expected is not None and layer.featureCount() != expected:
        raise RuntimeError('Unexpected feature count: ' + table)
    return layer


def export_layer(layer, destination, name, first):
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName='GPKG'
    options.layerName=name
    options.fileEncoding='UTF-8'
    options.layerOptions=['SPATIAL_INDEX=YES']
    options.actionOnExistingFile = (QgsVectorFileWriter.CreateOrOverwriteFile if first
                                    else QgsVectorFileWriter.CreateOrOverwriteLayer)
    options.attributes=[index for index,field in enumerate(layer.fields())
                        if field.name() not in ('geom','geom_clipped')]
    result=QgsVectorFileWriter.writeAsVectorFormatV3(layer,str(destination),QgsProject.instance().transformContext(),options)
    if result[0] != QgsVectorFileWriter.NoError:
        raise RuntimeError('GeoPackage export failed: ' + str(result))


def layer_aliases(layer):
    for index,field in enumerate(layer.fields()):
        name=field.name()
        if name.startswith('ot_'):
            if name.endswith('_area_m2'):
                label=name[:-8].upper() + ' — powierzchnia [m²], obliczenia EPSG:2180'
            elif name.endswith('_length_m'):
                label=name[:-9].upper() + ' — długość [m], obliczenia EPSG:2180'
            elif name.endswith('_count'):
                label=name[:-6].upper() + ' — liczba punktów'
            else:
                continue
            layer.setFieldAlias(index,label)
    layer.setDisplayExpression('"h3_id"')
    layer.setAbstract('Powiat biłgorajski 0602; BDOT10K, stan na koniec 2025 r. '
                      '65 miar, obliczenia w EPSG:2180. Geometria wynikowa EPSG:3857.')


def graduated_renderer(layer, field, colors):
    index=layer.fields().indexFromName(field)
    if index < 0:
        raise RuntimeError('Missing result metric: '+field)
    request=QgsFeatureRequest().setFlags(QgsFeatureRequest.NoGeometry).setSubsetOfAttributes([field],layer.fields())
    values=sorted(float(feature[index]) for feature in layer.getFeatures(request) if float(feature[index]) > 0)
    ranges=[]
    zero=QgsFillSymbol.createSimple({'color':'#f0f2f4','outline_color':'#c3cbd2','outline_width':'0.08'})
    ranges.append(QgsRendererRange(0,0,zero,'0'))
    lower=0
    if values:
        upper_bounds=sorted(set(values[min(len(values)-1,math.ceil(len(values)*fraction)-1)] for fraction in (0.25,0.5,0.75,1)))
        for number,upper in enumerate(upper_bounds):
            symbol=QgsFillSymbol.createSimple({'color':colors[min(number,len(colors)-1)],
                'outline_color':'#b7c0c8','outline_width':'0.08','joinstyle':'round'})
            ranges.append(QgsRendererRange(lower,upper,symbol,f'{lower:,.0f} – {upper:,.0f}'.replace(',',' ')))
            lower=upper
    return QgsGraduatedSymbolRenderer(field,ranges)


def make_project(project_path, package, manifest, live):
    project=QgsProject.instance()
    project.clear()
    crs=QgsCoordinateReferenceSystem('EPSG:3857')
    project.setCrs(crs)
    project.setTitle('Powiat biłgorajski — BDOT10K i H3 — EPSG:3857')
    project.setFileName(str(project_path))
    project.setFilePathStorage(Qgis.FilePathType.Relative)
    project.setBackgroundColor(QColor('#fafbfc'))
    root=project.layerTreeRoot()
    results=root.addGroup('Agregacja BDOT10K — obliczenia EPSG:2180, mapa EPSG:3857')
    displayed=None
    for level,count in COUNTS.items():
        group=results.addGroup(f'H3 {level} — {count:,} komórek'.replace(',',' '))
        for field,title,colors in THEMES:
            if live:
                layer=database_layer('grid',f'h3_r{level}_bdot_3857','geom_clipped','h3_id',title,count)
            else:
                layer=QgsVectorLayer(str(package)+f'|layername=h3_r{level}_bdot',title,'ogr')
            if not layer.isValid() or layer.crs().authid()!='EPSG:3857' or layer.featureCount()!=count:
                raise RuntimeError('Invalid result layer: '+str(level)+'/'+field)
            layer_aliases(layer)
            layer.setRenderer(graduated_renderer(layer,field,colors))
            project.addMapLayer(layer,False)
            node=group.addLayer(layer)
            visible=level==7 and field=='ot_bubd_a_area_m2'
            node.setItemVisibilityChecked(visible)
            node.setExpanded(False)
            if visible:
                displayed=layer
        group.setExpanded(level==7)
    if live:
        county=database_layer('grid','powiat_3857','geom','county_code','Granica powiatu biłgorajskiego',1)
        sources=root.addGroup('BDOT10K 2025 — 65 warstw źródłowych EPSG:2180')
        for item in manifest['layers']:
            layer=database_layer('bdot',item['layer'],'geom','source_fid',item['layer'].upper(),item['records'])
            project.addMapLayer(layer,False)
            node=sources.addLayer(layer)
            node.setItemVisibilityChecked(False)
            node.setExpanded(False)
        sources.setExpanded(False)
    else:
        county=QgsVectorLayer(str(package)+'|layername=powiat','Granica powiatu biłgorajskiego','ogr')
    if not county.isValid():
        raise RuntimeError('Invalid county boundary')
    county.renderer().setSymbol(QgsFillSymbol.createSimple({'style':'no','outline_color':'#243746','outline_width':'0.6','joinstyle':'round'}))
    project.addMapLayer(county,False)
    root.insertLayer(0,county).setExpanded(False)
    extent=county.extent()
    extent.scale(1.12)
    project.viewSettings().setDefaultViewExtent(QgsReferencedRectangle(extent,crs))
    if not project.write():
        raise RuntimeError('Could not save QGIS project')
    return project,county,displayed,extent


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--live-project',type=Path,required=True)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--reuse-export',action='store_true',help='Reuse a previously exported GeoPackage after checking layers.')
    args=parser.parse_args()
    args.output_dir.mkdir(parents=True,exist_ok=True)
    package=args.output_dir/'BDOT_Bilgorajski_wyniki_3857.gpkg'
    portable=args.output_dir/'Powiat_bilgorajski_BDOT_3857_przenosny.qgz'
    if (package.exists() and not args.reuse_export) or portable.exists() or args.live_project.exists():
        raise FileExistsError('Destination exists; select a new output location to preserve existing projects.')
    manifest=json.loads(args.manifest.read_text(encoding='utf-8-sig'))
    application=QgsApplication([],False)
    application.initQgis()
    if not package.exists():
        for number,(level,count) in enumerate(COUNTS.items()):
            layer=database_layer('grid',f'h3_r{level}_bdot_3857','geom_clipped','h3_id',f'H3 {level}',count)
            export_layer(layer,package,f'h3_r{level}_bdot',number==0)
        county=database_layer('grid','powiat_3857','geom','county_code','Powiat',1)
        export_layer(county,package,'powiat',False)
    else:
        for level,count in COUNTS.items():
            exported=QgsVectorLayer(str(package)+f'|layername=h3_r{level}_bdot','check','ogr')
            if not exported.isValid() or exported.featureCount()!=count or exported.crs().authid()!='EPSG:3857':
                raise RuntimeError('Existing GeoPackage does not match expected result layers.')
    project,county,displayed,extent=make_project(portable,package,manifest,False)
    settings=QgsMapSettings()
    settings.setDestinationCrs(QgsCoordinateReferenceSystem('EPSG:3857'))
    settings.setLayers([county,displayed])
    settings.setExtent(extent)
    settings.setOutputSize(QSize(1400,1400))
    settings.setBackgroundColor(QColor('#fafbfc'))
    job=QgsMapRendererParallelJob(settings)
    job.start();job.waitForFinished()
    preview=args.output_dir/'BDOT_Bilgorajski_budynki_3857.png'
    if not job.renderedImage().save(str(preview)):
        raise RuntimeError('Could not save map preview')
    make_project(args.live_project,package,manifest,True)
    print(json.dumps({'geopackage':str(package),'portable_project':str(portable),
                      'live_project':str(args.live_project),'preview':str(preview),
                      'calculation_epsg':2180,'geometry_epsg':3857,'metric_columns':65},ensure_ascii=True))
    # The process tears down remaining provider references after this script ends.


if __name__=='__main__':
    main()
