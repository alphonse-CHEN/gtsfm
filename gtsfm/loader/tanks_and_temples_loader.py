"""Loader for the Tanks & Temples dataset.

See https://www.tanksandtemples.org/download/ for more information.

Author: John Lambert
"""

import os
import tempfile
from enum import auto, Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import open3d
import trimesh
from gtsam import Cal3Bundler, Rot3, Pose3
from dask.distributed import Client, Future
from trimesh import Trimesh

import gtsfm.common.types as gtsfm_types
import gtsfm.utils.io as io_utils
import gtsfm.utils.logger as logger_utils
from gtsfm.common.image import Image
from gtsfm.common.keypoints import Keypoints
from gtsfm.frontend.correspondence_generator.keypoint_aggregator.keypoint_aggregator_base import KeypointAggregatorBase
from gtsfm.frontend.correspondence_generator.keypoint_aggregator.keypoint_aggregator_dedup import (
    KeypointAggregatorDedup,
)
from gtsfm.frontend.correspondence_generator.keypoint_aggregator.keypoint_aggregator_unique import (
    KeypointAggregatorUnique,
)
from gtsfm.loader.loader_base import LoaderBase
import gtsfm.visualization.open3d_vis_utils as open3d_vis_utils

logger = logger_utils.get_logger()


_DEFAULT_IMAGE_HEIGHT_PX = 1080
_DEFAULT_IMAGE_WIDTH_PX = 1920


class MeshReconstructionType(Enum):
    """Mesh reconstruction algorithm choices.

    The alpha shape [Edelsbrunner1983] is a generalization of a convex hull.
    See Edelsbrunner and D. G. Kirkpatrick and R. Seidel: On the shape of a set of points in the plane, 1983.
    https://www.cs.jhu.edu/~misha/Fall13b/Papers/Edelsbrunner93.pdf

    The ball pivoting algorithm (BPA) [Bernardini1999].
    See https://www.cs.jhu.edu/~misha/Fall13b/Papers/Bernardini99.pdf

    The Poisson surface reconstruction method [Kazhdan2006].
    See: M.Kazhdan and M. Bolitho and H. Hoppe: Poisson surface reconstruction, Eurographics, 2006.
    See https://hhoppe.com/poissonrecon.pdf
    """

    ALPHA_SHAPE = auto()
    BALL_PIVOTING = auto()
    POISSION_SURFACE = auto()


class TanksAndTemplesLoader(LoaderBase):
    def __init__(
        self,
        img_dir: str,
        poses_fpath: str,
        lidar_ply_fpath: str,
        ply_alignment_fpath: str,
        bounding_polyhedron_json_fpath: str,
        colmap_ply_fpath: str,
        max_resolution: int = 1080,
    ) -> None:
        """Initializes image file paths and GT camera poses.

        There are two coordinate frames -- the COLMAP coordinate frame, and the GT LiDAR coordinate frame.
        We move everything to the GT LiDAR coordinate frame.

        Args:
            img_dir: Path to where images of a single scene are stored.
            poses_fpath: Path to .log file containing COLMAP-reconstructed camera poses.
            lidar_ply_fpath:
            ply_alignment_fpath: The alignment text file contains the transformation matrix to align the COLMAP
                reconstruction to the corresponding ground-truth point cloud.
            bounding_polyhedron_json_fpath: Path to JSON file containing specification of bounding polyhedron
                to crop the COLMAP reconstructed point cloud.
            colmap_ply_fpath: 
        """
        super().__init__(max_resolution)
        self.ply_fpath = lidar_ply_fpath
        self.bounding_polyhedron_json_fpath = bounding_polyhedron_json_fpath
        self._image_paths = list(Path(img_dir).glob("*.jpg"))

        # Load the transform between LiDAR global coordinate frame and COLMAP global coordinate frame.
        T = np.loadtxt(fname=ply_alignment_fpath)
        self.lidar_T_colmap = Pose3(r=Rot3(T[:3,:3]), t=T[:3,3])

        self._use_gt_extrinsics = True
        # The reconstructions are made with an "out of the box" COLMAP configuration and are available as *.ply
        # files together with the camera poses (stored in *.log file format).
        colmapTi_gt_dict = _parse_redwood_data_log_file(poses_fpath)
        self.wTi_gt_dict = {k: self.lidar_T_colmap.compose(colmapTi) for k, colmapTi in colmapTi_gt_dict.items() }

        self._num_imgs = len(self.wTi_gt_dict)

    def __len__(self) -> int:
        """The number of images in the dataset.

        Returns:
            The number of images.
        """
        return self._num_imgs

    def get_image_full_res(self, index: int) -> Image:
        """Gets the image at the given index, at full resolution.

        Args:
            index: The index to fetch.

        Returns:
            The image at the query index.

        Raises:
            IndexError: If an out-of-bounds image index is requested.
        """
        if index < 0 or index >= len(self):
            raise IndexError(f"Image index {index} is invalid")

        # All should have shape (1080, 1920, 3)

        img = io_utils.load_image(self._image_paths[index])

        if img.height != _DEFAULT_IMAGE_HEIGHT_PX:
            raise ValueError('')
        if img.width != _DEFAULT_IMAGE_WIDTH_PX:
            raise ValueError('')
        return img

    def get_camera_intrinsics_full_res(self, index: int) -> Optional[Cal3Bundler]:
        """Gets the camera intrinsics at the given index, valid for a full-resolution image.

        Sony A7SM2, 35.6 x 23.8 mm

        Args:
            The index to fetch.

        Returns:
            Intrinsics for the given camera.
        """
        if index < 0 or index >= len(self):
            raise IndexError("Image index is invalid")

        # Use synthetic camera.
        H = _DEFAULT_IMAGE_HEIGHT_PX
        W = _DEFAULT_IMAGE_WIDTH_PX
        # Principal point offset:
        cx = W / 2
        cy = H / 2
        # Focal length:
        fx = 0.7 * W

        # if not self._use_gt_intrinsics:
        # TODO(johnwlambert): Add Sony A7SM2 to sensor DB, and get intrinsics from exif.
        #intrinsics = io_utils.load_image(self._image_paths[index]).get_intrinsics_from_exif()

        intrinsics = Cal3Bundler(fx, k1=0.0, k2=0.0, u0=cx, v0=cy)
        return intrinsics

    def get_camera_pose(self, index: int) -> Optional[Pose3]:
        """Gets the camera pose (in world coordinates) at the given index.

        Args:
            index: The index to fetch.

        Returns:
            The camera pose w_T_index.
        """
        if index < 0 or index >= len(self):
            raise IndexError("Image index is invalid")

        if not self._use_gt_extrinsics:
            return None

        # wTi = self._wTi_list[index]
        wTi = self.wTi_gt_dict[index]
        return wTi

    def get_lidar_point_cloud(self) -> open3d.geometry.PointCloud:
        """Returns ground-truth point cloud, captured using an industrial laser scanner."""
        return open3d.io.read_point_cloud(self.ply_fpath)

    def reconstruct_mesh(
        self,
        point_downsample_factor: int = 10,
        crop_by_polyhedron: bool = True,
        reconstruction_algorithm: MeshReconstructionType = MeshReconstructionType.ALPHA_SHAPE,
    ) -> open3d.geometry.TriangleMesh:
        """Reconstructs mesh from LiDAR PLY file.

        Args:
            point_downsample_factor
            crop_by_polyhedron: Whether to crop by a manually specified polyhedron, vs. simply
                by range from global origin.

        Returns:
            Reconstructed mesh.
        """
        pcd = self.get_lidar_point_cloud()
        #pcd = pcd.transform(self.lidar_T_colmap)
        if crop_by_polyhedron:
            pass
            pcd = crop_points_to_bounding_polyhedron(pcd, self.bounding_polyhedron_json_fpath)

        points, rgb = open3d_vis_utils.convert_colored_open3d_point_cloud_to_numpy(pcd)
        if not crop_by_polyhedron:
            max_radius = 4.0
            valid = np.linalg.norm(points, axis=1) < max_radius
            points = points[valid]
            rgb = rgb[valid]
        points = points[::point_downsample_factor]
        rgb = rgb[::point_downsample_factor]
        pcd = open3d_vis_utils.create_colored_point_cloud_open3d(points, rgb)
        pcd.estimate_normals()

        if reconstruction_algorithm == MeshReconstructionType.ALPHA_SHAPE:
            alpha = 0.5 # 0.1  # 0.03
            print(f"alpha={alpha:.3f}")
            mesh = open3d.geometry.TriangleMesh.create_from_point_cloud_alpha_shape(pcd, alpha)
            mesh.compute_vertex_normals()

        elif reconstruction_algorithm == MeshReconstructionType.BALL_PIVOTING:
            radii = [0.005, 0.01, 0.02, 0.04]
            rec_mesh = open3d.geometry.TriangleMesh.create_from_point_cloud_ball_pivoting(
                pcd, open3d.utility.DoubleVector(radii)
            )

        elif reconstruction_algorithm == MeshReconstructionType.POISSION_SURFACE:
            mesh, densities = open3d.geometry.TriangleMesh.create_from_point_cloud_poisson(pcd, depth=9)
        return mesh

    def generate_synthetic_correspondences(
        self,
        images: List[Future],
        image_pairs: List[Tuple[int, int]],
        deduplicate: bool = False
    ) -> Tuple[List[Keypoints], Dict[Tuple[int, int], np.ndarray]]:
        """Generates synthetic correspondences from virtual cameras and a ground-truth mesh.

        Args:
            deduplicate: whether to de-duplicate with a single image the detections received from each image pair.

        Returns:
            List of keypoints, one entry for each input image.
            Putative correspondences as indices of keypoints, for pairs of images. Mapping from image pair
                (i1,i2) to delayed task to compute putative correspondence indices. Correspondence indices
                are represented by an array of shape (K,2), for K correspondences.
        """
        mesh = self.reconstruct_mesh()
        open3d_mesh_path = tempfile.NamedTemporaryFile(suffix='.obj').name
        open3d.io.write_triangle_mesh(filename=open3d_mesh_path, mesh=mesh)

        camera_dict = {}

        aggregator: KeypointAggregatorBase = (
            KeypointAggregatorDedup() if deduplicate else KeypointAggregatorUnique()
        )
        keypoints_dict = {}
        putative_corr_idxs_dict = {}

        for (i1, i2) in image_pairs:
            if i1 not in camera_dict:
                camera_dict[i1] = self.get_camera(index=i1)
            if i2 not in camera_dict:
                camera_dict[i2] = self.get_camera(index=i2)
            keypoints_i1, keypoints_i2 = self.generate_synthetic_correspondences_for_image_pair(
                camera_i1=camera_dict[i1],
                camera_i2=camera_dict[i2],
                open3d_mesh_fpath=open3d_mesh_path
            )
            num_kpts = len(keypoints_i1)
            putative_corr_idxs = np.stack([np.arange(num_kpts), np.arange(num_kpts)], axis=-1)
            keypoints_dict[(i1,i2)] = (keypoints_i1, keypoints_i2)
            putative_corr_idxs_dict[(i1,i2)] = putative_corr_idxs
            print(f"Number of keypoints in {i1}: ", len(keypoints_i1))

        keypoints_list, putative_corr_idxs_dict = aggregator.aggregate(keypoints_dict=keypoints_dict)
        return keypoints_list, putative_corr_idxs_dict

    def generate_synthetic_correspondences_for_image_pair(
        self,
        camera_i1: gtsfm_types.CAMERA_TYPE,
        camera_i2: gtsfm_types.CAMERA_TYPE,
        open3d_mesh_fpath: str,
        num_sampled_3d_points: int = 1000
    ) -> Tuple[Keypoints, Keypoints]:
        """Generates synthetic correspondences for image pair.

        Args:
            camera_i1: First camera.
            camera_i2: Second camera.
            open3d_mesh_fpath

        Returns:
            Tuple of `Keypoints` objects, one for each image in the input image pair.
        """
        mesh = open3d.io.read_triangle_mesh(filename=open3d_mesh_fpath)

        trimesh_mesh = load_from_trimesh(open3d_mesh_fpath)

        # Sample random 3d points.
        pcd = mesh.sample_points_uniformly(number_of_points=num_sampled_3d_points)
        #pcd = mesh.sample_points_poisson_disk(number_of_points=500, pcl=pcd)
        points = np.asarray(pcd.points)

        wTi_list = [camera_i1.pose(), camera_i2.pose()]
        calibrations = [camera_i1.calibration(), camera_i2.calibration()]

        keypoints_i1 = []
        keypoints_i2 = []

        # keypoints_i1, cam_i1_valid = compute_points_in_camera_fov(points, camera_i1)
        # keypoints_i2, cam_i2_valid = compute_points_in_camera_fov(points, camera_i2)
        # import pdb; pdb.set_trace()
        # unoccluded_mask_i1 = calculate_point_occlusion_mask(points, camera_i1, trimesh_mesh)
        # unoccluded_mask_i2 = calculate_point_occlusion_mask(points, camera_i2, trimesh_mesh)
        # import pdb; pdb.set_trace()
        # valid = cam_i1_valid & cam_i2_valid & unoccluded_mask_i1 & unoccluded_mask_i2
        # keypoints_i1 = keypoints_i1[valid]
        # keypoints_i2 = keypoints_i2[valid]

        # TODO(johnwlambert): Vectorize this code. On CPU, rays cannot be simultaneously cast against mesh
        # due to RAM limitations.
        for point in points:

            # Try projecting into each camera. If inside FOV of both and unoccluded by mesh, keep.
            uv_i1 = verify_camera_fov_and_occlusion(camera_i1, point, trimesh_mesh)
            uv_i2 = verify_camera_fov_and_occlusion(camera_i2, point, trimesh_mesh)
            if uv_i1 is not None and uv_i2 is not None:
                keypoints_i1.append(uv_i1)
                keypoints_i2.append(uv_i2)

                visualize = True
                if visualize:
                    visualize_ray_to_sampled_mesh_point(camera_i1, point, wTi_list, calibrations, mesh)
                    
        keypoints_i1 = Keypoints(coordinates=np.array(keypoints_i1))
        keypoints_i2 = Keypoints(coordinates=np.array(keypoints_i2))
        print(f"Generated {len(keypoints_i1)} keypoints from sampled {num_sampled_3d_points} 3d points.")
        return keypoints_i1, keypoints_i2


def visualize_ray_to_sampled_mesh_point(
    camera: gtsfm_types.CAMERA_TYPE, point: np.ndarray, wTi_list: List[Pose3], calibrations, mesh
) -> None:
    """
    Args:
        camera: Camera to use.
        point: 3d point as (3,) array.
        wTi_list:
        calibrations
        mesh:
    """
    cam_center = camera.pose().translation()
    ray_dirs = point - camera.pose().translation()
    line_set = _make_line_plot(cam_center, cam_center + ray_dirs)
    # line_set = _make_line_plot(cam_center, camera.backproject(uv_reprojected, depth=1.0))
    point_cloud = np.reshape(point, (1,3))
    rgb = np.array([255,0,0]).reshape(1,3).astype(np.uint8)

    # Visualize two frustums, point, and mesh
    frustums = open3d_vis_utils.create_all_frustums_open3d(wTi_list, calibrations, frustum_ray_len=0.3)
    spheres = open3d_vis_utils.create_colored_spheres_open3d(
        point_cloud, rgb, sphere_radius=0.5
    )
    open3d.visualization.draw_geometries([mesh] + frustums + spheres + [line_set])



# def compute_points_in_camera_fov(points: np.ndarray, camera: gtsfm_types.CAMERA_TYPE) -> Tuple[np.ndarray, np.ndarray]:
#     """Project all 3d points to check if each lies within the camera field of view..

#     Args:
#         points: (N,3) array of 3d points.

#     Returns:
#         Keypoints array of shape (N,2) and boolean mask array of shape (N,).
#     """
#     N = points.shape[0]
#     mask = np.zeros(N, dtype=bool)
#     keypoints = np.zeros((N,2), dtype=np.float32)

#     for k, point in enumerate(points):
#         # Project to camera.
#         uv_reprojected, success_flag = camera.projectSafe(point)
#         # Check for projection error in camera.
#         mask[k] = success_flag
#         keypoints[k] = uv_reprojected

#     return keypoints, mask


# def calculate_point_occlusion_mask(points: np.ndarray, camera: gtsfm_types.CAMERA_TYPE, trimesh_mesh) -> np.ndarray:
#     """ """
#     N = points.shape[0]
#     # Cast ray through keypoint back towards scene point.
#     #cam_center = camera.pose().translation()
#     cam_center = np.repeat(camera.pose().translation().reshape((-1, 3)), N, axis=0)
#     ray_dirs = points - camera.pose().translation().reshape(1,3)

#     # Returns the location of where a ray hits a surface mesh.
#     # keypoint_ind: (M,) array of keypoint indices whose corresponding ray intersected the ground truth mesh.
#     # intersections_locations: (M, 3), array of ray intersection locations.
#     intersections, keypoint_ind, _ = trimesh_mesh.ray.intersects_location(
#         ray_origins=cam_center,
#         ray_directions=ray_dirs,
#         multiple_hits=False
#     )
#     if intersections.shape[0] != N:
#         raise ValueError(f"Invalid number of intersections {intersections.shape[0]} vs. {N}")
#     unoccluded_mask = np.linalg.norm(intersections - points, axis=1) < 0.01
#     return unoccluded_mask


def verify_camera_fov_and_occlusion(
    camera: gtsfm_types.CAMERA_TYPE, point: np.ndarray, trimesh_mesh,
) -> Optional[np.ndarray]:
    """Verifies point lies within camera FOV and is unoccluded by mesh faces.

    Args:
        camera: Camera to use.
        point: 3d point as (3,) array.
        trimesh_mesh: Trimesh mesh object to raycast against.

    Returns:
        2d keypoint as (2,) array.
    """
    # Get the camera associated with the measurement.
    # Project to camera.
    uv_reprojected, success_flag = camera.projectSafe(point)
    # Check for projection error in camera.
    if not success_flag:
        print("Skip failed: ", uv_reprojected, ", success: ", success_flag)
        return None

    # Cast ray through keypoint back towards scene.
    # cam_center = np.repeat(camera.pose().translation().reshape((-1, 3)), num_kpts, axis=0)
    cam_center = camera.pose().translation()
    # Choose an arbitrary depth for the ray direction.
    #ray_dirs = np.asarray([camera.backproject(uv_reprojected, depth=1.0) - cam_center[i, :] for i in range(num_kpts)])
    ray_dirs = point - camera.pose().translation()

    # Returns the location of where a ray hits a surface mesh.
    # keypoint_ind: (M,) array of keypoint indices whose corresponding ray intersected the ground truth mesh.
    # intersections_locations: (M, 3), array of ray intersection locations.
    intersections, keypoint_ind, _ = trimesh_mesh.ray.intersects_location(
        ray_origins=cam_center.reshape(1,3),
        ray_directions=ray_dirs.reshape(1,3),
        multiple_hits=False
    )
    
    if intersections.shape[0] > 1 or intersections.shape[0] == 0:
        import pdb; pdb.set_trace()

    # TODO(johnwlambert): Tune this tolerance threshold to allow looser matches.
    if not np.allclose(intersections[0], point):
        # There was a closer intersection along the ray than `point`, meaning `point` is occluded by the mesh.
        return None

    return uv_reprojected


def crop_points_to_bounding_polyhedron(pcd: open3d.geometry.PointCloud, json_fpath: str) -> open3d.geometry.PointCloud:
    """Crops a point cloud according to JSON-specified polyhedron crop bounds.

    Args:
        pcd: Input point cloud.
        json_fpath: Path to JSON file containing crop specification, including 'orthogonal_axis',
            'axis_min', 'axis_max', 'bounding_polygon'.

    Returns:
        Cropped point cloud, according to `SelectionPolygonVolume`.
    """
    vol = open3d.visualization.read_selection_polygon_volume(json_fpath)
    cropped_pcd = vol.crop_point_cloud(pcd)
    return cropped_pcd


def _parse_redwood_data_log_file(log_fpath: str) -> Dict[int, Pose3]:
    """Parses camera poses provided in the "Redwood Data" log file format.

    Trajectory File (.log) format reference: http://redwood-data.org/indoor/fileformat.html

    Args:
        log_fpath: Path to .log file containing COLMAP-reconstructed camera poses.

    Returns:
        Mapping from camera index to camera pose.
    """
    wTi_gt_dict = {}
    with open(log_fpath, "r") as f:
        data = f.readlines()

    # Every five lines are an item.
    if len(data) % 5 != 0:
        raise ValueError("Invalid log file format; must contain grouped 5-line entries.")
    num_images = len(data) // 5

    # The first line contains three numbers which store metadata.
    # In a typical camera pose trajectory file, the third number of the metadata is the frame number of the
    # corresponding depth image (starting from 1).
    parse_metadata = lambda x: int(x.split(" ")[0])
    parse_matrix_row = lambda x: [float(val) for val in x.split(" ")]

    for i in range(num_images):
        metadata = data[i * 5]
        cam_idx = parse_metadata(metadata)
        # The other four lines make up the homogeneous transformation matrix.
        lines = data[(i * 5) + 1 : (i + 1) * 5]
        # The transformation matrix maps a point from its local coordinates (in homogeneous form)
        # to the world coordinates: p_w = wTi * p_i
        wTi_gt = np.array([parse_matrix_row(line) for line in lines])
        wTi_gt_dict[cam_idx] = Pose3(wTi_gt)
    return wTi_gt_dict


def _make_line_plot(point1: np.ndarray, point2: np.ndarray) -> open3d.geometry.LineSet:
    """ """
    verts_worldfr = np.array([point1, point2])
    lines = [[0, 1]]
    # color is in range [0,1]
    color = (0,0,1)
    colors = [color for i in range(len(lines))]

    line_set = open3d.geometry.LineSet(
        points=open3d.utility.Vector3dVector(verts_worldfr),
        lines=open3d.utility.Vector2iVector(lines),
    )
    line_set.colors = open3d.utility.Vector3dVector(colors)
    return line_set


def load_from_trimesh(mesh_path: str):
    # Read in scene mesh as Trimesh object
    if not Path(mesh_path).exists():
        raise FileNotFoundError(f"No mesh found at {mesh_path}")
    mesh = trimesh.load(mesh_path, process=False, maintain_order=True)
    logger.info(
        "AstroVision loader read in mesh with %d vertices and %d faces.",
        mesh.vertices.shape[0],
        mesh.faces.shape[0],
    )
    return mesh

