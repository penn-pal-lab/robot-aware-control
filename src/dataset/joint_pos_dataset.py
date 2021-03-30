import os

from torch.utils.data.dataloader import DataLoader
from typing import Any
import cv2
import h5py
import imageio
import ipdb
import numpy as np
from ipdb import set_trace as st
import torch
from torchvision.datasets.folder import has_file_allowed_extension
import torchvision.transforms as tf
import torchvision.transforms.functional as F
import torch.utils.data as data
from tqdm import trange
from time import time


class JointPosDataset(data.Dataset):
    def __init__(
        self, hdf5_list, robot_list, config, augment_img=False, load_snippet=False
    ):
        """
        hdf5_list: list of hdf5 files to load
        robot_list: list of robot type for each hdf5 file
        split: load a random snippet, or the entire video depending on split.
        """
        self._traj_names = hdf5_list
        self._traj_robots = robot_list
        self._config = config
        self._data_root = config.data_root

        self._video_length = config.video_length
        if load_snippet:
            self._video_length = config.n_past + config.n_future
        self._action_dim = config.action_dim
        self._impute_autograsp_action = config.impute_autograsp_action
        self._augment_img = augment_img
        self._img_transform = tf.Compose(
            [tf.ToTensor(), tf.CenterCrop(config.image_width)]
        )
        # if self._augment_img:
        #     r = config.color_jitter_range
        #     self._crop_resize = tf.Compose(
        #         [
        #             tf.RandomCrop(config.random_crop_size),
        #             tf.Resize(config.image_width),
        #         ]
        #     )
        #     self._jitter_color = tf.ColorJitter(r,r,r,r)

        self._rng = np.random.RandomState(config.seed)
        self._memory = {}

    def __getitem__(self, idx):
        """
        Opens the hdf5 file and extracts the videos, actions, states, and masks
        """
        if idx in self._memory:
            return self._memory[idx]

        robonet_root = self._data_root
        name = self._traj_names[idx]
        hdf5_path = os.path.join(robonet_root, name)
        with h5py.File(hdf5_path, "r") as hf:
            ep_len = hf["frames"].shape[0]
            assert ep_len >= self._video_length, f"{ep_len}, {hdf5_path}"
            start = 0
            end = ep_len
            if ep_len > self._video_length:
                offset = ep_len - self._video_length
                start = self._rng.randint(0, offset + 1)
                end = start + self._video_length

            states = hf["states"][start:end].astype(np.float32)
            # actions = hf["actions"][start:end - 1].astype(np.float32)
            low = hf["low_bound"][:]
            high = hf["high_bound"][:]
            actions = self._load_actions(hf, low, high, start, end - 1)
            masks = hf["mask"][start:end].astype(np.float32)
            qpos = hf["qpos"][start:end].astype(np.float32)

            assert (
                len(states) == len(actions) + 1 == len(masks) == len(qpos)
            ), f"{hdf5_path}, {states.shape}, {actions.shape}, {masks.shape}"

            # preprocessing
            masks = torch.stack([self._img_transform(i) for i in masks])
            actions = self._preprocess_actions(states, actions, low, high, idx)
            states = self._preprocess_states(states, low, high)

            robot = hf.attrs["robot"]
        out = {
            "states": states,
            "actions": actions,
            "masks": masks,
            "robot": robot,
            "folder": os.path.basename(os.path.dirname(name)),
            "file_path": hdf5_path,
            "idx": idx,
            "qpos": qpos
        }

        return out

    def _load_actions(self, file_pointer, low, high, start, end):
        actions = file_pointer["actions"][:].astype(np.float32)
        a_T, adim = actions.shape[0], actions.shape[1]
        if self._action_dim == adim:
            return actions[start:end]
        elif self._impute_autograsp_action and adim + 1 == self._action_dim:
            action_append, old_actions = (
                np.zeros((a_T, 1)),
                actions,
            )
            next_state = file_pointer["states"][:][1:, -1]
            high_val, low_val = high[-1], low[-1]
            midpoint = (high_val + low_val) / 2.0

            for t, s in enumerate(next_state):
                if s > midpoint:
                    action_append[t, 0] = high_val
                else:
                    action_append[t, 0] = low_val
            new_actions = np.concatenate((old_actions, action_append), axis=-1)
            return new_actions[start:end].astype(np.float32)
        else:
            raise ValueError(f"file adim {adim}, target adim {self._action_dim}")

    def _preprocess_states(self, states, low, high):
        """
        states: T x 5 array of [x,y,z, r, f], r is rotation in radians, f is gripper force
        We only need to normalize the gripper force.
        XYZ is already normalized in the hdf5 file.
        yaw is in radians across all robots, so it is consistent.
        """
        states = torch.from_numpy(states)
        force_min, force_max = low[4], high[4]
        states[:, 4] = (states[:, 4] - force_min) / (force_max - force_min)
        return states

    def _convert_world_to_camera_pos(self, state, w_to_c):
        e_to_w = np.eye(4)
        e_to_w[:3, 3] = state[:3]
        e_to_c = w_to_c @ e_to_w
        pos_c = e_to_c[:3, 3]
        return pos_c

    def _denormalize(self, states, low, high):
        states = states * (high - low)
        states = states + low
        return states

    def _convert_actions(self, states, actions, w_to_c, low, high):
        """
        Concert raw actions to camera frame displacements
        """
        states[:, :3] = self._denormalize(states[:, :3], low[:3], high[:3])
        old_actions = actions.copy()
        for t in range(len(actions)):
            state = states[t]
            pos_c = self._convert_world_to_camera_pos(state, w_to_c)
            next_state = states[t].copy()
            next_state[:4] += old_actions[t][:4]
            next_pos_c = self._convert_world_to_camera_pos(next_state, w_to_c)
            true_offset_c = next_pos_c - pos_c
            actions[t][:3] = true_offset_c

    def _impute_camera_actions(self, states, actions, w_to_c, low, high):
        """
        Just calculate the true offset between states instead of using  the recorded actions.
        """
        states[:, :3] = self._denormalize(states[:, :3], low[:3], high[:3])
        for t in range(len(actions)):
            state = states[t]
            pos_c = self._convert_world_to_camera_pos(state, w_to_c)
            next_state = states[t + 1]
            next_pos_c = self._convert_world_to_camera_pos(next_state, w_to_c)
            true_offset_c = next_pos_c - pos_c
            actions[t][:3] = true_offset_c

    def _impute_true_actions(self, states, actions, low, high):
        """
        Set the action to what happened between states, not recorded actions.
        """
        states[:, :3] = self._denormalize(states[:, :3], low[:3], high[:3])
        for t in range(len(actions)):
            state = states[t][:3]
            next_state = states[t + 1][:3]
            true_offset_c = next_state - state
            actions[t][:3] = true_offset_c

    def _preprocess_actions(self, states, actions, low, high, idx):
        strategy = self._config.preprocess_action
        if strategy == "raw":
            return torch.from_numpy(actions)
        elif strategy == "state_infer":
            states = states.copy()
            self._impute_true_actions(states, actions, low, high)
            return torch.from_numpy(actions)
        else:
            raise ValueError

    def __len__(self):
        return len(self._traj_names)

def process_batch(data, device):
    data_keys = ["qpos", "states", "actions", "masks"]
    meta_keys = ["robot", "folder", "file_path", "idx"]
    # transpose from (B, L, C, W, H) to (L, B, C, W, H)
    for k in data_keys:
        data[k] = data[k].transpose_(1, 0).to(device, non_blocking=True)
    return data


def get_batch(loader, device):
    """Infinite batch generator for dataloader

    Args:
        loader (Dataloader): dataloader to get batches from
        device (torch.Device): GPU to put tensors on

    Yields:
        [type]: [description]
    """

    while True:
        for data in loader:
            yield process_batch(data, device)


if __name__ == "__main__":
    from src.config import argparser

    config, _ = argparser()
    config.data_root = "/home/ed/new_hdf5"
    config.batch_size = 64  # needs to be multiple of the # of robots
    config.video_length = 31
    config.image_width = 64
    # config.impute_autograsp_action = True
    config.num_workers = 2
    config.action_dim = 5

    hdf5_list = [
        d.path
        for d in os.scandir(config.data_root)
        if d.is_file() and has_file_allowed_extension(d.path, "hdf5")
    ]
    dataset = JointPosDataset(hdf5_list, config)
    test_loader = DataLoader(
        dataset,
        num_workers=config.data_threads,
        batch_size=config.batch_size,
        shuffle=True,
        drop_last=True,
        pin_memory=True,
    )
    test_it = iter(test_loader)

    for i in range(10):
        start = time()
        x, y = next(test_it)
        end = time()
        # print(y)
        print("data loading", end - start)
