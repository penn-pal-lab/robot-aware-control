import os

import matplotlib.pyplot as plt
import numpy as np
from gym import spaces, utils
from mujoco_py.generated import const
from PIL import Image, ImageFilter
from skimage.filters import gaussian

from src.env.fetch.fetch_env import FetchEnv
from src.env.fetch.rotations import mat2euler
from src.env.fetch.utils import reset_mocap2body_xpos, reset_mocap_welds, robot_get_obs

# Ensure we get the path separator correct on windows
MODEL_XML_PATH = os.path.join("fetch", "push.xml")
LARGE_MODEL_XML_PATH = os.path.join("fetch", "large_push.xml")


class FetchPushEnv(FetchEnv, utils.EzPickle):
    """
    Pushes a block. We extend FetchEnv for:
    1) Pixel observations
    2) Image goal sampling where robot and block moves to goal location
    3) reward_type: dense, weighted
    """

    def __init__(self, config):
        initial_qpos = {
            "robot0:slide0": 0.175,
            "robot0:slide1": 0.48,
            "robot0:slide2": 0.1,
            "object0:joint": [1, 0.75, 0.4, 1.0, 0.0, 0.0, 0.0],
        }
        self._robot_pixel_weight = config.robot_pixel_weight
        reward_type = config.reward_type
        self._img_dim = config.img_dim
        self._camera_name = config.camera_name
        self._multiview = config.multiview
        self._camera_ids = config.camera_ids
        self._pixels_ob = config.pixels_ob
        self._distance_threshold = {
            "object": config.object_dist_threshold,
            "gripper": config.gripper_dist_threshold,
        }
        self._robot_goal_distribution = config.robot_goal_distribution
        self._push_dist = config.push_dist
        self._background_img = None
        self._large_block = config.large_block
        xml_path = MODEL_XML_PATH
        if self._large_block:
            xml_path = LARGE_MODEL_XML_PATH
        self._blur_width = self._img_dim * 2
        self._sigma = config.blur_sigma
        self._unblur_cost_scale = config.unblur_cost_scale
        FetchEnv.__init__(
            self,
            xml_path,
            has_object=True,
            block_gripper=True,
            n_substeps=20,
            gripper_extra_height=0.0,
            target_in_the_air=False,
            target_offset=0.0,
            obj_range=0.15,
            target_range=0.15,
            distance_threshold=0.05,
            initial_qpos=initial_qpos,
            reward_type=reward_type,
            seed=config.seed,
        )
        utils.EzPickle.__init__(self)
        self.action_space = spaces.Box(-1.0, 1.0, shape=(3,), dtype="float32")

    def _get_obs(self):
        if self._pixels_ob:
            obs = self.render("rgb_array")
            return {
                "observation": obs.copy(),
                "achieved_goal": obs.copy(),
                "desired_goal": self.goal.copy(),
            }
        # positions
        grip_pos = self.sim.data.get_site_xpos("robot0:grip")
        dt = self.sim.nsubsteps * self.sim.model.opt.timestep
        grip_velp = self.sim.data.get_site_xvelp("robot0:grip") * dt
        robot_qpos, robot_qvel = robot_get_obs(self.sim)
        if self.has_object:
            object_pos = self.sim.data.get_site_xpos("object0")
            # rotations
            object_rot = mat2euler(self.sim.data.get_site_xmat("object0"))
            # velocities
            object_velp = self.sim.data.get_site_xvelp("object0") * dt
            object_velr = self.sim.data.get_site_xvelr("object0") * dt
            # gripper state
            object_rel_pos = object_pos - grip_pos
            object_velp -= grip_velp

        gripper_state = robot_qpos[-2:]
        gripper_vel = (
            robot_qvel[-2:] * dt
        )  # change to a scalar if the gripper is made symmetric
        achieved_goal = np.concatenate([object_pos, grip_pos])
        obs = np.concatenate(
            [
                grip_pos,
                object_pos.ravel(),
                object_rel_pos.ravel(),
                gripper_state,
                object_rot.ravel(),
                object_velp.ravel(),
                object_velr.ravel(),
                grip_velp,
                gripper_vel,
            ]
        )
        return {
            "observation": obs.copy(),
            "achieved_goal": achieved_goal.copy(),
            "desired_goal": self.goal.copy(),
        }

    def _reset_sim(self):
        self.sim.set_state(self.initial_state)

        # Randomize start position of object.
        # object_xpos = self.initial_gripper_xpos[:2]
        # while np.linalg.norm(object_xpos - self.initial_gripper_xpos[:2]) < 0.1:
        #     object_xpos = self.initial_gripper_xpos[:2] + self.np_random.uniform(-self.obj_range, self.obj_range, size=2)
        # object_qpos = self.sim.data.get_joint_qpos('object0:joint')
        # assert object_qpos.shape == (7,)
        # object_qpos[:2] = object_xpos
        # self.sim.data.set_joint_qpos('object0:joint', object_qpos)

        self.sim.forward()
        return True

    def reset(self):
        obs = super().reset()
        self._use_unblur = False
        return obs

    def _sample_goal(self):
        noise = np.zeros(3)
        # pushing axis noise
        # noise[0] = self.np_random.uniform(0.15, 0.15 + self.target_range, size=1)
        noise[0] = self._push_dist
        # side axis noise
        noise[1] = self.np_random.uniform(-0.02, 0.02, size=1)

        goal = self.initial_object_xpos[:3] + noise
        goal += self.target_offset
        goal[2] = self.height_offset

        init_state = self.get_state()
        # move block to target position
        obj_pose = [0, 0, 0, 1, 0, 0, 0]
        obj_pose[:3] = goal[:3]
        self.sim.data.set_joint_qpos("object0:joint", obj_pose)
        reset_mocap_welds(self.sim)
        self.sim.forward()
        # move robot behind block position or make it random
        obj_pos = self.sim.data.get_site_xpos("object0").copy()
        if self._robot_goal_distribution == "random":
            robot_noise = np.array([-0.1, 0, 0])  # 10cm behind block so no collide
            robot_noise[1] = self.np_random.uniform(-0.2, 0.2, size=1)  # side axis
            robot_noise[2] = self.np_random.uniform(0.01, 0.3, size=1)  # z axis
            gripper_target = obj_pos + robot_noise
        elif self._robot_goal_distribution == "behind_block":
            gripper_target = obj_pos + [-0.05, 0, 0]
        gripper_rotation = np.array([1.0, 0.0, 1.0, 0.0])
        self.sim.data.set_mocap_pos("robot0:mocap", gripper_target)
        self.sim.data.set_mocap_quat("robot0:mocap", gripper_rotation)
        for _ in range(10):
            self.sim.step()

        # set target site to obj pos
        site_id = self.sim.model.site_name2id("target0")
        sites_offset = (
            self.sim.data.site_xpos[site_id] - self.sim.model.site_pos[site_id]
        ).copy()
        obj_pos = self.sim.data.get_site_xpos("object0").copy()
        self.sim.model.site_pos[site_id] = obj_pos - sites_offset
        self.sim.forward()
        obj_pos = self.sim.data.get_site_xpos("object0").copy()
        robot_pos = self.sim.data.get_site_xpos("robot0:grip").copy()
        if self._pixels_ob:
            goal = self.render(mode="rgb_array")
        else:
            goal = np.concatenate([obj_pos, robot_pos])

        # record goal info for checking success later
        self.goal_pose = {"object": obj_pos, "gripper": robot_pos}
        if self.reward_type in ["inpaint-blur", "inpaint", "weighted", "blackrobot"]:
            self.goal_mask = self.get_robot_mask()
            if self.reward_type in ["inpaint-blur", "inpaint"]:
                # inpaint the goal image with robot pixels
                goal[self.goal_mask] = self._background_img[self.goal_mask]
            elif self.reward_type == "blackrobot":
                # set the robot pixels to 0
                goal[self.goal_mask] = np.zeros(3)

        if self.reward_type == "inpaint-blur":
            # https://stackoverflow.com/questions/25216382/gaussian-filter-in-scipy
            s = self._sigma
            w = self._blur_width
            t = (((w - 1) / 2) - 0.5) / s
            self._unblurred_goal = goal
            goal = np.uint8(255 * gaussian(goal, sigma=s, truncate=t, mode="nearest", multichannel=True))

        # reset to previous state
        self.set_state(init_state)
        reset_mocap2body_xpos(self.sim)
        reset_mocap_welds(self.sim)
        return goal

    def render(
        self,
        mode="rgb_array",
        width=512,
        height=512,
        camera_name=None,
        segmentation=False,
    ):
        if self._multiview:
            imgs = []
            for cam_id in self._camera_ids:
                camera_name = self.sim.model.camera_id2name(cam_id)
                img = super().render(
                    mode,
                    self._img_dim,
                    self._img_dim,
                    camera_name=camera_name,
                    segmentation=segmentation,
                )
                imgs.append(img)
            multiview_img = np.concatenate(imgs, axis=0)
            return multiview_img
        return super().render(
            mode,
            self._img_dim,
            self._img_dim,
            camera_name=self._camera_name,
            segmentation=segmentation,
        )

    def _render_callback(self):
        return

    def _is_success(self, achieved_goal, desired_goal, info):
        current_pose = {
            "object": self.sim.data.get_site_xpos("object0").copy(),
            "gripper": self.sim.data.get_site_xpos("robot0:grip").copy(),
        }
        for k, v in current_pose.items():
            dist = np.linalg.norm(v - self.goal_pose[k])
            info[f"{k}_dist"] = dist
            succ = dist < self._distance_threshold[k]
            info[f"{k}_success"] = float(succ)
        if self._robot_goal_distribution == "random":
            return info["object_success"]
        elif self._robot_goal_distribution == "behind_block":
            return float(info["object_success"] and info["gripper_success"])

    def weighted_cost(self, achieved_goal, goal, info):
        """
        inpaint-blur:
            need use_unblur boolean to decide when to switch from blur to unblur
            cost.
        """
        a = self._robot_pixel_weight
        ag_mask = self.get_robot_mask()
        if self.reward_type in ["inpaint", "inpaint-blur"]:
            # set robot pixels to background image
            achieved_goal[ag_mask] = self._background_img[ag_mask]
            if self.reward_type == "inpaint-blur":
                s = self._sigma
                w = self._blur_width
                t = (((w - 1) / 2) - 0.5) / s
                unblurred_ag = achieved_goal
                achieved_goal = 255 * gaussian(achieved_goal, sigma=s, truncate=t, mode="nearest", multichannel=True)
                blur_cost = np.linalg.norm(achieved_goal.astype(np.float) - goal.astype(np.float))
                d = blur_cost
                if self._use_unblur:
                    unblur_cost = np.linalg.norm(unblurred_ag.astype(np.float) - self._unblurred_goal.astype(np.float))
                    d = self._unblur_cost_scale * unblur_cost
                return -d

            else:
                pixel_costs = achieved_goal - goal
        elif self.reward_type == "weighted":
            # get costs per pixel
            pixel_costs = (achieved_goal - goal).astype(np.float64)
            pixel_costs[self.goal_mask] *= a
            pixel_costs[ag_mask] *= a
        elif self.reward_type == "blackrobot":
            # make robot black
            achieved_goal[ag_mask] = np.zeros(3)
            pixel_costs = achieved_goal.astype(np.float) - goal.astype(np.float)
        d = np.linalg.norm(pixel_costs)
        return -d

    def compute_reward(self, achieved_goal, goal, info):
        if self._pixels_ob:
            if self.reward_type in [
                "weighted",
                "inpaint",
                "inpaint-blur",
                "blackrobot",
            ]:
                return self.weighted_cost(achieved_goal, goal, info)
            # Compute distance between goal and the achieved goal.
            d = np.linalg.norm(achieved_goal.astype(np.float) - goal.astype(np.float))
            if self.reward_type == "sparse":
                return -(d > self.distance_threshold).astype(np.float32)
            elif self.reward_type == "dense":
                return -d

        return super().compute_reward(achieved_goal, goal, info)

    def get_robot_mask(self):
        # returns a binary mask where robot pixels are True
        seg = self.render(segmentation=True)
        types = seg[:, :, 0]
        ids = seg[:, :, 1]
        geoms = types == const.OBJ_GEOM
        geoms_ids = np.unique(ids[geoms])
        mask_dim = [self._img_dim, self._img_dim]
        if self._multiview:
            viewpoints = len(self._camera_ids)
            mask_dim[0] *= viewpoints
        mask = np.zeros(mask_dim, dtype=np.uint8)
        for i in geoms_ids:
            name = self.sim.model.geom_id2name(i)
            if name is not None and "robot0:" in name:
                mask[ids == i] = np.ones(1, dtype=np.uint8)
        return mask.astype(bool)

    def _get_background_img(self):
        """
        Renders the background scene for the environment for inpainting
        Returns an image (H, W, C)
        """
        init_state = self.get_state()
        # move block to out of scene
        obj_pose = [100, 0, 0, 1, 0, 0, 0]
        self.sim.data.set_joint_qpos("object0:joint", obj_pose)
        reset_mocap_welds(self.sim)
        self.sim.forward()
        # move robot gripper up
        robot_pos = self.sim.data.get_site_xpos("robot0:grip").copy()
        robot_noise = np.array([-1, 0, 0.5])
        gripper_target = robot_pos + robot_noise

        gripper_rotation = np.array([1.0, 0.0, 1.0, 0.0])
        self.sim.data.set_mocap_pos("robot0:mocap", gripper_target)
        self.sim.data.set_mocap_quat("robot0:mocap", gripper_rotation)
        for _ in range(10):
            self.sim.step()
        self.sim.forward()
        img = self.render(mode="rgb_array")
        # reset to previous state
        self.set_state(init_state)
        reset_mocap2body_xpos(self.sim)
        reset_mocap_welds(self.sim)
        return img

    def _env_setup(self, initial_qpos):
        for name, value in initial_qpos.items():
            self.sim.data.set_joint_qpos(name, value)
        reset_mocap_welds(self.sim)
        self.sim.forward()

        # Move end effector into position.
        if self._large_block:
            gripper_target = np.array(
                [-0.52 - 0.15, 0.005, -0.431 + self.gripper_extra_height]
            ) + self.sim.data.get_site_xpos("robot0:grip")
        else:
            gripper_target = np.array(
                [-0.498 - 0.15, 0.005, -0.431 + self.gripper_extra_height]
            ) + self.sim.data.get_site_xpos("robot0:grip")
        gripper_rotation = np.array([1.0, 0.0, 1.0, 0.0])
        self.sim.data.set_mocap_pos("robot0:mocap", gripper_target)
        self.sim.data.set_mocap_quat("robot0:mocap", gripper_rotation)
        for _ in range(10):
            self.sim.step()

        # Extract information for sampling goals.
        self.initial_gripper_xpos = self.sim.data.get_site_xpos("robot0:grip").copy()
        self.initial_object_xpos = self.sim.data.get_site_xpos("object0").copy()
        if self.has_object:
            self.height_offset = self.sim.data.get_site_xpos("object0")[2]

        if (
            self.reward_type in ["inpaint", "inpaint-blur"]
            and self._background_img is None
        ):
            self._background_img = self._get_background_img()

    def _set_action(self, action):
        assert action.shape == (3,)
        action = np.concatenate([action, [0]])
        super()._set_action(action)

    def generate_demo(self, behavior, record, save_goal):
        """
        Behaviors: occlude, occlude_all, push, only robot move to goal
        """
        from src.utils.video_recorder import VideoRecorder
        from collections import defaultdict

        title_dict = {
            "weighted": f"Don't Care a={self._robot_pixel_weight}",
            "dense": "L2",
            "inpaint": "inpaint",
            "inpaint-blur": f"inpaint-blur_sig{self._sigma}",
            "blackrobot": "blackrobot",
        }
        size = "large" if self._large_block else "small"
        vp = "multi" if self._multiview else "single"
        vr = VideoRecorder(
            self, path=f"{size}_{behavior}_{vp}_view.mp4", enabled=record
        )
        self.reset()
        # self.render("human")
        history = defaultdict(list)
        vr.capture_frame()
        if record:
            history["frame"].append(vr.last_frame)
        history["goal"] = self.goal.copy()
        if save_goal:
            imageio.imwrite(
                f"{size}_{title_dict[self.reward_type]}_goal.png", history["goal"]
            )

        def move(
            target,
            history,
            target_type="gripper",
            max_time=100,
            threshold=0.01,
            speed=10,
        ):
            if target_type == "gripper":
                gripper_xpos = self.sim.data.get_site_xpos("robot0:grip").copy()
                d = target - gripper_xpos
            elif target_type == "object":
                object_xpos = self.sim.data.get_site_xpos("object0").copy()
                d = target - object_xpos
            step = 0
            while np.linalg.norm(d) > threshold and step < max_time:
                # add some random noise to ac
                # ac = d + np.random.uniform(-0.03, 0.03, size=3)
                ac = d * speed
                _, _, _, info = self.step(ac)
                # self.render("human")
                for k, v in info.items():
                    history[k].append(v)
                vr.capture_frame()
                if record:
                    history["frame"].append(vr.last_frame)
                if target_type == "gripper":
                    gripper_xpos = self.sim.data.get_site_xpos("robot0:grip").copy()
                    d = target - gripper_xpos
                elif target_type == "object":
                    object_xpos = self.sim.data.get_site_xpos("object0").copy()
                    d = target - object_xpos
                # print(np.linalg.norm(d))
                step += 1

            if np.linalg.norm(d) > threshold:
                print("move failed")
            elif behavior == "push" and target_type == "object":
                goal_dist = np.linalg.norm(object_xpos - self.goal_pose["object"])
                print("goal object dist after push", goal_dist)

        def occlude():
            # move gripper above cube
            gripper_xpos = self.sim.data.get_site_xpos("robot0:grip")
            gripper_target = gripper_xpos + [0, 0, 0.048]
            move(gripper_target, history)
            # move gripper to occlude the cube
            gripper_xpos = self.sim.data.get_site_xpos("robot0:grip")
            gripper_target = gripper_xpos + [0.15, 0, 0]
            move(gripper_target, history)
            # move gripper downwards
            gripper_xpos = self.sim.data.get_site_xpos("robot0:grip")
            gripper_target = gripper_xpos + [0, 0, -0.061]
            move(gripper_target, history)
            vr.close()

        def occlude_all():
            # move gripper above cube
            gripper_xpos = self.sim.data.get_site_xpos("robot0:grip")
            gripper_target = gripper_xpos + [0, 0, 0.05]
            move(gripper_target, history)
            # move gripper to occlude the cube
            gripper_xpos = self.sim.data.get_site_xpos("robot0:grip")
            gripper_target = gripper_xpos + [0.25, 0, 0]
            move(gripper_target, history, speed=10, threshold=0.025)
            # move gripper downwards
            gripper_xpos = self.sim.data.get_site_xpos("robot0:grip")
            gripper_target = gripper_xpos + [0, 0, -0.061]
            move(gripper_target, history, threshold=0.02)
            vr.close()

        def push():
            # move gripper to center of cube
            gripper_xpos = self.sim.data.get_site_xpos("robot0:grip").copy()
            block_xpos = self.sim.data.get_site_xpos("object0").copy()
            gripper_target = gripper_xpos
            gripper_target[0] = block_xpos[0]
            move(gripper_target, history)
            # push the block
            obj_target = self.goal_pose["object"]
            if self._large_block:
                move(
                    obj_target, history, target_type="object", speed=20, threshold=0.015
                )
            else:
                move(
                    obj_target, history, target_type="object", speed=10, threshold=0.003
                )
            vr.close()

        def only_robot():
            # move gripper above cube
            gripper_xpos = self.sim.data.get_site_xpos("robot0:grip")
            gripper_target = gripper_xpos + [0, 0, 0.07]
            move(gripper_target, history)
            # move gripper to target robot pos
            gripper_xpos = self.sim.data.get_site_xpos("robot0:grip")
            gripper_target = self.goal_pose["gripper"]
            move(gripper_target, history, speed=10, threshold=0.025)
            vr.close()

        def rollout(history, path):
            frames = history["frame"]
            rewards = history["reward"]
            fig = plt.figure()
            rewards = -1 * np.array([0] + rewards)
            cols = len(frames)
            for n, (image, reward) in enumerate(zip(frames, rewards)):
                a = fig.add_subplot(2, cols, n + 1)
                imagegoal = np.concatenate([image, history["goal"]], axis=1)
                a.imshow(imagegoal)
                a.set_aspect("equal")
                # round reward to 2 decimals
                rew = f"{reward:0.2f}" if n > 0 else "Cost:"
                a.set_title(rew, fontsize=50)
                a.set_xticklabels([])
                a.set_xticks([])
                a.set_yticklabels([])
                a.set_yticks([])
                a.set_xlabel(f"step {n}", fontsize=40)
                # add goal img under every one
                # b = fig.add_subplot(2, cols, n + len(frames) + 1)
                # b.imshow(history["goal"])
                # b.set_aspect("equal")
                # obj =  f"{objd:0.3f}" if n > 0 else "Object Dist:"
                # b.set_title(obj, fontsize=50)
                # b.set_xticklabels([])
                # b.set_xticks([])
                # b.set_yticklabels([])
                # b.set_yticks([])
                # b.set_xlabel(f"goal", fontsize=40)

            fig.set_figheight(10)
            fig.set_figwidth(100)

            title = f"{title_dict[self.reward_type]} with {behavior} behavior"
            fig.suptitle(title, fontsize=50, fontweight="bold")
            fig.savefig(path)
            fig.clf()
            plt.close("all")

        if behavior == "occlude":
            occlude()
        elif behavior == "push":
            push()
        elif behavior == "occlude_all":
            occlude_all()
        elif behavior == "only_robot":
            only_robot()
        # rollout(history, f"{title_dict[self.reward_type]}_{behavior}.png")
        return history


def plot_behaviors_per_cost():
    """ Plots a cost function's performance over behaviors"""
    config, _ = argparser()
    # visualize the initialization
    cost_funcs = ["dontcare", "l2", "inpaint", "blackrobot", "alpha"]
    # cost_funcs = ["inpaint-blur"]
    normalize = False
    data = {}
    viewpoints = ["multiview"]
    behaviors = ["only_robot", "push", "occlude", "occlude_all"]
    # behaviors = ["push"]
    save_goal = False  # save a goal for each cost
    for behavior in behaviors:
        # only record once for each behavior
        record = False
        cost_traj = {}
        for cost in cost_funcs:
            cfg = deepcopy(config)
            if cost == "dontcare":
                cfg.reward_type = "weighted"
                cfg.robot_pixel_weight = 0
            elif cost == "l2":
                cfg.reward_type = "dense"
            elif cost == "inpaint":
                cfg.reward_type = "inpaint"
            elif cost == "inpaint-blur":
                cfg.reward_type = "inpaint-blur"
            elif cost == "blackrobot":
                cfg.reward_type = "blackrobot"
                cfg.robot_pixel_weight = 0
            elif cost == "alpha":
                # same as weighted but with alpha = 0.1
                cfg.reward_type = "weighted"
                cfg.robot_pixel_weight = 0.1

            cost_traj[cost] = {}
            for vp in viewpoints:
                cfg.multiview = vp == "multiview"
                env = FetchPushEnv(cfg)
                history = env.generate_demo(
                    behavior, record=record, save_goal=save_goal
                )
                cost_traj[cost][vp] = history
                env.close()
            record = False

        save_goal = False  # dont' need it for other behaviors
        data[behavior] = cost_traj

    if normalize:
        # get the min, max of costs across behaviors
        cost_min_dict = {vp: defaultdict(list) for vp in viewpoints}
        cost_max_dict = {vp: defaultdict(list) for vp in viewpoints}
        for behavior, cost_traj in data.items():
            for cost_fn, traj in cost_traj.items():
                for vp in viewpoints:
                    costs = -1 * np.array(traj[vp]["reward"])
                    cost_min_dict[vp][cost_fn].extend(costs)
                    cost_max_dict[vp][cost_fn].extend(costs)

    cmap = plt.get_cmap("Set1")
    for cost_fn in cost_funcs:
        for i, behavior in enumerate(behaviors):
            cost_traj = data[behavior][cost_fn]
            for vp in viewpoints:
                # graph the data
                size = "large" if cfg.large_block else "small"
                title = cost_fn
                if cost_fn == "inpaint-blur":
                    title = f"{cost_fn}-{env._sigma}"
                plt.title(f"{title} with {size} block")
                print(f"plotting {cost_fn} cost")
                costs = -1 * np.array(cost_traj[vp]["reward"])
                if normalize:
                    min = np.min(cost_min_dict[vp][cost_fn])
                    max = np.max(cost_max_dict[vp][cost_fn])
                    costs = (costs - min) / (max - min)

                timesteps = np.arange(len(costs)) + 1
                costs = np.array(costs)
                color = cmap(i)
                linestyle = "-" if vp == "multiview" else "--"
                plt.plot(
                    timesteps,
                    costs,
                    label=f"{behavior}_{vp[0]}",
                    linestyle=linestyle,
                    color=color,
                )

        plt.legend(loc="lower left", fontsize=9)
        plt.savefig(f"{size}_{cost_fn}_behaviors.png")
        plt.close("all")


def plot_costs_per_behavior():
    """ Plots the cost function for different behaviors"""
    config, _ = argparser()
    # visualize the initialization
    rewards = ["dontcare", "l2", "inpaint", "blackrobot", "alpha"]
    normalize = True
    data = {}
    behaviors = ["push", "occlude", "occlude_all", "only_robot"]
    save_goal = False  # save a goal for each cost
    for behavior in behaviors:
        # only record once for each behavior
        record = False
        cost_traj = {}
        for r in rewards:
            cfg = deepcopy(config)
            if r == "dontcare":
                cfg.reward_type = "weighted"
                cfg.robot_pixel_weight = 0
            elif r == "l2":
                cfg.reward_type = "dense"
            elif r == "inpaint":
                cfg.reward_type = "inpaint"
            elif r == "blackrobot":
                cfg.reward_type = "blackrobot"
                cfg.robot_pixel_weight = 0
            elif r == "alpha":
                # same as weighted but with alpha = 0.1
                cfg.reward_type = "weighted"
                cfg.robot_pixel_weight = 0.1

            env = FetchPushEnv(cfg)
            history = env.generate_demo(behavior, record=record, save_goal=save_goal)
            record = False
            cost_traj[r] = history
            env.close()

        save_goal = False  # dont' need it for other behaviors
        data[behavior] = cost_traj

    if normalize:
        # get the min, max of costs across behaviors
        cost_min_dict = defaultdict(list)
        cost_max_dict = defaultdict(list)
        for behavior, cost_traj in data.items():
            for cost_fn, traj in cost_traj.items():
                costs = -1 * np.array(traj["reward"])
                cost_min_dict[cost_fn].extend(costs)
                cost_max_dict[cost_fn].extend(costs)

    for behavior in behaviors:
        cost_traj = data[behavior]
        # graph the data
        size = "large" if cfg.large_block else "small"
        viewpoint = "multi" if cfg.multiview else "single"
        plt.title(f"Costs with {behavior} & {size} block & {viewpoint}-view")
        for cost_fn, traj in cost_traj.items():
            print(f"plotting {cost_fn} cost")
            costs = -1 * np.array(traj["reward"])
            if normalize:
                min = np.min(cost_min_dict[cost_fn])
                max = np.max(cost_max_dict[cost_fn])
                costs = (costs - min) / (max - min)
            timesteps = np.arange(len(costs)) + 1
            costs = np.array(costs)
            plt.plot(timesteps, costs, label=cost_fn)
        plt.legend(loc="upper right")
        plt.savefig(f"{size}_{behavior}_costs.png")
        plt.close("all")


if __name__ == "__main__":
    from src.config import argparser
    import imageio
    from time import time
    from copy import deepcopy
    from collections import defaultdict

    plot_behaviors_per_cost()
    # plot_costs_per_behavior()
    # config, _ = argparser()
    # env = FetchPushEnv(config)
    # env.reset()
    # env.render("human")
    # while True:
    #     # env.step(env.action_space.sample())
    #     env.render("human")
