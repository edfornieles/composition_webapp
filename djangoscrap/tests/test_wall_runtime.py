from __future__ import annotations

import json

from django.test import Client, TestCase

from djangoscrap.models import Composition, WallProfile, WallRun
from djangoscrap.wall_runtime import compute_wall_tick


class WallRuntimeTests(TestCase):
    def setUp(self):
        self.client = Client()
        for i in range(1, 13):
            Composition.objects.create(
                name=f"comp-{i}",
                type="classic",
                background_video=f"videos/backgrounds/bg{i}.mp4",
                foreground_video=f"videos/foregrounds/fg{i}.mp4",
                url=f"http://127.0.0.1:8000/comp-{i}/",
                composition_hashtags=["alpha", f"tag{i % 3}"],
            )

    def test_compute_wall_tick_is_deterministic_for_same_tick(self):
        profile = WallProfile.objects.create(name="p1", rows=3, cols=3, strict_live=True)
        run = WallRun.objects.create(
            profile=profile,
            status="running",
            thought_text="alpha urgency",
            scenario_state="neutral",
        )
        first = compute_wall_tick(run, tick_id=5)
        second = compute_wall_tick(run, tick_id=5)
        self.assertEqual(first["assignments"], second["assignments"])
        self.assertEqual(len(first["tiles"]), 9)

    def test_wall_control_start_and_state_returns_assignment(self):
        start = self.client.post(
            "/api/wall/control",
            data=json.dumps(
                {
                    "action": "start",
                    "character_keys": [],
                    "thought_text": "alpha",
                    "scenario_state": "intense",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(start.status_code, 200)
        self.assertTrue(start.json()["ok"])

        state = self.client.get("/api/wall/state", {"screen_id": "1"})
        self.assertEqual(state.status_code, 200)
        body = state.json()
        self.assertTrue(body["running"])
        self.assertGreater(body["tick_id"], 0)
        self.assertTrue(body["assignment"])
        self.assertTrue(body["assignment"]["composition_url"])

    def test_wall_next_tick_and_heartbeat(self):
        self.client.post(
            "/api/wall/control",
            data=json.dumps({"action": "start", "thought_text": "alpha"}),
            content_type="application/json",
        )
        before = self.client.get("/api/wall/state", {"screen_id": "1"}).json()
        next_res = self.client.post(
            "/api/wall/control",
            data=json.dumps({"action": "next"}),
            content_type="application/json",
        )
        self.assertEqual(next_res.status_code, 200)
        after = self.client.get("/api/wall/state", {"screen_id": "1"}).json()
        self.assertGreaterEqual(after["tick_id"], before["tick_id"] + 1)

        hb = self.client.post(
            "/api/wall/heartbeat",
            data=json.dumps(
                {
                    "screen_id": "1",
                    "last_applied_tick": after["tick_id"],
                    "clock_offset_ms": 5,
                    "drift_ms": 40,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(hb.status_code, 200)
        with_hb = self.client.get("/api/wall/state", {"screen_id": "1"}).json()
        self.assertTrue(any(str(row["screen_id"]) == "1" for row in with_hb.get("heartbeats", [])))
