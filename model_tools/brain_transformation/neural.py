import logging
from collections import Iterable
from typing import Optional, Union

from tqdm import tqdm

from brainscore.metrics import Score
from brainscore.model_interface import BrainModel
from brainscore.utils import fullname
from model_tools.activations.pca import LayerPCA
from model_tools.brain_transformation import TemporalIgnore
from result_caching import store_xarray, store


class LayerMappedModel(BrainModel):
    def __init__(self, identifier, activations_model, region_layer_map: Optional[dict] = None):
        self.identifier = identifier
        self.activations_model = activations_model
        self.region_layer_map = region_layer_map or {}
        self.recorded_regions = []

    def look_at(self, stimuli):
        layer_regions = {}
        for region in self.recorded_regions:
            layers = self.region_layer_map[region]
            if not isinstance(layers, Iterable) or isinstance(layers, (str, bytes)):
                layers = [layers]
            for layer in layers:
                assert layer not in layer_regions, f"layer {layer} has already been assigned for {layer_regions[layer]}"
                layer_regions[layer] = region
        activations = self.activations_model(stimuli, layers=list(layer_regions.keys()))
        activations['region'] = 'neuroid', [layer_regions[layer] for layer in activations['layer'].values]
        return activations

    def start_task(self, task):
        if task != BrainModel.Task.passive:
            raise NotImplementedError()

    def start_recording(self, recording_target: BrainModel.RecordingTarget):
        if str(recording_target) not in self.region_layer_map:
            raise NotImplementedError(f"Region {recording_target} is not committed")
        self.recorded_regions = [recording_target]

    def commit(self, region: str, layer: Union[str, list, tuple]):
        if isinstance(layer, list):
            layer = tuple(layer)
        self.region_layer_map[region] = layer


class LayerSelection:
    def __init__(self, model_identifier, activations_model, layers):
        """
        :param model_identifier: this is separate from the container name because it only refers to
            the combination of (model, preprocessing), i.e. no mapping.
        """
        self.model_identifier = model_identifier
        self._layer_scoring = LayerScores(model_identifier=model_identifier, activations_model=activations_model)
        self.layers = layers
        self._logger = logging.getLogger(fullname(self))

    def __call__(self, selection_identifier, benchmark):
        # for layer-mapping, attach LayerPCA so that we can cache activations
        model_identifier = self.model_identifier
        pca_hooked = LayerPCA.is_hooked(self._layer_scoring._activations_model)
        if not pca_hooked:
            pca_handle = LayerPCA.hook(self._layer_scoring._activations_model, n_components=1000)
            identifier = self._layer_scoring._activations_model.identifier
            self._layer_scoring._activations_model.identifier = identifier + "-pca_1000"
            model_identifier += "-pca_1000"

        result = self._call(model_identifier=model_identifier, selection_identifier=selection_identifier,
                            benchmark=benchmark)

        if not pca_hooked:
            pca_handle.remove()
            self._layer_scoring._activations_model.identifier = identifier
        return result

    @store(identifier_ignore=['assembly'])
    def _call(self, model_identifier, selection_identifier, benchmark):
        self._logger.debug("Finding best layer")
        layer_scores = self._layer_scoring(benchmark=benchmark, benchmark_identifier=selection_identifier,
                                           layers=self.layers, prerun=True)

        self._logger.debug("Layer scores (unceiled): " + ", ".join([
            f"{layer} -> {layer_scores.raw.sel(layer=layer, aggregation='center').values:.3f}"
            f"+-{layer_scores.raw.sel(layer=layer, aggregation='error').values:.3f}"
            for layer in layer_scores['layer'].values]))
        best_layer = layer_scores['layer'].values[layer_scores.sel(aggregation='center').argmax()]
        return best_layer


class LayerScores:
    def __init__(self, model_identifier, activations_model):
        self.model_identifier = model_identifier
        self._activations_model = activations_model
        self._logger = logging.getLogger(fullname(self))

    def __call__(self, benchmark, layers, benchmark_identifier=None, prerun=False):
        return self._call(model_identifier=self.model_identifier,
                          benchmark_identifier=benchmark_identifier or benchmark.identifier,
                          model=self._activations_model, benchmark=benchmark, layers=layers, prerun=prerun)

    @store_xarray(identifier_ignore=['model', 'benchmark', 'layers', 'prerun'], combine_fields={'layers': 'layer'})
    def _call(self, model_identifier, benchmark_identifier,  # storage fields
              model, benchmark, layers, prerun=False):
        if prerun:
            # pre-run activations together to avoid running every layer separately
            model(layers=layers, stimuli=benchmark._assembly.stimulus_set)

        layer_scores = []
        for layer in tqdm(layers, desc="layers"):
            layer_model = LayerMappedModel(identifier=f"{model_identifier}-{layer}",
                                           # per-layer identifier to avoid overlap
                                           activations_model=model, region_layer_map={benchmark.region: layer})
            layer_model = TemporalIgnore(layer_model)
            score = benchmark(layer_model)
            score = score.expand_dims('layer')
            score['layer'] = [layer]
            layer_scores.append(score)
        layer_scores = Score.merge(*layer_scores)
        layer_scores = layer_scores.sel(layer=layers)  # preserve layer ordering
        return layer_scores
