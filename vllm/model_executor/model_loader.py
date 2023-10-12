"""Utilities for selecting and loading models."""
import contextlib
from functools import partial
from typing import Type

import torch
import torch.nn as nn
from transformers import PretrainedConfig
from tensorizer.utils import get_mem_usage, no_init_or_tensor

from vllm.config import ModelConfig
from vllm.model_executor.models import *  # pylint: disable=wildcard-import
from vllm.model_executor.weight_utils import (get_quant_config,
                                              initialize_dummy_weights)

# TODO(woosuk): Lazy-load the model classes.
_MODEL_REGISTRY = {
    "AquilaModel": AquilaForCausalLM,
    "AquilaForCausalLM": AquilaForCausalLM,  # AquilaChat2
    "BaiChuanForCausalLM": BaiChuanForCausalLM,  # baichuan-7b
    "BaichuanForCausalLM": BaichuanForCausalLM,  # baichuan-13b
    "BloomForCausalLM": BloomForCausalLM,
    "FalconForCausalLM": FalconForCausalLM,
    "GPT2LMHeadModel": GPT2LMHeadModel,
    "GPTBigCodeForCausalLM": GPTBigCodeForCausalLM,
    "GPTJForCausalLM": GPTJForCausalLM,
    "GPTNeoXForCausalLM": GPTNeoXForCausalLM,
    "InternLMForCausalLM": InternLMForCausalLM,
    "LlamaForCausalLM": LlamaForCausalLM,
    "LLaMAForCausalLM": LlamaForCausalLM,  # For decapoda-research/llama-*
    "MistralForCausalLM": MistralForCausalLM,
    # transformers's mpt class has lower case
    "MptForCausalLM": MptForCausalLM,
    "MPTForCausalLM": MptForCausalLM,
    "OPTForCausalLM": OPTForCausalLM,
    "QWenLMHeadModel": QWenLMHeadModel,
    "RWForCausalLM": FalconForCausalLM,
}

# FIXME(woosuk): Remove this once all models support quantization.
_MODEL_CLASSES_SUPPORT_QUANTIZATION = [
    LlamaForCausalLM,
    MistralForCausalLM,
]

# FIXME(npratt): Remove this once all models support tensorizer.
_MODEL_CLASSES_SUPPORT_TENSORIZER = [
    MistralForCausalLM
]


@contextlib.contextmanager
def _set_default_torch_dtype(dtype: torch.dtype):
    """Sets the default torch dtype to the given dtype."""
    old_dtype = torch.get_default_dtype()
    torch.set_default_dtype(dtype)
    yield
    torch.set_default_dtype(old_dtype)


def _get_model_architecture(config: PretrainedConfig) -> Type[nn.Module]:
    architectures = getattr(config, "architectures", [])
    for arch in architectures:
        if arch in _MODEL_REGISTRY:
            return _MODEL_REGISTRY[arch]
    raise ValueError(
        f"Model architectures {architectures} are not supported for now. "
        f"Supported architectures: {list(_MODEL_REGISTRY.keys())}")


def get_model(model_config: ModelConfig) -> nn.Module:
    model_class = _get_model_architecture(model_config.hf_config)

    if model_config.load_format == "tensorizer" and model_class not in _MODEL_CLASSES_SUPPORT_TENSORIZER:
        raise ValueError(f"Tensorizer is not supported for {model_class}.")

    # Get the quantization config.
    quant_config = None
    if model_config.quantization is not None:
        if model_class not in _MODEL_CLASSES_SUPPORT_QUANTIZATION:
            raise ValueError(
                f"Quantization is not supported for {model_class}.")
        quant_config = get_quant_config(model_config.quantization,
                                        model_config.model,
                                        model_config.download_dir)
        capability = torch.cuda.get_device_capability()
        capability = capability[0] * 10 + capability[1]
        if capability < quant_config.get_min_capability():
            raise ValueError(
                f"The quantization method {model_config.quantization} is not "
                "supported for the current GPU. "
                f"Minimum capability: {quant_config.get_min_capability()}. "
                f"Current capability: {capability}.")
        supported_dtypes = quant_config.get_supported_act_dtypes()
        if model_config.dtype not in supported_dtypes:
            raise ValueError(
                f"{model_config.dtype} is not supported for quantization "
                f"method {model_config.quantization}. Supported dtypes: "
                f"{supported_dtypes}")

    print(f"Memory usage before model creation: {get_mem_usage()}")
    with _set_default_torch_dtype(model_config.dtype):
        # Create a model instance.
        if model_class in _MODEL_CLASSES_SUPPORT_QUANTIZATION:
            model_func = partial(model_class, model_config.hf_config, quant_config)
        else:
            model_func = partial(model_class, model_config.hf_config)

        if model_config.load_format == "tensorizer":
            model = no_init_or_tensor(lambda: model_func())
        else:
            model = model_func()

        # Load the weights from the cached or downloaded files.
        if model_config.load_format == "dummy":
            model = model.cuda()
            # NOTE(woosuk): For accurate performance evaluation, we assign
            # random values to the weights.
            initialize_dummy_weights(model)
        else:
            # Load the weights from the cached or downloaded files.
            model.load_weights(model_config.model, model_config.download_dir,
                               model_config.load_format, model_config.revision,
                               model_config.tensorizer_path)
            model = model.cuda()
    return model.eval()
