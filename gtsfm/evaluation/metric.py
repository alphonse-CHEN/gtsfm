"""Class to store metrics computed in different GTSfM modules.

Authors: Akshay Krishnan
"""
from __future__ import annotations

import numpy as np
from enum import Enum
from typing import Any, Dict, List, Union

import gtsfm.utils.io as io

DATA_KEY = "full_data"
SUMMARY_KEY = "summary"

class GtsfmMetric:
    """Class to store a metric computed in a GTSfM module."""

    class PlotType(Enum):
        BAR = 1         # For scalars
        BOX = 2         # For 1D distributions
        HISTOGRAM = 3   # For 1D distributions

    def _get_plot_types_for_dim(self, dim) -> List[PlotType]:
        if dim == 0:
            return [self.PlotType.BAR]
        if dim == 1:
            return [self.PlotType.BOX, self.PlotType.HISTOGRAM]
        return []

    def __init__(self, name: str, data: Union[np.array, float], plot_type: PlotType = None):
        if not isinstance(data, np.ndarray):
            data = np.array(data)
        if data.ndim > 1:
            raise ValueError('Metrics must be scalars on 1D-distributions.')
        self._name = name
        self._data = data
        self._dim = data.ndim

        plot_types_for_dim = self._get_plot_types_for_dim(self._dim)
        if plot_type is None:
            self._plot_type = plot_types_for_dim[0]
        else:
            if plot_type in plot_types_for_dim:
                self._plot_type = plot_type
            else:
                raise ValueError('Unsupported plot type for the data dimension')

    @property
    def name(self) -> str: 
        return self._name

    @property
    def data(self) -> np.array:
        return self._data

    @property
    def plot_type(self):
        return self._plot_type

    def get_distribution_percentiles(self) -> Dict[int, float]:
        query = list(range(0, 101, 10))
        percentiles = np.percentile(self._data, query)
        output = {}
        for i, q in enumerate(query):
            output[q] = percentiles[i]
        return output

    def get_summary_dict(self) -> Dict[str, Any]:
        if self._dim == 0:
            return {self._name: self._data}
        return {
            "min": np.min(self._data),
            "max": np.max(self._data),
            "median": np.median(self._data),
            "mean": np.mean(self._data),
            "stddev": np.std(self._data),
            "percentiles": self.get_distribution_percentiles(),
        }

    def get_metric_as_dict(self) -> Dict[str, Any]:
        if self._dim == 0:
            return self.get_summary_dict()

        return {
            self._name: {
                SUMMARY_KEY: self.get_summary_dict(),
                DATA_KEY: list(self._data),
            }
        }

    def save_to_json(self, json_filename):
        io.save_to_json(self.get_metric_as_dict())

    @classmethod
    def parse_from_dict(cls, metric_dict: Dict[str, Any]) -> GtsfmMetric:
        if len(metric_dict) != 1:
            raise AttributeError("Input metric dict should have a single key-value pair.")
        metric_name = list(metric_dict.keys())[0]
        metric_value = metric_dict[metric_name]
        
        # 1D distribution metrics
        if isinstance(metric_value, dict):
            if not DATA_KEY in metric_value:
                raise AttributeError('Unable to parse metrics dict: missing data field.')
            return cls(metric_name, metric_value[DATA_KEY])

        # Scalar metrics
        return cls(metric_name, metric_value)


class GtsfmMetricsGroup:
    """Stores GtsfmMetrics from the same module. """
    def __init__(self, name: str, metrics: List[GtsfmMetric]):
        self._name = name
        self._metrics = metrics

    @property
    def name(self):
        return self._name

    @property
    def metrics(self):
        return self._metrics

    def get_metrics_as_dict(self) -> Dict[str, Dict[str, Any]]:
        metrics_dict = {}
        for metric in self._metrics:
            metrics_dict.update(metric.get_metric_as_dict())
        return {self._name: metrics_dict}

    def save_to_json(self):
        io.save_to_json(self.get_metrics_as_dict())

    @classmethod
    def parse_from_dict(cls, metrics_group_dict) -> GtsfmMetricsGroup:
        if(len(metrics_dict) != 1):
            raise AttributeError('Metrics group dict must have a single key-value pair.')
        metrics_group_name = list(metrics_group_dict.keys())[0]
        metrics_dict = metrics_group_dict[metrics_group_name]
        gtsfm_metrics_list = []
        for metric_name, metric_value in metrics_dict:
            gtsfm_metrics_list.append(GtsfmMetric.parse_from_dict({metric_name: metric_value}))
        return GtsfmMetricsGroup(metrics_group_name, gtsfm_metrics_list)

    @classmethod
    def parse_from_json(cls, json_filename):            
        with open(json_filename) as f:
            metric_group_dict = json.load(f)
        return cls.parse_from_dict(metric_group_dict)