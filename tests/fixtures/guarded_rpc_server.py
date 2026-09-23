"""Offline subprocess fixture: exercise the real CLI/FD framing without native APIs."""

import os

from macos_harness import cli, guarded_ax, macos
from macos_harness.guarded import Snapshot, Target


class OfflineMac:
    def __init__(self):
        os.write(1, b"offline MacOS constructed once\n")


class OfflineBackend:
    def __init__(self, mac):
        self.mac = mac
        self.generation = 0
        self.target = Target(
            object(), object(), "state", "AXButton", "Save", ("press",)
        )

    def read_snapshot(self, app, limit, deadline):
        return Snapshot((42, 1, "offline", "/offline"), self.generation, (self.target,))

    def execute(self, *args):
        print("offline effect")
        self.generation += 1
        return {"status": "executed"}


macos.MacOS = OfflineMac
guarded_ax.NativeAXBackend = OfflineBackend
raise SystemExit(cli.main())
