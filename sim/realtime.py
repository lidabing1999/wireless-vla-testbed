"""Pace one independent MuJoCo environment while its UE waits for a policy."""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


class RealtimeControl:
    period = 50_000_000

    def __init__(self, output):
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.pending = None
        self.request = 0
        self.observed_ns = None
        self.log = output.open("w", buffering=1)
        self.gripper = -1.0
        self.tick = 0
        self.target = time.monotonic_ns()
        self.finished = self.target
        if group := os.environ.get("VLA_ARM_GROUP"):
            directory = Path(group)
            (directory / f"ready-{os.environ['VLA_ARM_ID']}").touch()
            while not (directory / "start.ns").exists():
                time.sleep(0.01)
            self.target = int((directory / "start.ns").read_text())

    def release(self):
        now = time.monotonic_ns()
        if now < self.target:
            time.sleep((self.target - now) / 1e9)
            now = time.monotonic_ns()
        self.skipped = (now - self.target) // self.period
        self.target += self.skipped * self.period
        self.tick += 1

    def submit(self, client, observation, observed_ns):
        self.request += 1
        self.observed_ns = observed_ns
        self.pending = self.pool.submit(client.infer, observation)

    def receive(self):
        if self.pending.done():
            reply = self.pending.result()
            self.pending = None
            return reply["actions"]
        return None

    def step(self, env, action, kind):
        self.gripper = float(action[-1])
        start = time.monotonic_ns()
        result = env.step(action)
        self.finished = time.monotonic_ns()
        self.log.write(json.dumps(dict(tick=self.tick, release_ns=self.target, start_ns=start,
                       end_ns=self.finished, missed_releases=self.skipped,
                       finished_in_period=self.finished <= self.target + self.period,
                       action=kind, request=self.request, observation_ns=self.observed_ns)) + "\n")
        self.target += self.period
        return result

    def hold(self):
        return [0.] * 6 + [self.gripper]

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.log.close()
