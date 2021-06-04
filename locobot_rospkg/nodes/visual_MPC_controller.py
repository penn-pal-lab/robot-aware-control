#! /usr/bin/env python

from __future__ import print_function

import sys
import time
from time import gmtime, strftime
import os
import pickle

from typing import Tuple

import actionlib
import cv2
import imageio
import ipdb
import numpy as np
import rospy
import torch
import torchvision.transforms as tf
from cv_bridge import CvBridge

from eef_control.msg import *
from locobot_rospkg.nodes.data_collection_client import (
    DEFAULT_PITCH,
    DEFAULT_ROLL,
    PUSH_HEIGHT,
    eef_control_client,
)
from pupil_apriltags import Detector
from scipy.spatial.transform.rotation import Rotation
from sensor_msgs.msg import Image
from src.cem.cem import CEMPolicy
from src.config import create_parser, str2bool
from src.env.robotics.masks.locobot_analytical_ik import (
    AnalyticInverseKinematics as AIK,
)
from src.env.robotics.masks.locobot_mask_env import LocobotMaskEnv
from src.prediction.models.dynamics import SVGConvModel
from src.utils.camera_calibration import camera_to_world_dict
from src.utils.state import DemoGoalState, State

start_offset = 0.15
START_POS = {
    "left": [0.29, -0.14],
    "right": [0.26, 0.13],
    "forward": [0.2 + 0.02, 0],
}

CAMERA_CALIB = np.array(
    [
        [0.008716, 0.75080825, -0.66046272, 0.77440888],
        [0.99985879, 0.00294645, 0.01654445, 0.02565873],
        [0.01436773, -0.66051366, -0.75067655, 0.64211797],
        [0.0, 0.0, 0.0, 1.0],
    ]
)


class Visual_MPC(object):
    def __init__(self, config, device="cuda"):
        # Creates the SimpleActionClient, passing the type of the action
        self.control_client = actionlib.SimpleActionClient(
            "eef_control", eef_control.msg.PoseControlAction
        )

        self.img_sub = rospy.Subscriber(
            "/camera/color/image_raw", Image, self.img_callback
        )
        self.depth_sub = rospy.Subscriber(
            "/camera/depth/image_rect_raw", Image, self.depth_callback
        )
        self.cv_bridge = CvBridge()
        self.img = np.zeros((480, 640, 3), dtype=np.uint8)
        self.depth = np.zeros((480, 640), dtype=np.uint16)

        self.device = device
        self.model = None
        self.config = config

        w, h = config.image_width, config.image_height
        self._img_transform = tf.Compose([tf.ToTensor(), tf.Resize((h, w))])
        self.t = 1
        self.target_img = None

        model = SVGConvModel(config)
        ckpt = torch.load(config.dynamics_model_ckpt, map_location=config.device)
        model.load_state_dict(ckpt["model"])
        model.eval()

        self.ik_solver = AIK()
        # self.env = LocobotMaskEnv(thick=False)
        self.env_thick = LocobotMaskEnv(thick=True)

        camTbase = CAMERA_CALIB
        if config.new_camera_calibration:
            camTbase = self.get_cam_calibration()
        self.set_camera_calibration(camTbase)

        self.policy = CEMPolicy(
            config,
            model,
            init_std=config.cem_init_std,
            action_candidates=config.action_candidates,
            horizon=config.horizon,
            cam_ext=camTbase,
        )

    def img_callback(self, data):
        self.img = self.cv_bridge.imgmsg_to_cv2(data)

    def depth_callback(self, data):
        self.depth = self.cv_bridge.imgmsg_to_cv2(data)

    def get_camera_pose_from_apriltag(self, detector=None):
        print("[INFO] detecting AprilTags...")
        if detector is None:
            detector = Detector(
                families="tag36h11",
                nthreads=1,
                quad_decimate=1.0,
                quad_sigma=0.0,
                refine_edges=1,
                decode_sharpening=0.25,
                debug=0,
            )

        gray = cv2.cvtColor(self.img, cv2.COLOR_BGR2GRAY)

        results = []
        results = detector.detect(
            gray,
            estimate_tag_pose=True,
            camera_params=[612.45, 612.45, 330.55, 248.61],
            tag_size=0.0353,
        )
        print("[INFO] {} total AprilTags detected".format(len(results)))

        if len(results) == 0:
            return None, None
        elif len(results) > 1:
            print("[Error] More than 1 AprilTag detected!")

        # loop over the AprilTag detection results
        for r in results:
            pose_t = r.pose_t
            pose_R = r.pose_R
        # Tag pose w.r.t. camera
        return pose_t, pose_R

    def get_cam_calibration(self):
        control_result = eef_control_client(
            self.control_client,
            target_pose=[0.35, 0, PUSH_HEIGHT, DEFAULT_PITCH, DEFAULT_ROLL],
        )
        time.sleep(5)
        control_result = eef_control_client(
            self.control_client,
            target_pose=[],
        )
        print(control_result.end_pose)
        # tag to camera transformation
        pose_t, pose_R = self.get_camera_pose_from_apriltag()
        if pose_t is None or pose_R is None:
            return None
        # TODO: figure out qpos / end effector accuracy
        # currently qpos is better than using end effector
        target_qpos = control_result.joint_angles
        # self.env.sim.data.qpos[self.env._joint_references] = target_qpos
        # self.env.sim.forward()
        self.env_thick.sim.data.qpos[self.env_thick._joint_references] = target_qpos
        self.env_thick.sim.forward()

        # tag to base transformation
        tagTbase = np.column_stack(
            (
                self.env_thick.sim.data.get_geom_xmat("ar_tag_geom"),
                self.env_thick.sim.data.get_geom_xpos("ar_tag_geom"),
            )
        )
        tagTbase = np.row_stack((tagTbase, [0, 0, 0, 1]))

        tagTcam = np.column_stack((pose_R, pose_t))
        tagTcam = np.row_stack((tagTcam, [0, 0, 0, 1]))

        # tag in camera to tag in robot transformation
        # For explanation, refer to anonymous's hand drawing
        tagcTtagw = np.array(
            [[0, 0, -1, 0], [0, -1, 0, 0], [-1, 0, 0, 0], [0, 0, 0, 1]]
        )

        camTbase = tagTbase @ tagcTtagw @ np.linalg.inv(tagTcam)
        print("camera2world:")
        print(camTbase)
        return camTbase

    def set_camera_calibration(self, camTbase):
        rot_matrix = camTbase[:3, :3]
        cam_pos = camTbase[:3, 3]
        rel_rot = Rotation.from_quat([0, 1, 0, 0])  # calculated
        cam_rot = Rotation.from_matrix(rot_matrix) * rel_rot

        cam_id = 0
        offset = [0, -0.015, 0.0125]
        # offset = [0, -0.007, 0.02]
        print("applying offset", offset)
        self.env_thick.sim.model.cam_pos[cam_id] = cam_pos + offset
        cam_quat = cam_rot.as_quat()
        self.env_thick.sim.model.cam_quat[cam_id] = [
            cam_quat[3],
            cam_quat[0],
            cam_quat[1],
            cam_quat[2],
        ]
        print("camera pose:")
        print(self.env_thick.sim.model.cam_pos[cam_id])
        print(self.env_thick.sim.model.cam_quat[cam_id])
        return camTbase

    def read_target_image(self):
        if self.target_img is None:
            print("Collect target image before MPC first!")
            return
        self.target_img = self._img_transform(self.target_img).to(self.device)

    def collect_target_img(self, eef_target):
        """ set up the scene and collect goal image """
        if len(eef_target) == 2:
            eef_target = [*eef_target, PUSH_HEIGHT, DEFAULT_PITCH, DEFAULT_ROLL]
        else:
            assert len(eef_target) == 5

        control_result = eef_control_client(
            self.control_client,
            target_pose=eef_target,
        )
        input("Move the object to the GOAL position. Press Enter to continue...")
        self.target_img = np.copy(self.img)
        self.target_eef = np.array(control_result.end_pose)

        qpos_from_eef = np.zeros(5)
        qpos_from_eef[0:4] = self.ik_solver.ik(
            self.target_eef,
            alpha=-DEFAULT_PITCH,
            cur_arm_config=np.array(control_result.joint_angles),
        )
        qpos_from_eef[4] = DEFAULT_ROLL

        self.target_qpos = qpos_from_eef

    def go_to_start_pose(self, eef_start):
        """ set up the starting scene """
        _ = eef_control_client(
            self.control_client,
            target_pose=[*eef_start, PUSH_HEIGHT, DEFAULT_PITCH, DEFAULT_ROLL],
        )
        input("Move the object close to the EEF. Press Enter to continue...")
        self.start_img = np.copy(self.img)
        self.start_img = self._img_transform(self.start_img).to(self.device)
        self.start_img = self.start_img.cpu().clamp_(0, 1).numpy()
        self.start_img = np.transpose(self.start_img, axes=(1, 2, 0))
        self.start_img = np.uint8(self.start_img * 255)

    def get_state(self) -> State:
        """Get the current State (eef, qpos, img) of the robot
        Returns:
            State: A namedtuple of current img, eef, qpos
        """
        img = np.copy(self.img)
        img = self._img_transform(img).to(self.device)
        img = img.cpu().clamp_(0, 1).numpy()
        img = np.transpose(img, axes=(1, 2, 0))
        img = np.uint8(img * 255)

        control_result = eef_control_client(self.control_client, target_pose=[])
        state = State(
            img=img,
            state=[*control_result.end_pose, DEFAULT_PITCH, DEFAULT_ROLL],
            qpos=control_result.joint_angles,
        )
        return state

    def create_start_goal(self) -> Tuple[State, DemoGoalState]:
        self.read_target_image()
        goal_visual = self.target_img.cpu().clamp_(0, 1).numpy()
        goal_visual = np.transpose(goal_visual, axes=(1, 2, 0))
        self.goal_visual = goal_visual = np.uint8(goal_visual * 255)

        start_visual = self.start_img
        imageio.imwrite(
            os.path.join(self.config.log_dir, "start_goal.png"),
            np.concatenate([start_visual, goal_visual], 1),
        )
        control_result = eef_control_client(self.control_client, target_pose=[])
        start = State(
            img=start_visual,
            state=[*control_result.end_pose, DEFAULT_PITCH, DEFAULT_ROLL],
            qpos=control_result.joint_angles,
        )

        mask = self.env_thick.generate_masks([self.target_qpos])[0]

        imageio.imwrite(
            os.path.join(self.config.log_dir, "goal_mask.png"), np.uint8(mask) * 255
        )

        mask = (self._img_transform(mask).type(torch.bool).type(torch.float32)).to(
            self.device
        )

        goal = DemoGoalState(imgs=[goal_visual], masks=[mask])

        return start, goal

    def cem(self, start: State, goal: DemoGoalState, step=0, opt_traj=None):
        actions = self.policy.get_action(start, goal, 0, step, opt_traj)
        return actions

    def execute_action(self, action):
        control_result = eef_control_client(self.control_client, target_pose=[])
        end_xy = [
            control_result.end_pose[0] + action[0],
            control_result.end_pose[1] + action[1],
        ]
        control_result = eef_control_client(
            self.control_client,
            target_pose=[*end_xy, PUSH_HEIGHT, DEFAULT_PITCH, DEFAULT_ROLL],
        )

    def execute_open_loop(self, actions):
        img = np.copy(self.img)
        img = self._img_transform(img)
        img = img.clamp_(0, 1).numpy()
        img = np.transpose(img, axes=(1, 2, 0))
        img = np.uint8(img * 255)

        img_goal = np.concatenate([img, self.goal_visual], 1)
        gif = [img_goal]
        for ac in actions:  # execute open loop actions for now
            vmpc.execute_action(ac)
            img = np.copy(self.img)
            img = self._img_transform(img)
            img = img.clamp_(0, 1).numpy()
            img = np.transpose(img, axes=(1, 2, 0))
            img = np.uint8(img * 255)
            img_goal = np.concatenate([img, self.goal_visual], 1)
            gif.append(img_goal)
        imageio.mimwrite("figures/open_loop.gif", gif, fps=2)


if __name__ == "__main__":
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    parser = create_parser()
    parser.add_argument("--execute_optimal_traj", type=str2bool, default=False)
    parser.add_argument("--new_camera_calibration", type=str2bool, default=False)
    parser.add_argument("--save_start_goal", type=str2bool, default=False)
    parser.add_argument("--load_start_goal", type=str, default=None)
    parser.add_argument("--push_type", type=str, default="forward")
    parser.add_argument("--object", type=str, default=" ")

    cf, unparsed = parser.parse_known_args()
    assert len(unparsed) == 0, unparsed
    cf.device = device
    push_type = cf.push_type
    dynamics_model = "vanilla"
    if "roboaware" in cf.dynamics_model_ckpt:
        dynamics_model = "roboaware"
    curr_time = strftime("%Y-%m-%d_%H:%M:%S", gmtime())
    cf.log_dir = os.path.join(
        cf.log_dir,
        push_type + "_" + cf.object + "_" + dynamics_model + "_" + cf.reward_type,
        "debug_cem_" + curr_time,
    )

    cf.debug_cem = True
    # cf.cem_init_std = 0.015
    # cf.action_candidates = 300
    cf.goal_img_with_wrong_robot = True  # makes the robot out of img by pointing up
    cf.cem_open_loop = False
    cf.max_episode_length = 4  # ep length of closed loop execution

    # Initializes a rospy node so that the SimpleActionClient can
    # publish and subscribe over ROS.
    rospy.init_node("visual_mpc_client")

    vmpc = Visual_MPC(config=cf)

    if cf.goal_img_with_wrong_robot:
        eef_start_pos = START_POS[push_type]
        eef_target_pos = [0.15, 0.0, 0.55, 0, DEFAULT_ROLL]
    else:
        eef_target_pos = [0.33, 0]
        eef_start_pos = [eef_target_pos[0], eef_target_pos[1] - start_offset]

    if cf.load_start_goal is not None:
        vmpc.go_to_start_pose(eef_start=eef_start_pos)
        with open(cf.load_start_goal, "rb") as f:
            start, goal = pickle.load(f)
        input("is the start scene ready?")
        vmpc.goal_visual = goal.imgs[0]
    else:
        vmpc.collect_target_img(eef_target_pos)
        vmpc.go_to_start_pose(eef_start=eef_start_pos)
        start, goal = vmpc.create_start_goal()
        if cf.save_start_goal:
            start_goal_file = input("name of start goal pkl file:")
            with open(start_goal_file, "wb") as f:
                pickle.dump([start, goal], f)

    if cf.execute_optimal_traj:
        # push towards camera
        print("executing optimal trajectory")
        actions = [[0.05, 0], [0.05, 0], [0.05, 0], [0.05, 0]]
        vmpc.execute_open_loop(actions)
        sys.exit()
    if cf.cem_open_loop:
        actions = vmpc.cem(start, goal)
        print(actions)
        input("execute actions?")
        vmpc.execute_open_loop(actions)
    else:
        dist = 0.03
        opt_traj = torch.tensor([[dist, 0]] * (cf.horizon - 1))
        if push_type == "left":
            opt_traj = torch.tensor([[0, dist]] * (cf.horizon - 1))
        elif push_type == "right":
            opt_traj = torch.tensor([[0, -dist]] * (cf.horizon - 1))
        img_goal = np.concatenate([start.img, vmpc.goal_visual], 1)
        gif = [img_goal]
        for t in range(cf.max_episode_length):
            act = vmpc.cem(start, goal, t, opt_traj)[0]  # get first action
            print(f"t={t}, executing {act}")
            vmpc.execute_action(act)
            start = vmpc.get_state()
            img_goal = np.concatenate([start.img, vmpc.goal_visual], 1)
            gif.append(img_goal)
        imageio.mimwrite(cf.log_dir + "/closed_loop.gif", gif, fps=2)
