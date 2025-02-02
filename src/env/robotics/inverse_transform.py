import os
import copy
import math
from mujoco_py import (
    utils,
    load_model_from_path,
    MjSim,
    MjViewer,
    MjRenderContextOffscreen,
)
from mujoco_py.modder import CameraModder
import numpy as np
from scipy.spatial.transform import Rotation as R
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D  # noqa: F401 unused import
import imageio
from gym.envs.robotics.rotations import euler2quat, mat2euler, quat2mat, quat2euler, mat2quat


def pixel_coord_np(width=640, height=480):
    """
    Pixel in homogenous coordinate
    Returns:
        Pixel coordinates:       [3, width * height]
    """
    x = np.linspace(0, width - 1, width).astype(np.int)
    y = np.linspace(0, height - 1, height).astype(np.int)
    [x, y] = np.meshgrid(x, y)

    return np.vstack((x.flatten(), y.flatten(), np.ones_like(x.flatten())))


def getHomogenousT(rot_matrix, pos):
    T = np.identity(4)
    T[:3, :3] = rot_matrix
    T[:3, -1] = pos.reshape(-1, 1).reshape(
        3,
    )
    return T

def get_world_to_cam(sim, width, height, camera_name):
    cam_id = sim.model.camera_name2id(camera_name)
    cam_quat = mat2quat(sim.data.cam_xmat[cam_id].reshape(3,3)) # this gets the global quaternion
    r = R.from_quat([cam_quat[1], cam_quat[2], cam_quat[3], cam_quat[0]])
    # position vector from world to camera
    cam_pos = sim.data.cam_xpos[cam_id].copy()
    fovy = sim.model.cam_fovy[cam_id]
    f = 0.5 * height / math.tan(fovy * math.pi / 360)
    # intrinsic matrix
    # fkn mujoco flips the horizontal image so intrinsic matrix needs -f
    K = np.array(((-f, 0, (width-1)/ 2.0), (0, f, (height-1) / 2.0), (0, 0, 1)))
    # extrinsic matrix (camera to world)
    T = getHomogenousT(r.as_matrix(), cam_pos) # 4 x 4
    return K @ np.linalg.inv(T)[:3] # (3 x 4) world to camera matrix

def get_pixel_coord(world_pos, camera_matrix):
    """Returns u,v pixel coordinates"""
    coords = camera_matrix @ world_pos # (3, 4) x (4, N)
    coords[:2] /= coords[2] # normalize to homogenous coordinates
    return int(round(coords[0])), int(round(coords[1]))

def main(width=128, height=128, camera_name="external_camera_0"):
    xml_path = os.path.join("fetch", "clutterpush.xml")
    fullpath = os.path.join(os.path.dirname(__file__), "assets", xml_path)
    model = load_model_from_path(fullpath)

    sim = MjSim(model)

    extent = sim.model.stat.extent
    near_ = sim.model.vis.map.znear * extent
    far_ = sim.model.vis.map.zfar * extent

    cam_id = sim.model.camera_name2id(camera_name)

    # intrinsics
    fovy = sim.model.cam_fovy[cam_id]
    f = 0.5 * height / math.tan(fovy * math.pi / 360)
    # fkn mujoco flips the horizontal image so camera matrix needs -f
    K = np.array(((-f, 0, (width-1)/ 2.0), (0, f, (height-1) / 2.0), (0, 0, 1)))
    K_inv = np.linalg.inv(K)

    # depth in meters
    image, depth = copy.deepcopy(
        sim.render(width=width, height=height, camera_name=camera_name, depth=True)
    )
    image = image[::-1]
    depth = depth[::-1]

    # imageio.imwrite("image.png", image)
    # imageio.imwrite("depth.png", depth)
    depth = -(
        near_ / (1 - depth * (1 - near_ / far_))
    )  # -1 because camera is looking along the -Z axis of its frame

    """
    replace pixel coords with keypoints coordinates in pixel space
    shape = (3,N) where N is no. of keypoints and third row is filled with 1s
    """
    pixel_coords = pixel_coord_np(width=width, height=height)
    cam_coords = K_inv[:3, :3] @ pixel_coords * depth.flatten()

    # camera orientation in world coordinate system
    # cam_quat = cam_modder.get_quat(camera_name) # quaternion is local, WRONG
    cam_quat = mat2quat(sim.data.cam_xmat[cam_id].reshape(3,3)) # this gets the global quaternion
    r = R.from_quat([cam_quat[1], cam_quat[2], cam_quat[3], cam_quat[0]])
    # position vector from world to camera
    cam_pos = sim.data.cam_xpos[cam_id].copy()

    T = getHomogenousT(r.as_matrix(), cam_pos)

    # get world coordinates
    cam_homogenous_coords = np.vstack((cam_coords, np.ones(cam_coords.shape[1])))
    world_coords = T @ cam_homogenous_coords
    world_coords[:3, :] = world_coords[:3, :] / world_coords[-1, :].reshape(1, -1)

    # forward projection start
    world_to_cam = get_world_to_cam(sim, width, height, camera_name)
    for i in range(3):
        world_pos = sim.data.get_site_xpos(f"object{i}")
        world_pos = np.concatenate([world_pos, [1]])
        u, v = get_pixel_coord(world_pos, world_to_cam)
        image[v, u] = (255, 255, 255)

    imageio.imwrite("test.png", image)
    # print(eef_pixels)
    import ipdb; ipdb.set_trace()

    # >>>>>>>>>> Working Forward Projection Code >>>>>>>>
    # eef_world_pos = sim.data.get_site_xpos("object0")
    # cam_ori = mat2euler(sim.data.get_camera_xmat(camera_name))
    # s = 0.5
    # output_size = [height, width] # Output size (Height and width) of the 2D projection label in pixel
    # object0_label = global2label(eef_world_pos, cam_pos, cam_ori, output_size, fov=fovy, s=s)

    # eef_world_pos = sim.data.get_site_xpos("object1")
    # object1_label = global2label(eef_world_pos, cam_pos, cam_ori, output_size, fov=fovy, s=s)

    # eef_world_pos = sim.data.get_site_xpos("object2")
    # object2_label = global2label(eef_world_pos, cam_pos, cam_ori, output_size, fov=fovy, s=s)
    # label = object0_label + object1_label + object2_label
    # label += 0.001
    # label = np.clip(label, 0, 1)
    # plt.imshow(label.reshape(height, width, 1) * image)
    # plt.show()

    # import ipdb; ipdb.set_trace()
    # <<<<<<<<< Working Forward Projection Code <<<<<<<<<

    # print(world_coords[:,((640*240)+320)])
    plt.imshow(image)
    fig = plt.figure()
    ax = fig.add_subplot(111, projection="3d")
    frame_flat = image.reshape((128 * 128, 3)) / 255  # normalize rgb to 0, 1 range
    wc_flat = world_coords[:3, :]
    ax.scatter(wc_flat[0, :], wc_flat[1, :], wc_flat[2, :], marker="o", c=frame_flat)
    plt.show()
    return world_coords

if __name__ == "__main__":
    # visualize depth image
    # visualize scene
    main()