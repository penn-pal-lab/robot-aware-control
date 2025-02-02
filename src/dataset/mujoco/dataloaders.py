import os
import random

from src.dataset.video_dataset import VideoDataset
from torchvision.datasets.folder import has_file_allowed_extension
from torch.utils.data import DataLoader


def create_split(config):
    file_type = "hdf5"
    files = [
        d.path
        for d in os.scandir(config.data_root)
        if d.is_file() and has_file_allowed_extension(d.path, file_type)
    ]
    random.seed(0)
    random.shuffle(files)
    val_start = round(config.train_val_split * len(files))
    train_files, val_files = files[:val_start], files[val_start:]
    Dset = VideoDataset
    train, val = Dset(train_files, config), Dset(val_files, config)
    return train, val


def create_loaders(config):
    train_data, test_data = create_split(config)
    train_loader = DataLoader(
        train_data,
        num_workers=config.data_threads,
        batch_size=config.batch_size,
        shuffle=True,
        drop_last=True,
        pin_memory=True,
    )

    test_loader = DataLoader(
        test_data,
        num_workers=config.data_threads,
        batch_size=config.batch_size,
        shuffle=True,
        drop_last=True,
        pin_memory=True,
    )
    return train_loader, test_loader


def get_batch(loader, device):
    while True:
        for sequence in loader:
            # transpose from (B, L, C, W, H) to (L, B, C, W, H)
            frames, robots, actions, masks = sequence
            frames = frames.transpose_(1, 0).to(device)
            robots = robots.transpose_(1, 0).to(device)
            actions = actions.transpose_(1, 0).to(device)
            masks = masks.transpose_(1, 0).unsqueeze_(2).to(device)
            yield frames, robots, actions, masks


if __name__ == "__main__":
    import numpy as np
    from src.config import argparser
    from torch.utils.data import DataLoader
    import h5py

    dataset_path = "data/mock"
    os.makedirs(dataset_path, exist_ok=True)
    ep_len = 10
    for i in range(20):
        # video frames of length 10
        frames = np.ones((ep_len, 128, 128, 3), dtype=np.uint8) * 255 * i // 20
        robot_state = np.ones((ep_len, 3)) * i
        actions = np.ones((ep_len - 1, 3)) * i
        # create a mock dataset hdf5 file
        filename = os.path.join(dataset_path, f"mock_{i}.hdf5")
        with h5py.File(filename, "w") as hf:
            hf.create_dataset("frames", data=frames, compression="gzip")
            hf.create_dataset("robot", data=robot_state, compression="gzip")
            hf.create_dataset("actions", data=actions, compression="gzip")

    config, _ = argparser()
    config.data_root = dataset_path
    config.robot_dim = 3
    config.device = "cpu"
    train_data, val = create_split(config)
    train_loader = DataLoader(
        train_data,
        num_workers=0,
        batch_size=2,
        shuffle=True,
        drop_last=True,
        pin_memory=True,
    )

    generator = get_batch(train_loader, config.device)
    data = next(generator)
    print(data[0].shape)
