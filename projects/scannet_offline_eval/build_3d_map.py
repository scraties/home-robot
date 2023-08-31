# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
"""
    Placeholder code to create 3D map from a scannet scene
    Currently not functional
"""
import argparse
import dataclasses
import sys
import timeit
from typing import Any, Dict, Tuple

import click
import matplotlib.pyplot as plt
import numpy as np
import torch
from eval.utils import eval_bboxes_and_print
from tqdm import tqdm

from home_robot.core.interfaces import Observations
from home_robot.datasets.scannet import ScanNetDataset
from home_robot.mapping.voxel import SparseVoxelMap
from home_robot.perception.detection.detic.detic_perception import DeticPerception


class ScanNetSparseVoxelMap(object):
    """Simple class to collect RGB, Depth, and Pose information for building 3d spatial-semantic
    maps for the robot. Needs to subscribe to:
    - color images
    - depth images
    - camera info
    - joint states/head camera pose
    - base pose (relative to world frame)

    This is an example collecting the data; not necessarily the way you should do it.
    """

    def __init__(
        self,
        semantic_sensor=None,
        visualize_planner=False,
        voxel_size: float = 0.01,
        background_instance_label=-1,
        device="cpu",
    ):
        self.device = device
        self.semantic_sensor = semantic_sensor
        self.visualize_planner = visualize_planner

        self.voxel_map = SparseVoxelMap(
            background_instance_label=background_instance_label,
            resolution=voxel_size,
            # min_iou=0.25
        )

    def step(self, obs: Observations, visualize_map=False):
        """Step the collector. Get a single observation of the world. Remove bad points, such as
        those from too far or too near the camera."""

        device = self.device

        if self.semantic_sensor is not None:
            # This is slow because it gets passed back + forth to the CPU
            obs = Observations(
                rgb=obs.cpu().numpy(), gps=None, compass=None, depth=None
            )
            res = self.semantic_sensor.predict(obs).task_observations
            instance_image = torch.from_numpy(res["instance_map"]).int().to(device)
            instance_classes = (
                torch.from_numpy(res["instance_classes"]).int().to(device)
            )
            instance_scores = torch.from_numpy(res["instance_scores"]).int().to(device)
            # semantic_frame = torch.from_numpy(res['semantic_frame']
        else:
            instance_image = obs.instance
            instance_classes = obs.task_observations["instance_classes"]
            instance_scores = obs.task_observations["instance_scores"]

        self.voxel_map.add(
            rgb=obs.rgb.float() / 255.0,
            depth=obs.depth.squeeze(-1),
            feats=obs.task_observations["features"],
            camera_K=obs.camera_K,
            camera_pose=obs.camera_pose,  # scene_obs['axis_align_mats'][i] @ scene_obs['poses'][i],
            instance_image=instance_image,
            instance_classes=instance_classes,
            instance_scores=instance_scores,
        )

        if visualize_map:
            # Now draw 2d
            self.voxel_map.get_2d_map(debug=True)

    def get_2d_map(self):
        """Get 2d obstacle map for low level motion planning and frontier-based exploration"""
        return self.voxel_map.get_2d_map()

    def show(self) -> Tuple[np.ndarray, np.ndarray]:
        """Display the aggregated point cloud."""
        return self.voxel_map.show(
            instances=True,
            height=1000,
            boxes_plot_together=False,
            boxes_name_int_to_display_name_dict=dict(
                enumerate(self.metadata.thing_classes)
            ),
            backend="pytorch3d",
        )


# TODO: use this code to create semantic sensor
# def create_semantic_sensor(
#     device_id: int = 0,
#     verbose: bool = True,
#     **kwargs,
# ):
#     """Create segmentation sensor and load config. Returns config from file, as well as a OvmmPerception object that can be used to label scenes."""
#     print("- Loading configuration")
#     config = load_config(visualize=False, **kwargs)

#     print("- Create and load vocabulary and perception model")
#     semantic_sensor = OvmmPerception(config, device_id, verbose, module="detic")
#     obj_name_to_id, rec_name_to_id = read_category_map_file(
#         config.ENVIRONMENT.category_map_file
#     )
#     vocab = build_vocab_from_category_map(obj_name_to_id, rec_name_to_id)
#     semantic_sensor.update_vocabulary_list(vocab, 0)
#     semantic_sensor.set_vocabulary(0)
#     return config, semantic_sensor


if __name__ == "__main__":
    from eval.utils import eval_bboxes_and_print

    parser = argparse.ArgumentParser(
        prog="build_3d_map",
        description="Builds 3D map of a scannet scene from a RGBD trajectory and evaluates object detection",
    )

    parser.add_argument(
        "--scannet-dir",
        default="/private/home/ssax/home-robot/src/home_robot/home_robot/datasets/scannet/data",
        help="Directory where scannet dataset lives",
    )
    parser.add_argument(
        "-f",
        "--frame-skip",
        type=int,
        default=180,
        help="Subsample every frame-skip frames in the trajectory",
    )
    parser.add_argument(
        "-s",
        "--scene-name",
        type=str,
        default="scene0192_00",
        help="Which ScanNet scene to load. scene0192_00 is a small scene, scene0000_00 is a large scene",
    )
    parser.add_argument(
        "--detector-config",
        type=str,
        default=None,
        help="Location of detector config file",
    )
    parser.add_argument(
        "-d",
        "--device",
        type=int,
        default=0,
        help="Which device to use for torch operations. -1 for cpu",
    )
    parser.add_argument(
        "--show-open3d",
        action="store_true",
        help="Display the scene in an Open3D visualizer window",
    )
    args = parser.parse_args()
    torch_device = "cpu" if args.device == -1 else f"cuda:{args.device}"

    # Load specific scene
    data = ScanNetDataset(
        root_dir=args.scannet_dir,
        frame_skip=args.frame_skip,
        # referit3d_config = ReferIt3dDataConfig(),
        # scanrefer_config = ScanReferDataConfig(),
    )
    idx = data.scene_list.index(args.scene_name)
    scene_obs = data.__getitem__(idx, show_progress=True)
    print(
        f"--Finished loading scannet scene: images of (h: {data.height}, w: {data.width}) - resized from ({data.DEFAULT_HEIGHT},{data.DEFAULT_WIDTH})"
    )

    # Set up detector
    # TODO: Instantiate this from config file and allow using detectors besides detic
    segmenter = DeticPerception(
        config_file=None,
        vocabulary="custom",
        custom_vocabulary=",".join(data.METAINFO["classes"]),
        checkpoint_file=None,
        sem_gpu_id=args.device,
    )

    # Get detections
    scene_obs["instance_map"] = []
    scene_obs["instance_classes"] = []
    scene_obs["instance_scores"] = []
    with torch.no_grad():
        instance_map, instance_classes, instance_scores = [], [], []
        semantic_frames = []
        for im in tqdm(
            scene_obs["images"].cpu().numpy(),
            desc="Running semantic_sensor one image at a time...",
        ):
            obs = Observations(rgb=im * 255, gps=None, compass=None, depth=None)
            res = segmenter.predict(obs).task_observations
            instance_map.append(
                torch.from_numpy(res["instance_map"]).int().to(torch_device)
            )
            instance_classes.append(
                torch.from_numpy(res["instance_classes"]).int().to(torch_device)
            )
            instance_scores.append(
                torch.from_numpy(res["instance_scores"]).float().to(torch_device)
            )
            semantic_frames.append(torch.from_numpy(res["semantic_frame"]).int())
        scene_obs["instance_map"] = torch.stack(instance_map, dim=0)
        scene_obs["instance_classes"] = instance_classes
        scene_obs["instance_scores"] = instance_scores
        scene_obs["semantic_frame"] = semantic_frames

    # Move other keys to device
    for key in ["images", "depths", "intrinsics", "poses"]:
        print(key)
        scene_obs[key] = scene_obs[key].to(torch_device)

    # Add to ScanNetSparseVoxelMap
    sn_svm = ScanNetSparseVoxelMap()
    n_frames = len(scene_obs["images"])
    with torch.no_grad():
        for i in tqdm(range(n_frames), desc="Adding observations to map"):
            obs = Observations(
                gps=None,  # (x, y) where positive x is forward, positive y is translation to left in meters
                compass=None,  # positive theta is rotation to left in radians - consistent with robot
                rgb=scene_obs["images"][i]
                * 255,  # (camera_height, camera_width, 3) in [0, 255]
                depth=scene_obs["depths"][i],  # (camera_height, camera_width) in meters
                semantic=semantic_frames[
                    i
                ],  # (camera_height, camera_width) in [0, num_sem_categories - 1]
                # Instance IDs per observation frame
                # Size: (camera_height, camera_width)
                # Range: 0 to max int
                instance=scene_obs["instance_map"][i],
                # Pose of the camera in world coordinates
                camera_pose=scene_obs["poses"][i],
                camera_K=scene_obs["intrinsics"][i],
                task_observations={
                    "instance_classes": scene_obs["instance_classes"][i],
                    "instance_scores": scene_obs["instance_scores"][i],
                    "features": scene_obs["images"][i],
                },
            )
            sn_svm.step(obs)

    # Evaluate against GT object detection + localization
    instances = sn_svm.voxel_map.get_instances()
    bounds = torch.stack([inst.bounds.cpu() for inst in instances])
    scores = torch.stack(
        [
            torch.mean(torch.stack([v.score for v in ins.instance_views])).cpu()
            for ins in instances
        ]
    )
    classes = torch.stack([inst.category_id.cpu() for inst in instances])
    detic_to_scannet_classes = torch.tensor(data.METAINFO["seg_valid_class_ids"])
    classes = detic_to_scannet_classes[classes]

    eval_bboxes_and_print(
        box_gt_bounds=[scene_obs["boxes_aligned"]],
        box_gt_class=[scene_obs["box_classes"]],
        box_pred_bounds=[bounds],
        box_pred_class=[classes],
        box_pred_scores=[scores],
        match_within_class=True,
        iou_thr=(0.25, 0.5, 0.75),
    )

    if args.show_open3d:
        sn_svm.voxel_map.show(backend="open3d")
