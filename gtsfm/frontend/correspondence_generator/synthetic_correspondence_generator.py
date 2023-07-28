"""Correspondence generator that creates synthetic keypoint correspondences using a 3d mesh.

Authors: John Lambert
"""
import tempfile
from typing import Dict, List, Tuple

from dask.distributed import Client, Future
import numpy as np
import open3d

from gtsfm.common.keypoints import Keypoints
from gtsfm.common.types import CAMERA_TYPE
from gtsfm.frontend.correspondence_generator.correspondence_generator_base import CorrespondenceGeneratorBase
from gtsfm.frontend.correspondence_generator.keypoint_aggregator.keypoint_aggregator_base import KeypointAggregatorBase
from gtsfm.frontend.correspondence_generator.keypoint_aggregator.keypoint_aggregator_dedup import (
    KeypointAggregatorDedup,
)
from gtsfm.frontend.correspondence_generator.keypoint_aggregator.keypoint_aggregator_unique import (
    KeypointAggregatorUnique,
)
from gtsfm.loader.loader_base import LoaderBase
from gtsfm.loader.tanks_and_temples_loader import TanksAndTemplesLoader


class SyntheticCorrespondenceGenerator(CorrespondenceGeneratorBase):
    """Pair-wise synthetic keypoint correspondence generator."""

    def __init__(self, dataset_root: str, scene_name: str, deduplicate: bool = True) -> None:
        """
        Args:
            dataset_root: Path to where Tanks & Temples dataset is stored.
            scene_name: Name of scene from Tanks & Temples dataset.
            deduplicate: Whether to de-duplicate with a single image the detections received from each image pair.
        """
        self._dataset_root = dataset_root
        self._scene_name = scene_name
        self._aggregator: KeypointAggregatorBase = (
            KeypointAggregatorDedup() if deduplicate else KeypointAggregatorUnique()
        )

    def generate_correspondences(
        self,
        client: Client,
        images: List[Future],
        image_pairs: List[Tuple[int, int]],
        num_sampled_3d_points: int = 500,
    ) -> Tuple[List[Keypoints], Dict[Tuple[int, int], np.ndarray]]:
        """Apply the correspondence generator to generate putative correspondences.

        Args:
            client: Dask client, used to execute the front-end as futures.
            images: List of all images, as futures.
            image_pairs: Indices of the pairs of images to estimate two-view pose and correspondences.

        Returns:
            List of keypoints, with one entry for each input image.
            Putative correspondences as indices of keypoints (N,2), for pairs of images (i1,i2).
        """
        dataset_root = self._dataset_root
        scene_name = self._scene_name

        img_dir = f"{dataset_root}/{scene_name}"
        poses_fpath = f"{dataset_root}/{scene_name}_COLMAP_SfM.log"
        lidar_ply_fpath = f"{dataset_root}/{scene_name}.ply"
        colmap_ply_fpath = f"{dataset_root}/{scene_name}_COLMAP.ply"
        ply_alignment_fpath = f"{dataset_root}/{scene_name}_trans.txt"
        bounding_polyhedron_json_fpath = f"{dataset_root}/{scene_name}.json"
        loader = TanksAndTemplesLoader(
            img_dir=img_dir,
            poses_fpath=poses_fpath,
            lidar_ply_fpath=lidar_ply_fpath,
            ply_alignment_fpath=ply_alignment_fpath,
            bounding_polyhedron_json_fpath=bounding_polyhedron_json_fpath,
            colmap_ply_fpath=colmap_ply_fpath,
        )

        mesh = loader.reconstruct_mesh()

        # Sample random 3d points. This sampling must occur only once, to avoid clusters from repeated sampling.
        pcd = mesh.sample_points_uniformly(number_of_points=num_sampled_3d_points)
        pcd = mesh.sample_points_poisson_disk(number_of_points=num_sampled_3d_points, pcl=pcd)
        sampled_points = np.asarray(pcd.points)

        # TODO(jolambert): File Open3d bug to add pickle support for TriangleMesh.
        open3d_mesh_path = tempfile.NamedTemporaryFile(suffix=".obj").name
        open3d.io.write_triangle_mesh(filename=open3d_mesh_path, mesh=mesh)

        loader_future = client.scatter(loader, broadcast=False)

        def apply_synthetic_corr_generator(
            loader_: LoaderBase,
            camera_i1: CAMERA_TYPE,
            camera_i2: CAMERA_TYPE,
            open3d_mesh_fpath: str,
        ) -> Tuple[Keypoints, Keypoints]:
            return loader_.generate_synthetic_correspondences_for_image_pair(camera_i1, camera_i2, open3d_mesh_fpath)

        pairwise_correspondence_futures = {
            (i1, i2): client.submit(
                apply_synthetic_corr_generator,
                loader_future,
                loader.get_camera(index=i1),
                loader.get_camera(index=i2),
                open3d_mesh_path,
                sampled_points,
            )
            for i1, i2 in image_pairs
        }

        pairwise_correspondences: Dict[Tuple[int, int], Tuple[Keypoints, Keypoints]] = client.gather(
            pairwise_correspondence_futures
        )

        keypoints_list, putative_corr_idxs_dict = self._aggregator.aggregate(keypoints_dict=pairwise_correspondences)
        return keypoints_list, putative_corr_idxs_dict
