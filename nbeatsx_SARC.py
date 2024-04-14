#| hide
import logging
import warnings

#| export
from typing import Tuple, Optional

import numpy as np
import torch
import torch.nn as nn

from neuralforecast.losses.pytorch import RMSE
from neuralforecast.common._base_windows import BaseWindows

#| hide
logging.getLogger("pytorch_lightning").setLevel(logging.ERROR)
warnings.filterwarnings("ignore")

#############################

class IdentityBasis(nn.Module):
    def __init__(self, backcast_size: int, forecast_size: int, out_features: int = 1):
        super().__init__()
        self.out_features = out_features
        self.forecast_size = forecast_size
        self.backcast_size = backcast_size

    def forward(self, theta: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        backcast = theta[:, : self.backcast_size]
        forecast = theta[:, self.backcast_size :]
        forecast = forecast.reshape(len(forecast), -1, self.out_features)
        return backcast, forecast


class TrendBasis(nn.Module):
    def __init__(
        self,
        degree_of_polynomial: int,
        backcast_size: int,
        forecast_size: int,
        out_features: int = 1,
    ):
        super().__init__()
        self.out_features = out_features
        polynomial_size = degree_of_polynomial + 1
        self.backcast_basis = nn.Parameter(
            torch.tensor(
                np.concatenate(
                    [
                        np.power(
                            np.arange(backcast_size, dtype=float) / backcast_size, i
                        )[None, :]
                        for i in range(polynomial_size)
                    ]
                ),
                dtype=torch.float32,
            ),
            requires_grad=False,
        )
        self.forecast_basis = nn.Parameter(
            torch.tensor(
                np.concatenate(
                    [
                        np.power(
                            np.arange(forecast_size, dtype=float) / forecast_size, i
                        )[None, :]
                        for i in range(polynomial_size)
                    ]
                ),
                dtype=torch.float32,
            ),
            requires_grad=False,
        )

    def forward(self, theta: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        polynomial_size = self.forecast_basis.shape[0]  # [polynomial_size, L+H]
        backcast_theta = theta[:, :polynomial_size]
        forecast_theta = theta[:, polynomial_size:]
        forecast_theta = forecast_theta.reshape(
            len(forecast_theta), polynomial_size, -1
        )
        backcast = torch.einsum("bp,pt->bt", backcast_theta, self.backcast_basis)
        forecast = torch.einsum("bpq,pt->btq", forecast_theta, self.forecast_basis)
        return backcast, forecast

class ExogenousBasis(nn.Module):
    # Reference: https://github.com/cchallu/nbeatsx
    def __init__(self, forecast_size: int):
        super().__init__()
        self.forecast_size = forecast_size

    def forward(self, theta: torch.Tensor, futr_exog: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        backcast_basis = futr_exog[:, :-self.forecast_size, :].permute(0, 2, 1)
        forecast_basis = futr_exog[:, -self.forecast_size:, :].permute(0, 2, 1)
        cut_point = forecast_basis.shape[1]
        backcast_theta=theta[:, cut_point:]
        forecast_theta=theta[:, :cut_point].reshape(
            len(theta), cut_point, -1
        )
     
        backcast = torch.einsum('bp,bpt->bt', backcast_theta, backcast_basis)
        forecast = torch.einsum('bpq,bpt->btq', forecast_theta, forecast_basis)
        
        return backcast, forecast

class SeasonalityBasis(nn.Module):
    def __init__(
        self,
        harmonics: int,
        backcast_size: int,
        forecast_size: int,
        out_features: int = 1,
    ):
        super().__init__()
        self.out_features = out_features
        frequency = np.append(
            np.zeros(1, dtype=float),
            np.arange(harmonics, harmonics / 2 * forecast_size, dtype=float)
            / harmonics,
        )[None, :]
        backcast_grid = (
            -2
            * np.pi
            * (np.arange(backcast_size, dtype=float)[:, None] / forecast_size)
            * frequency
        )
        forecast_grid = (
            2
            * np.pi
            * (np.arange(forecast_size, dtype=float)[:, None] / forecast_size)
            * frequency
        )

        backcast_cos_template = torch.tensor(
            np.transpose(np.cos(backcast_grid)), dtype=torch.float32
        )
        backcast_sin_template = torch.tensor(
            np.transpose(np.sin(backcast_grid)), dtype=torch.float32
        )
        backcast_template = torch.cat(
            [backcast_cos_template, backcast_sin_template], dim=0
        )

        forecast_cos_template = torch.tensor(
            np.transpose(np.cos(forecast_grid)), dtype=torch.float32
        )
        forecast_sin_template = torch.tensor(
            np.transpose(np.sin(forecast_grid)), dtype=torch.float32
        )
        forecast_template = torch.cat(
            [forecast_cos_template, forecast_sin_template], dim=0
        )

        self.backcast_basis = nn.Parameter(backcast_template, requires_grad=False)
        self.forecast_basis = nn.Parameter(forecast_template, requires_grad=False)

    def forward(self, theta: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        harmonic_size = self.forecast_basis.shape[0]  # [harmonic_size, L+H]
        backcast_theta = theta[:, :harmonic_size]
        forecast_theta = theta[:, harmonic_size:]
        forecast_theta = forecast_theta.reshape(len(forecast_theta), harmonic_size, -1)
        backcast = torch.einsum("bp,pt->bt", backcast_theta, self.backcast_basis)
        forecast = torch.einsum("bpq,pt->btq", forecast_theta, self.forecast_basis)
        return backcast, forecast

class SelfAttention(nn.Module):
    def __init__(self, input_size, dropout_prob):
        super(SelfAttention, self).__init__()
        self.query = nn.Linear(input_size, input_size)
        self.key = nn.Linear(input_size, input_size)
        self.value = nn.Linear(input_size, input_size)
        self.dropout = nn.Dropout(dropout_prob)
        self.softmax = nn.Softmax(dim=-1)

    def forward(self, x):
        q = self.query(x)
        k = self.key(x)
        v = self.value(x)

        scores = torch.matmul(q, k.transpose(-2, -1)) / torch.sqrt(torch.tensor(q.size(-1)).float())
        scores = self.softmax(scores)
        scores = self.dropout(scores)

        output = torch.matmul(scores, v)
        return output
    
#############################

import torch
import torch.nn as nn
from typing import Tuple

ACTIVATIONS = ["ReLU", "Softplus", "Tanh", "SELU", "LeakyReLU", "PReLU", "Sigmoid"]

class NBEATSBlock(nn.Module):
    """
    N-BEATS block with self attention and residual connections within MLP layers.
    """

    def __init__(
        self,
        input_size: int,
        h: int,
        futr_input_size: int,
        hist_input_size: int,
        stat_input_size: int,
        n_theta: int,
        mlp_units: list,
        basis: nn.Module,
        dropout_prob: float,
        activation: str,
        attention: bool = False,
    ):
        """ """
        super().__init__()

        self.dropout_prob = dropout_prob
        self.attention = attention
        self.futr_input_size = futr_input_size
        self.hist_input_size = hist_input_size
        self.stat_input_size = stat_input_size

        if self.attention:
            self.self_attention = SelfAttention(input_size=input_size, dropout_prob=dropout_prob)

        assert activation in ACTIVATIONS, f"{activation} is not in {ACTIVATIONS}"
        activ = getattr(nn, activation)()

        # Input vector for the block is
        # y_lags (input_size) + historical exogenous (hist_input_size*input_size) +
        # future exogenous (futr_input_size*input_size) + static exogenous (stat_input_size)
        # [ Y_[t-L:t], X_[t-L:t], F_[t-L:t+H], S ]
        input_size = (
            input_size
            + hist_input_size * input_size
            + futr_input_size * (input_size + h)
            + stat_input_size
        )

        hidden_layers = []
        in_features = input_size
        for idx, layer in enumerate(mlp_units):
            hidden_layers.append(nn.Linear(in_features, layer[0]))
            hidden_layers.append(activ)
            hidden_layers.append(nn.Dropout(p=self.dropout_prob))
            # Add residual connection after every two layers
            if idx % 2 == 0:
                hidden_layers.append(nn.Linear(layer[0], layer[1]))
                hidden_layers.append(nn.Dropout(p=self.dropout_prob))
                hidden_layers.append(nn.ReLU())
            in_features = layer[1]

        output_layer = [nn.Linear(in_features, n_theta)]
        layers = hidden_layers + output_layer
        self.layers = nn.Sequential(*layers)
        self.basis = basis

    def forward(
        self,
        insample_y: torch.Tensor,
        futr_exog: torch.Tensor,
        hist_exog: torch.Tensor,
        stat_exog: torch.Tensor,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        # Flatten MLP inputs [B, L+H, C] -> [B, (L+H)*C]
        # Concatenate [ Y_t, | X_{t-L},..., X_{t} | F_{t-L},..., F_{t+H} | S ]
        batch_size = len(insample_y)
        if self.hist_input_size > 0:
            insample_y = torch.cat(
                (insample_y, hist_exog.reshape(batch_size, -1)), dim=1
            )

        if self.futr_input_size > 0:
            insample_y = torch.cat(
                (insample_y, futr_exog.reshape(batch_size, -1)), dim=1
            )

        if self.stat_input_size > 0:
            insample_y = torch.cat(
                (insample_y, stat_exog.reshape(batch_size, -1)), dim=1
            )

        if self.attention:
            insample_y = self.self_attention(insample_y)

        # Compute local projection weights and projection
        theta = self.layers(insample_y)

        if isinstance(self.basis, ExogenousBasis):
            if self.futr_input_size > 0 and self.stat_input_size > 0:                
                futr_exog = torch.cat(
                    (
                        futr_exog,
                        stat_exog
                    ),
                    dim=2
                )
            elif self.futr_input_size >0:
                futr_exog = futr_exog
            elif self.stat_input_size >0:
                futr_exog = stat_exog
            else:
                raise(ValueError("No stats or future exogenous. ExogenousBlock not supported."))    
            backcast, forecast = self.basis(theta, futr_exog)
            return backcast, forecast
        else:
            backcast, forecast = self.basis(theta)
            return backcast, forecast

#############################

#| export
class NBEATSx(BaseWindows):
    """NBEATSx

    The Neural Basis Expansion Analysis with Exogenous variables (NBEATSx) is a simple
    and effective deep learning architecture. It is built with a deep stack of MLPs with
    doubly residual connections. The NBEATSx architecture includes additional exogenous
    blocks, extending NBEATS capabilities and interpretability. With its interpretable
    version, NBEATSx decomposes its predictions on seasonality, trend, and exogenous effects.

    **Parameters:**<br>
    `h`: int, Forecast horizon. <br>
    `input_size`: int, autorregresive inputs size, y=[1,2,3,4] input_size=2 -> y_[t-2:t]=[1,2].<br>
    `stat_exog_list`: str list, static exogenous columns.<br>
    `hist_exog_list`: str list, historic exogenous columns.<br>
    `futr_exog_list`: str list, future exogenous columns.<br>
    `exclude_insample_y`: bool=False, the model skips the autoregressive features y[t-input_size:t] if True.<br>
    `n_harmonics`: int, Number of harmonic oscillations in the SeasonalityBasis [cos(i * t/n_harmonics), sin(i * t/n_harmonics)]. Note that it will only be used if 'seasonality' is in `stack_types`.<br>
    `n_polynomials`: int, Number of polynomial terms for TrendBasis [1,t,...,t^n_poly]. Note that it will only be used if 'trend' is in `stack_types`.<br>
    `stack_types`: List[str], List of stack types. Subset from ['seasonality', 'trend', 'identity'].<br>
    `n_blocks`: List[int], Number of blocks for each stack. Note that len(n_blocks) = len(stack_types).<br>
    `mlp_units`: List[List[int]], Structure of hidden layers for each stack type. Each internal list should contain the number of units of each hidden layer. Note that len(n_hidden) = len(stack_types).<br>
    `dropout_prob_theta`: float, Float between (0, 1). Dropout for N-BEATS basis.<br>
    `activation`: str, activation from ['ReLU', 'Softplus', 'Tanh', 'SELU', 'LeakyReLU', 'PReLU', 'Sigmoid'].<br>
    `loss`: PyTorch module, instantiated train loss class from [losses collection](https://nixtla.github.io/neuralforecast/losses.pytorch.html).<br>
    `valid_loss`: PyTorch module=`loss`, instantiated valid loss class from [losses collection](https://nixtla.github.io/neuralforecast/losses.pytorch.html).<br>
    `max_steps`: int=1000, maximum number of training steps.<br>
    `learning_rate`: float=1e-3, Learning rate between (0, 1).<br>
    `num_lr_decays`: int=3, Number of learning rate decays, evenly distributed across max_steps.<br>
    `early_stop_patience_steps`: int=-1, Number of validation iterations before early stopping.<br>
    `val_check_steps`: int=100, Number of training steps between every validation loss check.<br>
    `batch_size`: int=32, number of different series in each batch.<br>
    `valid_batch_size`: int=None, number of different series in each validation and test batch, if None uses batch_size.<br>
    `windows_batch_size`: int=1024, number of windows to sample in each training batch, default uses all.<br>
    `inference_windows_batch_size`: int=-1, number of windows to sample in each inference batch, -1 uses all.<br>
    `start_padding_enabled`: bool=False, if True, the model will pad the time series with zeros at the beginning, by input size.<br>
    `step_size`: int=1, step size between each window of temporal data.<br>
    `scaler_type`: str='identity', type of scaler for temporal inputs normalization see [temporal scalers](https://nixtla.github.io/neuralforecast/common.scalers.html).<br>
    `random_seed`: int, random seed initialization for replicability.<br>
    `num_workers_loader`: int=os.cpu_count(), workers to be used by `TimeSeriesDataLoader`.<br>
    `drop_last_loader`: bool=False, if True `TimeSeriesDataLoader` drops last non-full batch.<br>
    `alias`: str, optional,  Custom name of the model.<br>
    `optimizer`: Subclass of 'torch.optim.Optimizer', optional, user specified optimizer instead of the default choice (Adam).<br>
    `optimizer_kwargs`: dict, optional, list of parameters used by the user specified `optimizer`.<br>
    `**trainer_kwargs`: int,  keyword trainer arguments inherited from [PyTorch Lighning's trainer](https://pytorch-lightning.readthedocs.io/en/stable/api/pytorch_lightning.trainer.trainer.Trainer.html?highlight=trainer).<br>

    **References:**<br>
    -[Kin G. Olivares, Cristian Challu, Grzegorz Marcjasz, Rafał Weron, Artur Dubrawski (2021).
    "Neural basis expansion analysis with exogenous variables: Forecasting electricity prices with NBEATSx".](https://arxiv.org/abs/2104.05522)
    """

    # Class attributes
    SAMPLING_TYPE = "windows"

    def __init__(
        self,
        h,
        input_size,
        futr_exog_list=None,
        hist_exog_list=None,
        stat_exog_list=None,
        exclude_insample_y=False,
        n_harmonics=2,
        n_polynomials=2,
        stack_types: list = ["identity", "trend", "seasonality"],
        n_blocks: list = [1, 1, 1],
        mlp_units: list = 3 * [[512, 512]],
        dropout_prob_theta=0.0,
        activation="ReLU",
        shared_weights=True,
        loss=RMSE(),
        valid_loss=None,
        max_steps: int = 1000,
        learning_rate: float = 1e-3,
        num_lr_decays: int = 3,
        early_stop_patience_steps: int = -1,
        val_check_steps: int = 100,
        batch_size=32,
        valid_batch_size: Optional[int] = None,
        windows_batch_size: int = 1024,
        inference_windows_batch_size: int = -1,
        start_padding_enabled: bool = False,
        step_size: int = 1,
        scaler_type: str = "identity",
        random_seed: int = 1,
        num_workers_loader: int = 0,
        drop_last_loader: bool = False,
        optimizer=None,
        optimizer_kwargs=None,
        **trainer_kwargs,
    ):
        # Protect horizon collapsed seasonality and trend NBEATSx-i basis
        if h == 1 and (("seasonality" in stack_types) or ("trend" in stack_types)):
            raise Exception(
                "Horizon `h=1` incompatible with `seasonality` or `trend` in stacks"
            )

        # Inherit BaseWindows class
        super(NBEATSx, self).__init__(h=h, 
                                      input_size = input_size,
                                      futr_exog_list=futr_exog_list,
                                      hist_exog_list=hist_exog_list,
                                      stat_exog_list=stat_exog_list,
                                      exclude_insample_y=exclude_insample_y,                                      
                                      loss=loss,
                                      valid_loss=valid_loss,
                                      max_steps=max_steps,
                                      learning_rate=learning_rate,
                                      num_lr_decays=num_lr_decays,
                                      early_stop_patience_steps=early_stop_patience_steps,
                                      val_check_steps=val_check_steps,
                                      batch_size=batch_size,
                                      valid_batch_size=valid_batch_size,
                                      windows_batch_size = windows_batch_size,
                                      inference_windows_batch_size=inference_windows_batch_size,
                                      start_padding_enabled=start_padding_enabled,
                                      step_size = step_size,
                                      scaler_type=scaler_type,
                                      num_workers_loader=num_workers_loader,
                                      drop_last_loader=drop_last_loader,
                                      random_seed=random_seed,
                                      optimizer=optimizer,
                                      optimizer_kwargs=optimizer_kwargs,
                                      **trainer_kwargs)

        # Architecture
        self.futr_input_size = len(self.futr_exog_list)
        self.hist_input_size = len(self.hist_exog_list)
        self.stat_input_size = len(self.stat_exog_list)

        blocks = self.create_stack(
            h=h,
            input_size=input_size,
            futr_input_size=self.futr_input_size,
            hist_input_size=self.hist_input_size,
            stat_input_size=self.stat_input_size,
            stack_types=stack_types,
            n_blocks=n_blocks,
            mlp_units=mlp_units,
            dropout_prob_theta=dropout_prob_theta,
            activation=activation,
            shared_weights=shared_weights,
            n_polynomials=n_polynomials,
            n_harmonics=n_harmonics,
        )
        self.blocks = torch.nn.ModuleList(blocks)

        # Adapter with Loss dependent dimensions
        if self.loss.outputsize_multiplier > 1:
            self.out = nn.Linear(
                in_features=h, out_features=h * self.loss.outputsize_multiplier
            )

    def create_stack(
        self,
        h,
        input_size,
        stack_types,
        n_blocks,
        mlp_units,
        dropout_prob_theta,
        activation,
        shared_weights,
        n_polynomials,
        n_harmonics,
        futr_input_size,
        hist_input_size,
        stat_input_size,
    ):
        block_list = []
        for i in range(len(stack_types)):
            for block_id in range(n_blocks[i]):
                # Shared weights
                if shared_weights and block_id > 0:
                    nbeats_block = block_list[-1]
                else:
                    if stack_types[i] == "seasonality":
                        n_theta = (
                            2
                            * (self.loss.outputsize_multiplier + 1)
                            * int(np.ceil(n_harmonics / 2 * h) - (n_harmonics - 1))
                        )
                        basis = SeasonalityBasis(
                            harmonics=n_harmonics,
                            backcast_size=input_size,
                            forecast_size=h,
                            out_features=self.loss.outputsize_multiplier,
                        )

                    elif stack_types[i] == "trend":
                        n_theta = (self.loss.outputsize_multiplier + 1) * (
                            n_polynomials + 1
                        )
                        basis = TrendBasis(
                            degree_of_polynomial=n_polynomials,
                            backcast_size=input_size,
                            forecast_size=h,
                            out_features=self.loss.outputsize_multiplier,
                        )

                    elif stack_types[i] == "identity":
                        n_theta = input_size + self.loss.outputsize_multiplier * h
                        basis = IdentityBasis(
                            backcast_size=input_size,
                            forecast_size=h,
                            out_features=self.loss.outputsize_multiplier,
                        )

                    elif stack_types[i] == "exogenous":
                        if futr_input_size + stat_input_size > 0:
                            n_theta = 2*(
                                futr_input_size + stat_input_size
                            )
                            basis = ExogenousBasis(forecast_size=h)

                    else:
                        raise ValueError(f"Block type {stack_types[i]} not found!")

                    nbeats_block = NBEATSBlock(
                        input_size=input_size,
                        h=h,
                        futr_input_size=futr_input_size,
                        hist_input_size=hist_input_size,
                        stat_input_size=stat_input_size,
                        n_theta=n_theta,
                        mlp_units=mlp_units,
                        basis=basis,
                        dropout_prob=dropout_prob_theta,
                        activation=activation,
                    )

                # Select type of evaluation and apply it to all layers of block
                block_list.append(nbeats_block)

        return block_list

    def forward(self, windows_batch):
        # Parse windows_batch
        insample_y = windows_batch["insample_y"]
        insample_mask = windows_batch["insample_mask"]
        futr_exog = windows_batch["futr_exog"]
        hist_exog = windows_batch["hist_exog"]
        stat_exog = windows_batch["stat_exog"]

        # NBEATSx' forward
        residuals = insample_y.flip(dims=(-1,))  # backcast init
        insample_mask = insample_mask.flip(dims=(-1,))

        forecast = insample_y[:, -1:, None]  # Level with Naive1
        block_forecasts = [forecast.repeat(1, self.h, 1)]
        for i, block in enumerate(self.blocks):
            backcast, block_forecast = block(
                insample_y=residuals,
                futr_exog=futr_exog,
                hist_exog=hist_exog,
                stat_exog=stat_exog,
            )
            residuals = (residuals - backcast) * insample_mask
            forecast = forecast + block_forecast

            if self.decompose_forecast:
                block_forecasts.append(block_forecast)

        # Adapting output's domain
        forecast = self.loss.domain_map(forecast)

        if self.decompose_forecast:
            # (n_batch, n_blocks, h)
            block_forecasts = torch.stack(block_forecasts)
            block_forecasts = block_forecasts.permute(1, 0, 2, 3)
            block_forecasts = block_forecasts.squeeze(-1)  # univariate output
            return block_forecasts
        else:
            return forecast