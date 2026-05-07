from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from osm_project_importer import (
    build_project_from_osm_xml,
    build_project_from_osmnx_graph,
    main,
)


class FakeGeometry:
    def __init__(self, coords):
        self.coords = coords


class FakeGraph:
    def nodes(self, data=True):
        return [
            (1, {"x": 82.9, "y": 55.0}),
            (2, {"x": 82.9003, "y": 55.0003}),
        ]

    def edges(self, keys=True, data=True):
        return [
            (
                1,
                2,
                0,
                {
                    "geometry": FakeGeometry(
                        [
                            (82.9, 55.0),
                            (82.9001, 55.0001),
                            (82.9002, 55.0002),
                            (82.9003, 55.0003),
                        ]
                    ),
                    "highway": "residential",
                    "oneway": False,
                },
            )
        ]


class OsmProjectImporterTest(unittest.TestCase):
    def test_osmnx_import_keeps_geometry_as_polyline_without_geometry_nodes(self):
        project = build_project_from_osmnx_graph(FakeGraph())

        self.assertEqual(sorted(project.network.nodes), ["OSM_1", "OSM_2"])
        self.assertEqual(len(project.network.links), 1)

        link = next(iter(project.network.links.values()))
        self.assertEqual(link.start_node_id, "OSM_1")
        self.assertEqual(link.end_node_id, "OSM_2")
        self.assertEqual(link.coords["type"], "polyline")
        self.assertEqual(link.coords["points"], [[82.9, 55.0], [82.9003, 55.0003]])
        self.assertGreater(link.length_km, 0)

    def test_xml_import_splits_at_shared_ref_but_not_internal_geometry_ref(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            osm_path = Path(temp_dir) / "roads.osm"
            osm_path.write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lon="82.90000" lat="55.00000" />
  <node id="2" lon="82.90001" lat="55.00001" />
  <node id="3" lon="82.90002" lat="55.00002" />
  <node id="4" lon="82.90003" lat="55.00003" />
  <node id="5" lon="82.89990" lat="55.00002" />
  <node id="6" lon="82.90010" lat="55.00002" />
  <way id="10">
    <nd ref="1" />
    <nd ref="2" />
    <nd ref="3" />
    <nd ref="4" />
    <tag k="highway" v="residential" />
    <tag k="name" v="Main" />
  </way>
  <way id="20">
    <nd ref="5" />
    <nd ref="3" />
    <nd ref="6" />
    <tag k="highway" v="residential" />
    <tag k="name" v="Cross" />
  </way>
</osm>
""",
                encoding="utf-8",
            )

            project = build_project_from_osm_xml(osm_path)

        self.assertNotIn("OSM_2", project.network.nodes)
        self.assertIn("OSM_3", project.network.nodes)
        self.assertEqual(len(project.network.links), 4)

        main_link = next(
            link
            for link in project.network.links.values()
            if link.start_node_id == "OSM_1" and link.end_node_id == "OSM_3"
        )
        self.assertEqual(main_link.coords["type"], "polyline")
        self.assertEqual(main_link.coords["points"], [[82.9, 55.0], [82.90002, 55.00002]])

    def test_xml_import_merges_technical_way_split_without_losing_geometry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            osm_path = Path(temp_dir) / "roads.osm"
            osm_path.write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lon="82.90000" lat="55.00000" />
  <node id="2" lon="82.90001" lat="55.00001" />
  <node id="3" lon="82.90002" lat="55.00002" />
  <node id="4" lon="82.90003" lat="55.00003" />
  <node id="5" lon="82.90004" lat="55.00004" />
  <way id="10">
    <nd ref="1" />
    <nd ref="2" />
    <nd ref="3" />
    <tag k="highway" v="residential" />
    <tag k="name" v="Main" />
  </way>
  <way id="11">
    <nd ref="3" />
    <nd ref="4" />
    <nd ref="5" />
    <tag k="highway" v="residential" />
    <tag k="name" v="Main" />
  </way>
</osm>
""",
                encoding="utf-8",
            )

            project = build_project_from_osm_xml(osm_path)

        self.assertNotIn("OSM_2", project.network.nodes)
        self.assertNotIn("OSM_3", project.network.nodes)
        self.assertNotIn("OSM_4", project.network.nodes)
        self.assertEqual(sorted(project.network.nodes), ["OSM_1", "OSM_5"])
        self.assertEqual(len(project.network.links), 1)

        link = next(iter(project.network.links.values()))
        self.assertEqual(link.start_node_id, "OSM_1")
        self.assertEqual(link.end_node_id, "OSM_5")
        self.assertEqual(link.coords["points"], [[82.9, 55.0], [82.90004, 55.00004]])

    def test_xml_import_keeps_node_at_visible_bend(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            osm_path = Path(temp_dir) / "roads.osm"
            osm_path.write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lon="82.90000" lat="55.00000" />
  <node id="2" lon="82.90001" lat="55.00001" />
  <node id="3" lon="82.90002" lat="55.00002" />
  <node id="4" lon="82.90003" lat="55.00001" />
  <node id="5" lon="82.90004" lat="55.00000" />
  <way id="10">
    <nd ref="1" />
    <nd ref="2" />
    <nd ref="3" />
    <nd ref="4" />
    <nd ref="5" />
    <tag k="highway" v="residential" />
    <tag k="name" v="Main" />
  </way>
</osm>
""",
                encoding="utf-8",
            )

            project = build_project_from_osm_xml(osm_path)

        self.assertIn("OSM_3", project.network.nodes)
        self.assertEqual(len(project.network.links), 2)

    def test_cli_writes_project_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            osm_path = temp_path / "roads.osm"
            output_path = temp_path / "project.json"
            osm_path.write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lon="82.90000" lat="55.00000" />
  <node id="2" lon="82.90001" lat="55.00001" />
  <node id="3" lon="82.90002" lat="55.00002" />
  <way id="10">
    <nd ref="1" />
    <nd ref="2" />
    <nd ref="3" />
    <tag k="highway" v="residential" />
  </way>
</osm>
""",
                encoding="utf-8",
            )

            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = main(
                    [
                        "--input",
                        str(osm_path),
                        "--output",
                        str(output_path),
                        "--intensity",
                        "700",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertTrue(output_path.exists())
            data = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(len(data["network"]["nodes"]), 2)
            self.assertEqual(len(data["network"]["links"]), 1)
            self.assertEqual(data["network"]["links"][0]["traffic_counts"]["car"], 700)


if __name__ == "__main__":
    unittest.main()
