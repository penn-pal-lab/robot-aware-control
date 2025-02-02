import os
import random
import ipdb

import torch
from torch.utils.data import DataLoader
from torchvision.datasets.folder import has_file_allowed_extension

from src.dataset.locobot.sim_pick_dataset import SimPickDataset

def create_loaders(config):
    file_type = "hdf5"
    files = []
    file_labels = []

    data_path = os.path.join(config.data_root, "locobot_pick_views", "c0")
    for d in os.scandir(data_path):
        if d.is_file() and has_file_allowed_extension(d.path, file_type):
            files.append(d.path)
            file_labels.append("locobot_c0")

    files = sorted(files)
    random.seed(config.seed)
    random.shuffle(files)

    # TODO: change dataset splitting
    n_test = 500
    n_train = 100000

    X_test = files[:n_test]
    y_test = file_labels[:n_test]

    X_train = files[n_test: n_test + n_train]
    y_train = file_labels[n_test: n_test + n_train]
    print("loaded locobot data", len(X_train) + len(X_test))

    augment_img = config.img_augmentation
    train_data = SimPickDataset(X_train, y_train, config, augment_img=augment_img)
    test_data = SimPickDataset(X_test, y_test, config)

    train_loader = DataLoader(
        train_data,
        num_workers=config.data_threads,
        batch_size=config.batch_size,
        shuffle=True,
        drop_last=False,
        pin_memory=True,
        generator=torch.Generator().manual_seed(config.seed),
    )
    test_loader = DataLoader(
        test_data,
        num_workers=config.data_threads,
        batch_size=config.test_batch_size,
        shuffle=False,
        drop_last=False,
        pin_memory=True,
    )
    return train_loader, test_loader

if __name__ == "__main__":
    from src.config import argparser

    config, _ = argparser()
    config.data_root = "/home/pallab/locobot_ws/src/roboaware/demos"
    config.batch_size = 16
    config.video_length = 15
    config.image_width = 64
    config.data_threads = 0
    config.action_dim = 5

    train_loader, test_loader = create_loaders(config)

    for data in train_loader:
        images = data["images"]
        states = data["states"]
