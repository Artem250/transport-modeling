from __future__ import annotations

import argparse
import sys

from PyQt5.QtWidgets import QApplication

from traffic_viz_test import MainWindow


def main() -> None:
    parser = argparse.ArgumentParser(description="Open CTM dynamic visualization for a chosen result JSON.")
    parser.add_argument("--map", default="map_nstu.osm", help="OSM map file used as background")
    parser.add_argument("--results", default="ctm_results_viz.json", help="CTM result JSON file")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    window = MainWindow(map_file=args.map, data_file=args.results)
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
