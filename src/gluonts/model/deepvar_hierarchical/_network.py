# Copyright 2018 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# or in the "license" file accompanying this file. This file is distributed
# on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language governing
# permissions and limitations under the License.

# Standard library imports
from typing import List
from itertools import product

# Third-party imports
import mxnet as mx

# First-party imports
from gluonts.core.component import validated
from gluonts.mx import Tensor
from gluonts.mx.distribution import Distribution, DistributionOutput
from gluonts.mx.distribution import EmpiricalDistribution
from gluonts.mx.util import assert_shape
from gluonts.mx.distribution import LowrankMultivariateGaussian
from gluonts.model.deepvar._network import (
    DeepVARNetwork,
    DeepVARTrainingNetwork,
    DeepVARPredictionNetwork,
)


class DeepVARHierarchicalNetwork(DeepVARNetwork):
    @validated()
    def __init__(
        self,
        M,
        A,
        num_layers: int,
        num_cells: int,
        cell_type: str,
        history_length: int,
        context_length: int,
        prediction_length: int,
        distr_output: DistributionOutput,
        dropout_rate: float,
        lags_seq: List[int],
        target_dim: int,
        cardinality: List[int] = [1],
        embedding_dimension: int = 1,
        scaling: bool = True,
        seq_axis: List[int] = None,
        **kwargs,
    ) -> None:
        super().__init__(
            num_layers=num_layers,
            num_cells=num_cells,
            cell_type=cell_type,
            history_length=history_length,
            context_length=context_length,
            prediction_length=prediction_length,
            distr_output=distr_output,
            dropout_rate=dropout_rate,
            lags_seq=lags_seq,
            target_dim=target_dim,
            cardinality=cardinality,
            embedding_dimension=embedding_dimension,
            scaling=scaling,
            **kwargs,
        )

        self.M = M
        self.A = A
        self.seq_axis = seq_axis

    def reconcile_samples(self, samples: Tensor) -> Tensor:
        """
        Computes coherent samples by projecting unconstrained `samples` using the matrix `self.M`.

        Parameters
        ----------
        samples
            Unconstrained samples
            Shape: (num_samples, batch_size, seq_len, target_dim) during training and
                   (num_parallel_samples x batch_size, seq_len, target_dim) during prediction.

        Returns
        -------
        Coherent samples
            Tensor, shape same as that of `samples`.

        """
        if self.seq_axis:
            # In this case, reconcile samples by going over each index in `seq_axis` iteratively.
            # Note that `seq_axis` can be more than one dimension.
            num_seq_axes = len(self.seq_axis)

            # bring the axes to iterate in the beginning
            samples = mx.nd.moveaxis(
                samples, self.seq_axis, list(range(num_seq_axes))
            )

            seq_axes_sizes = samples.shape[:num_seq_axes]
            out = [
                mx.nd.dot(samples[idx], self.M, transpose_b=True)
                # get the sequential index from the cross-product of their sizes.
                for idx in product(*[range(size) for size in seq_axes_sizes])
            ]

            # put the axis in the correct order again
            out = mx.nd.concat(*out, dim=0).reshape(samples.shape)
            out = mx.nd.moveaxis(
                out, list(range(len(self.seq_axis))), self.seq_axis
            )
            return out
        else:
            return mx.nd.dot(samples, self.M, transpose_b=True)

    def reconciliation_error(self, samples: Tensor) -> float:
        r"""
        Computes the reconciliation error defined by the L-infinity norm of the constraint violation:
                    || Ax ||_{\inf}

        Parameters
        ----------
        samples
            Samples. Shape: (num_parallel_samples*batch_size, 1, target_dim)
        Returns
        -------
        Reconciliation error
            Float

        """
        return mx.nd.max(
            mx.nd.abs(mx.nd.dot(samples, self.A, transpose_b=True))
        ).asnumpy()[0]

    def get_samples_for_loss(self, distr: Distribution) -> Tensor:
        """
        Get samples to compute the final loss. These are samples directly drawn from the given `distr` if coherence is
        not enforced yet; otherwise the drawn samples are reconciled.

        Parameters
        ----------
        distr
            Distribution instances

        Returns
        -------
        samples
            Tensor with shape (num_samples, batch_size, seq_len, target_dim)

        """
        samples = distr.sample_rep(
            num_samples=self.num_samples_for_loss, dtype="float32"
        )

        # Determine which epoch we are currently in.
        self.batch_no += 1
        epoch_no = self.batch_no // self.num_batches_per_epoch + 1
        epoch_frac = epoch_no / self.epochs

        if (
            self.coherent_train_samples
            and epoch_frac > self.warmstart_epoch_frac
        ):
            coherent_samples = self.reconcile_samples(samples)
            assert_shape(coherent_samples, samples.shape)
            return coherent_samples
        else:
            return samples

    def loss(self, F, target: Tensor, distr: Distribution) -> Tensor:
        """
        Computes loss given the output of the network in the form of distribution.
        The loss is given by:

            `self.CRPS_weight` * `loss_CRPS` + `self.likelihood_weight` * `neg_likelihoods`,

         where
          * `loss_CRPS` is computed on the samples drawn from the predicted `distr` (optionally after reconciling them),
          *  `neg_likelihoods` are either computed directly using the predicted `distr` or from the estimated
          distribution based on (coherent) samples, depending on the `sample_LH` flag.

        Parameters
        ----------
        F
        target
            Tensor with shape (batch_size, seq_len, target_dim)
        distr
            Distribution instances

        Returns
        -------
        Loss
            Tensor with shape (batch_size, seq_length, 1)

        """

        # Sample from the predicted distribution if we are computing CRPS loss or likelihood using the distribution
        # based on (coherent) samples.
        # Samples shape: (num_samples, batch_size, seq_len, target_dim)
        if self.sample_LH or (self.CRPS_weight > 0.0):
            samples = self.get_samples_for_loss(distr=distr)

        if self.sample_LH:
            # Estimate the distribution based on (coherent) samples.
            distr = LowrankMultivariateGaussian.fit(F, samples=samples, rank=0)

        neg_likelihoods = -distr.log_prob(target).expand_dims(axis=-1)

        loss_CRPS = F.zeros_like(neg_likelihoods)
        if self.CRPS_weight > 0.0:
            loss_CRPS = (
                EmpiricalDistribution(samples=samples, event_dim=1)
                .crps_univariate(x=target)
                .expand_dims(axis=-1)
            )

        return (
            self.CRPS_weight * loss_CRPS
            + self.likelihood_weight * neg_likelihoods
        )

    def post_process_samples(self, samples: Tensor) -> Tensor:
        """
        Reconcile samples if `coherent_pred_samples` is True.

        Parameters
        ----------
        samples
            Tensor of shape (num_parallel_samples*batch_size, 1, target_dim)

        Returns
        -------
            Tensor of coherent samples.

        """
        if not self.coherent_pred_samples:
            return samples
        else:
            coherent_samples = self.reconcile_samples(samples=samples)
            assert_shape(coherent_samples, samples.shape)

            # assert that A*X_proj ~ 0
            if self.assert_reconciliation:
                assert (
                    self.reconciliation_error(samples=coherent_samples) < 1e-2
                )

            return coherent_samples


class DeepVARHierarchicalTrainingNetwork(
    DeepVARHierarchicalNetwork, DeepVARTrainingNetwork
):
    def __init__(
        self,
        num_samples_for_loss: int,
        likelihood_weight: float,
        CRPS_weight: float,
        coherent_train_samples: bool,
        warmstart_epoch_frac: float,
        epochs: float,
        num_batches_per_epoch: float,
        sample_LH: bool,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.num_samples_for_loss = num_samples_for_loss
        self.likelihood_weight = likelihood_weight
        self.CRPS_weight = CRPS_weight
        self.coherent_train_samples = coherent_train_samples
        self.warmstart_epoch_frac = warmstart_epoch_frac
        self.epochs = epochs
        self.num_batches_per_epoch = num_batches_per_epoch
        self.batch_no = 0
        self.sample_LH = sample_LH

        # Assert CRPS_weight, likelihood_weight, and coherent_train_samples have harmonious values
        assert self.CRPS_weight >= 0.0, "CRPS weight must be non-negative"
        assert (
            self.likelihood_weight >= 0.0
        ), "Likelihood weight must be non-negative!"
        assert (
            self.likelihood_weight + self.CRPS_weight > 0.0
        ), "At least one of CRPS or likelihood weights must be non-zero"
        if self.CRPS_weight == 0.0 and self.coherent_train_samples:
            assert "No sampling being performed. coherent_train_samples flag is ignored"
        if not self.sample_LH == 0.0 and self.coherent_train_samples:
            assert "No sampling being performed. coherent_train_samples flag is ignored"
        if self.likelihood_weight == 0.0 and self.sample_LH:
            assert (
                "likelihood_weight is 0 but sample likelihoods are still being calculated. "
                "Set sample_LH=0 when likelihood_weight=0"
            )


class DeepVARHierarchicalPredictionNetwork(
    DeepVARHierarchicalNetwork, DeepVARPredictionNetwork
):
    @validated()
    def __init__(
        self,
        num_parallel_samples: int,
        assert_reconciliation: bool,
        coherent_pred_samples: bool,
        **kwargs,
    ) -> None:
        super().__init__(num_parallel_samples=num_parallel_samples, **kwargs)
        self.coherent_pred_samples = coherent_pred_samples
        self.assert_reconciliation = assert_reconciliation
