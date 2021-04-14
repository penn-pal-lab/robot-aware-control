from functools import partial
import os
from collections import defaultdict

import ipdb
from src.utils.plot import putText

import h5py
import imageio
import numpy as np
from src.config import argparser
from src.env.fetch.clutter_push import ClutterPushEnv
from tqdm import tqdm


def generate_demos(rank, config, behavior, record, num_trajectories, ep_len):
    """
    This generates demos, like random moving or block pushing.

    We first have the robot perform the behavior, save the trajectory, and render it with the robot inpainted, and also render with the robot in scene.
    Next, we replay the trajectory, but move the robot out of scene and move the block without the robot.

    This results in 3 types of video for the dataset.
    1. Video of inpainted robot pushing block
    2. Video of robot pushing block
    3. Video of blocks moving by themselves. This is equivalent to using a perfect inpainting method.

    rank: idx of the worker
    config: configuration
    behavior: what the robot will do. see env.generate_demo for types
    record: whether to record the gif or not
    num_trajectories: number of demos to generate
    ep_len: max length of the demo. only used by some behaviors.
    noise: action noise in behavior. only used by some behaviors
    """
    config.seed = rank
    env = ClutterPushEnv(config)
    len_stats = []
    obj_moved_stats = defaultdict(list)
    it = range(num_trajectories)
    if rank == 0:
        it = tqdm(it)
    for i in it:
        record = rank == 0 and record
        name = f"{behavior}_{rank}_{i}.hdf5"
        path = os.path.join(config.demo_dir, name)
        history = env.generate_demo(behavior)
        record_path = f"videos/{behavior}_{config.seed}_{i}.gif"
        obs = history["obs"]  # array of observation dictionaries
        len_stats.append(len(obs))
        object_inpaint_demo = []
        robot = []
        obj_poses = defaultdict(list)
        states = []
        robot_states = []
        masks = []
        eef_keypoints = []
        for ob in obs:
            if config.norobot_pixels_ob:
                masks.append(ob["mask"])
            robot_states.append(ob["robot"])
            object_inpaint_demo.append(ob["observation"])
            states.append(ob["state"])
            for obj in env._objects:
                obj_poses[obj + ":joint"].append(ob[obj + ":joint"])
            # add keypoints
            eef_keypoints.append(ob["0_eef_keypoint"])
        # record object changes
        for obj in env._objects:
            obj_change = np.linalg.norm(obj_poses[f"{obj}:joint"][-1][:2] - obj_poses[f"{obj}:joint"][0][:2])
            obj_moved_stats[obj].append(obj_change)

        object_inpaint_demo = np.asarray(object_inpaint_demo)
        robot = np.asarray(robot)
        actions = history["ac"]
        assert len(object_inpaint_demo) - 1 == len(actions)
        # now render the object only demonstration
        env = ClutterPushEnv(config)
        env.reset()
        # first move robot out of view
        env.sim.data.set_joint_qpos("robot0:slide2", 1)
        env.sim.data.set_joint_qpos("robot0:slide0", -1)
        env.sim.forward()
        # save the arm overhead state for rendering object only scene
        arm_overhead_state = env.get_flattened_state()
        # for each timestep, set objects, render the image, and save
        object_only_demo = []
        robot_demo = []
        gif = []
        for t in range(len(object_inpaint_demo)):
            for k, v in obj_poses.items():
                env.sim.data.set_joint_qpos(k, v[t])
            env.sim.forward()
            object_only_img = env.render()
            object_only_demo.append(object_only_img)
            # now render the with robot img
            env.set_flattened_state(states[t])
            robot_img = env.render(remove_robot=False)
            robot_demo.append(robot_img)
            if record:
                # concat with the inpainted image and background img
                gif_robot_img = robot_img.copy()
                putText(gif_robot_img, f"REAL", (10, 10))
                putText(gif_robot_img, f"{t}", (0, 126))

                gif_inpaint_img = object_inpaint_demo[t].copy()
                putText(gif_inpaint_img, f"INPAINT", (10, 10))

                gif_object_only_img = object_only_img.copy()
                putText(gif_object_only_img, f"NO-ROBOT", (10, 10))

                cost = -np.linalg.norm(gif_inpaint_img.astype(np.float) - gif_object_only_img.astype(np.float))
                putText(gif_inpaint_img, f"{cost:.0f}", (0, 126))
                img = np.concatenate(
                    [gif_robot_img, gif_inpaint_img, gif_object_only_img], axis=1
                )
                gif.append(img)
            env.set_flattened_state(arm_overhead_state)

        if record:
            imageio.mimwrite(record_path, gif)
        with h5py.File(path, "w") as hf:
            create_dataset = partial(hf.create_dataset, compression="gzip")
            hf.attrs["pushed_obj"] = str(history["pushed_obj"])
            create_dataset("states", data=states)
            create_dataset("actions", data=actions)
            create_dataset("robot_state", data=robot_states)
            if config.norobot_pixels_ob:
                create_dataset("masks", data=masks)
            # ground truth object demo
            create_dataset("object_only_demo", data=object_only_demo)
            # inpainted object demo
            create_dataset("object_inpaint_demo", data=object_inpaint_demo)
            #  noinpaint demo
            create_dataset("robot_demo", data=robot_demo)
            for obj in env._objects:
                create_dataset(obj + ":joint", data=obj_poses[obj + ":joint"])
            # eef keypoints
            create_dataset("0_eef_keypoints", data=eef_keypoints)
            # camera position and quaternion
            create_dataset("0_camera_pose", data=env.camera_poses[0])
            # camera intrinsics matrix
            create_dataset("0_camera_intrinsic", data=env.camera_intrinsics[0])

    # print out stats about the dataset
    stats_str = f"Avg len: {np.mean(len_stats)}\nstd: {np.std(len_stats)}\nmin: {np.min(len_stats)}\nmax: {np.max(len_stats)}\n"
    print(stats_str)
    stats_path = os.path.join(config.demo_dir, f"stats_{behavior}_{config.seed}.txt")
    with open(stats_path, "w") as f:
        f.write(stats_str)
         # count number of episodes where object moved
        for obj in env._objects:
            obj_move = np.asarray(obj_moved_stats[obj])
            stats_str = f"{obj} dist mean: {obj_move.mean()} min: {obj_move.min():.2f}, max: {obj_move.max():.2f}\n"
            f.write(stats_str)
            stats_str = f"Num episodes where {obj} moved more than 1cm {np.sum(obj_move > 0.01)}\n"
            f.write(stats_str)


def create_demo_dataset(config, num_demo, num_workers, record, behavior, ep_len):
    """
    Collect all demonstrations and save into demo_dir
    You can use multiple workers if generating 1000s of demonstrations
    """
    from multiprocessing import Process

    os.makedirs(config.demo_dir, exist_ok=True)
    if num_workers == 1:
        generate_demos(0, config, behavior, record, num_demo, ep_len)
    else:
        ps = []
        for i in range(num_workers):
            p = Process(
                target=generate_demos,
                args=(i, config, behavior, record, num_demo, ep_len),
            )
            ps.append(p)

        for p in ps:
            p.start()

        for p in ps:
            p.join()


def collect_demo_cem_data():
    """
    Used for collecting the demo dataset for demo CEM
    """
    num_demo = 100  # per worker
    num_workers = 1
    record = True
    behavior = "straight_push"
    ep_len = 12  # gonna be off by -1 because of reset but whatever

    config, _ = argparser()
    config.norobot_pixels_ob = True  # whether to inpaint the robot in observation

    config.reward_type = "inpaint"
    config.robot_mask_with_obj = False
    config.demo_dir = "demos/realisticrobotmask_straight_push"
    config.most_recent_background = False  # use static or mr background for inpaint
    config.multiview = True
    config.img_dim = 64
    config.camera_ids = [0, 2]
    create_demo_dataset(config, num_demo, num_workers, record, behavior, ep_len)


def collect_svg_data():
    """
    Generate video dataset for SVG model training
    Collect 7k noisy pushing, 3k truly random demonstrations
    Each demo is around 7-14 steps long, and the dataset will be around 100k images total
    """
    num_workers = 1
    num_push = 20000 // num_workers
    num_rand = 10000 // num_workers
    record = False
    ep_len = 12  # gonna be off by 1 because of reset but whatever

    config, _ = argparser()
    config.norobot_pixels_ob = True
    config.inpaint_eef = True
    config.reward_type = "inpaint"
    config.demo_dir = "demos/realvp_random_mask_svg_train"
    config.most_recent_background = False
    config.multiview = True
    config.img_dim = 64
    config.camera_ids = [0, 2]
    config.temporal_beta = 0.2  # control random policy's temporal correlation
    config.action_noise = 0.05
    # create_demo_dataset(config, num_push, num_workers, record, "straight_push", ep_len)
    create_demo_dataset(config, num_push, num_workers, record, "temporal_random_robot", ep_len)
    create_demo_dataset(config, num_rand, num_workers, record, "random_robot", ep_len)

def collect_camera_calibration_data():
    """
    We need some trajectories with:
    ground truth camera matrix
    ground truth camera pose (pos and quaternion)
    ground truth 3d eef position
    ground truth 2d eef position
    """
    num_demo = 100  # per worker
    num_workers = 1
    record = True
    behavior = "straight_push"
    ep_len = 12  # gonna be off by -1 because of reset but whatever

    config, _ = argparser()
    config.norobot_pixels_ob = False  # whether to inpaint the robot in observation

    config.reward_type = "inpaint"
    config.robot_mask_with_obj = False
    config.demo_dir = "demos/camera_calibration"
    config.most_recent_background = False  # use static or mr background for inpaint
    config.multiview = True
    config.img_dim = 64
    config.camera_ids = [0]
    create_demo_dataset(config, num_demo, num_workers, record, behavior, ep_len)

def collect_multiview_trajectory(
    rank, config, behavior, record, num_trajectories, ep_len
):
    # save the background image for inpainting?
    # save the robot segmentation mask?
    # or just save the inpainted image directly?
    config.seed = rank
    env = ClutterPushEnv(config)
    len_stats = []
    it = range(num_trajectories)
    all_frames = []
    all_world_coord = []
    all_keypoints = []
    if rank == 0:
        it = tqdm(it)
    for i in it:
        # only record first episode for sanity check
        record = rank == 0 and i == 0 and record
        name = f"{behavior}_{rank}_{i}.hdf5"
        path = os.path.join(config.demo_dir, name)
        history = env.generate_demo(behavior)
        obs = history["obs"]  # array of observation dictionaries
        len_stats.append(len(obs))
        frames = []
        robot = []
        world_coord = []
        keypoints = [] # dimension T x C x 4 x 2
        for ob in obs:
            world_coord.append(ob["world_coord"].transpose(1,2,0))
            frames.append(ob["observation"])
            robot.append(ob["robot"])
            # for each viewpoint: [robot, obj1, obj2, obj3]
            camera_keypoints = [] # C x 4 x 2
            for cam_id in config.camera_ids:
                kp = []
                kp.append(ob[f"{cam_id}_eef_keypoint"])
                for obj in env._objects:
                    kp.append(ob[f"{cam_id}_{obj}_keypoint"]) # 4 x 2
                camera_keypoints.append(kp) # C x 4 x 2
            keypoints.append(camera_keypoints) # T x C x 4 x 2

        frames = np.asarray(frames)
        world_coord = np.asarray(world_coord)
        keypoints = np.asarray(keypoints)

        all_world_coord.append(world_coord)
        all_frames.append(frames)
        all_keypoints.append(keypoints)
        # robot = np.asarray(robot)
        # actions = history["ac"]
        # assert len(frames) - 1 == len(actions)
    with h5py.File(path, "w") as hf:
        for i, (frame, world_coord, kp) in tqdm(enumerate(zip(all_frames, all_world_coord, all_keypoints))):
            hf.create_dataset(f"frame_{i}", data=frame, compression="gzip")
            hf.create_dataset(f"world_coord_{i}", data=world_coord, compression="gzip")
            hf.create_dataset(f"keypoints_{i}", data=kp, compression="gzip")
        # print("Frame shape:", all_frames.shape)
        # print("World Coord shape:", all_world_coord.shape)
        # hf.create_dataset("frames", data=all_frames, compression="gzip")
        # hf.create_dataset("world_coord", data=all_world_coord, compression="gzip")
        # hf.create_dataset("robot", data=robot, compression="gzip")
        # hf.create_dataset("actions", data=actions, compression="gzip")

    # print out stats about the dataset
    stats_str = f"Avg len: {np.mean(len_stats)}\nstd: {np.std(len_stats)}\nmin: {np.min(len_stats)}\nmax: {np.max(len_stats)}"
    print(stats_str)
    stats_path = os.path.join(config.demo_dir, f"stats_{behavior}_{config.seed}.txt")
    with open(stats_path, "w") as f:
        f.write(stats_str)

def collect_multiview_trajectories():
    """
    Collect multiview dataset with inpainting
    """
    from multiprocessing import Process

    num_trajectories = 2000  # per worker
    num_workers = 1
    record = False
    behavior = "straight_push"
    ep_len = 12  # gonna be off by -1 because of reset but whatever

    config, _ = argparser()
    config.large_block = True
    config.demo_dir = "/Datasets/tckn_data"
    config.multiview = True
    config.norobot_pixels_ob = False
    config.reward_type = "dense"
    config.img_dim = 128
    config.camera_ids = [0, 2]
    config.depth_ob = True
    os.makedirs(config.demo_dir, exist_ok=True)

    if num_workers == 1:
        collect_multiview_trajectory(
            0, config, behavior, record, num_trajectories, ep_len
        )
    else:
        ps = []
        for i in range(num_workers):
            if i % 2 == 0:
                behavior = "random_robot"
            else:
                behavior = "push"
            p = Process(
                target=collect_multiview_trajectory,
                args=(i, config, behavior, record, num_trajectories, ep_len),
            )
            ps.append(p)

        for p in ps:
            p.start()

        for p in ps:
            p.join()


if __name__ == "__main__":
    """
    Use this to collect demonstrations for svg / demo cem experiments
    """
    # collect_svg_data()
    # collect_demo_cem_data()
    collect_multiview_trajectories()
