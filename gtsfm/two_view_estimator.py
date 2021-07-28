"""Estimator which operates on a pair of images to compute relative pose and verified indices.

Authors: Ayush Baid, John Lambert
"""
import logging
from dataclasses import dataclass
from typing import Tuple, Optional

import dask
import numpy as np
from dask.delayed import Delayed
from dataclasses import dataclass
from gtsam import Cal3Bundler, Pose3, Rot3, Unit3
import trimesh

import gtsfm.utils.geometry_comparisons as comp_utils
import gtsfm.utils.logger as logger_utils
import gtsfm.utils.metrics as metric_utils
from gtsfm.common.keypoints import Keypoints
from gtsfm.frontend.matcher.matcher_base import MatcherBase
from gtsfm.frontend.verifier.homography import HomographyEstimator
from gtsfm.frontend.verifier.verifier_base import VerifierBase

logger = logger_utils.get_logger()

mpl_logger = logging.getLogger("matplotlib")
mpl_logger.setLevel(logging.WARNING)

pil_logger = logging.getLogger("PIL")
pil_logger.setLevel(logging.INFO)

# In case an epipolar geometry can be verified, it is checked whether
# the geometry describes a planar scene or panoramic view (pure rotation)
# described by a homography. This is a degenerate case, since epipolar
# geometry is only defined for a moving camera. If the inlier ratio of
# a homography comes close to the inlier ratio of the epipolar geometry,
# a planar or panoramic configuration is assumed.
# Based on COLMAP's front-end logic here:
#    https://github.com/colmap/colmap/blob/dev/src/estimators/two_view_geometry.cc#L230
MAX_H_INLIER_RATIO = 0.8

EPSILON = 1e-6


@dataclass(frozen=False)
class TwoViewEstimationReport:
    """Information about verifier result on an edge between two nodes (i1,i2).

    In the spirit of COLMAP's Report class:
    https://github.com/colmap/colmap/blob/dev/src/optim/ransac.h#L82

    Inlier ratio is defined in Heinly12eccv: https://www.cs.unc.edu/~jheinly/publications/eccv2012-heinly.pdf
    or in Slide 59: https://www.cc.gatech.edu/~afb/classes/CS4495-Fall2014/slides/CS4495-Ransac.pdf

    Args:
        v_corr_idxs: verified correspondence indices.
        num_inliers_est_model: #correspondences consistent with estimated model (not necessarily "correct")
        inlier_ratio_est_model: #matches consistent with est. model / # putative matches, i.e.
           measures how consistent the model is with the putative matches.
        num_inliers_gt_model: measures how well the verification worked, w.r.t. GT, i.e. #correct correspondences.
        inlier_ratio_gt_model: #correct matches/#putative matches. Only defined if GT relative pose provided.
        R_error_deg: relative pose error w.r.t. GT. Only defined if GT poses provided.
        U_error_deg: relative translation error w.r.t. GT. Only defined if GT poses provided.
        i2Ri1: relative rotation.
        i2Ui1: relative translation direction.
    """

    num_H_inliers: int
    H_inlier_ratio: float
    v_corr_idxs: np.ndarray
    num_inliers_est_model: float
    inlier_ratio_est_model: Optional[float] = None  # TODO: make not optional (pass from verifier)
    num_inliers_gt_model: Optional[float] = None
    precision_gt_model: Optional[float] = None
    recall_gt_model: Optional[float] = None
    inlier_mask_gt_model: Optional[np.ndarray] = None,
    avg_inlier_reproj_err: Optional[float] = None
    R_error_deg: Optional[float] = None
    U_error_deg: Optional[float] = None
    i2Ri1: Optional[Rot3] = None
    i2Ui1: Optional[Unit3] = None


class TwoViewEstimator:
    """Wrapper for running two-view relative pose estimation on image pairs in the dataset."""

    def __init__(
        self,
        matcher: MatcherBase,
        verifier: VerifierBase,
        eval_threshold_px: float,
        min_num_inliers_acceptance: int,
    ) -> None:
        """Initializes the two-view estimator from matcher and verifier.

        Args:
            matcher: matcher to use.
            verifier: verifier to use.
            eval_threshold_px: distance threshold for marking a correspondence pair as inlier during evaluation
                (not during estimation).
            min_num_inliers_acceptance: minimum number of inliers that must agree w/ estimated model, to use
                image pair.
        """
        self._matcher = matcher
        self._verifier = verifier
        self._corr_metric_dist_threshold = eval_threshold_px
        # Note: homography estimation threshold must match the E / F thresholds for #inliers to be comparable
        self._homography_estimator = HomographyEstimator(verifier._estimation_threshold_px)
        self._min_num_inliers_acceptance = min_num_inliers_acceptance

    def get_corr_metric_dist_threshold(self) -> float:
        """Getter for the distance threshold used in the metric for correct correspondences."""
        return self._corr_metric_dist_threshold

    def create_computation_graph(
        self,
        keypoints_i1_graph: Delayed,
        keypoints_i2_graph: Delayed,
        descriptors_i1_graph: Delayed,
        descriptors_i2_graph: Delayed,
        camera_intrinsics_i1_graph: Delayed,
        camera_intrinsics_i2_graph: Delayed,
        im_shape_i1_graph: Delayed,
        im_shape_i2_graph: Delayed,
        i2Ti1_expected_graph: Optional[Delayed] = None,
        wTi1_expected_graph: Optional[Delayed] = None,
        wTi2_expected_graph: Optional[Delayed] = None,
        scene_mesh_expected: Optional[Delayed] = None,
    ) -> Tuple[Delayed, Delayed, Delayed, Optional[Delayed], Optional[Delayed], Optional[Delayed]]:
        """Create delayed tasks for matching and verification.

        Args:
            keypoints_i1_graph: keypoints for image i1.
            keypoints_i2_graph: keypoints for image i2.
            descriptors_i1_graph: corr. descriptors for image i1.
            descriptors_i2_graph: corr. descriptors for image i2.
            camera_intrinsics_i1_graph: intrinsics for camera i1.
            camera_intrinsics_i2_graph: intrinsics for camera i2.
            im_shape_i1_graph: image shape for image i1.
            im_shape_i2_graph: image shape for image i2.
            i2Ti1_expected_graph (optional): ground truth relative pose, used for evaluation if available. Defaults to
                                             None.
            wTi1_expected_graph (optional): ground truth pose of the world frame relative to i1.
            wTi2_expected_graph (optional): ground truth pose of the world frame relative to i2.
            scene_mesh_expected (optional): ground truth triangular surface mesh of the scene in the world frame.

        Returns:
            Computed relative rotation wrapped as Delayed.
            Computed relative translation direction wrapped as Delayed.
            Indices of verified correspondences wrapped as Delayed.
            Error in relative rotation wrapped as Delayed
            Error in relative translation direction wrapped as Delayed.
            Correspondence correctness metrics wrapped as Delayed.
        """

        # graph for matching to obtain putative correspondences
        corr_idxs_graph = self._matcher.create_computation_graph(
            keypoints_i1_graph,
            keypoints_i2_graph,
            descriptors_i1_graph,
            descriptors_i2_graph,
            im_shape_i1_graph,
            im_shape_i2_graph,
        )

        # verification on putative correspondences to obtain relative pose
        # and verified correspondences
        (i2Ri1_graph, i2Ui1_graph, v_corr_idxs_graph, inlier_ratio_est_model) = self._verifier.create_computation_graph(
            keypoints_i1_graph,
            keypoints_i2_graph,
            corr_idxs_graph,
            camera_intrinsics_i1_graph,
            camera_intrinsics_i2_graph,
        )

        # Evaluate the computed relative pose
        if i2Ti1_expected_graph is not None:
            pose_error_graphs = dask.delayed(compute_relative_pose_metrics)(
                i2Ri1_graph, i2Ui1_graph, i2Ti1_expected_graph
            )
        else:
            pose_error_graphs = (None, None)

        # Evaluate the correspondences
        if (wTi1_expected_graph is not None and 
            wTi2_expected_graph is not None and 
            scene_mesh_expected is not None):
            corr_metric_graph = dask.delayed(compute_correspondence_metrics)(
                keypoints_i1_graph,
                keypoints_i2_graph,
                v_corr_idxs_graph,
                camera_intrinsics_i1_graph,
                camera_intrinsics_i2_graph,
                i2Ti1_expected_graph,
                self._corr_metric_dist_threshold,
                wTi1_expected_graph,
                wTi2_expected_graph,
                scene_mesh_expected,
            )
            number_correct, precision, recall, inlier_mask, avg_inlier_reproj_err = (
                corr_metric_graph[0], corr_metric_graph[1], corr_metric_graph[2], corr_metric_graph[3], corr_metric_graph[4]
            )
        elif i2Ti1_expected_graph is not None:
            corr_metric_graph = dask.delayed(compute_correspondence_metrics)(
                keypoints_i1_graph,
                keypoints_i2_graph,
                v_corr_idxs_graph,
                camera_intrinsics_i1_graph,
                camera_intrinsics_i2_graph,
                i2Ti1_expected_graph,
                self._corr_metric_dist_threshold,
            )
            number_correct, precision, recall, inlier_mask, avg_inlier_reproj_err = (
                corr_metric_graph[0], corr_metric_graph[1], corr_metric_graph[2], corr_metric_graph[3], corr_metric_graph[4]
            )
        else:
            number_correct, precision, recall, inlier_mask, avg_inlier_reproj_err = [None for x in range(5)]


        result = dask.delayed(self._homography_estimator.estimate)(
            keypoints_i1_graph,
            keypoints_i2_graph,
            match_indices=corr_idxs_graph,
        )
        num_H_inliers, H_inlier_ratio = result[0], result[1]

        R_error_deg, U_error_deg = pose_error_graphs[0], pose_error_graphs[1]

        two_view_report_graph = dask.delayed(generate_two_view_report)(
            inlier_ratio_est_model,
            R_error_deg,
            U_error_deg,
            number_correct,
            precision,
            recall,
            inlier_mask,
            avg_inlier_reproj_err,
            v_corr_idxs_graph,
            num_H_inliers,
            H_inlier_ratio,
        )

        result = dask.delayed(self.check_for_degeneracy)(
            two_view_report_graph, i2Ri1_graph, i2Ui1_graph, v_corr_idxs_graph
        )
        i2Ri1_graph, i2Ui1_graph, v_corr_idxs_graph, two_view_report_graph = result[0], result[1], result[2], result[3]

        return (i2Ri1_graph, i2Ui1_graph, v_corr_idxs_graph, two_view_report_graph)

    def check_for_degeneracy(
        self,
        two_view_report: TwoViewEstimationReport,
        i2Ri1: Optional[Rot3],
        i2Ui1: Optional[Unit3],
        v_corr_idxs: np.ndarray,
    ) -> Tuple[Optional[Rot3], Optional[Unit3], np.ndarray]:
        """ """
        insufficient_inliers = two_view_report.num_inliers_est_model < self._min_num_inliers_acceptance

        H_EF_inlier_ratio = two_view_report.num_H_inliers / (two_view_report.num_inliers_est_model + EPSILON)
        is_planar_or_panoramic = H_EF_inlier_ratio > MAX_H_INLIER_RATIO

        # TODO: technically this should almost always be non-zero, just need to move up to earlier
        valid_model = two_view_report.num_inliers_est_model > 0
        if valid_model:
            logger.info("H_EF_inlier_ratio: %.2f", H_EF_inlier_ratio)

        if (valid_model and is_planar_or_panoramic) or (valid_model and insufficient_inliers):

            if is_planar_or_panoramic:
                logger.info("Planar or panoramic; pose from homography currently not supported.")
            if insufficient_inliers:
                logger.info("Insufficient number of inliers.")

            i2Ri1 = None
            i2Ui1 = None
            v_corr_idxs = np.array([], dtype=np.uint64)
            # remove mention of errors in the report
            two_view_report.R_error_deg = None
            two_view_report.U_error_deg = None

        two_view_report.i2Ri1 = i2Ri1
        two_view_report.i2Ui1 = i2Ui1

        return i2Ri1, i2Ui1, v_corr_idxs, two_view_report


def generate_two_view_report(
    inlier_ratio_est_model: float,
    R_error_deg: float,
    U_error_deg: float,
    number_correct: int,
    precision: float,
    recall: float,
    inlier_mask: np.ndarray,
    avg_inlier_reproj_err: float,
    v_corr_idxs: np.ndarray,
    num_H_inliers: int,
    H_inlier_ratio: float,
) -> TwoViewEstimationReport:
    """Wrapper around class constructor for Dask."""
    two_view_report = TwoViewEstimationReport(
        inlier_ratio_est_model=inlier_ratio_est_model,
        num_inliers_est_model=v_corr_idxs.shape[0],
        num_inliers_gt_model=number_correct,
        precision_gt_model=precision,
        recall_gt_model=recall,
        inlier_mask_gt_model=inlier_mask,
        avg_inlier_reproj_err=avg_inlier_reproj_err,
        v_corr_idxs=v_corr_idxs,
        R_error_deg=R_error_deg,
        U_error_deg=U_error_deg,
        num_H_inliers=num_H_inliers,
        H_inlier_ratio=H_inlier_ratio,
    )
    return two_view_report


def compute_correspondence_metrics(
    keypoints_i1: Keypoints,
    keypoints_i2: Keypoints,
    corr_idxs_i1i2: np.ndarray,
    intrinsics_i1: Cal3Bundler,
    intrinsics_i2: Cal3Bundler,
    i2Ti1: Pose3,
    epipolar_distance_threshold: float,
    wTi1: Optional[Pose3] = None,
    wTi2: Optional[Pose3] = None,
    scene_mesh: Optional[trimesh.Trimesh] = None,
) -> Tuple[int, float, float, np.ndarray, float]:
    """Compute the metrics for the generated verified correspondence.

    Args:
        keypoints_i1: detected keypoints in image i1.
        keypoints_i2: detected keypoints in image i2.
        corr_idxs_i1i2: indices of correspondences.
        intrinsics_i1: intrinsics for i1.
        intrinsics_i2: intrinsics for i2.
        i2Ti1: relative pose.
        epipolar_distance_threshold: max epipolar distance to qualify as a correct match.
        wTi1 (optional): ground truth pose of the world frame relative to i1.
        wTi2 (optional): ground truth pose of the world frame relative to i2.
        scene_mesh (optional): ground truth triangular surface mesh of the scene in the world frame.

    Returns:
        Number of inlier correspondences to ground truth epipolar geometry, i.e. #correct correspondences.
        Inlier Ratio, i.e. ratio of correspondences which are correct w.r.t. given relative pose.
    """
    if corr_idxs_i1i2.size == 0:
        return 0, float("Nan"), float("Nan"), None, float("Nan")

    if None not in (wTi1, wTi2, scene_mesh):
        gt_corr_i1i2_model = metric_utils.compute_gt_correspondences(
            keypoints_i1,
            keypoints_i2,
            intrinsics_i1,
            intrinsics_i2,
            wTi1,
            wTi2,
            scene_mesh,
        )
        inlier_mask, avg_inlier_reproj_error = metric_utils.mesh_inlier_correspondences(
            keypoints_i1.extract_indices(corr_idxs_i1i2[:, 0]),
            keypoints_i2.extract_indices(corr_idxs_i1i2[:, 1]),
            intrinsics_i1,
            intrinsics_i2,
            wTi1,
            wTi2,
            scene_mesh,
        )
        logger.info(f'avg. inlier reproj. error:   {avg_inlier_reproj_error}')
        logger.info(f'Recall: {np.count_nonzero(inlier_mask) / gt_corr_i1i2_model.shape[0]}')
    else:
        inlier_mask = metric_utils.count_correct_correspondences(
            keypoints_i1.extract_indices(corr_idxs_i1i2[:, 0]),
            keypoints_i2.extract_indices(corr_idxs_i1i2[:, 1]),
            intrinsics_i1,
            intrinsics_i2,
            i2Ti1,
            epipolar_distance_threshold,
        )
        avg_inlier_reproj_error = None
        gt_corr_i1i2_model = np.array([])
    num_inliers_gt_model = np.count_nonzero(inlier_mask) # TP
    pmr_gt_model = num_inliers_gt_model / max(len(keypoints_i1), len(keypoints_i2)) # TP / (Total Features)
    precision_gt_model = num_inliers_gt_model / inlier_mask.size # TP / (TP + FP)
    recall_gt_model = num_inliers_gt_model / gt_corr_i1i2_model.shape[0] # TP / (TP + FN)
    logger.info(f'# corr outliers:   {inlier_mask.size - num_inliers_gt_model}')
    logger.info(f'Precision: {precision_gt_model}')
    return num_inliers_gt_model, precision_gt_model, recall_gt_model, inlier_mask, avg_inlier_reproj_error


def compute_relative_pose_metrics(
    i2Ri1_computed: Optional[Rot3], i2Ui1_computed: Optional[Unit3], i2Ti1_expected: Pose3
) -> Tuple[Optional[float], Optional[float]]:
    """Compute the metrics on relative camera pose.

    Args:
        i2Ri1_computed: computed relative rotation.
        i2Ui1_computed: computed relative translation direction.
        i2Ti1_expected: expected relative pose.

    Returns:
        Rotation error, in degrees
        Unit translation error, in degrees
    """
    R_error_deg = comp_utils.compute_relative_rotation_angle(i2Ri1_computed, i2Ti1_expected.rotation())
    U_error_deg = comp_utils.compute_relative_unit_translation_angle(
        i2Ui1_computed, Unit3(i2Ti1_expected.translation())
    )

    return (R_error_deg, U_error_deg)
