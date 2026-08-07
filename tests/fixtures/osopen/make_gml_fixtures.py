"""Builds oml_sample.gml, roads_sample.gml and greenspace_sample.gml: tiny,
byte-faithful GML fixtures for test_os_gml.py's offline parser tests.

Run by hand whenever a fixture needs regenerating:

    PYTHONPATH=src python tests/fixtures/osopen/make_gml_fixtures.py

Unlike tests/fixtures/inspire/make_gml_fixture.py, this script reads no
live source: it writes the exact element structures already captured, live,
in this task's own facts file (task-2-facts.md) and root-header probes, with
posList and pos coordinates translated so every point in every fixture
falls inside 280000-320000 easting, 160000-200000 northing (the Vale of
Glamorgan range the brief specifies), since the real samples were pulled
from three different OS tiles (SS, HP and ST) whose own real coordinates
mostly fall outside that box.

## Provenance, feature by feature

Byte-faithful (structure AND coordinates, copied verbatim from a live
download, no translation needed because the real values already fall
inside the target box):

- `oml:Building` (id `id6798B687-...`), `oml:ImportantBuilding` (id
  `idE1185F72-...`) and `oml:RailwayTrack` (id `id5260D34D-...`): all three
  real samples in task-2-facts.md, from the SS tile, already sit inside
  280000-320000 / 160000-200000.
- `oml:NamedPlace` "Atlantic View" and `oml:RailwayStation` "Manorbier":
  captured live from the saved SS zip (task-2-report.md records both in
  full; task-2-facts.md had marked these two types "counted but not
  sampled"). NamedPlace's real easting (205661) is shifted by +90000 to
  295661; its northing (197917) already fits and is untouched. Both real
  elements confirm the plan's assumed property names (`distinctiveName`,
  `classification`) exactly, so no property-map deviation was needed for
  either type.
- `ogsp:AccessPoint` (id `idF601D2D1-...`) and the root of `ogsp:
  GreenspaceSite` (id `id49E9C726-...`, function, distinctiveName1):
  task-2-facts.md's own verbatim samples. AccessPoint's real easting
  (241118.93) is shifted by +58881.07 to 300000.0; its northing
  (199670.71) already fits. GreenspaceSite's own posList is given only as
  a head ("...") in the facts file; its first two real points are kept
  verbatim after a +69893.26 northing shift (297393.08/297393.06 easting
  already fit; 100106.74/100106.81 northing did not), and the ring is
  closed out with three more points forming a plain rectangle, since the
  real ring's remaining points were never captured.
- The real minimal `road:RoadLink` (id `idFCAB4420-...`, no name1 or
  number): task-2-facts.md's complete sample, translated by
  (dx=-165034.97, dy=-1044688.77) to bring its whole line (originally in
  the HP square, Shetland) inside the target box; every field, every
  namespace, every element order copied as printed there.

Constructed (the brief's own instruction, not a live sample, because the
facts file had none to copy):

- `oml:FunctionalSite` "Silverton Church of England Primary School":
  distinctiveName/siteTheme/classification are the real sample's own text
  verbatim; the MultiSurface/Surface/patches/PolygonPatch/exterior/
  interior element NESTING is the real shape, but the ring coordinates
  are a plain rectangle with a smaller rectangular hole (the brief's own
  Step 1: "add a hole to the real sample"), since the real sample's own
  posList was cut short in the facts file before it closed.
- `oml:Woodland`: facts.md states this type is "the same shape as
  Building (geometry + featureCode only)" but gives no concrete sample.
  Its featureCode (10071) is illustrative only, not independently probed
  against a real Woodland element, matching the precedent
  make_listing_fixture.py's own docstring sets for its GB/ST entries.
- `oml:Road`: OpenMapLocal's own second most common feature type (66,189
  over SS per facts.md) but not one OML_TYPES ever wants; included once,
  minimally, purely so test_os_gml.py can assert that a real, present,
  non-trivial feature type outside OML_TYPES is skipped rather than
  raising or being miscounted. Its featureCode is illustrative for the
  same reason Woodland's is.
- The second `road:RoadLink`, WITH name1 and roadClassificationNumber:
  task-2-facts.md's own census (67 of 363 real Vale of Glamorgan RoadLinks
  carry roadClassificationNumber, 31 carry name1) proves this shape is
  real and common, but gives no complete concrete example. Built to the
  same element shape and order as the real minimal RoadLink, with
  trunkRoad and primaryRoute BOTH set true (the real minimal one has both
  false), so the fixture pair exercises both boolean outcomes of `==
  "true"` rather than only the false one.

## Root wrapper: os:metadata, gml:boundedBy, xmlns declarations

Each fixture's root `os:FeatureCollection` carries only the namespaces its
OWN elements actually use (never the full real wrapper's own extra ISO
19115/19139 namespaces -- gmd, gco, gss, gsr, gts -- or `road`'s own
tn-ro/highway/gn, none of which any element in these fixtures reaches),
per the facts file's own instruction ("the fixture must declare every
namespace its elements use"). `gml:boundedBy`'s own envelope is the
target box itself (280000/160000 to 320000/200000), and each `os:
metadata` xlink:href is the real per-product URL this script's own probe
of the live root wrapper found (`http://www.os.uk/xml/products/OML.xml`,
`.../OSOpenRoads.xml`, `.../GSO.xml`): read, never a URL this project's
own parser ever echoes into a message, so committing it in a FIXTURE
carries none of the "no URL in an exception message" risk that rule
guards against.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

_FIXTURE_DIR = Path(__file__).resolve().parent

OML_SAMPLE_PATH = _FIXTURE_DIR / "oml_sample.gml"
ROADS_SAMPLE_PATH = _FIXTURE_DIR / "roads_sample.gml"
GREENSPACE_SAMPLE_PATH = _FIXTURE_DIR / "greenspace_sample.gml"

_XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8"?>\n'

_OML_SAMPLE_BODY = """\
<os:FeatureCollection xmlns:os="http://namespaces.os.uk/product/1.0" xmlns:oml="http://namespaces.os.uk/open/oml/1.0" xmlns:gml="http://www.opengis.net/gml/3.2" xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" gml:id="OSOpenMapLocal" xsi:schemaLocation="http://namespaces.os.uk/open/oml/1.0 https://ordnancesurvey.co.uk/xml/open/oml/1.0/OSOpenMapLocal.xsd">
  <gml:boundedBy>
    <gml:Envelope srsName="urn:ogc:def:crs:EPSG::27700" srsDimension="2">
      <gml:lowerCorner>280000 160000</gml:lowerCorner>
      <gml:upperCorner>320000 200000</gml:upperCorner>
    </gml:Envelope>
  </gml:boundedBy>
  <os:metadata xlink:href="http://www.os.uk/xml/products/OML.xml"/>
  <os:featureMember>
    <oml:Building gml:id="id6798B687-8592-42FB-929F-A62D5CCD3EA9">
      <oml:geometry>
        <gml:Surface gml:id="id6798B687-8592-42FB-929F-A62D5CCD3EA9-0" srsName="urn:ogc:def:crs:EPSG::27700" srsDimension="2">
          <gml:patches>
            <gml:PolygonPatch>
              <gml:exterior>
                <gml:LinearRing>
                  <gml:posList>286268.63 190853.9 286258.67 190848.07 286282.66 190804.33 286293.15 190810.13 286268.63 190853.9</gml:posList>
                </gml:LinearRing>
              </gml:exterior>
            </gml:PolygonPatch>
          </gml:patches>
        </gml:Surface>
      </oml:geometry>
      <oml:featureCode>15014</oml:featureCode>
    </oml:Building>
  </os:featureMember>
  <os:featureMember>
    <oml:ImportantBuilding gml:id="idE1185F72-DC07-4ABA-803B-025ADE096147">
      <oml:buildingTheme codeSpace="http://www.os.uk/xml/codelists/map/BuildingThemeOML.xml">Religious Buildings</oml:buildingTheme>
      <oml:classification codeSpace="http://www.os.uk/xml/codelists/map/BuildingClassificationOML.xml">Place Of Worship</oml:classification>
      <oml:geometry>
        <gml:Surface gml:id="idE1185F72-DC07-4ABA-803B-025ADE096147-0" srsName="urn:ogc:def:crs:EPSG::27700" srsDimension="2">
          <gml:patches>
            <gml:PolygonPatch>
              <gml:exterior>
                <gml:LinearRing>
                  <gml:posList>299904.88 191571.03 299900.06 191579.89 299895.2 191577.25 299900.03 191568.38 299904.88 191571.03</gml:posList>
                </gml:LinearRing>
              </gml:exterior>
            </gml:PolygonPatch>
          </gml:patches>
        </gml:Surface>
      </oml:geometry>
      <oml:featureCode>15025</oml:featureCode>
    </oml:ImportantBuilding>
  </os:featureMember>
  <os:featureMember>
    <oml:FunctionalSite gml:id="id0167CD58-0785-41AC-80CD-38AC24103754">
      <oml:distinctiveName>Silverton Church of England Primary School</oml:distinctiveName>
      <oml:siteTheme codeSpace="http://www.os.uk/xml/codelists/sites/SiteTheme.xml">Education</oml:siteTheme>
      <oml:classification codeSpace="http://www.os.uk/xml/codelists/map/SiteClassificationOML.xml">Primary Education</oml:classification>
      <oml:geometry>
        <gml:MultiSurface gml:id="id0167CD58-0785-41AC-80CD-38AC24103754-0" srsName="urn:ogc:def:crs:EPSG::27700" srsDimension="2">
          <gml:surfaceMember>
            <gml:Surface gml:id="id0167CD58-0785-41AC-80CD-38AC24103754-1">
              <gml:patches>
                <gml:PolygonPatch>
                  <gml:exterior>
                    <gml:LinearRing>
                      <gml:posList>300000 180000 300050 180000 300050 180040 300000 180040 300000 180000</gml:posList>
                    </gml:LinearRing>
                  </gml:exterior>
                  <gml:interior>
                    <gml:LinearRing>
                      <gml:posList>300010 180010 300020 180010 300020 180020 300010 180020 300010 180010</gml:posList>
                    </gml:LinearRing>
                  </gml:interior>
                </gml:PolygonPatch>
              </gml:patches>
            </gml:Surface>
          </gml:surfaceMember>
        </gml:MultiSurface>
      </oml:geometry>
    </oml:FunctionalSite>
  </os:featureMember>
  <os:featureMember>
    <oml:Woodland gml:id="idAA11BB22-CC33-DD44-EE55-FF6677889900">
      <oml:geometry>
        <gml:Surface gml:id="idAA11BB22-CC33-DD44-EE55-FF6677889900-0" srsName="urn:ogc:def:crs:EPSG::27700" srsDimension="2">
          <gml:patches>
            <gml:PolygonPatch>
              <gml:exterior>
                <gml:LinearRing>
                  <gml:posList>310000 165000 310030 165000 310030 165025 310000 165025 310000 165000</gml:posList>
                </gml:LinearRing>
              </gml:exterior>
            </gml:PolygonPatch>
          </gml:patches>
        </gml:Surface>
      </oml:geometry>
      <oml:featureCode>10071</oml:featureCode>
    </oml:Woodland>
  </os:featureMember>
  <os:featureMember>
    <oml:RailwayTrack gml:id="id5260D34D-2755-4F73-8062-0240E7FBCDC5">
      <oml:classification codeSpace="http://www.os.uk/xml/codelists/map/RailwayTrackClassificationOML.xml">Multi Track</oml:classification>
      <oml:geometry>
        <gml:LineString gml:id="id5260D34D-2755-4F73-8062-0240E7FBCDC5-0" srsName="urn:ogc:def:crs:EPSG::27700" srsDimension="2">
          <gml:posList>297006.92 168951.45 296887.86 169139.32 296833.32 169201.14 296762.79 169258.93</gml:posList>
        </gml:LineString>
      </oml:geometry>
      <oml:featureCode>15300</oml:featureCode>
    </oml:RailwayTrack>
  </os:featureMember>
  <os:featureMember>
    <oml:NamedPlace gml:id="id2FBE0D94-CB03-4109-A989-F8FB3C35B50D">
      <oml:distinctiveName>Atlantic View</oml:distinctiveName>
      <oml:classification codeSpace="http://www.os.uk/xml/codelists/map/NamedPlaceClassificationOML.xml">Populated Place</oml:classification>
      <oml:fontHeight codeSpace="http://www.os.uk/xml/codelists/map/FontHeightClassificationOML.xml">Small</oml:fontHeight>
      <oml:textOrientation uom="degrees">0</oml:textOrientation>
      <oml:geometry>
        <gml:Point gml:id="id2FBE0D94-CB03-4109-A989-F8FB3C35B50D-0" srsName="urn:ogc:def:crs:EPSG::27700" srsDimension="2">
          <gml:pos>295661 197917</gml:pos>
        </gml:Point>
      </oml:geometry>
      <oml:featureCode>15801</oml:featureCode>
    </oml:NamedPlace>
  </os:featureMember>
  <os:featureMember>
    <oml:Road gml:id="idA0B1C2D3-E4F5-4677-8899-AABBCCDDEEFF">
      <oml:geometry>
        <gml:LineString gml:id="idA0B1C2D3-E4F5-4677-8899-AABBCCDDEEFF-0" srsName="urn:ogc:def:crs:EPSG::27700" srsDimension="2">
          <gml:posList>315000 165000 315010 165005</gml:posList>
        </gml:LineString>
      </oml:geometry>
      <oml:featureCode>15320</oml:featureCode>
    </oml:Road>
  </os:featureMember>
</os:FeatureCollection>
"""

_ROADS_SAMPLE_BODY = """\
<os:FeatureCollection xmlns:os="http://namespaces.os.uk/product/1.0" xmlns:road="http://namespaces.os.uk/Open/Roads/1.0" xmlns:net="urn:x-inspire:specification:gmlas:Network:3.2" xmlns:tn="urn:x-inspire:specification:gmlas:CommonTransportElements:3.0" xmlns:gml="http://www.opengis.net/gml/3.2" xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" gml:id="OSOpenRoads" xsi:schemaLocation="http://namespaces.os.uk/Open/Roads/1.0 https://www.ordnancesurvey.co.uk/xml/open/roads/1.0/OSOpenRoads.xsd">
  <gml:boundedBy>
    <gml:Envelope srsName="urn:ogc:def:crs:EPSG::27700" srsDimension="2">
      <gml:lowerCorner>280000 160000</gml:lowerCorner>
      <gml:upperCorner>320000 200000</gml:upperCorner>
    </gml:Envelope>
  </gml:boundedBy>
  <os:metadata xlink:href="http://www.os.uk/xml/products/OSOpenRoads.xml"/>
  <os:featureMember>
    <road:RoadLink gml:id="idFCAB4420-B9EA-42E2-923C-BAAFA790F1AC">
      <net:beginLifespanVersion xsi:nil="true" nilReason="inapplicable"/>
      <net:inNetwork xsi:nil="true"/>
      <net:centrelineGeometry>
        <gml:LineString gml:id="idFCAB4420-B9EA-42E2-923C-BAAFA790F1AC-0" srsName="urn:ogc:def:crs:EPSG::27700" srsDimension="2">
          <gml:posList>300000 170000 300089.03 170120.23 300206.93 170221.93 300210.41 170228.33 300230.56 170265.38 300341.03 170418.23 300369 170495.14</gml:posList>
        </gml:LineString>
      </net:centrelineGeometry>
      <net:fictitious>false</net:fictitious>
      <net:endNode xlink:href="#id11123F70-73AC-4BFC-A29D-EB9BFA48EBC7"/>
      <net:startNode xlink:href="#idE742341F-0BCA-42D9-B309-C5CAA57EED82"/>
      <tn:validFrom xsi:nil="true" nilReason="inapplicable"/>
      <road:roadClassification codeSpace="http://www.os.uk/xml/codelists/RoadClassificationValue.xml">Unknown</road:roadClassification>
      <road:roadFunction codeSpace="http://www.os.uk/xml/codelists/RoadFunctionValue.xml">Restricted Local Access Road</road:roadFunction>
      <road:formOfWay codeSpace="http://www.os.uk/xml/codelists/FormOfWayTypeValue.xml">Single Carriageway</road:formOfWay>
      <road:length uom="m">625</road:length>
      <road:loop>false</road:loop>
      <road:primaryRoute>false</road:primaryRoute>
      <road:trunkRoad>false</road:trunkRoad>
    </road:RoadLink>
  </os:featureMember>
  <os:featureMember>
    <road:RoadLink gml:id="id0A1B2C3D-4E5F-6789-ABCD-EF0123456789">
      <net:beginLifespanVersion xsi:nil="true" nilReason="inapplicable"/>
      <net:inNetwork xsi:nil="true"/>
      <net:centrelineGeometry>
        <gml:LineString gml:id="id0A1B2C3D-4E5F-6789-ABCD-EF0123456789-0" srsName="urn:ogc:def:crs:EPSG::27700" srsDimension="2">
          <gml:posList>305000 175000 305050 175020 305100 175040</gml:posList>
        </gml:LineString>
      </net:centrelineGeometry>
      <net:fictitious>false</net:fictitious>
      <net:endNode xlink:href="#idEE000000-0000-0000-0000-000000000001"/>
      <net:startNode xlink:href="#idEE000000-0000-0000-0000-000000000002"/>
      <tn:validFrom xsi:nil="true" nilReason="inapplicable"/>
      <road:roadClassification codeSpace="http://www.os.uk/xml/codelists/RoadClassificationValue.xml">A Road</road:roadClassification>
      <road:roadFunction codeSpace="http://www.os.uk/xml/codelists/RoadFunctionValue.xml">A Road</road:roadFunction>
      <road:formOfWay codeSpace="http://www.os.uk/xml/codelists/FormOfWayTypeValue.xml">Single Carriageway</road:formOfWay>
      <road:name1>Culver Way</road:name1>
      <road:roadClassificationNumber>A4050</road:roadClassificationNumber>
      <road:length uom="m">812</road:length>
      <road:loop>false</road:loop>
      <road:primaryRoute>true</road:primaryRoute>
      <road:trunkRoad>true</road:trunkRoad>
    </road:RoadLink>
  </os:featureMember>
</os:FeatureCollection>
"""

_GREENSPACE_SAMPLE_BODY = """\
<os:FeatureCollection xmlns:os="http://namespaces.os.uk/product/1.0" xmlns:ogsp="http://namespaces.ordnancesurvey.co.uk/Open/Greenspace/1.0" xmlns:gml="http://www.opengis.net/gml/3.2" xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" gml:id="OSOpenGreenspace" xsi:schemaLocation="http://namespaces.ordnancesurvey.co.uk/Open/Greenspace/1.0 https://www.ordnancesurvey.co.uk/xml/open/greenspace/1.0/OSOpenGreenspace.xsd">
  <gml:description>Ordnance Survey Crown Copyright 2026</gml:description>
  <gml:boundedBy>
    <gml:Envelope srsName="urn:ogc:def:crs:EPSG::27700" srsDimension="2">
      <gml:lowerCorner>280000 160000</gml:lowerCorner>
      <gml:upperCorner>320000 200000</gml:upperCorner>
    </gml:Envelope>
  </gml:boundedBy>
  <os:metadata xlink:href="http://www.os.uk/xml/products/GSO.xml"/>
  <os:featureMember>
    <ogsp:GreenspaceSite gml:id="id49E9C726-4964-A491-E063-8CCAA00A0EF3">
      <ogsp:function codeSpace="http://www.os.uk/xml/codelists/OpenFunctionValue">Public Park Or Garden</ogsp:function>
      <ogsp:distinctiveName1>Killerton Gardens</ogsp:distinctiveName1>
      <ogsp:geometry>
        <gml:MultiSurface gml:id="id49E9C726-4964-A491-E063-8CCAA00A0EF3-0" srsName="urn:ogc:def:crs:EPSG::27700" srsDimension="2">
          <gml:surfaceMember>
            <gml:Surface gml:id="id49E9C726-4964-A491-E063-8CCAA00A0EF3-1">
              <gml:patches>
                <gml:PolygonPatch>
                  <gml:exterior>
                    <gml:LinearRing>
                      <gml:posList>297393.08 170106.74 297420 170106.74 297420 170130 297393.08 170130 297393.08 170106.74</gml:posList>
                    </gml:LinearRing>
                  </gml:exterior>
                </gml:PolygonPatch>
              </gml:patches>
            </gml:Surface>
          </gml:surfaceMember>
        </gml:MultiSurface>
      </ogsp:geometry>
    </ogsp:GreenspaceSite>
  </os:featureMember>
  <os:featureMember>
    <ogsp:AccessPoint gml:id="idF601D2D1-9D6F-4A8F-8814-030B76CDFC90">
      <ogsp:accessType codeSpace="http://www.os.uk/xml/codelists/AccessTypeValue">Pedestrian</ogsp:accessType>
      <ogsp:refToGreenspaceSite>id49E9C737-AEA2-A491-E063-8CCAA00A0EF3</ogsp:refToGreenspaceSite>
      <ogsp:geometry>
        <gml:Point gml:id="idF601D2D1-9D6F-4A8F-8814-030B76CDFC90-0" srsName="urn:ogc:def:crs:EPSG::27700" srsDimension="2">
          <gml:pos>300000 199670.71</gml:pos>
        </gml:Point>
      </ogsp:geometry>
    </ogsp:AccessPoint>
  </os:featureMember>
</os:FeatureCollection>
"""

_FIXTURES = {
    OML_SAMPLE_PATH: _OML_SAMPLE_BODY,
    ROADS_SAMPLE_PATH: _ROADS_SAMPLE_BODY,
    GREENSPACE_SAMPLE_PATH: _GREENSPACE_SAMPLE_BODY,
}


def main() -> None:
    for path, body in _FIXTURES.items():
        content = _XML_DECLARATION + body
        # Self-check: every fixture this script writes must itself be
        # well-formed XML, checked here rather than trusted, since a typo
        # in one of the raw strings above would otherwise only surface
        # once test_os_gml.py happened to exercise that exact fixture.
        ET.fromstring(content)
        path.write_text(content, encoding="utf-8")
        print(f"Wrote {path} ({len(content)} bytes).")


if __name__ == "__main__":
    main()
