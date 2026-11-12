#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""Thin launcher for run_task.ps1 / run_one_task.ps1.

Single source of truth lives one level up: D:\04_Tools\Workflow\close_workflow_cli.py
Keep this file a shim so encoding/behavior fixes never diverge between copies.
"""

import runpy
from pathlib import Path

runpy.run_path(
    str(Path(__file__).resolve().parent.parent / "close_workflow_cli.py"),
    run_name="__main__",
)
