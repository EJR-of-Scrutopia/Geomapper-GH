<?xml version="1.0" encoding="UTF-8"?>
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
