from __future__ import annotations

import contextlib
import io
import json
import shutil
import unittest
import uuid
from pathlib import Path

from osm_project_importer import (
    build_project_from_osm_xml,
    build_project_from_osmnx_graph,
    main,
)


@contextlib.contextmanager
def workspace_tempdir():
    root = Path(__file__).resolve().parents[1] / "tmp_tests"
    root.mkdir(exist_ok=True)
    temp_path = root / f"tmp_{uuid.uuid4().hex}"
    temp_path.mkdir()
    try:
        yield str(temp_path)
    finally:
        shutil.rmtree(temp_path, ignore_errors=True)
        try:
            root.rmdir()
        except OSError:
            pass


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
        self.assertEqual(project.network.nodes["OSM_1"].node_type, "boundary")
        self.assertEqual(project.network.nodes["OSM_2"].node_type, "boundary")

        link = next(iter(project.network.links.values()))
        self.assertEqual(link.start_node_id, "OSM_1")
        self.assertEqual(link.end_node_id, "OSM_2")
        self.assertEqual(link.coords["type"], "polyline")
        self.assertEqual(link.coords["points"], [[82.9, 55.0], [82.9003, 55.0003]])
        self.assertGreater(link.length_km, 0)

    def test_xml_import_splits_at_shared_ref_but_not_internal_geometry_ref(self):
        with workspace_tempdir() as temp_dir:
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
    <tag k="oneway" v="yes" />
  </way>
  <way id="20">
    <nd ref="5" />
    <nd ref="3" />
    <nd ref="6" />
    <tag k="highway" v="residential" />
    <tag k="name" v="Cross" />
    <tag k="oneway" v="yes" />
  </way>
</osm>
""",
                encoding="utf-8",
            )

            project = build_project_from_osm_xml(osm_path)

        self.assertNotIn("OSM_2", project.network.nodes)
        self.assertIn("OSM_3", project.network.nodes)
        self.assertEqual(project.network.nodes["OSM_3"].node_type, "intersection")
        self.assertEqual(len(project.network.links), 4)

        main_link = next(
            link
            for link in project.network.links.values()
            if link.start_node_id == "OSM_1" and link.end_node_id == "OSM_3"
        )
        self.assertEqual(main_link.coords["type"], "polyline")
        self.assertEqual(main_link.coords["points"], [[82.9, 55.0], [82.90002, 55.00002]])

    def test_xml_import_merges_technical_way_split_without_losing_geometry(self):
        with workspace_tempdir() as temp_dir:
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
    <tag k="oneway" v="yes" />
  </way>
  <way id="11">
    <nd ref="3" />
    <nd ref="4" />
    <nd ref="5" />
    <tag k="highway" v="residential" />
    <tag k="name" v="Main" />
    <tag k="oneway" v="yes" />
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

    def test_xml_import_keeps_visible_bend_as_polyline_point_without_node(self):
        with workspace_tempdir() as temp_dir:
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
    <tag k="oneway" v="yes" />
  </way>
</osm>
""",
                encoding="utf-8",
            )

            project = build_project_from_osm_xml(osm_path)

        self.assertNotIn("OSM_3", project.network.nodes)
        self.assertEqual(sorted(project.network.nodes), ["OSM_1", "OSM_5"])
        self.assertEqual(len(project.network.links), 1)
        link = next(iter(project.network.links.values()))
        self.assertEqual(
            link.coords["points"],
            [[82.9, 55.0], [82.90002, 55.00002], [82.90004, 55.0]],
        )

    def test_xml_import_keeps_attribute_change_node(self):
        with workspace_tempdir() as temp_dir:
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
    <tag k="oneway" v="yes" />
    <tag k="lanes" v="1" />
  </way>
  <way id="11">
    <nd ref="3" />
    <nd ref="4" />
    <nd ref="5" />
    <tag k="highway" v="residential" />
    <tag k="name" v="Main" />
    <tag k="oneway" v="yes" />
    <tag k="lanes" v="2" />
  </way>
</osm>
""",
                encoding="utf-8",
            )

            project = build_project_from_osm_xml(osm_path)

        self.assertIn("OSM_3", project.network.nodes)
        self.assertEqual(project.network.nodes["OSM_3"].node_type, "attribute_change")
        self.assertEqual(len(project.network.links), 2)

    def test_xml_import_merges_bidirectional_same_road_continuation(self):
        with workspace_tempdir() as temp_dir:
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

        self.assertEqual(sorted(project.network.nodes), ["OSM_1", "OSM_5"])
        self.assertEqual(len(project.network.links), 2)
        directions = {
            (link.start_node_id, link.end_node_id)
            for link in project.network.links.values()
        }
        self.assertEqual(directions, {("OSM_1", "OSM_5"), ("OSM_5", "OSM_1")})

    def test_cli_writes_project_json(self):
        with workspace_tempdir() as temp_dir:
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
            self.assertEqual(len(data["network"]["links"]), 2)
            self.assertEqual(data["network"]["links"][0]["traffic_counts"]["car"], 700)


if __name__ == "__main__":
    unittest.main()
