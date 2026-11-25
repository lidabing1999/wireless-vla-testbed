# SPDX-License-Identifier: Apache-2.0
# Adapted from Physical Intelligence OpenPI examples/libero/main.py.
import collections
import json
import uuid
import dataclasses
import logging
import math
import pathlib

import imageio
from libero.libero import benchmark
from libero.libero import get_libero_path
from libero.libero.envs import OffScreenRenderEnv
import numpy as np
from openpi_client import image_tools
from openpi_client import websocket_client_policy as _websocket_client_policy
import tqdm
import tyro
from realtime import RealtimeControl

LIBERO_DUMMY_ACTION = [0.0] * 6 + [-1.0]
LIBERO_ENV_RESOLUTION = 256  # resolution used to render training data


@dataclasses.dataclass
class Args:
    #################################################################################################################
    # Model server parameters
    #################################################################################################################
    host: str = "0.0.0.0"
    port: int = 8000
    resize_size: int = 224
    replan_steps: int = 5
    clock: str = "blocking"
    max_control_steps: int | None = None

    #################################################################################################################
    # LIBERO environment-specific parameters
    #################################################################################################################
    task_suite_name: str = (
        "libero_spatial"  # Task suite. Options: libero_spatial, libero_object, libero_goal, libero_10, libero_90
    )
    num_steps_wait: int = 10  # Number of steps to wait for objects to stabilize i n sim
    num_trials_per_task: int = 50  # Number of rollouts per task

    #################################################################################################################
    # Utils
    #################################################################################################################
    video_out_path: str = "data/libero/videos"  # Path to save videos

    task_id: int = -1  # -1 evaluates all tasks; otherwise one task.
    seed: int = 7  # Random Seed (for reproducibility)


def eval_libero(args: Args) -> None:
    # Set random seed
    np.random.seed(args.seed)

    # Initialize LIBERO task suite
    benchmark_dict = benchmark.get_benchmark_dict()
    task_suite = benchmark_dict[args.task_suite_name]()
    num_tasks_in_suite = task_suite.n_tasks
    logging.info(f"Task suite: {args.task_suite_name}")

    output_dir = pathlib.Path(args.video_out_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.task_suite_name == "libero_spatial":
        max_steps = 220  # longest training demo has 193 steps
    elif args.task_suite_name == "libero_object":
        max_steps = 280  # longest training demo has 254 steps
    elif args.task_suite_name == "libero_goal":
        max_steps = 300  # longest training demo has 270 steps
    elif args.task_suite_name == "libero_10":
        max_steps = 520  # longest training demo has 505 steps
    elif args.task_suite_name == "libero_90":
        max_steps = 400  # longest training demo has 373 steps
    else:
        raise ValueError(f"Unknown task suite: {args.task_suite_name}")

    if args.max_control_steps is not None:
        max_steps = args.max_control_steps
    client = _websocket_client_policy.WebsocketClientPolicy(args.host, args.port)

    # Start evaluation
    total_episodes, total_successes = 0, 0
    if args.task_id < -1 or args.task_id >= num_tasks_in_suite:
        raise ValueError('task-id is outside the selected suite')
    task_ids = range(num_tasks_in_suite) if args.task_id == -1 else [args.task_id]
    for task_id in tqdm.tqdm(task_ids):
        # Get task
        task = task_suite.get_task(task_id)

        # Get default LIBERO initial states
        initial_states = task_suite.get_task_init_states(task_id)

        # Initialize LIBERO environment and task description
        env, task_description = _get_libero_env(task, LIBERO_ENV_RESOLUTION, args.seed)

        # Start episodes
        task_episodes, task_successes = 0, 0
        for episode_idx in tqdm.tqdm(range(args.num_trials_per_task)):
            logging.info(f"\nTask: {task_description}")

            # Reset environment
            env.reset()
            action_plan = collections.deque()

            # Set initial states
            obs = env.set_init_state(initial_states[episode_idx])

            # Setup
            t = 0
            replay_images = []
            done = False
            rollout_error = None
            trace_name = ("control.jsonl" if args.num_trials_per_task == 1 and args.task_id >= 0
                          else f"control-task{task_id}-trial{episode_idx}.jsonl")
            clock = RealtimeControl(output_dir / trace_name) if args.clock == "realtime" else None

            logging.info(f"Starting episode {task_episodes+1}...")
            while t < max_steps + args.num_steps_wait:
                try:
                    if clock:
                        clock.release()
                    # IMPORTANT: Do nothing for the first few timesteps because the simulator drops objects
                    # and we need to wait for them to fall
                    if t < args.num_steps_wait:
                        obs, reward, done, info = (clock.step(env, LIBERO_DUMMY_ACTION, "warmup")
                                                  if clock else env.step(LIBERO_DUMMY_ACTION))
                        t += 1
                        continue

                    # Get preprocessed image
                    # IMPORTANT: rotate 180 degrees to match train preprocessing
                    img = np.ascontiguousarray(obs["agentview_image"][::-1, ::-1])
                    wrist_img = np.ascontiguousarray(obs["robot0_eye_in_hand_image"][::-1, ::-1])
                    img = image_tools.convert_to_uint8(
                        image_tools.resize_with_pad(img, args.resize_size, args.resize_size)
                    )
                    wrist_img = image_tools.convert_to_uint8(
                        image_tools.resize_with_pad(wrist_img, args.resize_size, args.resize_size)
                    )

                    # Save preprocessed image for replay video
                    replay_images.append(img)

                    if not action_plan:
                        # Finished executing previous action chunk -- compute new chunk
                        # Prepare observations dict
                        element = {
                            "observation/image": img,
                            "observation/wrist_image": wrist_img,
                            "observation/state": np.concatenate(
                                (
                                    obs["robot0_eef_pos"],
                                    _quat2axisangle(obs["robot0_eef_quat"]),
                                    obs["robot0_gripper_qpos"],
                                )
                            ),
                            "prompt": str(task_description),
                        }

                        # Query model to get action
                        if clock:
                            if clock.pending is None:
                                clock.submit(client, element, clock.finished)
                            action_chunk = clock.receive()
                            if action_chunk is None:
                                obs, reward, done, info = clock.step(env, clock.hold(), "hold")
                                t += 1
                                if done:
                                    break
                                continue
                        else:
                            action_chunk = client.infer(element)["actions"]
                        assert (
                            len(action_chunk) >= args.replan_steps
                        ), f"We want to replan every {args.replan_steps} steps, but policy only predicts {len(action_chunk)} steps."
                        action_plan.extend(action_chunk[: args.replan_steps])

                    action = action_plan.popleft()

                    # Execute action in environment
                    obs, reward, done, info = (clock.step(env, action.tolist(), "policy")
                                               if clock else env.step(action.tolist()))
                    if done:
                        break
                    t += 1

                except Exception as e:
                    rollout_error = repr(e)
                    logging.error(f"Caught exception: {e}")
                    break

            if clock:
                clock.close()
            task_episodes += 1
            total_episodes += 1

            # Save a replay video of the episode
            done = bool(env.check_success()) and rollout_error is None
            task_successes += int(done)
            total_successes += int(done)
            suffix = "success" if done else "failure"
            terminal_image = np.ascontiguousarray(obs["agentview_image"][::-1, ::-1])
            terminal_video_image = image_tools.convert_to_uint8(
                image_tools.resize_with_pad(terminal_image, args.resize_size, args.resize_size)
            )
            replay_images.append(terminal_video_image)
            run_id = f"task{task_id}_trial{episode_idx}_seed{args.seed}_{uuid.uuid4().hex[:12]}"
            imageio.imwrite(output_dir / f"{run_id}_{suffix}_terminal.png", terminal_image)
            for label, fraction in [("start", 0.0), ("middle", 0.5)]:
                frame_id = round(fraction * (len(replay_images) - 1))
                imageio.imwrite(output_dir / f"{run_id}_{label}.png", replay_images[frame_id])
            metadata = {"suite": args.task_suite_name, "task_id": task_id,
                        "task": task_description, "trial": episode_idx, "seed": args.seed,
                        "success": done, "error": rollout_error, "frames": len(replay_images)}
            (output_dir / f"{run_id}_{suffix}.json").write_text(json.dumps(metadata, indent=2))
            task_segment = task_description.replace(" ", "_")
            imageio.mimwrite(
                pathlib.Path(args.video_out_path) / f"{run_id}_{task_segment}_{suffix}.mp4",
                [np.asarray(x) for x in replay_images],
                fps=20 if clock else 10,
            )

            # Log current results
            logging.info(f"Success: {done}")
            logging.info(f"# episodes completed so far: {total_episodes}")
            logging.info(f"# successes: {total_successes} ({total_successes / total_episodes * 100:.1f}%)")

        env.close()

        # Log final results
        logging.info(f"Current task success rate: {float(task_successes) / float(task_episodes)}")
        logging.info(f"Current total success rate: {float(total_successes) / float(total_episodes)}")

    logging.info(f"Total success rate: {float(total_successes) / float(total_episodes)}")
    logging.info(f"Total episodes: {total_episodes}")


def _get_libero_env(task, resolution, seed):
    """Initializes and returns the LIBERO environment, along with the task description."""
    task_description = task.language
    task_bddl_file = pathlib.Path(get_libero_path("bddl_files")) / task.problem_folder / task.bddl_file
    env_args = {"bddl_file_name": task_bddl_file, "camera_heights": resolution, "camera_widths": resolution}
    env = OffScreenRenderEnv(**env_args)
    env.seed(seed)  # IMPORTANT: seed seems to affect object positions even when using fixed initial state
    return env, task_description


def _quat2axisangle(quat):
    """
    Copied from robosuite: https://github.com/ARISE-Initiative/robosuite/blob/eafb81f54ffc104f905ee48a16bb15f059176ad3/robosuite/utils/transform_utils.py#L490C1-L512C55
    """
    # clip quaternion
    if quat[3] > 1.0:
        quat[3] = 1.0
    elif quat[3] < -1.0:
        quat[3] = -1.0

    den = np.sqrt(1.0 - quat[3] * quat[3])
    if math.isclose(den, 0.0):
        # This is (close to) a zero degree rotation, immediately return
        return np.zeros(3)

    return (quat[:3] * 2.0 * math.acos(quat[3])) / den


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    tyro.cli(eval_libero)
