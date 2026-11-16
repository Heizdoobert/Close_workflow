#!/bin/bash
python3 -c "from flow_engine import bench; assert bench.component_files() == sorted({f for v in bench.COMPONENTS.values() for f in v})"
