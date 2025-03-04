# Copyright 2020 - 2021 MONAI Consortium
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
A collection of "vanilla" transforms for intensity adjustment
https://github.com/Project-MONAI/MONAI/wiki/MONAI_Design
"""

import copy
from collections.abc import Iterable
from functools import partial
from typing import Any, Callable, List, Optional, Sequence, Tuple, Union
from warnings import warn

import numpy as np
import torch

from monai.config import DtypeLike
from monai.config.type_definitions import NdarrayOrTensor, NdarrayTensor
from monai.data.utils import get_random_patch, get_valid_patch_size
from monai.networks.layers import GaussianFilter, HilbertTransform, SavitzkyGolayFilter
from monai.transforms.transform import RandomizableTransform, Transform
from monai.transforms.utils import Fourier, equalize_hist, is_positive, rescale_array
from monai.utils import (
    PT_BEFORE_1_7,
    InvalidPyTorchVersionError,
    convert_data_type,
    convert_to_dst_type,
    ensure_tuple,
    ensure_tuple_rep,
    ensure_tuple_size,
    fall_back_tuple,
)
from monai.utils.enums import TransformBackends
from monai.utils.type_conversion import convert_to_tensor, get_equivalent_dtype

__all__ = [
    "RandGaussianNoise",
    "RandRicianNoise",
    "ShiftIntensity",
    "RandShiftIntensity",
    "StdShiftIntensity",
    "RandStdShiftIntensity",
    "RandBiasField",
    "ScaleIntensity",
    "RandScaleIntensity",
    "NormalizeIntensity",
    "ThresholdIntensity",
    "ScaleIntensityRange",
    "AdjustContrast",
    "RandAdjustContrast",
    "ScaleIntensityRangePercentiles",
    "MaskIntensity",
    "DetectEnvelope",
    "SavitzkyGolaySmooth",
    "GaussianSmooth",
    "RandGaussianSmooth",
    "GaussianSharpen",
    "RandGaussianSharpen",
    "RandHistogramShift",
    "GibbsNoise",
    "RandGibbsNoise",
    "KSpaceSpikeNoise",
    "RandKSpaceSpikeNoise",
    "RandCoarseDropout",
    "HistogramNormalize",
    "LocalPatchShuffling",
]


class RandGaussianNoise(RandomizableTransform):
    """
    Add Gaussian noise to image.

    Args:
        prob: Probability to add Gaussian noise.
        mean: Mean or “centre” of the distribution.
        std: Standard deviation (spread) of distribution.
    """

    backend = [TransformBackends.TORCH, TransformBackends.NUMPY]

    def __init__(self, prob: float = 0.1, mean: Union[Sequence[float], float] = 0.0, std: float = 0.1) -> None:
        RandomizableTransform.__init__(self, prob)
        self.mean = mean
        self.std = std
        self._noise: np.ndarray

    def randomize(self, im_shape: Sequence[int]) -> None:
        super().randomize(None)
        self._noise = self.R.normal(self.mean, self.R.uniform(0, self.std), size=im_shape)

    def __call__(self, img: NdarrayOrTensor) -> NdarrayOrTensor:
        """
        Apply the transform to `img`.
        """
        self.randomize(img.shape)
        if self._noise is None:
            raise RuntimeError("randomized factor should not be None.")
        if not self._do_transform:
            return img
        noise, *_ = convert_to_dst_type(self._noise, img)
        return img + noise


class RandRicianNoise(RandomizableTransform):
    """
    Add Rician noise to image.
    Rician noise in MRI is the result of performing a magnitude operation on complex
    data with Gaussian noise of the same variance in both channels, as described in `Noise in Magnitude Magnetic Resonance Images
    <https://doi.org/10.1002/cmr.a.20124>`_. This transform is adapted from
    `DIPY<https://github.com/dipy/dipy>`_. See also: `The rician distribution of noisy mri data
    <https://doi.org/10.1002/mrm.1910340618>`_.

    Args:
        prob: Probability to add Rician noise.
        mean: Mean or "centre" of the Gaussian distributions sampled to make up
            the Rician noise.
        std: Standard deviation (spread) of the Gaussian distributions sampled
            to make up the Rician noise.
        channel_wise: If True, treats each channel of the image separately.
        relative: If True, the spread of the sampled Gaussian distributions will
            be std times the standard deviation of the image or channel's intensity
            histogram.
        sample_std: If True, sample the spread of the Gaussian distributions
            uniformly from 0 to std.
    """

    backend = [TransformBackends.TORCH, TransformBackends.NUMPY]

    def __init__(
        self,
        prob: float = 0.1,
        mean: Union[Sequence[float], float] = 0.0,
        std: Union[Sequence[float], float] = 1.0,
        channel_wise: bool = False,
        relative: bool = False,
        sample_std: bool = True,
    ) -> None:
        RandomizableTransform.__init__(self, prob)
        self.prob = prob
        self.mean = mean
        self.std = std
        self.channel_wise = channel_wise
        self.relative = relative
        self.sample_std = sample_std
        self._noise1: NdarrayOrTensor
        self._noise2: NdarrayOrTensor

    def _add_noise(self, img: NdarrayTensor, mean: float, std: float):
        dtype_np = get_equivalent_dtype(img.dtype, np.ndarray)
        im_shape = img.shape
        _std = self.R.uniform(0, std) if self.sample_std else std
        self._noise1 = self.R.normal(mean, _std, size=im_shape).astype(dtype_np)
        self._noise2 = self.R.normal(mean, _std, size=im_shape).astype(dtype_np)
        if isinstance(img, torch.Tensor):
            n1 = torch.tensor(self._noise1, device=img.device)
            n2 = torch.tensor(self._noise2, device=img.device)
            return torch.sqrt((img + n1) ** 2 + n2 ** 2)

        return np.sqrt((img + self._noise1) ** 2 + self._noise2 ** 2)

    def __call__(self, img: NdarrayTensor) -> NdarrayTensor:
        """
        Apply the transform to `img`.
        """
        super().randomize(None)
        if not self._do_transform:
            return img
        if self.channel_wise:
            _mean = ensure_tuple_rep(self.mean, len(img))
            _std = ensure_tuple_rep(self.std, len(img))
            for i, d in enumerate(img):
                img[i] = self._add_noise(d, mean=_mean[i], std=_std[i] * d.std() if self.relative else _std[i])
        else:
            if not isinstance(self.mean, (int, float)):
                raise RuntimeError("If channel_wise is False, mean must be a float or int number.")
            if not isinstance(self.std, (int, float)):
                raise RuntimeError("If channel_wise is False, std must be a float or int number.")
            std = self.std * img.std() if self.relative else self.std
            if not isinstance(std, (int, float)):
                raise RuntimeError("std must be a float or int number.")
            img = self._add_noise(img, mean=self.mean, std=std)
        return img


class ShiftIntensity(Transform):
    """
    Shift intensity uniformly for the entire image with specified `offset`.

    Args:
        offset: offset value to shift the intensity of image.
    """

    backend = [TransformBackends.TORCH, TransformBackends.NUMPY]

    def __init__(self, offset: float) -> None:
        self.offset = offset

    def __call__(self, img: NdarrayOrTensor, offset: Optional[float] = None) -> NdarrayOrTensor:
        """
        Apply the transform to `img`.
        """

        offset = self.offset if offset is None else offset
        out = img + offset
        if isinstance(out, torch.Tensor):
            return out.type(img.dtype)
        return out.astype(img.dtype)  # type: ignore


class RandShiftIntensity(RandomizableTransform):
    """
    Randomly shift intensity with randomly picked offset.
    """

    backend = [TransformBackends.TORCH, TransformBackends.NUMPY]

    def __init__(self, offsets: Union[Tuple[float, float], float], prob: float = 0.1) -> None:
        """
        Args:
            offsets: offset range to randomly shift.
                if single number, offset value is picked from (-offsets, offsets).
            prob: probability of shift.
        """
        RandomizableTransform.__init__(self, prob)
        if isinstance(offsets, (int, float)):
            self.offsets = (min(-offsets, offsets), max(-offsets, offsets))
        elif len(offsets) != 2:
            raise ValueError("offsets should be a number or pair of numbers.")
        else:
            self.offsets = (min(offsets), max(offsets))
        self._offset = self.offsets[0]
        self._shfiter = ShiftIntensity(self._offset)

    def randomize(self, data: Optional[Any] = None) -> None:
        self._offset = self.R.uniform(low=self.offsets[0], high=self.offsets[1])
        super().randomize(None)

    def __call__(self, img: NdarrayOrTensor, factor: Optional[float] = None) -> NdarrayOrTensor:
        """
        Apply the transform to `img`.

        Args:
            img: input image to shift intensity.
            factor: a factor to multiply the random offset, then shift.
                can be some image specific value at runtime, like: max(img), etc.

        """
        self.randomize()
        if not self._do_transform:
            return img
        return self._shfiter(img, self._offset if factor is None else self._offset * factor)


class StdShiftIntensity(Transform):
    """
    Shift intensity for the image with a factor and the standard deviation of the image
    by: ``v = v + factor * std(v)``.
    This transform can focus on only non-zero values or the entire image,
    and can also calculate the std on each channel separately.

    Args:
        factor: factor shift by ``v = v + factor * std(v)``.
        nonzero: whether only count non-zero values.
        channel_wise: if True, calculate on each channel separately. Please ensure
            that the first dimension represents the channel of the image if True.
        dtype: output data type, defaults to float32.
    """

    backend = [TransformBackends.TORCH, TransformBackends.NUMPY]

    def __init__(
        self, factor: float, nonzero: bool = False, channel_wise: bool = False, dtype: DtypeLike = np.float32
    ) -> None:
        self.factor = factor
        self.nonzero = nonzero
        self.channel_wise = channel_wise
        self.dtype = dtype

    def _stdshift(self, img: NdarrayOrTensor) -> NdarrayOrTensor:
        ones: Callable
        std: Callable
        if isinstance(img, torch.Tensor):
            ones = torch.ones
            std = partial(torch.std, unbiased=False)
        else:
            ones = np.ones
            std = np.std

        slices = (img != 0) if self.nonzero else ones(img.shape, dtype=bool)
        if slices.any():
            offset = self.factor * std(img[slices])
            img[slices] = img[slices] + offset
        return img

    def __call__(self, img: NdarrayOrTensor) -> NdarrayOrTensor:
        """
        Apply the transform to `img`.
        """
        img, *_ = convert_data_type(img, dtype=self.dtype)
        if self.channel_wise:
            for i, d in enumerate(img):
                img[i] = self._stdshift(d)  # type: ignore
        else:
            img = self._stdshift(img)
        return img


class RandStdShiftIntensity(RandomizableTransform):
    """
    Shift intensity for the image with a factor and the standard deviation of the image
    by: ``v = v + factor * std(v)`` where the `factor` is randomly picked.
    """

    backend = [TransformBackends.TORCH, TransformBackends.NUMPY]

    def __init__(
        self,
        factors: Union[Tuple[float, float], float],
        prob: float = 0.1,
        nonzero: bool = False,
        channel_wise: bool = False,
        dtype: DtypeLike = np.float32,
    ) -> None:
        """
        Args:
            factors: if tuple, the randomly picked range is (min(factors), max(factors)).
                If single number, the range is (-factors, factors).
            prob: probability of std shift.
            nonzero: whether only count non-zero values.
            channel_wise: if True, calculate on each channel separately.
            dtype: output data type, defaults to float32.

        """
        RandomizableTransform.__init__(self, prob)
        if isinstance(factors, (int, float)):
            self.factors = (min(-factors, factors), max(-factors, factors))
        elif len(factors) != 2:
            raise ValueError("factors should be a number or pair of numbers.")
        else:
            self.factors = (min(factors), max(factors))
        self.factor = self.factors[0]
        self.nonzero = nonzero
        self.channel_wise = channel_wise
        self.dtype = dtype

    def randomize(self, data: Optional[Any] = None) -> None:
        self.factor = self.R.uniform(low=self.factors[0], high=self.factors[1])
        super().randomize(None)

    def __call__(self, img: NdarrayOrTensor) -> NdarrayOrTensor:
        """
        Apply the transform to `img`.
        """
        self.randomize()
        if not self._do_transform:
            return img
        shifter = StdShiftIntensity(
            factor=self.factor, nonzero=self.nonzero, channel_wise=self.channel_wise, dtype=self.dtype
        )
        return shifter(img)


class ScaleIntensity(Transform):
    """
    Scale the intensity of input image to the given value range (minv, maxv).
    If `minv` and `maxv` not provided, use `factor` to scale image by ``v = v * (1 + factor)``.
    """

    backend = [TransformBackends.TORCH, TransformBackends.NUMPY]

    def __init__(
        self, minv: Optional[float] = 0.0, maxv: Optional[float] = 1.0, factor: Optional[float] = None
    ) -> None:
        """
        Args:
            minv: minimum value of output data.
            maxv: maximum value of output data.
            factor: factor scale by ``v = v * (1 + factor)``. In order to use
                this parameter, please set `minv` and `maxv` into None.
        """
        self.minv = minv
        self.maxv = maxv
        self.factor = factor

    def __call__(self, img: NdarrayOrTensor) -> NdarrayOrTensor:
        """
        Apply the transform to `img`.

        Raises:
            ValueError: When ``self.minv=None`` or ``self.maxv=None`` and ``self.factor=None``. Incompatible values.

        """
        if self.minv is not None and self.maxv is not None:
            return rescale_array(img, self.minv, self.maxv, img.dtype)
        if self.factor is not None:
            out = img * (1 + self.factor)
            out, *_ = convert_data_type(out, dtype=img.dtype)
            return out
        raise ValueError("Incompatible values: minv=None or maxv=None and factor=None.")


class RandScaleIntensity(RandomizableTransform):
    """
    Randomly scale the intensity of input image by ``v = v * (1 + factor)`` where the `factor`
    is randomly picked.
    """

    backend = ScaleIntensity.backend

    def __init__(self, factors: Union[Tuple[float, float], float], prob: float = 0.1) -> None:
        """
        Args:
            factors: factor range to randomly scale by ``v = v * (1 + factor)``.
                if single number, factor value is picked from (-factors, factors).
            prob: probability of scale.

        """
        RandomizableTransform.__init__(self, prob)
        if isinstance(factors, (int, float)):
            self.factors = (min(-factors, factors), max(-factors, factors))
        elif len(factors) != 2:
            raise ValueError("factors should be a number or pair of numbers.")
        else:
            self.factors = (min(factors), max(factors))
        self.factor = self.factors[0]

    def randomize(self, data: Optional[Any] = None) -> None:
        self.factor = self.R.uniform(low=self.factors[0], high=self.factors[1])
        super().randomize(None)

    def __call__(self, img: NdarrayOrTensor) -> NdarrayOrTensor:
        """
        Apply the transform to `img`.
        """
        self.randomize()
        if not self._do_transform:
            return img
        scaler = ScaleIntensity(minv=None, maxv=None, factor=self.factor)
        return scaler(img)


class RandBiasField(RandomizableTransform):
    """
    Random bias field augmentation for MR images.
    The bias field is considered as a linear combination of smoothly varying basis (polynomial)
    functions, as described in `Automated Model-Based Tissue Classification of MR Images of the Brain
    <https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber=811270>`_.
    This implementation adapted from `NiftyNet
    <https://github.com/NifTK/NiftyNet>`_.
    Referred to `Longitudinal segmentation of age-related white matter hyperintensities
    <https://www.sciencedirect.com/science/article/pii/S1361841517300257?via%3Dihub>`_.

    Args:
        degree: degree of freedom of the polynomials. The value should be no less than 1.
            Defaults to 3.
        coeff_range: range of the random coefficients. Defaults to (0.0, 0.1).
        dtype: output data type, defaults to float32.
        prob: probability to do random bias field.

    """

    def __init__(
        self,
        degree: int = 3,
        coeff_range: Tuple[float, float] = (0.0, 0.1),
        dtype: DtypeLike = np.float32,
        prob: float = 1.0,
    ) -> None:
        RandomizableTransform.__init__(self, prob)
        if degree < 1:
            raise ValueError("degree should be no less than 1.")
        self.degree = degree
        self.coeff_range = coeff_range
        self.dtype = dtype

        self._coeff = [1.0]

    def _generate_random_field(self, spatial_shape: Sequence[int], degree: int, coeff: Sequence[float]):
        """
        products of polynomials as bias field estimations
        """
        rank = len(spatial_shape)
        coeff_mat = np.zeros((degree + 1,) * rank)
        coords = [np.linspace(-1.0, 1.0, dim, dtype=np.float32) for dim in spatial_shape]
        if rank == 2:
            coeff_mat[np.tril_indices(degree + 1)] = coeff
            return np.polynomial.legendre.leggrid2d(coords[0], coords[1], coeff_mat)
        if rank == 3:
            pts: List[List[int]] = [[0, 0, 0]]
            for i in range(degree + 1):
                for j in range(degree + 1 - i):
                    for k in range(degree + 1 - i - j):
                        pts.append([i, j, k])
            if len(pts) > 1:
                pts = pts[1:]
            np_pts = np.stack(pts)
            coeff_mat[np_pts[:, 0], np_pts[:, 1], np_pts[:, 2]] = coeff
            return np.polynomial.legendre.leggrid3d(coords[0], coords[1], coords[2], coeff_mat)
        raise NotImplementedError("only supports 2D or 3D fields")

    def randomize(self, data: np.ndarray) -> None:
        super().randomize(None)
        n_coeff = int(np.prod([(self.degree + k) / k for k in range(1, len(data.shape[1:]) + 1)]))
        self._coeff = self.R.uniform(*self.coeff_range, n_coeff).tolist()

    def __call__(self, img: np.ndarray):
        """
        Apply the transform to `img`.
        """
        self.randomize(data=img)
        if not self._do_transform:
            return img
        num_channels, *spatial_shape = img.shape
        _bias_fields = np.stack(
            [
                self._generate_random_field(spatial_shape=spatial_shape, degree=self.degree, coeff=self._coeff)
                for _ in range(num_channels)
            ],
            axis=0,
        )
        return (img * np.exp(_bias_fields)).astype(self.dtype)


class NormalizeIntensity(Transform):
    """
    Normalize input based on provided args, using calculated mean and std if not provided.
    This transform can normalize only non-zero values or entire image, and can also calculate
    mean and std on each channel separately.
    When `channel_wise` is True, the first dimension of `subtrahend` and `divisor` should
    be the number of image channels if they are not None.

    Args:
        subtrahend: the amount to subtract by (usually the mean).
        divisor: the amount to divide by (usually the standard deviation).
        nonzero: whether only normalize non-zero values.
        channel_wise: if using calculated mean and std, calculate on each channel separately
            or calculate on the entire image directly.
        dtype: output data type, defaults to float32.
    """

    backend = [TransformBackends.TORCH, TransformBackends.NUMPY]

    def __init__(
        self,
        subtrahend: Union[Sequence, NdarrayOrTensor, None] = None,
        divisor: Union[Sequence, NdarrayOrTensor, None] = None,
        nonzero: bool = False,
        channel_wise: bool = False,
        dtype: DtypeLike = np.float32,
    ) -> None:
        self.subtrahend = subtrahend
        self.divisor = divisor
        self.nonzero = nonzero
        self.channel_wise = channel_wise
        self.dtype = dtype

    @staticmethod
    def _mean(x):
        if isinstance(x, np.ndarray):
            return np.mean(x)
        x = torch.mean(x.float())
        return x.item() if x.numel() == 1 else x

    @staticmethod
    def _std(x):
        if isinstance(x, np.ndarray):
            return np.std(x)
        x = torch.std(x.float(), unbiased=False)
        return x.item() if x.numel() == 1 else x

    def _normalize(self, img: NdarrayOrTensor, sub=None, div=None) -> NdarrayOrTensor:
        img, *_ = convert_data_type(img, dtype=torch.float32)

        if self.nonzero:
            slices = img != 0
        else:
            if isinstance(img, np.ndarray):
                slices = np.ones_like(img, dtype=bool)
            else:
                slices = torch.ones_like(img, dtype=torch.bool)
        if not slices.any():
            return img

        _sub = sub if sub is not None else self._mean(img[slices])
        if isinstance(_sub, (torch.Tensor, np.ndarray)):
            _sub, *_ = convert_to_dst_type(_sub, img)
            _sub = _sub[slices]

        _div = div if div is not None else self._std(img[slices])
        if np.isscalar(_div):
            if _div == 0.0:
                _div = 1.0
        elif isinstance(_div, (torch.Tensor, np.ndarray)):
            _div, *_ = convert_to_dst_type(_div, img)
            _div = _div[slices]
            _div[_div == 0.0] = 1.0

        img[slices] = (img[slices] - _sub) / _div
        return img

    def __call__(self, img: NdarrayOrTensor) -> NdarrayOrTensor:
        """
        Apply the transform to `img`, assuming `img` is a channel-first array if `self.channel_wise` is True,
        """
        if self.channel_wise:
            if self.subtrahend is not None and len(self.subtrahend) != len(img):
                raise ValueError(f"img has {len(img)} channels, but subtrahend has {len(self.subtrahend)} components.")
            if self.divisor is not None and len(self.divisor) != len(img):
                raise ValueError(f"img has {len(img)} channels, but divisor has {len(self.divisor)} components.")

            for i, d in enumerate(img):
                img[i] = self._normalize(  # type: ignore
                    d,
                    sub=self.subtrahend[i] if self.subtrahend is not None else None,
                    div=self.divisor[i] if self.divisor is not None else None,
                )
        else:
            img = self._normalize(img, self.subtrahend, self.divisor)

        out, *_ = convert_data_type(img, dtype=self.dtype)
        return out


class ThresholdIntensity(Transform):
    """
    Filter the intensity values of whole image to below threshold or above threshold.
    And fill the remaining parts of the image to the `cval` value.

    Args:
        threshold: the threshold to filter intensity values.
        above: filter values above the threshold or below the threshold, default is True.
        cval: value to fill the remaining parts of the image, default is 0.
    """

    def __init__(self, threshold: float, above: bool = True, cval: float = 0.0) -> None:
        if not isinstance(threshold, (int, float)):
            raise ValueError("threshold must be a float or int number.")
        self.threshold = threshold
        self.above = above
        self.cval = cval

    def __call__(self, img: np.ndarray) -> np.ndarray:
        """
        Apply the transform to `img`.
        """
        return np.asarray(
            np.where(img > self.threshold if self.above else img < self.threshold, img, self.cval), dtype=img.dtype
        )


class ScaleIntensityRange(Transform):
    """
    Apply specific intensity scaling to the whole numpy array.
    Scaling from [a_min, a_max] to [b_min, b_max] with clip option.

    Args:
        a_min: intensity original range min.
        a_max: intensity original range max.
        b_min: intensity target range min.
        b_max: intensity target range max.
        clip: whether to perform clip after scaling.
    """

    def __init__(self, a_min: float, a_max: float, b_min: float, b_max: float, clip: bool = False) -> None:
        self.a_min = a_min
        self.a_max = a_max
        self.b_min = b_min
        self.b_max = b_max
        self.clip = clip

    def __call__(self, img: np.ndarray):
        """
        Apply the transform to `img`.
        """
        if self.a_max - self.a_min == 0.0:
            warn("Divide by zero (a_min == a_max)", Warning)
            return img - self.a_min + self.b_min

        img = (img - self.a_min) / (self.a_max - self.a_min)
        img = img * (self.b_max - self.b_min) + self.b_min
        if self.clip:
            img = np.asarray(np.clip(img, self.b_min, self.b_max))
        return img


class AdjustContrast(Transform):
    """
    Changes image intensity by gamma. Each pixel/voxel intensity is updated as::

        x = ((x - min) / intensity_range) ^ gamma * intensity_range + min

    Args:
        gamma: gamma value to adjust the contrast as function.
    """

    def __init__(self, gamma: float) -> None:
        if not isinstance(gamma, (int, float)):
            raise ValueError("gamma must be a float or int number.")
        self.gamma = gamma

    def __call__(self, img: np.ndarray):
        """
        Apply the transform to `img`.
        """
        epsilon = 1e-7
        img_min = img.min()
        img_range = img.max() - img_min
        return np.power(((img - img_min) / float(img_range + epsilon)), self.gamma) * img_range + img_min


class RandAdjustContrast(RandomizableTransform):
    """
    Randomly changes image intensity by gamma. Each pixel/voxel intensity is updated as::

        x = ((x - min) / intensity_range) ^ gamma * intensity_range + min

    Args:
        prob: Probability of adjustment.
        gamma: Range of gamma values.
            If single number, value is picked from (0.5, gamma), default is (0.5, 4.5).
    """

    def __init__(self, prob: float = 0.1, gamma: Union[Sequence[float], float] = (0.5, 4.5)) -> None:
        RandomizableTransform.__init__(self, prob)

        if isinstance(gamma, (int, float)):
            if gamma <= 0.5:
                raise ValueError(
                    "if gamma is single number, must greater than 0.5 and value is picked from (0.5, gamma)"
                )
            self.gamma = (0.5, gamma)
        elif len(gamma) != 2:
            raise ValueError("gamma should be a number or pair of numbers.")
        else:
            self.gamma = (min(gamma), max(gamma))

        self.gamma_value: float

    def randomize(self, data: Optional[Any] = None) -> None:
        super().randomize(None)
        self.gamma_value = self.R.uniform(low=self.gamma[0], high=self.gamma[1])

    def __call__(self, img: np.ndarray):
        """
        Apply the transform to `img`.
        """
        self.randomize()
        if self.gamma_value is None:
            raise ValueError("gamma_value is not set.")
        if not self._do_transform:
            return img
        adjuster = AdjustContrast(self.gamma_value)
        return adjuster(img)


class ScaleIntensityRangePercentiles(Transform):
    """
    Apply range scaling to a numpy array based on the intensity distribution of the input.

    By default this transform will scale from [lower_intensity_percentile, upper_intensity_percentile] to [b_min, b_max], where
    {lower,upper}_intensity_percentile are the intensity values at the corresponding percentiles of ``img``.

    The ``relative`` parameter can also be set to scale from [lower_intensity_percentile, upper_intensity_percentile] to the
    lower and upper percentiles of the output range [b_min, b_max]

    For example:

    .. code-block:: python
        :emphasize-lines: 11, 22

        image = np.array(
            [[[1, 2, 3, 4, 5],
              [1, 2, 3, 4, 5],
              [1, 2, 3, 4, 5],
              [1, 2, 3, 4, 5],
              [1, 2, 3, 4, 5],
              [1, 2, 3, 4, 5]]])

        # Scale from lower and upper image intensity percentiles
        # to output range [b_min, b_max]
        scaler = ScaleIntensityRangePercentiles(10, 90, 0, 200, False, False)
        print(scaler(image))
        [[[0., 50., 100., 150., 200.],
          [0., 50., 100., 150., 200.],
          [0., 50., 100., 150., 200.],
          [0., 50., 100., 150., 200.],
          [0., 50., 100., 150., 200.],
          [0., 50., 100., 150., 200.]]]

        # Scale from lower and upper image intensity percentiles
        # to lower and upper percentiles of the output range [b_min, b_max]
        rel_scaler = ScaleIntensityRangePercentiles(10, 90, 0, 200, False, True)
        print(rel_scaler(image))
        [[[20., 60., 100., 140., 180.],
          [20., 60., 100., 140., 180.],
          [20., 60., 100., 140., 180.],
          [20., 60., 100., 140., 180.],
          [20., 60., 100., 140., 180.],
          [20., 60., 100., 140., 180.]]]


    Args:
        lower: lower intensity percentile.
        upper: upper intensity percentile.
        b_min: intensity target range min.
        b_max: intensity target range max.
        clip: whether to perform clip after scaling.
        relative: whether to scale to the corresponding percentiles of [b_min, b_max].
    """

    def __init__(
        self, lower: float, upper: float, b_min: float, b_max: float, clip: bool = False, relative: bool = False
    ) -> None:
        if lower < 0.0 or lower > 100.0:
            raise ValueError("Percentiles must be in the range [0, 100]")
        if upper < 0.0 or upper > 100.0:
            raise ValueError("Percentiles must be in the range [0, 100]")
        self.lower = lower
        self.upper = upper
        self.b_min = b_min
        self.b_max = b_max
        self.clip = clip
        self.relative = relative

    def __call__(self, img: np.ndarray):
        """
        Apply the transform to `img`.
        """
        a_min = np.percentile(img, self.lower)
        a_max = np.percentile(img, self.upper)
        b_min = self.b_min
        b_max = self.b_max

        if self.relative:
            b_min = ((self.b_max - self.b_min) * (self.lower / 100.0)) + self.b_min
            b_max = ((self.b_max - self.b_min) * (self.upper / 100.0)) + self.b_min

        scalar = ScaleIntensityRange(a_min=a_min, a_max=a_max, b_min=b_min, b_max=b_max, clip=False)
        img = scalar(img)

        if self.clip:
            img = np.asarray(np.clip(img, self.b_min, self.b_max))

        return img


class MaskIntensity(Transform):
    """
    Mask the intensity values of input image with the specified mask data.
    Mask data must have the same spatial size as the input image, and all
    the intensity values of input image corresponding to the selected values
    in the mask data will keep the original value, others will be set to `0`.

    Args:
        mask_data: if `mask_data` is single channel, apply to every channel
            of input image. if multiple channels, the number of channels must
            match the input data. the intensity values of input image corresponding
            to the selected values in the mask data will keep the original value,
            others will be set to `0`. if None, must specify the `mask_data` at runtime.
        select_fn: function to select valid values of the `mask_data`, default is
            to select `values > 0`.

    """

    def __init__(self, mask_data: Optional[np.ndarray] = None, select_fn: Callable = is_positive) -> None:
        self.mask_data = mask_data
        self.select_fn = select_fn

    def __call__(self, img: np.ndarray, mask_data: Optional[np.ndarray] = None) -> np.ndarray:
        """
        Args:
            mask_data: if mask data is single channel, apply to every channel
                of input image. if multiple channels, the channel number must
                match input data. mask_data will be converted to `bool` values
                by `mask_data > 0` before applying transform to input image.

        Raises:
            - ValueError: When both ``mask_data`` and ``self.mask_data`` are None.
            - ValueError: When ``mask_data`` and ``img`` channels differ and ``mask_data`` is not single channel.

        """
        mask_data = self.mask_data if mask_data is None else mask_data
        if mask_data is None:
            raise ValueError("must provide the mask_data when initializing the transform or at runtime.")

        mask_data = np.asarray(self.select_fn(mask_data))
        if mask_data.shape[0] != 1 and mask_data.shape[0] != img.shape[0]:
            raise ValueError(
                "When mask_data is not single channel, mask_data channels must match img, "
                f"got img channels={img.shape[0]} mask_data channels={mask_data.shape[0]}."
            )

        return np.asarray(img * mask_data)


class SavitzkyGolaySmooth(Transform):
    """
    Smooth the input data along the given axis using a Savitzky-Golay filter.

    Args:
        window_length: Length of the filter window, must be a positive odd integer.
        order: Order of the polynomial to fit to each window, must be less than ``window_length``.
        axis: Optional axis along which to apply the filter kernel. Default 1 (first spatial dimension).
        mode: Optional padding mode, passed to convolution class. ``'zeros'``, ``'reflect'``, ``'replicate'``
            or ``'circular'``. Default: ``'zeros'``. See ``torch.nn.Conv1d()`` for more information.
    """

    backend = [TransformBackends.NUMPY]

    def __init__(self, window_length: int, order: int, axis: int = 1, mode: str = "zeros"):

        if axis < 0:
            raise ValueError("axis must be zero or positive.")

        self.window_length = window_length
        self.order = order
        self.axis = axis
        self.mode = mode
        self.img_t: torch.Tensor = torch.tensor(0.0)

    def __call__(self, img: NdarrayOrTensor) -> torch.Tensor:
        """
        Args:
            img: array containing input data. Must be real and in shape [channels, spatial1, spatial2, ...].

        Returns:
            array containing smoothed result.

        """
        self.img_t = convert_to_tensor(img)

        # add one to transform axis because a batch axis will be added at dimension 0
        savgol_filter = SavitzkyGolayFilter(self.window_length, self.order, self.axis + 1, self.mode)
        # convert to Tensor and add Batch axis expected by HilbertTransform
        out: torch.Tensor = savgol_filter(self.img_t.unsqueeze(0)).squeeze(0)
        return out


class DetectEnvelope(Transform):
    """
    Find the envelope of the input data along the requested axis using a Hilbert transform.
    Requires PyTorch 1.7.0+ and the PyTorch FFT module (which is not included in NVIDIA PyTorch Release 20.10).

    Args:
        axis: Axis along which to detect the envelope. Default 1, i.e. the first spatial dimension.
        N: FFT size. Default img.shape[axis]. Input will be zero-padded or truncated to this size along dimension
        ``axis``.

    """

    def __init__(self, axis: int = 1, n: Union[int, None] = None) -> None:

        if PT_BEFORE_1_7:
            raise InvalidPyTorchVersionError("1.7.0", self.__class__.__name__)

        if axis < 0:
            raise ValueError("axis must be zero or positive.")

        self.axis = axis
        self.n = n

    def __call__(self, img: np.ndarray):
        """

        Args:
            img: numpy.ndarray containing input data. Must be real and in shape [channels, spatial1, spatial2, ...].

        Returns:
            np.ndarray containing envelope of data in img along the specified axis.

        """
        # add one to transform axis because a batch axis will be added at dimension 0
        hilbert_transform = HilbertTransform(self.axis + 1, self.n)
        # convert to Tensor and add Batch axis expected by HilbertTransform
        input_data = torch.as_tensor(np.ascontiguousarray(img)).unsqueeze(0)
        return np.abs(hilbert_transform(input_data).squeeze(0).numpy())


class GaussianSmooth(Transform):
    """
    Apply Gaussian smooth to the input data based on specified `sigma` parameter.
    A default value `sigma=1.0` is provided for reference.

    Args:
        sigma: if a list of values, must match the count of spatial dimensions of input data,
            and apply every value in the list to 1 spatial dimension. if only 1 value provided,
            use it for all spatial dimensions.
        approx: discrete Gaussian kernel type, available options are "erf", "sampled", and "scalespace".
            see also :py:meth:`monai.networks.layers.GaussianFilter`.

    """

    def __init__(self, sigma: Union[Sequence[float], float] = 1.0, approx: str = "erf") -> None:
        self.sigma = sigma
        self.approx = approx

    def __call__(self, img: np.ndarray):
        gaussian_filter = GaussianFilter(img.ndim - 1, self.sigma, approx=self.approx)
        input_data = torch.as_tensor(np.ascontiguousarray(img), dtype=torch.float).unsqueeze(0)
        return gaussian_filter(input_data).squeeze(0).detach().numpy()


class RandGaussianSmooth(RandomizableTransform):
    """
    Apply Gaussian smooth to the input data based on randomly selected `sigma` parameters.

    Args:
        sigma_x: randomly select sigma value for the first spatial dimension.
        sigma_y: randomly select sigma value for the second spatial dimension if have.
        sigma_z: randomly select sigma value for the third spatial dimension if have.
        prob: probability of Gaussian smooth.
        approx: discrete Gaussian kernel type, available options are "erf", "sampled", and "scalespace".
            see also :py:meth:`monai.networks.layers.GaussianFilter`.

    """

    def __init__(
        self,
        sigma_x: Tuple[float, float] = (0.25, 1.5),
        sigma_y: Tuple[float, float] = (0.25, 1.5),
        sigma_z: Tuple[float, float] = (0.25, 1.5),
        prob: float = 0.1,
        approx: str = "erf",
    ) -> None:
        RandomizableTransform.__init__(self, prob)
        self.sigma_x = sigma_x
        self.sigma_y = sigma_y
        self.sigma_z = sigma_z
        self.approx = approx

        self.x = self.sigma_x[0]
        self.y = self.sigma_y[0]
        self.z = self.sigma_z[0]

    def randomize(self, data: Optional[Any] = None) -> None:
        super().randomize(None)
        self.x = self.R.uniform(low=self.sigma_x[0], high=self.sigma_x[1])
        self.y = self.R.uniform(low=self.sigma_y[0], high=self.sigma_y[1])
        self.z = self.R.uniform(low=self.sigma_z[0], high=self.sigma_z[1])

    def __call__(self, img: np.ndarray):
        self.randomize()
        if not self._do_transform:
            return img
        sigma = ensure_tuple_size(tup=(self.x, self.y, self.z), dim=img.ndim - 1)
        return GaussianSmooth(sigma=sigma, approx=self.approx)(img)


class GaussianSharpen(Transform):
    """
    Sharpen images using the Gaussian Blur filter.
    Referring to: http://scipy-lectures.org/advanced/image_processing/auto_examples/plot_sharpen.html.
    The algorithm is shown as below

    .. code-block:: python

        blurred_f = gaussian_filter(img, sigma1)
        filter_blurred_f = gaussian_filter(blurred_f, sigma2)
        img = blurred_f + alpha * (blurred_f - filter_blurred_f)

    A set of default values `sigma1=3.0`, `sigma2=1.0` and `alpha=30.0` is provide for reference.

    Args:
        sigma1: sigma parameter for the first gaussian kernel. if a list of values, must match the count
            of spatial dimensions of input data, and apply every value in the list to 1 spatial dimension.
            if only 1 value provided, use it for all spatial dimensions.
        sigma2: sigma parameter for the second gaussian kernel. if a list of values, must match the count
            of spatial dimensions of input data, and apply every value in the list to 1 spatial dimension.
            if only 1 value provided, use it for all spatial dimensions.
        alpha: weight parameter to compute the final result.
        approx: discrete Gaussian kernel type, available options are "erf", "sampled", and "scalespace".
            see also :py:meth:`monai.networks.layers.GaussianFilter`.

    """

    def __init__(
        self,
        sigma1: Union[Sequence[float], float] = 3.0,
        sigma2: Union[Sequence[float], float] = 1.0,
        alpha: float = 30.0,
        approx: str = "erf",
    ) -> None:
        self.sigma1 = sigma1
        self.sigma2 = sigma2
        self.alpha = alpha
        self.approx = approx

    def __call__(self, img: np.ndarray):
        gaussian_filter1 = GaussianFilter(img.ndim - 1, self.sigma1, approx=self.approx)
        gaussian_filter2 = GaussianFilter(img.ndim - 1, self.sigma2, approx=self.approx)
        input_data = torch.as_tensor(np.ascontiguousarray(img), dtype=torch.float).unsqueeze(0)
        blurred_f = gaussian_filter1(input_data)
        filter_blurred_f = gaussian_filter2(blurred_f)
        return (blurred_f + self.alpha * (blurred_f - filter_blurred_f)).squeeze(0).detach().numpy()


class RandGaussianSharpen(RandomizableTransform):
    """
    Sharpen images using the Gaussian Blur filter based on randomly selected `sigma1`, `sigma2` and `alpha`.
    The algorithm is :py:class:`monai.transforms.GaussianSharpen`.

    Args:
        sigma1_x: randomly select sigma value for the first spatial dimension of first gaussian kernel.
        sigma1_y: randomly select sigma value for the second spatial dimension(if have) of first gaussian kernel.
        sigma1_z: randomly select sigma value for the third spatial dimension(if have) of first gaussian kernel.
        sigma2_x: randomly select sigma value for the first spatial dimension of second gaussian kernel.
            if only 1 value `X` provided, it must be smaller than `sigma1_x` and randomly select from [X, sigma1_x].
        sigma2_y: randomly select sigma value for the second spatial dimension(if have) of second gaussian kernel.
            if only 1 value `Y` provided, it must be smaller than `sigma1_y` and randomly select from [Y, sigma1_y].
        sigma2_z: randomly select sigma value for the third spatial dimension(if have) of second gaussian kernel.
            if only 1 value `Z` provided, it must be smaller than `sigma1_z` and randomly select from [Z, sigma1_z].
        alpha: randomly select weight parameter to compute the final result.
        approx: discrete Gaussian kernel type, available options are "erf", "sampled", and "scalespace".
            see also :py:meth:`monai.networks.layers.GaussianFilter`.
        prob: probability of Gaussian sharpen.

    """

    def __init__(
        self,
        sigma1_x: Tuple[float, float] = (0.5, 1.0),
        sigma1_y: Tuple[float, float] = (0.5, 1.0),
        sigma1_z: Tuple[float, float] = (0.5, 1.0),
        sigma2_x: Union[Tuple[float, float], float] = 0.5,
        sigma2_y: Union[Tuple[float, float], float] = 0.5,
        sigma2_z: Union[Tuple[float, float], float] = 0.5,
        alpha: Tuple[float, float] = (10.0, 30.0),
        approx: str = "erf",
        prob: float = 0.1,
    ) -> None:
        RandomizableTransform.__init__(self, prob)
        self.sigma1_x = sigma1_x
        self.sigma1_y = sigma1_y
        self.sigma1_z = sigma1_z
        self.sigma2_x = sigma2_x
        self.sigma2_y = sigma2_y
        self.sigma2_z = sigma2_z
        self.alpha = alpha
        self.approx = approx

    def randomize(self, data: Optional[Any] = None) -> None:
        super().randomize(None)
        self.x1 = self.R.uniform(low=self.sigma1_x[0], high=self.sigma1_x[1])
        self.y1 = self.R.uniform(low=self.sigma1_y[0], high=self.sigma1_y[1])
        self.z1 = self.R.uniform(low=self.sigma1_z[0], high=self.sigma1_z[1])
        sigma2_x = (self.sigma2_x, self.x1) if not isinstance(self.sigma2_x, Iterable) else self.sigma2_x
        sigma2_y = (self.sigma2_y, self.y1) if not isinstance(self.sigma2_y, Iterable) else self.sigma2_y
        sigma2_z = (self.sigma2_z, self.z1) if not isinstance(self.sigma2_z, Iterable) else self.sigma2_z
        self.x2 = self.R.uniform(low=sigma2_x[0], high=sigma2_x[1])
        self.y2 = self.R.uniform(low=sigma2_y[0], high=sigma2_y[1])
        self.z2 = self.R.uniform(low=sigma2_z[0], high=sigma2_z[1])
        self.a = self.R.uniform(low=self.alpha[0], high=self.alpha[1])

    def __call__(self, img: np.ndarray):
        self.randomize()
        if not self._do_transform:
            return img
        sigma1 = ensure_tuple_size(tup=(self.x1, self.y1, self.z1), dim=img.ndim - 1)
        sigma2 = ensure_tuple_size(tup=(self.x2, self.y2, self.z2), dim=img.ndim - 1)
        return GaussianSharpen(sigma1=sigma1, sigma2=sigma2, alpha=self.a, approx=self.approx)(img)


class RandHistogramShift(RandomizableTransform):
    """
    Apply random nonlinear transform to the image's intensity histogram.

    Args:
        num_control_points: number of control points governing the nonlinear intensity mapping.
            a smaller number of control points allows for larger intensity shifts. if two values provided, number of
            control points selecting from range (min_value, max_value).
        prob: probability of histogram shift.
    """

    def __init__(self, num_control_points: Union[Tuple[int, int], int] = 10, prob: float = 0.1) -> None:
        RandomizableTransform.__init__(self, prob)

        if isinstance(num_control_points, int):
            if num_control_points <= 2:
                raise ValueError("num_control_points should be greater than or equal to 3")
            self.num_control_points = (num_control_points, num_control_points)
        else:
            if len(num_control_points) != 2:
                raise ValueError("num_control points should be a number or a pair of numbers")
            if min(num_control_points) <= 2:
                raise ValueError("num_control_points should be greater than or equal to 3")
            self.num_control_points = (min(num_control_points), max(num_control_points))

    def randomize(self, data: Optional[Any] = None) -> None:
        super().randomize(None)
        num_control_point = self.R.randint(self.num_control_points[0], self.num_control_points[1] + 1)
        self.reference_control_points = np.linspace(0, 1, num_control_point)
        self.floating_control_points = np.copy(self.reference_control_points)
        for i in range(1, num_control_point - 1):
            self.floating_control_points[i] = self.R.uniform(
                self.floating_control_points[i - 1], self.floating_control_points[i + 1]
            )

    def __call__(self, img: np.ndarray) -> np.ndarray:
        self.randomize()
        if not self._do_transform:
            return img
        img_min, img_max = img.min(), img.max()
        reference_control_points_scaled = self.reference_control_points * (img_max - img_min) + img_min
        floating_control_points_scaled = self.floating_control_points * (img_max - img_min) + img_min
        return np.asarray(
            np.interp(img, reference_control_points_scaled, floating_control_points_scaled), dtype=img.dtype
        )


class RandGibbsNoise(RandomizableTransform):
    """
    Naturalistic image augmentation via Gibbs artifacts. The transform
    randomly applies Gibbs noise to 2D/3D MRI images. Gibbs artifacts
    are one of the common type of type artifacts appearing in MRI scans.

    The transform is applied to all the channels in the data.

    For general information on Gibbs artifacts, please refer to:
    https://pubs.rsna.org/doi/full/10.1148/rg.313105115
    https://pubs.rsna.org/doi/full/10.1148/radiographics.22.4.g02jl14949


    Args:
        prob (float): probability of applying the transform.
        alpha (float, Sequence(float)): Parametrizes the intensity of the Gibbs noise filter applied. Takes
            values in the interval [0,1] with alpha = 0 acting as the identity mapping.
            If a length-2 list is given as [a,b] then the value of alpha will be
            sampled uniformly from the interval [a,b]. 0 <= a <= b <= 1.
        as_tensor_output: if true return torch.Tensor, else return np.array. default: True.
    """

    def __init__(self, prob: float = 0.1, alpha: Sequence[float] = (0.0, 1.0), as_tensor_output: bool = True) -> None:

        if len(alpha) != 2:
            raise ValueError("alpha length must be 2.")
        if alpha[1] > 1 or alpha[0] < 0:
            raise ValueError("alpha must take values in the interval [0,1]")
        if alpha[0] > alpha[1]:
            raise ValueError("When alpha = [a,b] we need a < b.")

        self.alpha = alpha
        self.sampled_alpha = -1.0  # stores last alpha sampled by randomize()
        self.as_tensor_output = as_tensor_output

        RandomizableTransform.__init__(self, prob=prob)

    def __call__(self, img: Union[np.ndarray, torch.Tensor]) -> Union[torch.Tensor, np.ndarray]:

        # randomize application and possibly alpha
        self._randomize(None)

        if self._do_transform:
            # apply transform
            transform = GibbsNoise(self.sampled_alpha, self.as_tensor_output)
            img = transform(img)
        else:
            if isinstance(img, np.ndarray) and self.as_tensor_output:
                img = torch.Tensor(img)
            elif isinstance(img, torch.Tensor) and not self.as_tensor_output:
                img = img.detach().cpu().numpy()
        return img

    def _randomize(self, _: Any) -> None:
        """
        (1) Set random variable to apply the transform.
        (2) Get alpha from uniform distribution.
        """
        super().randomize(None)
        self.sampled_alpha = self.R.uniform(self.alpha[0], self.alpha[1])


class GibbsNoise(Transform, Fourier):
    """
    The transform applies Gibbs noise to 2D/3D MRI images. Gibbs artifacts
    are one of the common type of type artifacts appearing in MRI scans.

    The transform is applied to all the channels in the data.

    For general information on Gibbs artifacts, please refer to:

    `An Image-based Approach to Understanding the Physics of MR Artifacts
    <https://pubs.rsna.org/doi/full/10.1148/rg.313105115>`_.

    `The AAPM/RSNA Physics Tutorial for Residents
    <https://pubs.rsna.org/doi/full/10.1148/radiographics.22.4.g02jl14949>`_

    Args:
        alpha: Parametrizes the intensity of the Gibbs noise filter applied. Takes
            values in the interval [0,1] with alpha = 0 acting as the identity mapping.
        as_tensor_output: if true return torch.Tensor, else return np.array. Default: True.
    """

    def __init__(self, alpha: float = 0.5, as_tensor_output: bool = True) -> None:

        if alpha > 1 or alpha < 0:
            raise ValueError("alpha must take values in the interval [0,1].")
        self.alpha = alpha
        self.as_tensor_output = as_tensor_output

    def __call__(self, img: Union[np.ndarray, torch.Tensor]) -> Union[torch.Tensor, np.ndarray]:
        n_dims = len(img.shape[1:])

        if isinstance(img, np.ndarray):
            img = torch.Tensor(img)
        # FT
        k = self.shift_fourier(img, n_dims)
        # build and apply mask
        k = self._apply_mask(k)
        # map back
        img = self.inv_shift_fourier(k, n_dims)

        return img if self.as_tensor_output else img.cpu().detach().numpy()

    def _apply_mask(self, k: torch.Tensor) -> torch.Tensor:
        """Builds and applies a mask on the spatial dimensions.

        Args:
            k (np.ndarray): k-space version of the image.
        Returns:
            masked version of the k-space image.
        """
        shape = k.shape[1:]

        # compute masking radius and center
        r = (1 - self.alpha) * np.max(shape) * np.sqrt(2) / 2.0
        center = (np.array(shape) - 1) / 2

        # gives list w/ len==self.dim. Each dim gives coordinate in that dimension
        coords = np.ogrid[tuple(slice(0, i) for i in shape)]

        # need to subtract center coord and then square for Euc distance
        coords_from_center_sq = [(coord - c) ** 2 for coord, c in zip(coords, center)]
        dist_from_center = np.sqrt(sum(coords_from_center_sq))
        mask = dist_from_center <= r

        # add channel dimension into mask
        mask = np.repeat(mask[None], k.shape[0], axis=0)

        # apply binary mask
        k_masked = k * torch.tensor(mask, device=k.device)
        return k_masked


class KSpaceSpikeNoise(Transform, Fourier):
    """
    Apply localized spikes in `k`-space at the given locations and intensities.
    Spike (Herringbone) artifact is a type of data acquisition artifact which
    may occur during MRI scans.

    For general information on spike artifacts, please refer to:

    `AAPM/RSNA physics tutorial for residents: fundamental physics of MR imaging
    <https://pubmed.ncbi.nlm.nih.gov/16009826>`_.

    `Body MRI artifacts in clinical practice: A physicist's and radiologist's
    perspective <https://doi.org/10.1002/jmri.24288>`_.

    Args:
        loc: spatial location for the spikes. For
            images with 3D spatial dimensions, the user can provide (C, X, Y, Z)
            to fix which channel C is affected, or (X, Y, Z) to place the same
            spike in all channels. For 2D cases, the user can provide (C, X, Y)
            or (X, Y).
        k_intensity: value for the log-intensity of the
            `k`-space version of the image. If one location is passed to ``loc`` or the
            channel is not specified, then this argument should receive a float. If
            ``loc`` is given a sequence of locations, then this argument should
            receive a sequence of intensities. This value should be tested as it is
            data-dependent. The default values are the 2.5 the mean of the
            log-intensity for each channel.
        as_tensor_output: if ``True`` return torch.Tensor, else return np.array.
            Default: ``True``.

    Example:
        When working with 4D data, ``KSpaceSpikeNoise(loc = ((3,60,64,32), (64,60,32)), k_intensity = (13,14))``
        will place a spike at `[3, 60, 64, 32]` with `log-intensity = 13`, and
        one spike per channel located respectively at `[: , 64, 60, 32]`
        with `log-intensity = 14`.
    """

    def __init__(
        self,
        loc: Union[Tuple, Sequence[Tuple]],
        k_intensity: Optional[Union[Sequence[float], float]] = None,
        as_tensor_output: bool = True,
    ):

        self.loc = ensure_tuple(loc)
        self.as_tensor_output = as_tensor_output
        self.k_intensity = k_intensity

        # assert one-to-one relationship between factors and locations
        if isinstance(k_intensity, Sequence):
            if not isinstance(loc[0], Sequence):
                raise ValueError(
                    "If a sequence is passed to k_intensity, then a sequence of locations must be passed to loc"
                )
            if len(k_intensity) != len(loc):
                raise ValueError("There must be one intensity_factor value for each tuple of indices in loc.")
        if isinstance(self.loc[0], Sequence) and k_intensity is not None and not isinstance(self.k_intensity, Sequence):
            raise ValueError("There must be one intensity_factor value for each tuple of indices in loc.")

    def __call__(self, img: Union[np.ndarray, torch.Tensor]) -> Union[torch.Tensor, np.ndarray]:
        """
        Args:
            img: image with dimensions (C, H, W) or (C, H, W, D)
        """
        # checking that tuples in loc are consistent with img size
        self._check_indices(img)

        if len(img.shape) < 3:
            raise RuntimeError("Image needs a channel direction.")
        if isinstance(self.loc[0], int) and len(img.shape) == 4 and len(self.loc) == 2:
            raise RuntimeError("Input images of dimension 4 need location tuple to be length 3 or 4")
        if isinstance(self.loc[0], Sequence) and len(img.shape) == 4 and min(map(lambda x: len(x), self.loc)) == 2:
            raise RuntimeError("Input images of dimension 4 need location tuple to be length 3 or 4")

        n_dims = len(img.shape[1:])

        if isinstance(img, np.ndarray):
            img = torch.Tensor(img)
        # FT
        k = self.shift_fourier(img, n_dims)
        log_abs = torch.log(torch.absolute(k) + 1e-10)
        phase = torch.angle(k)

        k_intensity = self.k_intensity
        # default log intensity
        if k_intensity is None:
            k_intensity = tuple(torch.mean(log_abs, dim=tuple(range(-n_dims, 0))) * 2.5)

        # highlight
        if isinstance(self.loc[0], Sequence):
            for idx, val in zip(self.loc, ensure_tuple(k_intensity)):
                self._set_spike(log_abs, idx, val)
        else:
            self._set_spike(log_abs, self.loc, k_intensity)
        # map back
        k = torch.exp(log_abs) * torch.exp(1j * phase)
        img = self.inv_shift_fourier(k, n_dims)

        return img if self.as_tensor_output else img.cpu().detach().numpy()

    def _check_indices(self, img) -> None:
        """Helper method to check consistency of self.loc and input image.

        Raises assertion error if any index in loc is out of bounds."""

        loc = list(self.loc)
        if not isinstance(loc[0], Sequence):
            loc = [loc]
        for i in range(len(loc)):
            if len(loc[i]) < len(img.shape):
                loc[i] = [0] + list(loc[i])

        for i in range(len(img.shape)):
            if img.shape[i] <= max(x[i] for x in loc):
                raise ValueError(
                    f"The index value at position {i} of one of the tuples in loc = {self.loc} is out of bounds for current image."
                )

    def _set_spike(self, k: torch.Tensor, idx: Tuple, val: Union[Sequence[float], float]):
        """
        Helper function to introduce a given intensity at given location.

        Args:
            k: intensity array to alter.
            idx: index of location where to apply change.
            val: value of intensity to write in.
        """
        if len(k.shape) == len(idx):
            k[idx] = val[idx[0]] if isinstance(val, Sequence) else val
        elif len(k.shape) == 4 and len(idx) == 3:
            k[:, idx[0], idx[1], idx[2]] = val  # type: ignore
        elif len(k.shape) == 3 and len(idx) == 2:
            k[:, idx[0], idx[1]] = val  # type: ignore


class RandKSpaceSpikeNoise(RandomizableTransform, Fourier):
    """
    Naturalistic data augmentation via spike artifacts. The transform applies
    localized spikes in `k`-space, and it is the random version of
    :py:class:`monai.transforms.KSpaceSpikeNoise`.

    Spike (Herringbone) artifact is a type of data acquisition artifact which
    may occur during MRI scans. For general information on spike artifacts,
    please refer to:

    `AAPM/RSNA physics tutorial for residents: fundamental physics of MR imaging
    <https://pubmed.ncbi.nlm.nih.gov/16009826>`_.

    `Body MRI artifacts in clinical practice: A physicist's and radiologist's
    perspective <https://doi.org/10.1002/jmri.24288>`_.

    Args:
        prob: probability of applying the transform, either on all
            channels at once, or channel-wise if ``channel_wise = True``.
        intensity_range: pass a tuple
            (a, b) to sample the log-intensity from the interval (a, b)
            uniformly for all channels. Or pass sequence of intevals
            ((a0, b0), (a1, b1), ...) to sample for each respective channel.
            In the second case, the number of 2-tuples must match the number of
            channels.
            Default ranges is `(0.95x, 1.10x)` where `x` is the mean
            log-intensity for each channel.
        channel_wise: treat each channel independently. True by
            default.
        as_tensor_output: if True return torch.Tensor, else
            return np.array. default: True.

    Example:
        To apply `k`-space spikes randomly with probability 0.5, and
        log-intensity sampled from the interval [11, 12] for each channel
        independently, one uses
        ``RandKSpaceSpikeNoise(prob=0.5, intensity_range=(11, 12), channel_wise=True)``
    """

    def __init__(
        self,
        prob: float = 0.1,
        intensity_range: Optional[Sequence[Union[Sequence[float], float]]] = None,
        channel_wise=True,
        as_tensor_output: bool = True,
    ):

        self.intensity_range = intensity_range
        self.channel_wise = channel_wise
        self.as_tensor_output = as_tensor_output
        self.sampled_k_intensity: List = []
        self.sampled_locs: List[Tuple] = []

        if intensity_range is not None and isinstance(intensity_range[0], Sequence) and not channel_wise:
            raise ValueError("When channel_wise = False, intensity_range should be a 2-tuple (low, high) or None.")

        super().__init__(prob)

    def __call__(self, img: Union[np.ndarray, torch.Tensor]) -> Union[torch.Tensor, np.ndarray]:
        """
        Apply transform to `img`. Assumes data is in channel-first form.

        Args:
            img: image with dimensions (C, H, W) or (C, H, W, D)
        """
        if (
            self.intensity_range is not None
            and isinstance(self.intensity_range[0], Sequence)
            and len(self.intensity_range) != img.shape[0]
        ):
            raise RuntimeError(
                "If intensity_range is a sequence of sequences, then there must be one (low, high) tuple for each channel."
            )

        self.sampled_k_intensity = []
        self.sampled_locs = []

        if not isinstance(img, torch.Tensor):
            img = torch.Tensor(img)

        intensity_range = self._make_sequence(img)
        self._randomize(img, intensity_range)

        # build/appy transform only if there are spike locations
        if self.sampled_locs:
            transform = KSpaceSpikeNoise(self.sampled_locs, self.sampled_k_intensity, self.as_tensor_output)
            return transform(img)

        return img if self.as_tensor_output else img.detach().numpy()

    def _randomize(self, img: torch.Tensor, intensity_range: Sequence[Sequence[float]]) -> None:
        """
        Helper method to sample both the location and intensity of the spikes.
        When not working channel wise (channel_wise=False) it use the random
        variable ``self._do_transform`` to decide whether to sample a location
        and intensity.

        When working channel wise, the method randomly samples a location and
        intensity for each channel depending on ``self._do_transform``.
        """
        # randomizing per channel
        if self.channel_wise:
            for i, chan in enumerate(img):
                super().randomize(None)
                if self._do_transform:
                    self.sampled_locs.append((i,) + tuple(self.R.randint(0, k) for k in chan.shape))
                    self.sampled_k_intensity.append(self.R.uniform(intensity_range[i][0], intensity_range[i][1]))
        # working with all channels together
        else:
            super().randomize(None)
            if self._do_transform:
                spatial = tuple(self.R.randint(0, k) for k in img.shape[1:])
                self.sampled_locs = [(i,) + spatial for i in range(img.shape[0])]
                if isinstance(intensity_range[0], Sequence):
                    self.sampled_k_intensity = [self.R.uniform(p[0], p[1]) for p in intensity_range]
                else:
                    self.sampled_k_intensity = [self.R.uniform(intensity_range[0], intensity_range[1])] * len(img)

    def _make_sequence(self, x: torch.Tensor) -> Sequence[Sequence[float]]:
        """
        Formats the sequence of intensities ranges to Sequence[Sequence[float]].
        """
        if self.intensity_range is None:
            # set default range if one not provided
            return self._set_default_range(x)

        if not isinstance(self.intensity_range[0], Sequence):
            return (ensure_tuple(self.intensity_range),) * x.shape[0]
        return ensure_tuple(self.intensity_range)

    def _set_default_range(self, img: torch.Tensor) -> Sequence[Sequence[float]]:
        """
        Sets default intensity ranges to be sampled.

        Args:
            img: image to transform.
        """
        n_dims = len(img.shape[1:])

        k = self.shift_fourier(img, n_dims)
        log_abs = torch.log(torch.absolute(k) + 1e-10)
        shifted_means = torch.mean(log_abs, dim=tuple(range(-n_dims, 0))) * 2.5
        return tuple((i * 0.95, i * 1.1) for i in shifted_means)


class RandCoarseDropout(RandomizableTransform):
    """
    Randomly coarse dropout regions in the image, then fill in the rectangular regions with specified value.
    Or keep the rectangular regions and fill in the other areas with specified value.
    Refer to papers: https://arxiv.org/abs/1708.04552, https://arxiv.org/pdf/1604.07379
    And other implementation: https://albumentations.ai/docs/api_reference/augmentations/transforms/
    #albumentations.augmentations.transforms.CoarseDropout.

    Args:
        holes: number of regions to dropout, if `max_holes` is not None, use this arg as the minimum number to
            randomly select the expected number of regions.
        spatial_size: spatial size of the regions to dropout, if `max_spatial_size` is not None, use this arg
            as the minimum spatial size to randomly select size for every region.
            if some components of the `spatial_size` are non-positive values, the transform will use the
            corresponding components of input img size. For example, `spatial_size=(32, -1)` will be adapted
            to `(32, 64)` if the second spatial dimension size of img is `64`.
        dropout_holes: if `True`, dropout the regions of holes and fill value, if `False`, keep the holes and
            dropout the outside and fill value. default to `True`.
        fill_value: target value to fill the dropout regions, if providing a number, will use it as constant
            value to fill all the regions. if providing a tuple for the `min` and `max`, will randomly select
            value for every pixel / voxel from the range `[min, max)`. if None, will compute the `min` and `max`
            value of input image then randomly select value to fill, default to None.
        max_holes: if not None, define the maximum number to randomly select the expected number of regions.
        max_spatial_size: if not None, define the maximum spatial size to randomly select size for every region.
            if some components of the `max_spatial_size` are non-positive values, the transform will use the
            corresponding components of input img size. For example, `max_spatial_size=(32, -1)` will be adapted
            to `(32, 64)` if the second spatial dimension size of img is `64`.
        prob: probability of applying the transform.

    """

    def __init__(
        self,
        holes: int,
        spatial_size: Union[Sequence[int], int],
        dropout_holes: bool = True,
        fill_value: Optional[Union[Tuple[float, float], float]] = None,
        max_holes: Optional[int] = None,
        max_spatial_size: Optional[Union[Sequence[int], int]] = None,
        prob: float = 0.1,
    ) -> None:
        RandomizableTransform.__init__(self, prob)
        if holes < 1:
            raise ValueError("number of holes must be greater than 0.")
        self.holes = holes
        self.spatial_size = spatial_size
        self.dropout_holes = dropout_holes
        if isinstance(fill_value, (tuple, list)):
            if len(fill_value) != 2:
                raise ValueError("fill value should contain 2 numbers if providing the `min` and `max`.")
        self.fill_value = fill_value
        self.max_holes = max_holes
        self.max_spatial_size = max_spatial_size
        self.hole_coords: List = []

    def randomize(self, img_size: Sequence[int]) -> None:
        super().randomize(None)
        size = fall_back_tuple(self.spatial_size, img_size)
        self.hole_coords = []  # clear previously computed coords
        num_holes = self.holes if self.max_holes is None else self.R.randint(self.holes, self.max_holes + 1)
        for _ in range(num_holes):
            if self.max_spatial_size is not None:
                max_size = fall_back_tuple(self.max_spatial_size, img_size)
                size = tuple(self.R.randint(low=size[i], high=max_size[i] + 1) for i in range(len(img_size)))
            valid_size = get_valid_patch_size(img_size, size)
            self.hole_coords.append((slice(None),) + get_random_patch(img_size, valid_size, self.R))

    def __call__(self, img: np.ndarray):
        self.randomize(img.shape[1:])
        ret = img
        if self._do_transform:
            fill_value = (img.min(), img.max()) if self.fill_value is None else self.fill_value

            if self.dropout_holes:
                for h in self.hole_coords:
                    if isinstance(fill_value, (tuple, list)):
                        ret[h] = self.R.uniform(fill_value[0], fill_value[1], size=img[h].shape)
                    else:
                        ret[h] = fill_value
            else:
                if isinstance(fill_value, (tuple, list)):
                    ret = self.R.uniform(fill_value[0], fill_value[1], size=img.shape).astype(img.dtype)
                else:
                    ret = np.full_like(img, fill_value)
                for h in self.hole_coords:
                    ret[h] = img[h]
        return ret


class HistogramNormalize(Transform):
    """
    Apply the histogram normalization to input image.
    Refer to: https://github.com/facebookresearch/CovidPrognosis/blob/master/covidprognosis/data/transforms.py#L83.

    Args:
        num_bins: number of the bins to use in histogram, default to `256`. for more details:
            https://numpy.org/doc/stable/reference/generated/numpy.histogram.html.
        min: the min value to normalize input image, default to `0`.
        max: the max value to normalize input image, default to `255`.
        mask: if provided, must be ndarray of bools or 0s and 1s, and same shape as `image`.
            only points at which `mask==True` are used for the equalization.
            can also provide the mask along with img at runtime.
        dtype: data type of the output, default to `float32`.

    """

    def __init__(
        self,
        num_bins: int = 256,
        min: int = 0,
        max: int = 255,
        mask: Optional[np.ndarray] = None,
        dtype: DtypeLike = np.float32,
    ) -> None:
        self.num_bins = num_bins
        self.min = min
        self.max = max
        self.mask = mask
        self.dtype = dtype

    def __call__(self, img: np.ndarray, mask: Optional[np.ndarray] = None) -> np.ndarray:
        return equalize_hist(
            img=img,
            mask=mask if mask is not None else self.mask,
            num_bins=self.num_bins,
            min=self.min,
            max=self.max,
            dtype=self.dtype,
        )


class LocalPatchShuffling(RandomizableTransform):
    """
    Takes a 3D image and based on input of the local patch size, shuffles the pixels of the local patch within it.
    This process is repeated a for N number of times where every time a different random block is selected for local
    pixel shuffling.

    Kang, Guoliang, et al. "Patchshuffle regularization." arXiv preprint arXiv:1707.07103 (2017).
    """

    def __init__(
        self,
        prob: float = 1.0,
        number_blocks: int = 1000,
        blocksize_ratio: int = 10,
        channel_wise: bool = True,
        device: Optional[torch.device] = None,
        image_only: bool = False,
    ) -> None:
        """
        Args:
            prob: The chance of this transform occuring on the given volume.
            number_blocks: Total number of time a random 3D block will be selected for local shuffling of pixels/voxels
                contained in the block.
            blocksize_ratio: This ratio can be used to estimate the local 3D block sizes that will be selected.
            channel_wise: If True, treats each channel of the image separately.
            device: device on which the tensor will be allocated.
            image_only: if True return only the image volume, otherwise return (image, affine).
        """
        RandomizableTransform.__init__(self, prob)
        self.prob = prob
        self.number_blocks = number_blocks
        self.blocksize_ratio = blocksize_ratio
        self.channel_wise = channel_wise

    def _local_patch_shuffle(self, img: Union[torch.Tensor, np.ndarray], number_blocks: int, blocksize_ratio: int):
        im_shape = img.shape
        img_copy = copy.deepcopy(img)
        for _each_block in range(number_blocks):

            block_size_x = self.R.randint(1, im_shape[0] // blocksize_ratio)
            block_size_y = self.R.randint(1, im_shape[1] // blocksize_ratio)
            block_size_z = self.R.randint(1, im_shape[2] // blocksize_ratio)

            noise_x = self.R.randint(0, im_shape[0] - block_size_x)
            noise_y = self.R.randint(0, im_shape[1] - block_size_y)
            noise_z = self.R.randint(0, im_shape[2] - block_size_z)

            local_patch = img[
                noise_x : noise_x + block_size_x,
                noise_y : noise_y + block_size_y,
                noise_z : noise_z + block_size_z,
            ]

            local_patch = local_patch.flatten()
            self.R.shuffle(local_patch)
            local_patch = local_patch.reshape((block_size_x, block_size_y, block_size_z))

            img_copy[
                noise_x : noise_x + block_size_x, noise_y : noise_y + block_size_y, noise_z : noise_z + block_size_z
            ] = local_patch

        shuffled_image = img_copy
        return shuffled_image

    def __call__(
        self,
        img: Union[np.ndarray, torch.Tensor],
        # spatial_size: Optional[Union[Sequence[int], int]] = None,
        # mode: Optional[Union[GridSampleMode, str]] = None,
        # padding_mode: Optional[Union[GridSamplePadMode, str]] = None,
    ):
        """
        Args:
            img: shape must be (num_channels, H, W[, D]),

        """

        super().randomize(None)
        if not self._do_transform:
            return img

        if self.channel_wise:
            # img = self._local_patch_shuffle(img=img)
            for i, _d in enumerate(img):
                img[i] = self._local_patch_shuffle(
                    img=img[i], blocksize_ratio=self.blocksize_ratio, number_blocks=self.number_blocks
                )
        else:
            raise AssertionError("If channel_wise is False, the image needs to be set to channel first")
        return img
