"""
In the spirit of "Growing Consensus" (Son16cvpr)
https://www.cv-foundation.org/openaccess/content_cvpr_2016/papers/Son_Solving_Small-Piece_Jigsaw_CVPR_2016_paper.pdf

Authors: John Lambert
"""

import logging
import os
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple

import matplotlib.pyplot as plt
import numpy as np
from gtsam import Cal3Bundler, PinholeCameraCal3Bundler, Pose3, Rot3, Unit3

import gtsfm.multi_view_optimizer as multi_view_optimizer
import gtsfm.utils.graph as graph_utils
import gtsfm.utils.logger as logger_utils
import gtsfm.utils.metrics as metrics_utils
from gtsfm.averaging.rotation.shonan import ShonanRotationAveraging
from gtsfm.averaging.translation.averaging_1dsfm import TranslationAveraging1DSFM
from gtsfm.bundle.bundle_adjustment import BundleAdjustmentOptimizer
from gtsfm.common.keypoints import Keypoints
from gtsfm.data_association.data_assoc import DataAssociation, TriangulationParam
from gtsfm.evaluation.metrics import GtsfmMetricsGroup
from gtsfm.two_view_estimator import TwoViewEstimationReport
from gtsfm.view_graph_estimator.view_graph_estimator_base import ViewGraphEstimatorBase

logger = logger_utils.get_logger()


class ThreeViewRegistrationMethod(str, Enum):
    """Two supported modes for 3-view registration:
    1. PNP (a la incremental SfM) or
    2. Global rotation averaging + translation averaging.
    """

    PNP: str = "PNP"
    AVERAGING: str = "AVERAGING"


class ReprojectionErrorViewGraphEstimator(ViewGraphEstimatorBase):
    """Triangulates and bundle adjusts points in 3-views and uses their reprojection error to reason about outliers.

    Alternatively, could use triangulation + PNP.
    """

    def __init__(
        self, registration_method: ThreeViewRegistrationMethod = ThreeViewRegistrationMethod.AVERAGING
    ) -> None:
        """ """
        self._registration_method = registration_method
        self._rot_avg_module = ShonanRotationAveraging()
        self._trans_avg_module = TranslationAveraging1DSFM(robust_measurement_noise=True)

        # TODO: could limit to length 3 tracks.
        self._data_association_module = DataAssociation(
            reproj_error_thresh=100,
            min_track_len=2,
            mode=TriangulationParam.NO_RANSAC,
            num_ransac_hypotheses=20,
            save_track_patches_viz=False,
        )

        self._bundle_adjustment_module = BundleAdjustmentOptimizer(
            output_reproj_error_thresh=3,  # for post-optimization filtering
            robust_measurement_noise=True,
            shared_calib=False,
        )

    def run(
        self,
        i2Ri1_dict: Dict[Tuple[int, int], Rot3],
        i2Ui1_dict: Dict[Tuple[int, int], Unit3],
        calibrations: List[Cal3Bundler],
        corr_idxs_i1i2: Dict[Tuple[int, int], np.ndarray],
        keypoints: List[Keypoints],
        two_view_reports: Dict[Tuple[int, int], TwoViewEstimationReport],
    ) -> Set[Tuple[int, int]]:
        """Estimates the view graph using the rotation consistency constraint in a cycle of 3 edges.

        Args:
            i2Ri1_dict: Dict from (i1, i2) to relative rotation of i1 with respect to i2.
            i2Ui1_dict: Dict from (i1, i2) to relative translation direction of i1 with respect to i2.
            calibrations: list of calibrations for each image.
            corr_idxs_i1i2: Dict from (i1, i2) to indices of verified correspondences from i1 to i2.
            keypoints: keypoints for each images.
            two_view_reports: Dict from (i1, i2) to the TwoViewEstimationReport of the edge.

        Returns:
            Edges of the view-graph, which are the subset of the image pairs in the input args.
        """

        valid_edges = set()

        logger.info("Input number of edges: %d" % len(i2Ri1_dict))
        input_edges: List[Tuple[int, int]] = self._get_valid_input_edges(i2Ri1_dict)
        triplets: List[Tuple[int, int, int]] = graph_utils.extract_cyclic_triplets_from_edges(input_edges)

        support_per_triplet = []
        reproj_error_per_triplet = []
        max_gt_rot_error_in_cycle = []
        max_gt_trans_error_in_cycle = []

        logger.info("Number of triplets: %d" % len(triplets))
        for i0, i1, i2 in triplets:  # sort order guaranteed
            logger.info("On triplet (%d,%d,%d)", i0, i1, i2)
            i2Ri1_dict_subscene, i2Ui1_dict_subscene, corr_idxs_i1_i2_subscene, _ = self._filter_with_edges(
                i2Ri1_dict=i2Ri1_dict,
                i2Ui1_dict=i2Ui1_dict,
                corr_idxs_i1i2=corr_idxs_i1i2,
                two_view_reports=two_view_reports,
                edges_to_select=[(i0, i1), (i1, i2), (i0, i2)],
            )
            if self._registration_method == ThreeViewRegistrationMethod.AVERAGING:
                logger.setLevel(logging.WARNING)
                wTi_list, reproj_errors, _, _, _ = self.optimize_three_views_averaging(
                    i2Ri1_dict_subscene, i2Ui1_dict_subscene, calibrations, corr_idxs_i1_i2_subscene, keypoints
                )
                logger.setLevel(logging.INFO)

            elif self._registration_method == ThreeViewRegistrationMethod.PNP:
                self.optimize_three_views_incremental()

            # require at least 500 points with reproj error under 5 px?
            MAX_ALLOWED_REPROJ_ERROR = 5
            support = (reproj_errors < MAX_ALLOWED_REPROJ_ERROR).sum()
            MIN_REQUIRED_SUPPORT = 500
            logger.info("Triplet had %d inliers according to reproj. error.", support)
            if support > MIN_REQUIRED_SUPPORT:
                valid_edges.add((i0, i1))
                valid_edges.add((i1, i2))
                valid_edges.add((i0, i2))

            # form 3 edges e_i, e_j, e_k between fully connected subgraph (nodes i0,i1,i2)
            edges = [(i0, i1), (i1, i2), (i0, i2)]
            rot_errors = [two_view_reports[e].R_error_deg for e in edges]
            trans_errors = [two_view_reports[e].U_error_deg for e in edges]
            gt_known = all([err is not None for err in rot_errors])
            # if ground truth unknown, cannot estimate error w.r.t. GT
            max_rot_error = max(rot_errors) if gt_known else None
            max_trans_error = max(trans_errors) if gt_known else None
            
            support_per_triplet.append(support)
            reproj_error_per_triplet.append(np.nanmedian(reproj_errors))
            max_gt_rot_error_in_cycle.append(max_rot_error)
            max_gt_trans_error_in_cycle.append(max_trans_error)

        self.__save_plots(
            support_per_triplet,
            reproj_error_per_triplet,
            max_gt_rot_error_in_cycle,
            max_gt_trans_error_in_cycle,
        )
        return valid_edges

    def __save_plots(
        self,
        support_per_triplet: List[float],
        reproj_error_per_triplet: List[float],
        max_gt_rot_error_in_cycle: List[float],
        max_gt_trans_error_in_cycle: List[float],
    ) -> None:
        """Save information about proxy error metric vs. GT error metric."""

        plt.scatter(
            support_per_triplet,
            max_gt_rot_error_in_cycle,
            10,
            color="g",
            marker=".",
            label="max rotation error in cycle"
        )
        plt.scatter(
            support_per_triplet,
            max_gt_trans_error_in_cycle,
            10,
            color="r",
            marker=".",
            label="max translation error in cycle"
        )
        plt.xlabel("Support (#Inliers)")
        plt.ylabel("GT Angular Error")
        plt.legend(loc="upper right")
        plt.savefig(os.path.join("plots", "gt_error_vs_support.jpg"), dpi=500)
        plt.close("all")

        plt.scatter(
            reproj_error_per_triplet,
            max_gt_rot_error_in_cycle,
            10,
            color="g",
            marker=".",
            label="max rotation error in cycle"
        )
        plt.scatter(
            reproj_error_per_triplet,
            max_gt_trans_error_in_cycle,
            10,
            color="r",
            marker=".",
            label="max translation error in cycle"
        )
        plt.xlabel("Reprojection Error")
        plt.ylabel("GT Angular Error")
        plt.legend(loc="upper right")
        plt.savefig(os.path.join("plots", "gt_error_vs_reproj_error.jpg"), dpi=500)

    def optimize_three_views_averaging(
        self,
        i2Ri1_dict: Dict[Tuple[int, int], Rot3],
        i2Ui1_dict: Dict[Tuple[int, int], Unit3],
        calibrations: List[Cal3Bundler],
        corr_idxs_i1_i2: Dict[Tuple[int, int], np.ndarray],
        keypoints_list: List[Keypoints],
        cameras_gt: Optional[List[PinholeCameraCal3Bundler]] = None,
    ) -> Tuple[List[Optional[Pose3]], np.ndarray, Optional[GtsfmMetricsGroup], GtsfmMetricsGroup, GtsfmMetricsGroup]:
        """Use 3-view averaging to estimate global camera poses, and then compute per-point reprojection errors.

        Args:
            i2Ri1_dict: Dict from (i1, i2) to relative rotation of i1 with respect to i2.
            i2Ui1_dict: Dict from (i1, i2) to relative translation direction of i1 with respect to i2.
            calibrations: list of calibrations for each image.
            corr_idxs_i1i2: Dict from (i1, i2) to indices of verified correspondences from i1 to i2.
            keypoints_list: keypoints for each images.
            cameras_gt: cameras with GT intrinsics and GT extrinsics.

        Returns:
            wTi_list: estimated camera poses.
            reproj_errors: reprojection errors.
            ra_metrics: 3-view rotation averaging metrics.
            ta_metrics: 3-view translation averaging metrics.
            ba_metrics: 3-view bundle adjustment metrics.
        """
        if cameras_gt is not None:
            gt_wTi_list = [cam.pose() for cam in cameras_gt]
        else:
            gt_wTi_list = None

        # num_images is arbitrary, as long as it is larger than the largest key
        num_images = max([max(i1, i2) for (i1, i2) in i2Ri1_dict.keys()]) + 1
        wRi_list = self._rot_avg_module.run(num_images, i2Ri1_dict)

        wti_list, ta_metrics = self._trans_avg_module.run(num_images, i2Ui1_dict, wRi_list, gt_wTi_list=gt_wTi_list)

        if gt_wTi_list:
            ra_metrics = metrics_utils.compute_global_rotation_metrics(wRi_list, wti_list, gt_wTi_list)
        else:
            ra_metrics = None

        init_cameras_dict = multi_view_optimizer.init_cameras(wRi_list, wti_list, calibrations)
        ba_input_data, _ = self._data_association_module.run(
            num_images=num_images,
            cameras=init_cameras_dict,
            corr_idxs_dict=corr_idxs_i1_i2,
            keypoints_list=keypoints_list,
            images=None,
            cameras_gt=cameras_gt,
        )

        unfiltered_data, filtered_data = self._bundle_adjustment_module.run(ba_input_data)
        reproj_errors = unfiltered_data.get_scene_reprojection_errors()
        wTi_list = unfiltered_data.get_camera_poses()

        ba_metrics = self._bundle_adjustment_module.evaluate(unfiltered_data, filtered_data, cameras_gt)
        return wTi_list, reproj_errors, ra_metrics, ta_metrics, ba_metrics

    def optimize_three_views_incremental(self) -> None:
        """
        Estimate global camera poses for 3-views using PNP / absolute pose estimation.
        """
        import pycolmap

        reconstructions = pycolmap.incremental_mapping(
            database_path, image_dir, models_path, num_threads=min(multiprocessing.cpu_count(), 16)
        )
