"""Class to store metrics computed in different GTSfM modules.

Authors: Akshay Krishnan
"""
from __future__ import annotations

import json
from enum import Enum
from typing import Any, Dict, List, Union

import numpy as np

import gtsfm.utils.io as io

"""Keys to access data in the dictionary of the metric, or the JSON file. 
   If metric is a distribution, it is saved to JSON in the below format: 
   metric_name: {
       DATA_KEY: {
           .. raw data if stored ..
       }
       SUMMARY_KEY: {
            .. summary (stats) of distribution ..    
       }
   }
   If the metric is scalar, it is stored simply as {metric_name: value}. 
"""
DATA_KEY = "full_data"
SUMMARY_KEY = "summary"


class GtsfmMetric:
    """Class to store a metric computed in a GTSfM module."""

    class PlotType(Enum):
        BAR = 1  # For scalars
        BOX = 2  # For 1D distributions
        HISTOGRAM = 3  # For 1D distributions

    def __init__(
        self,
        name: str,
        data: Optional[Union[np.array, float, List[Union[int, float]]]] = None,
        summary: Optional[Dict[str, Any]] = None,
        store_full_data: bool = True,
        plot_type: PlotType = None,
    ) -> GtsfmMetric:
    """Creates a GtsfmMetric. 
       Args: 
            name: name of the metric
            data: All values of the metric, optional, uses summary if not provided.
            summary: A summary dict of the metric, generated previously using the same class. 
                     Has to be provided if data = None.
            store_full_data: Whether all the values have to be stored and saved or only summary is required. True by default.
            plot_type: The plot to use for visualization of the metric. 
                       Defaults:
                          PlotType.BAR if data is a scalar
                          PlotType.BOX if data is a distribution (other option is PlotType.HISTOGRAM)
                        It is inferred from the summary if plot_type is not provided and summary is.
    """
        if summary is None and data is None:
            raise ValueError("Data and summary cannot both be None.")

        self._name = name
        if data is not None:
            # Cast to a numpy array
            if not isinstance(data, np.ndarray):
                data = np.array(data)
            if data.ndim > 1:
                raise ValueError("Metrics must be scalars on 1D-distributions.")

            # Save dimension and plot_type for data
            self._dim = data.ndim
            plot_types_for_dim = self._get_plot_types_for_dim(self._dim)
            if plot_type is None:
                if summary is not None:
                    self._plot_type = self.PlotType.HISTOGRAM if "histogram" in summary else self.PlotType.BOX
                else:
                    self._plot_type = plot_types_for_dim[0]
            elif plot_type in plot_types_for_dim:
                self._plot_type = plot_type
            else:
                raise ValueError("Unsupported plot type for the data dimension")

            # Create a summary if the data is a 1D distribution
            if self._dim == 1:
                self._summary = self._create_summary(data)

            # Store full data only if its a scalar or if asked to.
            if self._dim == 0 or store_full_data:
                self._data = data
            else:
                self._data = None
        else:
            self._dim = 1
            self._summary = summary
            self._plot_type = self.PlotType.HISTOGRAM if "histogram" in summary else self.PlotType.BOX
            self._data = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def data(self) -> np.array:
        return self._data

    @property
    def plot_type(self) -> PlotType:
        return self._plot_type

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def summary(self) -> Dict[str, Any]:
        return self._summary

    def _get_plot_types_for_dim(self, dim: int) -> List[PlotType]:
        if dim == 0:
            return [self.PlotType.BAR]
        if dim == 1:
            return [self.PlotType.BOX, self.PlotType.HISTOGRAM]
        return []

    def _get_distribution_histogram(self, data: np.ndarray) -> Dict[str, Union[float, int]]:
        """Returns the histogram of data as a dictionary.

        If the data is float, the keys of the dictionary are interval buckets, and if the data is int, the keys are also int.

        Args:
            data: 1D array of all values of the metric

        Returns:
            Histogram of data as a dict from bucket to count.
        """
        if data.size == 0:
            print("Requested histogram for empty data metric, returning None.")
            return None
        if isinstance(data.tolist()[0], int):
            # One bin for each integer
            bins = int(np.max(data) - np.min(data) + 1)
            discrete = True
        else:
            bins = 10
            discrete = False
        count, bins = np.histogram(data, bins=bins)
        count = count.tolist()
        bins = bins.tolist()
        bins_lower = bins[:-1]
        bins_upper = bins[1:]

        histogram = {}
        for i in range(len(count)):
            if discrete:
                key = str(int(bins_lower[i]))
            else:
                key = "%.2f-%.2f" % (bins_lower[i], bins_upper[i])
            histogram[key] = count[i]
        return histogram

    def _create_summary(self, data: np.ndarray) -> Dict[str, Any]:
        """Creates a summary of the given data.

        This is useful for analysis as data can be very large. The summary is a dict contains the following fields:
            - Min, max, median of data
            - Mean and std dev of data
            - Either quartiles or histogram of the data depending on plot_type of this metric.

        Args:
            data: 1D array of all values of the metric

        Returns:
            summary as a dict that can be serialized to JSON for storage.
        """
        if data.ndim != 1:
            raise ValueError("Metric must be a 1D distribution to get summary.")
        summary = {
            "min": np.min(data).tolist(),
            "max": np.max(data).tolist(),
            "median": np.nanmedian(data).tolist(),
            "mean": np.nanmean(data).tolist(),
            "stddev": np.nanstd(data).tolist(),
        }
        if self._plot_type == self.PlotType.BOX:
            summary.update({"quartiles": self._get_distribution_quartiles(data)})
        elif self._plot_type == self.PlotType.HISTOGRAM:
            summary.update({"histogram": self._get_distribution_histogram(data)})
        return summary

    def _get_distribution_quartiles(self, data: np.ndarray) -> Dict[int, float]:
        """Computes quartiles for the provided 1D data distribution.

        Args:
            data: 1D distribution of metric values

        Returns:
            Quartiles of the data as a dict where keys are q0, q1, q2, q3, and q4
        """
        query = list(range(0, 101, 25))
        quartiles = np.percentile(data, query)
        output = {}
        for i, q in enumerate(query):
            output["q" + str(i)] = quartiles[i].tolist()
        return output

    def get_metric_as_dict(self) -> Dict[str, Any]:
        """Provides a dict based representation of the metric that can be serialized to JSON.

        The dict contains a single element, for which the key is the name of the metric.
        If metric is a distribution, the dict is in the below format: 
        {
            metric_name: {
               DATA_KEY: {
                   .. raw data if stored ..
               }
               SUMMARY_KEY: {
                    .. summary (stats) of distribution ..    
               }
            }
        }
        If the metric is scalar, it is stored simply as {metric_name: value}. 

        Returns:
            The metric as a dict representation explained above.
        """
        if self._dim == 0:
            return {self._name: self._data.tolist()}

        metric_dict = {SUMMARY_KEY: self.summary}
        if self._data is not None:
            metric_dict[DATA_KEY] = self._data.tolist()
        return {self._name: metric_dict}

    def save_to_json(self, json_filename: str):
        """Saves this metric's dict representation to a JSON file.

        Args:
            Path to the json file.
        """
        io.save_json_file(json_filename, self.get_metric_as_dict())

    @classmethod
    def parse_from_dict(cls, metric_dict: Dict[str, Any]) -> GtsfmMetric:
        """Creates a GtsfmMetric by parsing a dict representation.

        It is assumed that the dict representation is the format created by GtsfmMetric.

        Args:
            metric_dict: Dict representation of the metric.

        Returns:
            Parsed GtsfmMetric instance.
        """
        if len(metric_dict) != 1:
            raise AttributeError("Input metric dict should have a single key-value pair.")

        metric_name = list(metric_dict.keys())[0]
        metric_value = metric_dict[metric_name]

        # 1D distribution metrics
        if isinstance(metric_value, dict):
            data = None
            summary = None
            if DATA_KEY in metric_value:
                data = metric_value[DATA_KEY]
            if SUMMARY_KEY in metric_value:
                summary = metric_value[SUMMARY_KEY]
            return cls(metric_name, data=data, summary=summary)

        # Scalar metrics
        return cls(metric_name, metric_value)


class GtsfmMetricsGroup:
    """Stores GtsfmMetrics from the same module. """

    def __init__(self, name: str, metrics: List[GtsfmMetric]) -> GtsfmMetricsGroup:
        self._name = name
        self._metrics = metrics

    @property
    def name(self) -> str:
        return self._name

    @property
    def metrics(self) -> List[GtsfmMetric]:
        return self._metrics

    def add_metric(self, metric: GtsfmMetric):
        self._metrics.append(metric)

    def add_metrics(self, metrics: List[GtsfmMetric]):
        self._metrics.extend(metrics)

    def extend(self, metrics_group: GtsfmMetricsGroup):
        self._metrics.extend(metrics_group.metrics)

    def get_metrics_as_dict(self) -> Dict[str, Dict[str, Any]]:
        """Creates the dictionary representation of the metrics group. 

        This is the below format:
        {
            metrics_group_name: {
                metric1_name: metric1_dict
                metric2_name: metric2_dict
                ...
            }
        }

        Returns:
            metrics group dictionary representation.
        """
        metrics_dict = {}
        for metric in self._metrics:
            metrics_dict.update(metric.get_metric_as_dict())
        return {self._name: metrics_dict}

    def save_to_json(self, path: str):
        """Saves the dictionary representation of the metrics group to json.
        
        Args:
            path: path to json file.
        """
        io.save_json_file(path, self.get_metrics_as_dict())

    @classmethod
    def parse_from_dict(cls, metrics_group_dict: Dict[str, Any]) -> GtsfmMetricsGroup:
        """Creates a metric group from its dictionary representation. 
        
        Args: 
            metrics_group_dict: Dictionary representation generated by get_metrics_as_dict().

        Returns: 
            A new GtsfmMetricsGroup parsed from the dict.
        """
        if len(metrics_group_dict) != 1:
            raise AttributeError("Metrics group dict must have a single key-value pair.")
        metrics_group_name = list(metrics_group_dict.keys())[0]
        metrics_dict = metrics_group_dict[metrics_group_name]
        gtsfm_metrics_list = []
        for metric_name, metric_value in metrics_dict.items():
            gtsfm_metrics_list.append(GtsfmMetric.parse_from_dict({metric_name: metric_value}))
        return GtsfmMetricsGroup(metrics_group_name, gtsfm_metrics_list)

    @classmethod
    def parse_from_json(cls, json_filename: str) -> GtsfmMetricsGroup:
        """Loads the JSON file that contains the metrics group represented as dict and parses it.

        Args:
            json_filename: Path to the JSON file.
        Returns:
            A new GtsfmMetricsGroup parsed from the JSON.
        """
        with open(json_filename) as f:
            metric_group_dict = json.load(f)
        return cls.parse_from_dict(metric_group_dict)