"""Trace LIBERO steps and OpenPI client timing on the same monotonic clock."""
import json
import os
import runpy
import sys
import threading
import time
from pathlib import Path

from libero.libero import benchmark
from libero.libero.envs import OffScreenRenderEnv
from openpi_client import msgpack_numpy
from openpi_client.websocket_client_policy import WebsocketClientPolicy

trace_path = Path(sys.argv[1])
entrypoint = sys.argv[2]
sys.argv = sys.argv[2:]
state_index = int(os.environ.get("LIBERO_INIT_INDEX", "0"))
original_states = benchmark.Benchmark.get_task_init_states
original_step = OffScreenRenderEnv.step
step_number = 0
request_number = 0
trace_lock = threading.Lock()


def record(event, **fields):
    with trace_lock:
        trace.write(json.dumps({"event": event, **fields}) + "\n")


def select_state(self, task_id):
    return [original_states(self, task_id)[state_index]]


def timed_infer(self, observation):
    global request_number
    request_number += 1
    step_at_start = step_number
    start = time.monotonic_ns()
    data = self._packer.pack(observation)
    packed = time.monotonic_ns()
    tx_wall = time.time_ns()
    try:
        self._ws.send(data)
        reply = self._ws.recv()
        rx_wall = time.time_ns()
        received = time.monotonic_ns()
        if isinstance(reply, str):
            raise RuntimeError(f"Error in inference server:\n{reply}")
        response = msgpack_numpy.unpackb(reply)
    except Exception:
        record("infer_error", number=request_number, start_ns=start, end_ns=time.monotonic_ns())
        raise
    end = time.monotonic_ns()
    record(
        "infer",
        number=request_number,
        step=step_at_start,
        start_ns=start,
        end_ns=end,
        tx_wall_ns=tx_wall,
        rx_wall_ns=rx_wall,
        wire_interval_ns=received-packed,
        pack_ns=packed-start,
        unpack_ns=end-received,
        observation_bytes=len(data),
        response_bytes=len(reply),
        returned_steps=len(response["actions"]),
        server_infer_ms=(response.get("server_timing") or {}).get("infer_ms"),
    )
    return response


def timed_step(self, action):
    global step_number
    step_number += 1
    start = time.monotonic_ns()
    sim_start = float(self.sim.data.time)
    result = original_step(self, action)
    end = time.monotonic_ns()
    record(
        "step",
        number=step_number,
        start_ns=start,
        end_ns=end,
        sim_start_s=sim_start,
        sim_end_s=float(self.sim.data.time),
        done=bool(result[2]),
    )
    return result


benchmark.Benchmark.get_task_init_states = select_state
WebsocketClientPolicy.infer = timed_infer
OffScreenRenderEnv.step = timed_step
with trace_path.open("w", buffering=1) as trace:
    record("configuration", initial_state_index=state_index)
    runpy.run_path(entrypoint, run_name="__main__")
