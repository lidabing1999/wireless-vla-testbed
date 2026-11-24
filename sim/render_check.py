"""Load the default LIBERO task and check headless rendering without OpenPI."""
from pathlib import Path

from libero.libero import benchmark, get_libero_path
from libero.libero.envs import OffScreenRenderEnv

suite = benchmark.get_benchmark_dict()["libero_goal"]()
task = suite.get_task(1)
bddl = Path(get_libero_path("bddl_files")) / task.problem_folder / task.bddl_file
env = OffScreenRenderEnv(bddl_file_name=str(bddl), camera_heights=256, camera_widths=256)
try:
    env.seed(7)
    env.reset()
    env.set_init_state(suite.get_task_init_states(1)[0])
    obs, *_ = env.step([0.0] * 6 + [-1.0])
    frame = obs["agentview_image"]
    if frame.shape != (256, 256, 3) or frame.max() == frame.min():
        raise RuntimeError(f"unexpected frame {frame.shape}")
    print(f"LIBERO/MuJoCo rendered {frame.shape} for {task.language}.")
finally:
    env.close()
