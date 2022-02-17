import json
import warnings
from abc import ABC
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Union, Dict, Type, List, Tuple, Generator

import tensorflow as tf
import torch

from nebullvm.config import NVIDIA_FILENAMES, NO_COMPILER_INSTALLATION
from nebullvm.inference_learners.base import (
    BaseInferenceLearner,
    LearnerMetadata,
    PytorchBaseInferenceLearner,
    TensorflowBaseInferenceLearner,
)
from nebullvm.base import ModelParams, DeepLearningFramework

if torch.cuda.is_available():
    try:
        import tensorrt as trt
        import polygraphy
    except ImportError:
        if not NO_COMPILER_INSTALLATION:
            from nebullvm.installers.installers import install_tensor_rt

            warnings.warn(
                "No TensorRT valid installation has been found. "
                "Trying to install it from source."
            )
            install_tensor_rt()
            import tensorrt as trt
            import polygraphy
        else:
            warnings.warn(
                "No TensorRT valid installation has been found. "
                "It won't be possible to use it in the following steps."
            )


@dataclass
class NvidiaInferenceLearner(BaseInferenceLearner, ABC):
    engine: Any
    input_names: List[str]
    output_names: List[str]
    cuda_stream: Any = None
    nvidia_logger: Any = None

    def _get_metadata(self, **kwargs) -> LearnerMetadata:
        metadata = {
            key: self.__dict__[key] for key in ("input_names", "output_names")
        }
        metadata.update(kwargs)
        return LearnerMetadata.from_model(self, **metadata)

    def _synchronize_stream(self):
        raise NotImplementedError()

    @property
    def stream_ptr(self):
        raise NotImplementedError()

    @staticmethod
    def _get_default_cuda_stream() -> Any:
        raise NotImplementedError()

    @staticmethod
    def check_env():
        if not torch.cuda.is_available():
            raise SystemError(
                "You are trying to run an optimizer developed for NVidia gpus "
                "on a machine not connected to any GPU supporting CUDA."
            )

    def __post_init__(self):
        self.check_env()
        if self.nvidia_logger is None:
            self.nvidia_logger = trt.Logger(trt.Logger.WARNING)
        if self.cuda_stream is None:
            self.cuda_stream = self._get_default_cuda_stream()

    @classmethod
    def from_engine_path(
        cls,
        network_parameters: ModelParams,
        engine_path: Union[str, Path],
        input_names: List[str],
        output_names: List[str],
        nvidia_logger: Any = None,
        cuda_stream: Any = None,
        **kwargs,
    ):
        if kwargs:
            warnings.warn(
                f"Debug: Got extra keywords in "
                f"NvidiaInferenceLearner::from_engine_path: {kwargs}"
            )
        if nvidia_logger is None:
            nvidia_logger = trt.Logger(trt.Logger.WARNING)
        runtime = trt.Runtime(nvidia_logger)
        with open(engine_path, "rb") as f:
            serialized_engine = f.read()
        engine = runtime.deserialize_cuda_engine(serialized_engine)
        return cls(
            network_parameters=network_parameters,
            engine=engine,
            input_names=input_names,
            output_names=output_names,
            nvidia_logger=nvidia_logger,
            cuda_stream=cuda_stream,
        )

    def _predict_tensors(
        self,
        input_ptrs: Generator[Any, None, None],
        output_ptrs: Generator[Any, None, None],
    ):
        context = self.engine.create_execution_context()
        buffers = [None] * (len(self.input_names) + len(self.output_names))
        input_idxs = (
            self.engine[input_name] for input_name in self.input_names
        )
        output_idxs = (
            self.engine[output_name] for output_name in self.output_names
        )
        for input_idx, input_ptr in zip(input_idxs, input_ptrs):
            buffers[input_idx] = input_ptr
        for output_idx, output_ptr in zip(output_idxs, output_ptrs):
            buffers[output_idx] = output_ptr
        context.execute_async_v2(buffers, self.stream_ptr)
        self._synchronize_stream()

    def save(self, path: Union[str, Path], **kwargs):
        path = Path(path)
        serialized_engine = self.engine.serialize()
        with open(path / NVIDIA_FILENAMES["engine"], "wb") as fout:
            fout.write(serialized_engine)
        metadata = self._get_metadata(**kwargs)
        with open(path / NVIDIA_FILENAMES["metadata"], "w") as fout:
            json.dump(metadata.to_dict(), fout)

    @classmethod
    def load(cls, path: Union[Path, str], **kwargs):
        with open(path / NVIDIA_FILENAMES["metadata"], "r") as fin:
            metadata = json.load(fin)
        metadata.update(kwargs)
        metadata["network_parameters"] = ModelParams(
            **metadata["network_parameters"]
        )
        return cls.from_engine_path(
            path / NVIDIA_FILENAMES["engine"], **metadata
        )


class PytorchNvidiaInferenceLearner(
    NvidiaInferenceLearner, PytorchBaseInferenceLearner
):
    def _synchronize_stream(self):
        self.cuda_stream.synchronize()

    @staticmethod
    def _get_default_cuda_stream() -> Any:
        return torch.cuda.default_stream()

    @property
    def stream_ptr(self):
        return self.cuda_stream.cuda_stream

    def predict(self, *input_tensors: torch.Tensor) -> Tuple[torch.Tensor]:
        input_tensors = [input_tensor.cuda() for input_tensor in input_tensors]
        output_tensors = [
            torch.Tensor(
                self.network_parameters.batch_size,
                *output_size,
            ).cuda()
            for output_size in self.network_parameters.output_sizes
        ]
        input_ptrs = (
            input_tensor.data_ptr() for input_tensor in input_tensors
        )
        output_ptrs = (
            output_tensor.data_ptr() for output_tensor in output_tensors
        )
        self._predict_tensors(input_ptrs, output_ptrs)
        return tuple(output_tensor.cpu() for output_tensor in output_tensors)


class TensorflowNvidiaInferenceLearner(
    NvidiaInferenceLearner, TensorflowBaseInferenceLearner
):
    def _synchronize_stream(self):
        self.cuda_stream.synchronize()

    @staticmethod
    def _get_default_cuda_stream() -> Any:
        return polygraphy.Stream()

    @property
    def stream_ptr(self):
        return self.cuda_stream.ptr

    def predict(self, *input_tensors: tf.Tensor) -> Tuple[tf.Tensor]:
        cuda_input_arrays = [
            polygraphy.DeviceArray.copy_from(
                input_tensor.numpy(), stream=self.cuda_stream
            )
            for input_tensor in input_tensors
        ]
        cuda_output_arrays = [
            polygraphy.DeviceArray(
                shape=(self.network_parameters.batch_size, *output_size)
            )
            for output_size in self.network_parameters.output_sizes
        ]
        input_ptrs = (cuda_array.ptr for cuda_array in cuda_input_arrays)
        output_ptrs = (cuda_array.ptr for cuda_array in cuda_output_arrays)
        self._predict_tensors(input_ptrs, output_ptrs)
        for cuda_input_array in cuda_input_arrays:
            cuda_input_array.free()
        return tuple(
            self._convert_to_array_and_free_memory(array)
            for array in cuda_output_arrays
        )

    @staticmethod
    def _convert_to_array_and_free_memory(cuda_array) -> tf.Tensor:
        array = cuda_array.numpy()
        cuda_array.free()
        return array


NVIDIA_INFERENCE_LEARNERS: Dict[
    DeepLearningFramework, Type[NvidiaInferenceLearner]
] = {
    DeepLearningFramework.PYTORCH: PytorchNvidiaInferenceLearner,
    DeepLearningFramework.TENSORFLOW: TensorflowNvidiaInferenceLearner,
}
