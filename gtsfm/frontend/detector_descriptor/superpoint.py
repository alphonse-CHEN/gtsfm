"""Superpoint detector+descriptor implementation.

The network was proposed in 'SuperPoint: Self-Supervised Interest Point Detection and Description' and is implemented
by wrapping over the authors' implementation.

References:
- https://arxiv.org/abs/1712.07629
- https://github.com/magicleap/SuperGluePretrainedNetwork

Authors: Ayush Baid
"""
from pathlib import Path
from typing import Tuple, Union

import numpy as np
import torch

import gtsfm.utils.images as image_utils
import gtsfm.utils.logger as logger_utils
from gtsfm.common.image import Image
from gtsfm.common.keypoints import Keypoints
from gtsfm.frontend.detector_descriptor.detector_descriptor_base import DetectorDescriptorBase
from thirdparty.SuperGluePretrainedNetwork.models.superpoint import SuperPoint

logger = logger_utils.get_logger()
ROOT_PATH = Path(__file__).resolve().parent.parent.parent.parent
MODEL_WEIGHTS_PATH = (
    ROOT_PATH / "thirdparty" / "SuperGluePretrainedNetwork" / "models" / "weights" / "superpoint_v1.pth"
)


class SuperPointDetectorDescriptor(DetectorDescriptorBase):
    """Superpoint Detector+Descriptor implementation."""

    def __init__(self, use_cuda: bool = True, weights_path: Union[Path, str] = MODEL_WEIGHTS_PATH) -> None:
        """Configures the object.

        Args:
            max_keypoints: max keypoints to detect in an image.
            use_cuda (optional): flag controlling the use of GPUs via CUDA. Defaults to True.
            weights_path (optional): Path to the model weights. Defaults to MODEL_WEIGHT_PATH.
        """
        super().__init__()
        self._use_cuda = use_cuda and torch.cuda.is_available()
        self._config = {"weights_path": weights_path}

    def detect_and_describe(self, image: Image) -> Tuple[Keypoints, np.ndarray]:
        """Jointly generate keypoint detections and their associated descriptors from a single image."""
        device = torch.device("cuda" if self._use_cuda else "cpu")
        model = SuperPoint(self._config).to(device)
        model.eval()
        # TODO: fix inference issue #110

        # Compute features.
        image_tensor = torch.from_numpy(
            np.expand_dims(image_utils.rgb_to_gray_cv(image).value_array.astype(np.float32) / 255.0, (0, 1))
        ).to(device)
        with torch.no_grad():
            model_results = model({"image": image_tensor})
        torch.cuda.empty_cache()

        # Unpack results.
        coordinates = model_results["keypoints"][0].detach().cpu().numpy()
        scores = model_results["scores"][0].detach().cpu().numpy()
        keypoints = Keypoints(coordinates, scales=None, responses=scores)
        descriptors = model_results["descriptors"][0].detach().cpu().numpy().T

        # Filter by image mask.
        logger.info("HERE")
        logger.info(image.mask)
        assert False
        if image.mask is not None:
            logger.debug("Filtering by mask.")
            keypoints, descriptors = self.filter_by_mask(image.mask, keypoints, descriptors)

        # Filter by scores.
        keypoints, descriptors = self.filter_by_response(keypoints, descriptors)

        return keypoints, descriptors
