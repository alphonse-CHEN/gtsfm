"""Correspondence generator that utilizes direct matching of keypoints across an image pair, without descriptors.

Authors: John Lambert
"""

from typing import Dict, List, Tuple

from dask.delayed import Delayed
import numpy as np

from gtsfm.common.keypoints import Keypoints
from gtsfm.frontend.correspondence_generator.correspondence_generator.base import CorrespondenceGeneratorBase
from gtsfm.frontend.correspondence_generator.keypoint_aggregator.keypoint_aggregator_dedup import (
    KeypointAggregatorDedup,
)
from gtsfm.frontend.correspondence_generator.keypoint_aggregator.keypoint_aggregator_unique import (
    KeypointAggregatorUnique,
)
from gtsfm.frontend.matcher.image_matcher_base import ImageMatcherBase


class QuadraticImageCorrespondenceGenerator(CorrespondenceGeneratorBase):
    """Pair-wise direct matching of images (e.g. transformer-based)."""

    def __init__(self, matcher: ImageMatcherBase, deduplicate: bool = True) -> None:
        """
        Args:
            matcher: matcher to use.
            deduplicate: whether to de-duplicate with a single image the detections received from each image pair.
        """
        self._matcher = matcher

        if deduplicate:
            self._aggregator = KeypointAggregatorDedup()
        else:
            self._aggregator = KeypointAggregatorUnique()

    def create_computation_graph(
        self,
        image_graph: List[Delayed],
        image_shapes: List[Tuple[int, int]],
        image_pair_indices: List[Tuple[int, int]],
    ) -> Tuple[List[Keypoints], Dict[Tuple[int, int], np.ndarray]]:
        """Create Dask computation graph for correspondence generation.

        Args:
            image_graph: list of N images.
            image_shapes: list of N image shapes, as tuples (height,width) in pixels.
            image_pair_indices: list of image pairs, each represented by a tuple (i1,i2).

        Return:
            delayed_keypoints: list of keypoints, for each image.
            delayed_putative_corr_idxs_dict: mapping from image pair (i1,i2) to putative correspondence indices.
              Correspondence indices are represented by an array of shape (K,2), for K correspondences.
        """
        delayed_keypoints_dict = {}
        delayed_putative_corr_idxs_dict = {}

        for (i1, i2) in image_pair_indices:
            delayed_dets_i1, delayed_dets_i2 = self._matcher.create_computation_graph(image_graph[i1], image_graph[i2])
            delayed_keypoints_dict[i1, i2] = (delayed_dets_i1, delayed_dets_i2)

        # combine the keypoints into massive arrays, aggregated from over all pairs.
        delayed_keypoints, delayed_putative_corr_idxs_dict = self._aggregator.create_computation_graph(
            delayed_keypoints_dict
        )

        return delayed_keypoints, delayed_putative_corr_idxs_dict
